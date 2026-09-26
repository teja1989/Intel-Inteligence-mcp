"""stdio ↔ Streamable HTTP bridge with automatic token handling.

The MCP client (VS Code, Inspector, …) launches this as a local stdio server. Every
JSON-RPC message it writes is POSTed, unchanged, to the remote endpoint, with:

* `Authorization: Bearer <token>`: from the client-credentials source, refreshed
  before expiry; on a 401 the bridge gets a new token and retries ONCE.
* The customer header, if configured (set by the developer's config, never by a model).
* The protocol's per-message HTTP headers (`MCP-Protocol-Version`, and for 2026-07-28
  `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*`), computed with the SDK's own helpers so
  the bridge can't drift from what the SDK client sends.
* `Mcp-Session-Id` echoed back if a (legacy, stateful) server sets one.

Responses (JSON or SSE) are written back to stdout as they arrive. Messages are not
modified, and nothing is logged except metadata (method, status), never payloads or
tokens.

Limitations (fine for this server, which is stateless and never calls the client):
no standalone GET stream for server-initiated messages, no JSON-RPC batches.
"""

import asyncio
import json
import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

import httpx
from mcp.shared.inbound import (
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    NAME_BEARING_METHODS,
    encode_header_value,
    mcp_param_headers,
    x_mcp_header_map,
)
from mcp.types import PROTOCOL_VERSION_META_KEY

from telco_mcp_lab.connect.settings import ConnectSettings
from telco_mcp_lab.connect.token import ClientCredentials, TokenError, http_client

log = logging.getLogger(__name__)

SESSION_HEADER = "mcp-session-id"
INTERNAL_ERROR = -32603
LONG_LIVED = frozenset({"subscriptions/listen"})  # modern long-lived streams: no read timeout


