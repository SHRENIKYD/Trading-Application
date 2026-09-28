"""Deterministic safety rules. Nothing here depends on the LLM: every rule is enforced in code.

Order of checks for each action (first match wins):
  1. kill switch      -> stop the session
  2. domain allowlist -> BLOCK navigation outside the allowed sites
  3. amount limit     -> BLOCK any typed/submitted amount above max_amount (no approval possible)
  4. financial terms  -> spend cap reached                          ? BLOCK
                         spend would take the treasury below floor  ? BLOCK
                         known dollar amount <= auto_approve_usd    ? AUTO-APPROVE
                         otherwise                                  : ask a human (APPROVE)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .config import GuardrailConfig
from .treasury import Treasury

FINANCIAL_TRIGGER_RE = re.compile(
    r"\b(confirm|sign|approve|send|transfer|pay|withdraw|deposit|swap|bridge|stake|mint|buy|purchase|"
    r"checkout|connect|permit|authori[sz]e|wallet|seed|recovery|private\s+key)",
    re.IGNORECASE,
)

# A form field whose name/label matches this holds a money amount.
AMOUNT_FIELD_RE = re.compile(r"amount|value|qty|quantity|price|total|sum|\beth\b|usdc?|\bsol\b|\bbtc\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"^\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)\s*$")
# Amounts written with a currency, e.g. "$5", "5 ETH", "0.25 usdc".
CURRENCY_AMOUNT_RE = re.compile(
    r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)|(\d+(?:,\d{3})*(?:\.\d+)?)\s*(eth|usdc|usdt|usd|sol|btc|dollars?)\b",
    re.IGNORECASE,
)
DOLLAR_UNITS = {"usd", "usdc", "usdt", "dollar", "dollars"}

ALLOW, APPROVE, AUTO_APPROVE, BLOCK = "allow", "approve", "auto_approve", "block"


@dataclass
class Verdict:
    decision: str
    rule: str = ""
    reason: str = ""
    triggers: list[str] = field(default_factory=list)
    usd: float | None = None  # dollar value of the spend, when it can be worked out


def verify_financial_safety(action_json: dict, element_text: str = "") -> list[str]:
    """Return the financial trigger terms found in an action and its target element."""
    if action_json.get("action") not in ("GOTO", "CLICK", "TYPE"):
        return []
    haystack = " ".join(str(action_json.get(key, "")) for key in ("url", "selector", "text"))
    haystack = f"{haystack} {element_text}"
    return sorted({re.sub(r"\s+", " ", match.lower()) for match in FINANCIAL_TRIGGER_RE.findall(haystack)})


def _to_float(text: str) -> float:
    return float(text.replace(",", ""))


def money_mentions(action: dict, element_text: str, form_fields: list[tuple[str, str]]) -> list[tuple[float, bool]]:
    """Every money amount this action would type or submit, as (value, is_dollars).
    Values without a dollar sign or dollar currency are in the page's own unit (e.g. ETH)."""
    found: list[tuple[float, bool]] = []
    texts = [str(action.get(key, "")) for key in ("url", "selector", "text")] + [element_text]
    for text in texts:
        for dollar, plain, unit in CURRENCY_AMOUNT_RE.findall(text):
            found.append((_to_float(dollar or plain), bool(dollar) or unit.lower() in DOLLAR_UNITS))
    if action.get("action") == "TYPE" and AMOUNT_FIELD_RE.search(element_text + " " + action.get("selector", "")):
        match = NUMBER_RE.match(action.get("text", ""))
        if match:
            found.append((_to_float(match.group(1)), action.get("text", "").strip().startswith("$")))
    if action.get("action") == "CLICK":
        for name, value in form_fields:
            match = NUMBER_RE.match(value)
            if match and AMOUNT_FIELD_RE.search(name):
                found.append((_to_float(match.group(1)), value.strip().startswith("$")))
    return found


def find_amounts(action: dict, element_text: str, form_fields: list[tuple[str, str]]) -> list[float]:
    return [value for value, _ in money_mentions(action, element_text, form_fields)]


def spend_usd(action: dict, element_text: str, form_fields: list[tuple[str, str]], unit_usd: float | None) -> float | None:
    """Largest dollar value in the action, or None when there is no amount or one cannot be converted."""
    mentions = money_mentions(action, element_text, form_fields)
    if not mentions:
        return None
    values = []
    for value, is_dollars in mentions:
        if is_dollars:
            values.append(value)
        elif unit_usd:
            values.append(value * unit_usd)
        else:
            return None
    return max(values)


