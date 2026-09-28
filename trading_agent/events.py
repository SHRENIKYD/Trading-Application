"""Structured session log: every step is one JSON line on disk, one readable line on the console,
and one message on any live subscriber queue (used by the dashboard)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("agent")

# Event type -> (log level, console summary template). Templates read keys from the event data.
_CONSOLE = {
    "session_started": (logging.INFO, "Session {session} started: goal={goal!r} start={start_url} allowed={allowed_domains}"),
    "goal_started": (logging.INFO, "Goal {number} (chosen by {chosen_by}): {goal} - starting at {start_url}"),
    "planning": (logging.INFO, "Planning the next task ({goals_done} done so far)..."),
    "llm_invalid": (logging.WARNING, "Invalid LLM output (attempt {attempt}/{max_attempts}): {error}"),
    "action_proposed": (logging.INFO, "Action {step}: {action}"),
    "guardrail_blocked": (logging.WARNING, "BLOCKED by guardrail [{rule}]: {reason}"),
    "approval_requested": (logging.WARNING, "Approval required ({approvals_used}/{max_approvals} used): triggers={triggers}"),
    "approval_decided": (logging.WARNING, "Decision: {decision} {action}"),
    "action_result": (logging.INFO, "Result: {result}"),
    "repeat_refused": (logging.WARNING, "Refused to repeat an action that already failed twice: {action}"),
    "treasury": (logging.INFO, "Treasury: balance ${balance_usd:.2f}, spent ${spent_usd:.2f}, "
                               "floor ${floor_usd:.2f}, may still spend ${headroom_usd:.2f}"),
    "evaluate": (logging.INFO, "EVALUATE - model's claim (unverified): {reason}"),
    "audit": (logging.INFO, "AUDIT (verified) - payments confirmed on-chain: {paid} (${paid_usd:.2f}); approved but "
                            "no payment: {no_payment}; not verified: {unchecked}; blocked: {blocked}"),
    "wallet_popup_open": (logging.WARNING, "Wallet popup open - agent paused until you finish in MetaMask."),
    "wallet_popup_closed": (logging.INFO, "Wallet popup closed (gave up waiting: {timed_out}); agent continues."),
    "payment_check_started": (logging.INFO, "Checking the wallet on-chain for the payment..."),
    "awaiting_wallet_confirmation": (logging.WARNING, "Waiting up to {timeout_s:.0f}s for the payment to leave the "
                                                      "wallet - confirm or reject it in MetaMask."),
    "wallet_continue_requested": (logging.INFO, "Dashboard: you are done in MetaMask - the agent stops waiting."),
    "payment_check": (logging.INFO, "On-chain check: {note}; {spent_eth:g} ETH left the wallet."),
    "audit_warning": (logging.WARNING, "AUDIT (verified) - {message}"),
    "session_ended": (logging.INFO, "Session {session} ended: {reason} after {actions} actions"),
}


class EventLog:
    def __init__(self, log_dir: Path) -> None:
        self.session = uuid.uuid4().hex[:8]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"session-{stamp}-{self.session}.jsonl"
        self._file = self.path.open("a", encoding="utf-8")
        self._seq = 0
        self._subscribers: list[asyncio.Queue] = []
        self.history: list[dict] = []

    def subscribe(self) -> asyncio.Queue:
        """A queue that first replays every event so far, then receives new ones."""
        queue: asyncio.Queue = asyncio.Queue()
        for event in self.history:
            queue.put_nowait(event)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def emit(self, event_type: str, **data) -> dict:
        self._seq += 1
        event = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session": self.session,
            "seq": self._seq,
            "type": event_type,
            "data": data,
        }
        event = json.loads(json.dumps(event, default=str))
        self._file.write(json.dumps(event) + "\n")
        self._file.flush()
        self.history.append(event)
        if event_type in _CONSOLE:
            level, template = _CONSOLE[event_type]
            log.log(level, template.format(session=self.session, **data))
        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def close(self) -> None:
        self._file.close()


def configure_logging(log_dir: Path) -> None:
    """Readable log to the console and to logs/agent.log; the JSONL session files hold full detail."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / "agent.log", encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
