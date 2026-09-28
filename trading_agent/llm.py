from __future__ import annotations

import time

import httpx
import ollama

from .actions import parse_action
from .config import AgentConfig
from .errors import ActionFormatError, AgentFatalError
from .events import EventLog
from .prompts import FORMAT_FEEDBACK_PROMPT


class LLMClient:
    def __init__(self, cfg: AgentConfig, events: EventLog) -> None:
        self.cfg = cfg
        self.events = events
        self.client = ollama.AsyncClient(host=cfg.ollama_host, timeout=cfg.llm_timeout_s)

    async def _chat(self, messages: list[dict[str, str]]) -> str:
        try:
            response = await self.client.chat(
                model=self.cfg.model,
                messages=messages,
                format="json",
                options={"temperature": 0.1, "num_ctx": self.cfg.num_ctx},
            )
        except ollama.ResponseError as exc:
            raise AgentFatalError(
                f"Ollama error ({exc.status_code}): {exc.error}. Is the model pulled? Run: ollama pull {self.cfg.model}"
            ) from exc
        except (ConnectionError, httpx.HTTPError) as exc:
            raise AgentFatalError(f"Cannot reach Ollama at {self.cfg.ollama_host}: {exc}. Run: ollama serve") from exc
        return response.message.content or ""

    async def next_action(self, messages: list[dict[str, str]]) -> dict | None:
        """Ask for an action, feeding format errors back until valid or retries are exhausted."""
        return await self.ask(messages, parse_action)

    async def ask(self, messages: list[dict[str, str]], parse):
        """Ask for a JSON reply checked by `parse` (raises ActionFormatError); None if every retry fails."""
        messages = list(messages)
        attempts = self.cfg.max_format_retries
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            raw = await self._chat(messages)
            self.events.emit("llm_response", attempt=attempt, raw=raw,
                             latency_ms=round((time.perf_counter() - started) * 1000))
            try:
                return parse(raw)
            except ActionFormatError as exc:
                self.events.emit("llm_invalid", attempt=attempt, max_attempts=attempts, error=str(exc), raw=raw[:300])
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": FORMAT_FEEDBACK_PROMPT.format(error=exc)},
                ]
        return None
