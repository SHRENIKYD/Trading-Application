"""The agent loop: observe -> ask the LLM -> check guardrails -> (ask a human) -> act -> log."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from . import browser
from .approvals import ApprovalRequest, Approver
from .chain import ChainError, WalletChain, WalletSnapshot
from .config import MAX_WAIT_SECONDS, AgentConfig
from .events import EventLog
from .guardrails import APPROVE, AUTO_APPROVE, BLOCK, Guardrails, RepeatGuard
from .llm import LLMClient
from .actions import parse_plan
from .prompts import OBSERVATION_PROMPT, PLANNER_PROMPT, SYSTEM_PROMPT
from .treasury import Treasury

MONEY_RULES = {"financial_terms", "amount_limit", "spend_cap", "survival_floor"}
BROWSER_CLOSED = "stopped: the agent's browser was closed"
STARTUP_SETTLE_S = 3.0


class BrowserAgent:
    def __init__(self, cfg: AgentConfig, approver: Approver, events: EventLog) -> None:
        self.cfg = cfg
        self.approver = approver
        self.events = events
        self.treasury = Treasury(cfg.treasury_file, cfg.start_usd, cfg.floor_usd)
        self.guardrails = Guardrails(cfg.guardrails, self.treasury)
        self.llm = LLMClient(cfg, events)
        self.history: list[dict[str, str]] = []
        self.set_goal(cfg.goal or "")
        self.page: Page | None = None
        self.last_claim: str | None = None
        # (outcome, decided_by_or_rule, action, usd) per money-related action;
        # outcome is PAID, NO_PAYMENT, UNCHECKED or BLOCKED.
        self.money_log: list[tuple[str, str, dict, float | None]] = []
        self.step_count = 0
        self.chain = WalletChain(cfg.rpc_url, cfg.wallet_address) if cfg.rpc_url and cfg.wallet_address else None
        self.popups: browser.WalletPopups | None = None
        self.browser_closed = False
        self.wallet_continue = False

    # -- bookkeeping --------------------------------------------------------

    def set_goal(self, goal: str) -> None:
        """Start a fresh goal: new system prompt, empty history and repeat memory."""
        self.goal = goal
        self.system_prompt = SYSTEM_PROMPT.format(
            goal=goal, max_wait=MAX_WAIT_SECONDS, domains=", ".join(self.cfg.guardrails.allowed_domains))
        self.history = []
        self.repeat_guard = RepeatGuard()
        self.last_claim = None

    def remember(self, url: str, last_result: str, action: dict) -> None:
        """Compact rolling history (no page bodies) to stay inside the model's context window."""
        self.history += [
            {"role": "user", "content": f"[URL]: {url}\nLAST ACTION RESULT: {last_result}"},
            {"role": "assistant", "content": json.dumps(action)},
        ]
        del self.history[:-2 * self.cfg.history_turns]

    def audit(self) -> None:
        """What really happened to money-related actions, independent of the model's claims."""
        count = lambda outcome: sum(1 for entry in self.money_log if entry[0] == outcome)
        paid, no_payment, unchecked, blocked = (count(o) for o in ("PAID", "NO_PAYMENT", "UNCHECKED", "BLOCKED"))
        paid_usd = round(sum(u or 0 for o, _, _, u in self.money_log if o == "PAID"), 6)
        self.events.emit("audit", paid=paid, paid_usd=paid_usd, no_payment=no_payment, unchecked=unchecked,
                         blocked=blocked, onchain=self.chain is not None,
                         entries=[{"outcome": o, "by": r, "action": a, "usd": u} for o, r, a, u in self.money_log])
        if self.chain is not None and not paid and (blocked or no_payment):
            self.events.emit("audit_warning", message="no payment was confirmed on-chain; any claim of a completed "
                                                      "payment is FALSE.")
        if unchecked:
            self.events.emit("audit_warning", message=f"{unchecked} approved action(s) were not verified on-chain; "
                                                      f"check the wallet's activity yourself.")

    def blocked(self, rule: str, reason: str, action: dict) -> str:
        self.events.emit("guardrail_blocked", rule=rule, reason=reason, action=action)
        if rule in MONEY_RULES:
            self.money_log.append(("BLOCKED", rule, action, None))
        return f"BLOCKED by guardrail [{rule}]: {reason}. This action did NOT happen. Choose a different action."

    # -- one action ---------------------------------------------------------

    async def step(self, page: Page, action: dict) -> str:
        kind = action["action"]
        target = None
        if kind in ("CLICK", "TYPE"):
            if urlparse(page.url).scheme not in ("http", "https"):
                return self.blocked("page_scheme", f"{kind} is only allowed on http(s) pages, not {page.url}", action)
            target = await browser.resolve_target(page, action["selector"])
            if target is None:
                return f"FAILED: no visible element matches selector {action['selector']!r}."

        description = target.description if target else ""
        form_fields = target.form_fields if target else []
        verdict = self.guardrails.evaluate(action, description, form_fields)
        if verdict.decision == BLOCK:
            return self.blocked(verdict.rule, verdict.reason, action)

        approved_by = ""
        if verdict.decision == AUTO_APPROVE:
            approved_by = "auto"
            self.events.emit("approval_decided", decision="AUTO-APPROVED", action=action, usd=verdict.usd,
                             reason=verdict.reason)
        elif verdict.decision == APPROVE:
            request = ApprovalRequest(page.url, action, description, verdict.triggers, form_fields,
                                      self.guardrails.approvals_used, self.cfg.guardrails.max_approvals, verdict.usd,
                                      self.treasury.snapshot())
            self.events.emit("approval_requested", id=request.id, page_url=page.url, action=action,
                             element=description, triggers=verdict.triggers, reason=verdict.reason,
                             form_fields=form_fields, empty_fields=request.empty_fields,
                             approvals_used=request.approvals_used, max_approvals=request.max_approvals,
                             usd=verdict.usd, treasury=request.treasury, timeout_s=self.approver.timeout_s)
            if not await self.approver.approve(request):
                by = self.approver.last_decided_by
                self.events.emit("approval_decided", id=request.id, decision="BLOCKED", by=by, action=action)
                self.money_log.append(("BLOCKED", "human", action, None))
                why = {"timeout": "no answer in time", "unattended rules": "unattended mode allows no approvals"}.get(
                    by, "the run was stopped" if by.startswith("Stop") else "by you")
                return f"BLOCKED ({why}). This action did NOT happen. Choose a different action."
            approved_by = "human"
            self.events.emit("approval_decided", id=request.id, decision="APPROVED",
                             by=self.approver.last_decided_by, action=action)

        before = None
        if approved_by:
            if self.popups:
                self.popups.mark()
            if self.chain:
                try:
                    before = await self.chain.snapshot()
                except ChainError as exc:
                    self.events.emit("audit_warning", message=f"could not read the wallet before paying ({exc}).")

        failure = await browser.perform(page, action, target, self.cfg)
        if failure:
            return failure

        if not self.guardrails.domain_allowed(page.url):
            left = page.url
            try:
                await page.go_back(timeout=self.cfg.navigation_timeout_ms)
            except Exception:
                await page.goto(self.cfg.start_url, timeout=self.cfg.navigation_timeout_ms)
            return self.blocked("domain_allowlist", f"the action led to {left}, outside the allowed sites; went back", action)

        if approved_by:
            await self.wait_for_wallet(appear_s=5.0)
            outcome, usd, note = await self.settle(verdict, before)
            self.guardrails.record_execution(verdict, json.dumps(action), usd)
            self.money_log.append((outcome, approved_by, action, usd))
            self.events.emit("treasury", approvals_used=self.guardrails.approvals_used, **self.treasury.snapshot())
            return f"OK: {kind} executed ({approved_by}-approved). {note} Now on {page.url}"
        return f"OK: {kind} executed. Now on {page.url}"

    async def settle(self, verdict, before: WalletSnapshot | None) -> tuple[str, float | None, str]:
        """Decide what an approved action really cost: PAID (confirmed on-chain), NO_PAYMENT (nothing left the
        wallet) or UNCHECKED (no on-chain check; the page estimate is charged). Returns (outcome, usd, note)."""
        if self.chain is None or before is None:
            if verdict.usd is None:
                self.events.emit("audit_warning", message="approved action had no known dollar amount and on-chain "
                                                          "checking is off; the treasury was NOT charged.")
            return "UNCHECKED", verdict.usd, "Payment NOT verified on-chain (checking is off)."
        expects_payment = verdict.usd is not None
        self.events.emit("payment_check_started")
        if expects_payment:
            # MetaMask may confirm in a window the agent cannot see, so wait on the blockchain itself.
            self.events.emit("awaiting_wallet_confirmation", timeout_s=self.cfg.wallet_wait_s)
        try:
            check = await self.chain.check_spend(
                before, detect_s=self.cfg.wallet_wait_s if expects_payment else 20.0,
                stop_waiting=lambda: self.wallet_continue or self.browser_closed
                or self.guardrails.kill_reason() is not None)
        except ChainError as exc:
            self.events.emit("audit_warning", message=f"on-chain check failed ({exc}); charged the page estimate.")
            return "UNCHECKED", verdict.usd, "Payment could NOT be verified on-chain."
        unit = self.cfg.guardrails.unit_usd
        usd = round(check.spent_eth * unit, 6) if check.sent and unit else None
        self.events.emit("payment_check", sent=check.sent, confirmed=check.confirmed, spent_eth=check.spent_eth,
                         usd=usd, note=check.note)
        self.wallet_continue = False
        if not check.sent and (check.confirmed or not expects_payment):
            return "NO_PAYMENT", 0.0, "NO payment left the wallet (checked on-chain)."
        if not check.sent:
            self.events.emit("audit_warning", message=f"{check.note}; a late confirmation could still go through, so "
                                                      f"the page estimate was charged to stay safe.")
            return "UNCHECKED", verdict.usd, "No payment seen on-chain yet; it may still arrive."
        if not check.confirmed:
            self.events.emit("audit_warning", message=f"{check.note}; charged the page estimate until it confirms.")
            return "UNCHECKED", verdict.usd, "A payment was sent but is not confirmed yet."
        if usd is None:
            self.events.emit("audit_warning", message=f"{check.spent_eth:g} ETH left the wallet but no token price is "
                                                      f"set (--unit-usd); the treasury was NOT charged.")
        return "PAID", usd, f"Payment confirmed on-chain: {check.spent_eth:g} ETH left the wallet, fees included."

    async def check_chain(self) -> str | None:
        """Confirm the RPC endpoint is on the expected chain. Returns a stop reason when the run must not start."""
        if self.chain is None:
            return None
        cfg = self.cfg
        try:
            chain_id = await self.chain.chain_id()
        except ChainError as exc:
            if cfg.expected_chain_id is not None:
                self.events.emit("audit_warning", message=f"cannot reach {cfg.network_name} ({exc}); refusing to run "
                                                          f"because payments could not be verified.")
                return "stopped: blockchain endpoint unreachable"
            self.events.emit("audit_warning", message=f"cannot reach the RPC endpoint ({exc}); "
                                                      f"payments will not be verified on-chain.")
            return None
        self.events.emit("chain_connected", chain_id=chain_id, wallet=cfg.wallet_address, network=cfg.network_name)
        if cfg.expected_chain_id is not None and chain_id != cfg.expected_chain_id:
            self.events.emit("audit_warning", message=f"endpoint is on chain {chain_id}, expected {cfg.network_name} "
                                                      f"({cfg.expected_chain_id}); refusing to run.")
            return f"stopped: wrong blockchain ({chain_id})"
        return None

    async def wait_for_wallet(self, appear_s: float) -> None:
        """Pause while a wallet popup is open so you can unlock, connect or confirm before the agent moves on."""
        if self.popups is None:
            return
        await self.popups.wait_for_user(
            appear_s, self.cfg.wallet_wait_s,
            lambda: self.wallet_continue or self.browser_closed or self.guardrails.kill_reason() is not None,
            on_open=lambda: self.events.emit("wallet_popup_open"),
            on_closed=lambda timed_out: self.events.emit("wallet_popup_closed", timed_out=timed_out))
        self.wallet_continue = False

    def request_wallet_continue(self) -> None:
        """'I'm done in MetaMask - continue' from the dashboard: stop waiting on the current wallet step."""
        self.wallet_continue = True
        self.events.emit("wallet_continue_requested")

    # -- session ------------------------------------------------------------

    async def run(self) -> None:
        cfg = self.cfg
        end_reason = "error"
        self.events.emit("session_started", goal=cfg.goal or f"Mission: {cfg.mission}", mission=cfg.mission,
                         unattended=cfg.unattended, max_goals=cfg.max_goals, start_url=cfg.start_url, model=cfg.model,
                         allowed_domains=list(cfg.guardrails.allowed_domains), max_amount=cfg.guardrails.max_amount,
                         max_approvals=cfg.guardrails.max_approvals, max_actions=cfg.max_actions,
                         auto_approve_usd=cfg.guardrails.auto_approve_usd, unit_usd=cfg.guardrails.unit_usd,
                         treasury=self.treasury.snapshot())
        try:
            async with async_playwright() as pw:
                context = await browser.launch_context(pw, cfg)
                context.on("close", lambda _: setattr(self, "browser_closed", True))
                self.popups = browser.WalletPopups(context)
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    refusal = await self.check_chain()
                    await page.goto(cfg.start_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout_ms)
                    await browser.wait_for_network_idle(page, cfg.network_idle_timeout_ms)
                    # The wallet may open its own window as the browser starts; give it a moment, then ignore it.
                    await asyncio.sleep(STARTUP_SETTLE_S)
                    self.popups.mark()
                    try:
                        self.page = page
                        end_reason = refusal or await self._mission(context)
                    except PlaywrightError:
                        if not self.browser_closed:
                            raise
                        end_reason = BROWSER_CLOSED
                        self.audit()
                finally:
                    try:
                        await context.close()
                    except PlaywrightError:
                        pass  # already closed by you
        except BaseException:
            end_reason = "interrupted or crashed"
            raise
        finally:
            if self.chain:
                await self.chain.close()
            self.events.emit("session_ended", reason=end_reason, actions=self.step_count)

    # -- goals -------------------------------------------------------------

    async def _mission(self, context) -> str:
        """Run the given goal; in self-directed mode keep planning and running new goals until a stop condition."""
        cfg = self.cfg
        done: list[tuple[str, str]] = []
        if cfg.goal:
            goal, url, by = cfg.goal, cfg.start_url, "you"
        else:
            plan = await self.plan_next(done)
            if "stop" in plan:
                return f"mission ended: {plan['stop']}"
            goal, url, by = plan["goal"], plan["start_url"], "agent"
        number = 0
        while True:
            number += 1
            self.set_goal(goal)
            self.events.emit("goal_started", number=number, goal=goal, start_url=url, chosen_by=by)
            if number > 1 or url != cfg.start_url:
                await self.open_start(context, url)
            reason = await self._loop(context)
            if not cfg.mission or not reason.startswith(("model finished", "goal abandoned")):
                return reason
            done.append((goal, self.last_claim or reason))
            if number >= cfg.max_goals:
                return f"stopped: goal limit ({cfg.max_goals}) reached"
            if self.treasury.headroom <= 0:
                return "stopped: reached the budget floor"
            plan = await self.plan_next(done)
            if "stop" in plan:
                return f"mission ended: {plan['stop']}"
            goal, url, by = plan["goal"], plan["start_url"], "agent"

    async def plan_next(self, done: list[tuple[str, str]]) -> dict:
        """Ask the model for the next goal toward the mission. Invalid replies end the mission."""
        cfg, t = self.cfg, self.treasury
        history = "\n".join(f"{i}. {goal} -> result: {claim}" for i, (goal, claim) in enumerate(done[-8:], 1)) or "(none)"
        prompt = PLANNER_PROMPT.format(
            mission=cfg.mission, domains=", ".join(cfg.guardrails.allowed_domains), balance=t.balance,
            floor=t.floor, headroom=t.headroom, auto=cfg.guardrails.auto_approve_usd, done=history)
        self.events.emit("planning", goals_done=len(done))
        plan = await self.llm.ask([{"role": "system", "content": prompt},
                                   {"role": "user", "content": "Plan the next task."}],
                                  lambda raw: parse_plan(raw, self.guardrails.domain_allowed))
        if plan is None:
            plan = {"stop": "the model could not produce a valid plan"}
        self.events.emit("plan", **plan)
        return plan

    async def open_start(self, context, url: str) -> None:
        if self.page is None or self.page.is_closed():
            self.page = await context.new_page()
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.navigation_timeout_ms)
            await browser.wait_for_network_idle(self.page, self.cfg.network_idle_timeout_ms)
        except PlaywrightError as exc:
            self.events.emit("audit_warning", message=f"could not open {url}: {exc.message.splitlines()[0]}")

    # -- one goal ------------------------------------------------------------

    async def _loop(self, context) -> str:
        cfg = self.cfg
        last_result = "Session started." if self.step_count == 0 else "New goal started."
        since_check = 0
        while True:
            if reason := self.guardrails.kill_reason():
                self.events.emit("kill_switch", reason=reason)
                self.audit()
                return f"killed: {reason}"
            if self.browser_closed:
                self.audit()
                return BROWSER_CLOSED
            if cfg.unattended and self.step_count >= cfg.max_total_actions:
                self.audit()
                return f"stopped: total action limit ({cfg.max_total_actions}) reached"

            if since_check >= cfg.max_actions:
                self.events.emit("health_check", actions=self.step_count, automatic=cfg.unattended)
                if not cfg.unattended and not await self.approver.confirm(
                        f"Health check: continue for another {cfg.max_actions} actions?"):
                    self.audit()
                    return "stopped at health check"
                since_check = 0

            page = self.page
            if page.is_closed():
                page = self.page = await context.new_page()
                last_result = "Previous tab was closed; opened a blank tab. Use GOTO."

            if self.popups and self.popups.open_popups():
                await self.wait_for_wallet(appear_s=0.0)

            state = await browser.observe(page, cfg.max_page_chars)
            self.events.emit("observation", url=state.url, clickables=state.clickables, inputs=state.inputs,
                             text=state.text)
            observation = OBSERVATION_PROMPT.format(
                step=since_check + 1, limit=cfg.max_actions, last_result=last_result, url=state.url,
                clickables=state.clickables, inputs=state.inputs, text=state.text or "(empty)")
            messages = [{"role": "system", "content": self.system_prompt}, *self.history,
                        {"role": "user", "content": observation}]

            action = await self.llm.next_action(messages)
            if action is None:
                if cfg.unattended:
                    self.audit()
                    return "goal abandoned: the model's replies could not be parsed"
                if not await self.approver.confirm(
                        f"LLM produced invalid JSON {cfg.max_format_retries} times in a row. Retry this step?"):
                    self.audit()
                    return "LLM output could not be parsed"
                continue

            self.step_count += 1
            step_no = self.step_count
            since_check += 1
            self.events.emit("action_proposed", step=step_no, action=json.dumps(action))

            if action["action"] == "EVALUATE":
                self.last_claim = action["reason"]
                self.events.emit("evaluate", reason=action["reason"])
                self.audit()
                return "model finished (EVALUATE)"

            if reason := self.guardrails.kill_reason():
                self.events.emit("kill_switch", reason=reason)
                self.audit()
                return f"killed: {reason}"

            self.remember(state.url, last_result, action)
            if refusal := self.repeat_guard.refusal(action):
                last_result = refusal
                self.events.emit("repeat_refused", step=step_no, action=action)
            else:
                last_result = await self.step(page, action)
                self.repeat_guard.record(action, failed=not last_result.startswith("OK"))
            self.events.emit("action_result", step=step_no, action=action, result=last_result, url=page.url)
