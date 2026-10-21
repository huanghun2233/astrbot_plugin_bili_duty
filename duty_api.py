"""HTTP + WebSocket API for the bili-duty plugin.

Runs on its own loopback port (default 127.0.0.1:6198) so other programs can
talk to the plugin the same way SnowLuma talks to AstrBot: a plain JSON
request/response protocol over HTTP, plus a WebSocket endpoint that mirrors
OneBot's ``{action, params, echo}`` -> ``{status, retcode, data, echo}`` shape.

Every response uses the same envelope as the bili CLI::

    {"ok": true,  "data": ...}
    {"ok": false, "error": {"code": "...", "message": "..."}}

Handlers are injected by ``main.py`` so this module stays independent of
AstrBot internals.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class ApiError(Exception):
    """Raised by handlers to return a structured error."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class DutyApiServer:
    def __init__(
        self,
        handlers: dict[str, Handler],
        *,
        host: str = "127.0.0.1",
        port: int = 6198,
        token: str = "",
        version: str = "0.0.0",
        logger: Any = None,
    ) -> None:
        self.handlers = handlers
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.version = version
        self.logger = logger
        self.started_at = time.time()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._connections = 0

    # ------------------------------------------------------------------ life
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        app = web.Application(client_max_size=1 << 20)
        app.router.add_get("/health", self._handle_http_get("health"))
        app.router.add_get("/status", self._handle_http_get("status"))
        app.router.add_get("/hot", self._handle_http_get("hot"))
        app.router.add_get("/video", self._handle_http_get("video"))
        app.router.add_get("/daily", self._handle_http_get("daily"))
        app.router.add_post("/daily", self._handle_http_post("daily"))
        app.router.add_post("/notify", self._handle_http_post("notify"))
        app.router.add_get("/ws", self._handle_ws)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        if self.logger:
            self.logger.info(f"[bili-duty] api listening {self.url} (ws {self.url.replace('http', 'ws')}/ws)")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    # ------------------------------------------------------------------ auth
    def _authorized(self, request: web.Request) -> bool:
        if not self.token:
            return True
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:].strip() == self.token:
            return True
        return request.query.get("token", "") == self.token

    # --------------------------------------------------------------- helpers
    async def _dispatch(self, action: str, params: dict[str, Any]) -> Any:
        handler = self.handlers.get(action)
        if handler is None:
            raise ApiError("unknown_action", f"没有这个 action：{action}", status=404)
        return await handler(params)

    def _error_payload(self, exc: BaseException) -> tuple[dict[str, Any], int]:
        if isinstance(exc, ApiError):
            return {"ok": False, "error": {"code": exc.code, "message": exc.message}}, exc.status
        return (
            {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc) or "内部错误"}},
            500,
        )

    def _handle_http_get(self, action: str) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def handler(request: web.Request) -> web.Response:
            if not self._authorized(request):
                return web.json_response(
                    {"ok": False, "error": {"code": "unauthorized", "message": "token 不正确"}},
                    status=401,
                )
            params: dict[str, Any] = dict(request.query)
            try:
                data = await self._dispatch(action, params)
                return web.json_response({"ok": True, "data": data})
            except BaseException as exc:  # noqa: BLE001 - report any failure as JSON
                payload, status = self._error_payload(exc)
                return web.json_response(payload, status=status)

        return handler

    def _handle_http_post(self, action: str) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def handler(request: web.Request) -> web.Response:
            if not self._authorized(request):
                return web.json_response(
                    {"ok": False, "error": {"code": "unauthorized", "message": "token 不正确"}},
                    status=401,
                )
            params: dict[str, Any] = dict(request.query)
            if request.can_read_body:
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        params.update(body)
                except (json.JSONDecodeError, ValueError):
                    return web.json_response(
                        {"ok": False, "error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}},
                        status=400,
                    )
            try:
                data = await self._dispatch(action, params)
                return web.json_response({"ok": True, "data": data})
            except BaseException as exc:  # noqa: BLE001
                payload, status = self._error_payload(exc)
                return web.json_response(payload, status=status)

        return handler

    # --------------------------------------------------------------------- ws
    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.json_response(
                {"ok": False, "error": {"code": "unauthorized", "message": "token 不正确"}},
                status=401,
            )
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._connections += 1
        await ws.send_json(
            {
                "type": "hello",
                "plugin": "astrbot_plugin_bili_duty",
                "version": self.version,
                "port": self.port,
                "actions": sorted(self.handlers),
            }
        )
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    if message.type == web.WSMsgType.ERROR:
                        break
                    continue
                try:
                    request_obj = json.loads(message.data)
                except json.JSONDecodeError:
                    await ws.send_json({"status": "failed", "retcode": 1, "message": "请求不是合法 JSON"})
                    continue
                if not isinstance(request_obj, dict):
                    await ws.send_json({"status": "failed", "retcode": 1, "message": "请求必须是 JSON 对象"})
                    continue
                action = str(request_obj.get("action") or "").strip()
                params = request_obj.get("params") or {}
                echo = request_obj.get("echo")
                if not isinstance(params, dict):
                    await ws.send_json(
                        {"status": "failed", "retcode": 1, "message": "params 必须是对象", "echo": echo}
                    )
                    continue
                try:
                    data = await self._dispatch(action, params)
                    await ws.send_json(
                        {"action": action, "echo": echo, "status": "ok", "retcode": 0, "data": data}
                    )
                except BaseException as exc:  # noqa: BLE001
                    payload, _ = self._error_payload(exc)
                    error = payload.get("error") or {}
                    await ws.send_json(
                        {
                            "action": action,
                            "echo": echo,
                            "status": "failed",
                            "retcode": 1,
                            "message": error.get("message") or str(exc),
                        }
                    )
        finally:
            self._connections -= 1
        return ws
