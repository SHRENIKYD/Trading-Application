"""Where a human says yes or no. The agent depends only on the Approver interface, so the terminal
prompt and the dashboard's Approve/Block buttons are interchangeable."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .events import EventLog

log = logging.getLogger("agent")


@dataclass
class ApprovalRequest:
    page_url: str
    action: dict
    element_text: str
    triggers: list[str]
    form_fields: list[tuple[str, str]]
    approvals_used: int
    max_approvals: int
    usd: float | None
    treasury: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    @property
    def empty_fields(self) -> list[str]:
        return [name for name, value in self.form_fields if not value]


class Approver(Protocol):
    # Who made the last decision: "terminal", "dashboard", "timeout" or a stop reason.
    last_decided_by: str
    timeout_s: float | None

    async def approve(self, request: ApprovalRequest) -> bool: ...

    async def confirm(self, question: str) -> bool: ...


async def _ask(question: str) -> bool:
    """Blocking y/N prompt run off the event loop. Anything but an explicit yes is a no."""
    try:
        answer = await asyncio.to_thread(input, question)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


class TerminalApprover:
    last_decided_by = "terminal"
    timeout_s = None

    async def approve(self, request: ApprovalRequest) -> bool:
        print("\n" + "=" * 72)
        print("FINANCIAL SAFETY GUARDRAIL - human approval required")
        print(f"  Page URL : {request.page_url}")
        print(f"  Action   : {json.dumps(request.action)}")
        print(f"  Element  : {request.element_text[:200] or '(n/a)'}")
        print(f"  Triggers : {', '.join(request.triggers)}")
        print(f"  Approvals: {request.approvals_used} of {request.max_approvals} used this session")
        cost = f"${request.usd:.2f}" if request.usd is not None else "UNKNOWN (not charged to the treasury)"
        print(f"  Cost     : {cost}; balance ${request.treasury['balance_usd']:.2f}, "
              f"floor ${request.treasury['floor_usd']:.2f}, may still spend ${request.treasury['headroom_usd']:.2f}")
        if request.form_fields:
            print("  Form     : " + ", ".join(f"{name} = {value[:80] if value else '(EMPTY)'}"
                                          for name, value in request.form_fields))
            if request.empty_fields:
                print("  WARNING  : the form has EMPTY fields. Approving now may submit an incomplete payment.")
        print("  Wallet popups are never operated by the agent; review them yourself in the browser.")
        print("=" * 72)
        return await _ask("Approve this action? [y/N] ")

    async def confirm(self, question: str) -> bool:
        return await _ask(f"{question} [y/N] ")


class WebApprover:
    """Decisions come from the dashboard. No answer within timeout_s counts as Block."""

    def __init__(self, events: EventLog, timeout_s: float) -> None:
        self.events = events
        self.timeout_s = timeout_s
        self.last_decided_by = ""
        self._pending: dict[str, asyncio.Future] = {}

    def is_pending(self, request_id: str) -> bool:
        return request_id in self._pending and not self._pending[request_id].done()

    def decide(self, request_id: str, approve: bool) -> bool:
        """Resolve a pending request from the dashboard. Returns False if it is unknown or already decided."""
        if not self.is_pending(request_id):
            return False
        self._pending[request_id].set_result((approve, "dashboard"))
        return True

    def cancel_all(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_result((False, reason))

    async def _wait(self, request_id: str) -> bool:
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            approved, self.last_decided_by = await asyncio.wait_for(future, self.timeout_s)
        except TimeoutError:
            approved, self.last_decided_by = False, "timeout"
        finally:
            self._pending.pop(request_id, None)
        return approved

    async def approve(self, request: ApprovalRequest) -> bool:
        log.warning("Approval needed - decide on the dashboard (blocks itself after %ds).", self.timeout_s)
        return await self._wait(request.id)

    async def confirm(self, question: str) -> bool:
        request_id = uuid.uuid4().hex[:10]
        return await self._ask_dashboard(request_id, question)

    async def _ask_dashboard(self, request_id: str, question: str) -> bool:
        self.events.emit("confirm_requested", id=request_id, question=question, timeout_s=self.timeout_s)
        log.warning("%s - answer on the dashboard (defaults to No after %ds).", question, self.timeout_s)
        answer = await self._wait(request_id)
        self.events.emit("confirm_decided", id=request_id, answer="yes" if answer else "no", by=self.last_decided_by)
        return answer


CONNECT_ONLY = {"connect", "wallet"}


class UnattendedApprover(WebApprover):
    """No human answers on the dashboard. Rules decide instead:
    - a request flagged only by "connect"/"wallet" with no amount (connecting a wallet) is allowed;
    - everything else that needed a human is refused.
    Payments within the auto-approve limit never reach an approver. MetaMask's own confirmation stays with you."""

    timeout_s = None

    async def approve(self, request: ApprovalRequest) -> bool:
        allowed = request.usd is None and set(request.triggers) <= CONNECT_ONLY
        self.last_decided_by = "unattended rules"
        return allowed

    async def confirm(self, question: str) -> bool:
        self.last_decided_by = "unattended rules"
        return False