def host_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "about":
        return True
    host = (parsed.hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in allowed_domains)


class RepeatGuard:
    """Stops the model from retrying an action that keeps failing the same way."""

    def __init__(self, max_failures: int = 2) -> None:
        self.max_failures = max_failures
        self._last_key: str | None = None
        self._failures = 0

    @staticmethod
    def _key(action: dict) -> str:
        return json.dumps(action, sort_keys=True)

    def refusal(self, action: dict) -> str | None:
        """A message when this exact action already failed max_failures times in a row, else None."""
        if self._key(action) == self._last_key and self._failures >= self.max_failures:
            return (f"FAILED: not tried again - this exact action already failed {self._failures} times in a row. "
                    f"Choose a different action, e.g. the exact text from CLICKABLE ELEMENTS or a selector from INPUT FIELDS.")
        return None

    def record(self, action: dict, failed: bool) -> None:
        key = self._key(action)
        if not failed:
            self._last_key, self._failures = None, 0
        elif key == self._last_key:
            self._failures += 1
        else:
            self._last_key, self._failures = key, 1


class Guardrails:
    def __init__(self, cfg: GuardrailConfig, treasury: Treasury | None = None) -> None:
        self.cfg = cfg
        self.treasury = treasury
        self.approvals_used = 0
        self._kill_reason: str | None = None

    def kill(self, reason: str) -> None:
        self._kill_reason = reason

    def kill_reason(self) -> str | None:
        if self._kill_reason:
            return self._kill_reason
        if self.cfg.kill_file.exists():
            return f"kill switch file '{self.cfg.kill_file}' exists"
        return None

    def domain_allowed(self, url: str) -> bool:
        return host_allowed(url, self.cfg.allowed_domains)

    def evaluate(self, action: dict, element_text: str = "", form_fields: list[tuple[str, str]] | None = None) -> Verdict:
        form_fields = form_fields or []
        if action["action"] == "GOTO" and not self.domain_allowed(action["url"]):
            return Verdict(BLOCK, "domain_allowlist",
                           f"{urlparse(action['url']).hostname} is not in the allowed sites {list(self.cfg.allowed_domains)}")

        over = [amount for amount in find_amounts(action, element_text, form_fields) if amount > self.cfg.max_amount]
        if over:
            return Verdict(BLOCK, "amount_limit",
                           f"amount {max(over):g} exceeds the limit of {self.cfg.max_amount:g} (set with --max-amount)")

        triggers = verify_financial_safety(action, element_text)
        if not triggers:
            return Verdict(ALLOW)
        if self.approvals_used >= self.cfg.max_approvals:
            return Verdict(BLOCK, "spend_cap",
                           f"{self.cfg.max_approvals} approved money actions already used this session "
                           f"(set with --max-approvals)", triggers)
        usd = spend_usd(action, element_text, form_fields, self.cfg.unit_usd)
        if self.treasury is not None and usd is not None and not self.treasury.can_spend(usd):
            return Verdict(BLOCK, "survival_floor",
                           f"spending ${usd:.2f} would take the balance ${self.treasury.balance:.2f} below the "
                           f"floor ${self.treasury.floor:.2f} (may still spend ${self.treasury.headroom:.2f})",
                           triggers, usd)
        if self.treasury is not None and usd is not None and usd <= self.cfg.auto_approve_usd:
            return Verdict(AUTO_APPROVE, "auto_approve",
                           f"${usd:.2f} is within the auto-approve limit of ${self.cfg.auto_approve_usd:.2f}",
                           triggers, usd)
        reason = "needs human approval" if usd is not None else "needs human approval (dollar amount unknown)"
        return Verdict(APPROVE, "financial_terms", reason, triggers, usd)

    def record_execution(self, verdict: Verdict, detail: str, charge_usd: float | None | object = ...) -> None:
        """Count an executed money action and charge the treasury: the confirmed on-chain cost when given,
        otherwise the estimate from the page (verdict.usd). None or 0 charges nothing."""
        self.approvals_used += 1
        amount = verdict.usd if charge_usd is ... else charge_usd
        if self.treasury is not None and amount:
            self.treasury.record_spend(amount, detail)