class Bridge:
    def __init__(
        self,
        settings: ConnectSettings,
        tokens: ClientCredentials,
        write: Callable[[dict[str, Any]], None],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._s = settings
        self._tokens = tokens
        self._write = write
        self._http = http_client(settings, settings.url, transport=transport)
        self._session_id: str | None = None
        self._legacy_version: str | None = None  # negotiated by `initialize`
        self._modern_version: str | None = None  # last seen in `_meta`
        self._param_maps: dict[str, Any] = {}  # tool → x-mcp-header map (from tools/list)
        self._pending: dict[Any, tuple[str, bool]] = {}  # request id → (method, modern?)
        self._in_flight: dict[Any, asyncio.Task[None]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ outbound
    def headers_for(self, msg: dict[str, Any]) -> dict[str, str]:
        h = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        if self._s.customer:
            h[self._s.customer_header] = self._s.customer
        if self._session_id:
            h[SESSION_HEADER] = self._session_id
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        modern = meta.get(PROTOCOL_VERSION_META_KEY)
        if isinstance(method, str) and isinstance(modern, str):
            self._modern_version = modern
            h[MCP_PROTOCOL_VERSION_HEADER] = modern
            h[MCP_METHOD_HEADER] = method
            name_key = NAME_BEARING_METHODS.get(method)
            if name_key and isinstance(name := params.get(name_key), str):
                h[MCP_NAME_HEADER] = encode_header_value(name)
            tool = params.get("name") if method == "tools/call" else None
            if isinstance(tool, str) and (header_map := self._param_maps.get(tool)) is not None:
                h.update(mcp_param_headers(header_map, params.get("arguments") or {}))
        elif method != "initialize" and (version := self._legacy_version or self._modern_version):
            h[MCP_PROTOCOL_VERSION_HEADER] = version
        return h

    def handle(self, msg: dict[str, Any]) -> None:
        """Dispatch one message from the client (non-blocking)."""
        method, mid = msg.get("method"), msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "notifications/cancelled":
            target = params.get("requestId")
            info = self._pending.get(target)
            if info is not None and info[1]:  # 2026-07-28 HTTP: cancel = abort the POST
                if task := self._in_flight.get(target):
                    task.cancel()
                return
        headers = self.headers_for(msg)
        if isinstance(method, str) and mid is not None:
            self._pending[mid] = (method, MCP_METHOD_HEADER in headers)
        task = asyncio.create_task(self._forward(msg, headers))
        if isinstance(method, str) and mid is not None:
            self._in_flight[mid] = task
            task.add_done_callback(lambda _t, m=mid: self._done(m))

    def _done(self, mid: Any) -> None:
        self._in_flight.pop(mid, None)
        self._pending.pop(mid, None)

    async def drain(self, seconds: float) -> None:
        if self._in_flight:
            await asyncio.wait(list(self._in_flight.values()), timeout=seconds)

    async def _forward(self, msg: dict[str, Any], headers: dict[str, str]) -> None:
        mid, method = msg.get("id"), msg.get("method")
        is_request = isinstance(method, str) and mid is not None
        body = json.dumps(msg, separators=(",", ":")).encode()
        timeout = (
            httpx.Timeout(self._s.timeout_s, read=None)  # stream stays open by design
            if method in LONG_LIVED
            else httpx.Timeout(self._s.timeout_s)
        )
        try:
            for attempt in (1, 2):
                token = await self._tokens.token(force=attempt == 2)
                auth = {"authorization": f"Bearer {token}"}
                async with self._http.stream(
                    "POST", self._s.url, content=body, headers=headers | auth, timeout=timeout
                ) as r:
                    if r.status_code == 401 and attempt == 1:
                        log.info("401 from MCP endpoint: refreshing token and retrying once")
                        continue
                    if sid := r.headers.get(SESSION_HEADER):
                        self._session_id = sid
                    log.info("%s -> HTTP %s", method or "response", r.status_code)
                    await self._relay(r, mid if is_request else None)
                    return
        except TokenError as exc:
            if is_request:
                self._error(mid, f"Bridge: {exc}")
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            log.warning("MCP endpoint unreachable: %s", type(exc).__name__)
            if is_request:
                self._error(mid, f"Bridge: MCP endpoint unreachable ({type(exc).__name__})")

    async def _relay(self, r: httpx.Response, request_id: Any) -> None:
        ctype = r.headers.get("content-type", "")
        if r.status_code in (202, 204):
            return
        if ctype.startswith("text/event-stream"):
            data: list[str] = []
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    self._emit_raw("\n".join(data))
                    data = []
            if data:
                self._emit_raw("\n".join(data))
            return
        raw = (await r.aread()).decode("utf-8", errors="replace")
        if ctype.startswith("application/json") and raw.strip():
            self._emit_raw(raw)
        elif request_id is not None:
            hint = (
                " (token rejected: is this client registered for this environment?)"
                if r.status_code in (401, 403)
                else ""
            )
            self._error(request_id, f"Bridge: HTTP {r.status_code} from MCP endpoint{hint}")

    # ------------------------------------------------------------------ inbound
    def _emit_raw(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            log.warning("non-JSON message from MCP endpoint dropped")
            return
        for m in payload if isinstance(payload, list) else [payload]:
            if isinstance(m, dict):
                self._observe(m)
                self._write(m)

    def _observe(self, m: dict[str, Any]) -> None:
        """Learn what later requests need: negotiated version, tool header maps."""
        info = self._pending.get(m.get("id"))
        result = m.get("result")
        if info is None or not isinstance(result, dict):
            return
        method, _ = info
        if method == "initialize" and isinstance(v := result.get("protocolVersion"), str):
            self._legacy_version = v
        elif method == "tools/list":
            for tool in result.get("tools", []):
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    self._param_maps[tool["name"]] = x_mcp_header_map(tool.get("inputSchema"))

    def _error(self, mid: Any, message: str) -> None:
        self._write(
            {"jsonrpc": "2.0", "id": mid, "error": {"code": INTERNAL_ERROR, "message": message}}
        )


# ---------------------------------------------------------------------- stdio loop
def _stdout_writer() -> Callable[[dict[str, Any]], None]:
    lock = threading.Lock()
    out = sys.stdout.buffer

    def write(m: dict[str, Any]) -> None:
        line = json.dumps(m, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with lock:
            out.write(line)
            out.flush()

    return write


async def run_stdio(settings: ConnectSettings, drain_timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def reader() -> None:  # a thread: portable, unlike connect_read_pipe on Windows
        for line in sys.stdin.buffer:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, daemon=True).start()
    tokens = ClientCredentials(settings)
    bridge = Bridge(settings, tokens, _stdout_writer())
    try:
        while (line := await queue.get()) is not None:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning("ignoring a non-JSON line from the client")
                continue
            if isinstance(msg, dict):
                bridge.handle(msg)
            else:
                log.warning("JSON-RPC batches are not supported; message ignored")
        await bridge.drain(drain_timeout)  # finish in-flight requests after stdin closes
    finally:
        await bridge.aclose()
