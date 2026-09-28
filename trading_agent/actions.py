from __future__ import annotations

import json
from urllib.parse import urlparse

from .config import ALLOWED_ACTIONS, MAX_WAIT_SECONDS
from .errors import ActionFormatError


def _require_str(data: dict, action: str, key: str, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ActionFormatError(f"{action} requires a non-empty string field '{key}'.")
    return value if allow_empty else value.strip()


def validate_action(data: object) -> dict:
    """Normalize a decoded JSON value into a canonical action dict or raise ActionFormatError."""
    if not isinstance(data, dict):
        raise ActionFormatError("The top-level JSON value must be an object.")
    action = str(data.get("action", "")).strip().upper()
    if action not in ALLOWED_ACTIONS:
        raise ActionFormatError(f"'action' must be one of {sorted(ALLOWED_ACTIONS)}.")

    if action == "GOTO":
        url = _require_str(data, action, "url")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ActionFormatError("GOTO 'url' must be an absolute http:// or https:// URL.")
        return {"action": action, "url": url}
    if action == "CLICK":
        return {"action": action, "selector": _require_str(data, action, "selector")}
    if action == "TYPE":
        return {
            "action": action,
            "selector": _require_str(data, action, "selector"),
            "text": _require_str(data, action, "text", allow_empty=True),
        }
    if action == "WAIT":
        try:
            seconds = int(data.get("seconds"))
        except (TypeError, ValueError):
            raise ActionFormatError("WAIT requires an integer field 'seconds'.") from None
        if not 1 <= seconds <= MAX_WAIT_SECONDS:
            raise ActionFormatError(f"WAIT 'seconds' must be between 1 and {MAX_WAIT_SECONDS}.")
        return {"action": action, "seconds": seconds}
    return {"action": action, "reason": _require_str(data, action, "reason")}


def extract_json(raw: str) -> object:
    """The JSON object inside raw LLM output, tolerating code fences and prose around it."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ActionFormatError("No JSON object found in the response.")
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ActionFormatError(f"Invalid JSON ({exc.msg} at char {exc.pos}).") from exc


def parse_action(raw: str) -> dict:
    """Extract the JSON object from raw LLM output and validate it as an action."""
    return validate_action(extract_json(raw))


def parse_plan(raw: str, url_allowed) -> dict:
    """Validate a planner reply: {"goal", "start_url"} on a reachable site, or {"stop": reason}."""
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ActionFormatError("The reply must be a JSON object.")
    if isinstance(data.get("stop"), str) and data["stop"].strip():
        return {"stop": data["stop"].strip()[:300]}
    goal, url = data.get("goal"), data.get("start_url")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 500:
        raise ActionFormatError("'goal' must be a non-empty string of at most 500 characters.")
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ActionFormatError("'start_url' must be an absolute http(s) URL.")
    if not url_allowed(url):
        raise ActionFormatError(f"'start_url' {url} is not on a reachable site.")
    return {"goal": goal.strip(), "start_url": url}
