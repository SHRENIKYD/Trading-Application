"""Local dashboard: serves the page, streams session events (Server-Sent Events) and accepts control requests.

Bound to 127.0.0.1 only. Every control request must carry the per-run token that is embedded in the page,
and must not come from another origin, so websites open in the agent's browser cannot press the buttons.
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path

from aiohttp import web

from ..approvals import WebApprover
from ..errors import AgentFatalError
from ..events import EventLog
from ..guardrails import Guardrails

log = logging.getLogger("agent")
STATIC = Path(__file__).parent / "static"


class Dashboard:
    def __init__(self, events: EventLog, guardrails: Guardrails, approver: WebApprover, port: int,
                 wallet_continue=lambda: None) -> None:
        self.events = events
        self.guardrails = guardrails
        self.approver = approver
        self.wallet_continue = wallet_continue
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.url = f"http://127.0.0.1:{port}/"
        self._runner: web.AppRunner | None = None

    def _authorized(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        same_origin = origin in (None, self.url.rstrip("/"), f"http://localhost:{self.port}")
        return same_origin and secrets.compare_digest(request.headers.get("X-Dashboard-Token", ""), self.token)

    async def index(self, request: web.Request) -> web.Response:
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.token)
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
        await response.prepare(request)
        queue = self.events.subscribe()
        try:
            while True:
                event = await queue.get()
                await response.write(f"data: {json.dumps(event)}\n\n".encode())
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            self.events.unsubscribe(queue)
        return response

    async def log_page(self, request: web.Request) -> web.Response:
        return web.Response(text=(STATIC / "log.html").read_text(encoding="utf-8"), content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def session_log(self, request: web.Request) -> web.Response:
        return web.Response(text=self.events.path.read_text(encoding="utf-8"), content_type="text/plain",
                            charset="utf-8", headers={"Cache-Control": "no-store"})

    async def stop(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        self.guardrails.kill("Stop pressed on the dashboard")
        self.events.emit("stop_requested", source="dashboard")
        self.approver.cancel_all("Stop pressed on the dashboard")
        return web.json_response({"ok": True})

    async def wallet_done(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        self.wallet_continue()
        return web.json_response({"ok": True})

    async def decision(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
            request_id, approve = body["id"], body["approve"]
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "expected JSON {id, approve}"}, status=400)
        if not isinstance(request_id, str) or not isinstance(approve, bool):
            return web.json_response({"error": "id must be a string and approve a boolean"}, status=400)
        if not self.approver.decide(request_id, approve):
            return web.json_response({"error": "not pending (already decided, timed out or unknown)"}, status=409)
        return web.json_response({"ok": True})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/events", self.stream)
        app.router.add_get("/session.jsonl", self.session_log)
        app.router.add_get("/log", self.log_page)
        app.router.add_post("/api/stop", self.stop)
        app.router.add_post("/api/decision", self.decision)
        app.router.add_post("/api/wallet-done", self.wallet_done)
        app.router.add_static("/static/", STATIC)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, "127.0.0.1", self.port).start()
        except OSError as exc:
            await self._runner.cleanup()
            raise AgentFatalError(f"Dashboard port {self.port} is in use (another run may still be open - press "
                                  f"Ctrl+C in its terminal), or pass --dashboard-port with a free port.") from exc
        log.info("Dashboard: %s", self.url)

    async def close(self) -> None:
        if self._runner:
            await self._runner.cleanup()
