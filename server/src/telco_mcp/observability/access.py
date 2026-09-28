"""One access-log line per HTTP request (logger `telco_mcp.access`).

    INFO request  http.request.method=POST url.path=/mcp http.response.status_code=200
                  event.duration=12345678 mcp.method=tools/call mcp.tool=list_orders
                  mcp.client_id=care-agent mcp.protocol_version=2026-07-28

What is logged: method, path, status, duration (ns, ECS), the MCP method and tool
name from the protocol headers (`Mcp-Method`, `Mcp-Name`), the protocol version, and
the authenticated client ID. What is NOT: query strings, bodies, `Mcp-Param-*`
headers (they can carry tool arguments), Authorization, IP addresses.

Health checks log at DEBUG (probes every few seconds would drown everything else).
5xx logs at ERROR, everything else at INFO.
"""

import logging
import re
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("telco_mcp.access")
_SAFE = re.compile(r"^[A-Za-z0-9_./:-]{1,64}$")  # header values we echo must be boring


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            value = v.decode("latin-1")
            return value if _SAFE.match(value) else "invalid"
    return None


def _client_id(scope: Scope) -> str | None:
    user: Any = scope.get("user")  # set by the SDK's auth middleware after validation
    token = getattr(user, "access_token", None)
    return getattr(token, "client_id", None)


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter_ns()
        status = 500  # if the app dies before responding, that's what the client sees

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            fields = {
                "http.request.method": scope.get("method"),
                "url.path": path,
                "http.response.status_code": status,
                "event.duration": time.perf_counter_ns() - started,
                "mcp.method": _header(scope, b"mcp-method"),
                "mcp.tool": _header(scope, b"mcp-name"),
                "mcp.protocol_version": _header(scope, b"mcp-protocol-version"),
                "mcp.client_id": _client_id(scope),
            }
            fields = {k: v for k, v in fields.items() if v is not None}
            if path.endswith("/healthz"):
                level = logging.DEBUG
            else:
                level = logging.ERROR if status >= 500 else logging.INFO
            log.log(level, "request", extra={"fields": fields})
