"""Analytics headers the MCP server adds to every gateway call.

    X-Client-Id: care-agent-internal     the validated client that caused the call
    X-MCP-Tool:  get_account_summary     the MCP tool that made it

So the gateway's analytics can attribute backend traffic to agents and tools. W3C
`traceparent` is sent as well (observability/telemetry.py) to join logs.

Security:
* The values come ONLY from the validated call identity (request_context.py), never
  from headers the caller sent, so a caller can't spoof them.
* The caller's token is NEVER forwarded (MCP spec: no token passthrough). The gateway
  sees the server's own token in Authorization and these identity headers.
* Header names are configurable per environment (GATEWAY_HEADER_CLIENT_ID,
  GATEWAY_HEADER_TOOL; empty = don't send) and validated at startup: they can't
  replace Authorization, Host, cookies, content or trace headers.
* A value that isn't a plain token (e.g. a registry entry with odd characters) is sent
  as "invalid" rather than passed through.

Performance: two dict lookups and a regex match per call, no I/O, no locks; safe on
the event loop.

Java/Spring equivalent: a `ClientHttpRequestInterceptor` reading the authentication
from the `SecurityContext` and adding headers.
"""

import re

from telco_mcp.request_context import current_call

HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# Headers configuration must never be able to set (auth, routing, framing, tracing).
RESERVED = frozenset(
    {
        "authorization", "proxy-authorization", "cookie", "host", "content-length",
        "content-type", "transfer-encoding", "connection", "te", "upgrade",
        "traceparent", "tracestate", "baggage", "idempotency-key",
    }
)  # fmt: skip
_VALUE = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")


def check_header_name(name: str, setting: str) -> str:
    """'' disables the header; otherwise a safe, non-reserved name."""
    if name == "":
        return name
    if not HEADER_NAME.match(name):
        raise ValueError(f"{setting}: header names may use letters, digits and '-' only")
    if name.lower() in RESERVED or name.lower().startswith(("proxy-", "sec-")):
        raise ValueError(f"{setting}: {name!r} is reserved and can't carry analytics")
    return name


class AnalyticsHeaders:
    """Builds the analytics headers for the gateway call in progress."""

    def __init__(self, client_id_header: str, tool_header: str) -> None:
        if client_id_header and client_id_header.lower() == tool_header.lower():
            raise ValueError("GATEWAY_HEADER_CLIENT_ID and GATEWAY_HEADER_TOOL must differ")
        self._client_id_header = client_id_header
        self._tool_header = tool_header

    def for_current_call(self) -> dict[str, str]:
        call = current_call()
        if call is None:  # not inside a tool call (e.g. a script): nothing to attribute
            return {}
        headers: dict[str, str] = {}
        if self._client_id_header:
            headers[self._client_id_header] = _safe(call.client_id)
        if self._tool_header:
            headers[self._tool_header] = _safe(call.tool)
        return headers


def _safe(value: str) -> str:
    return value if _VALUE.match(value) else "invalid"
