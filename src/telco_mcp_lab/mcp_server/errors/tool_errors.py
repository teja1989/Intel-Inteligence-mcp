"""Turn downstream failures into tool errors a model can act on.

MCP has two error channels (see docs/01-concepts.md §3). Everything here uses
the *tool execution error* channel: we raise the SDK's `ToolError`, and the
client receives `isError: true` with our text, which it feeds back to the model
so the model can self-correct.

Rules for every message:
  1. Say what went wrong in domain terms ("account not found"), not HTTP terms.
  2. Say whether retrying helps, and what to do instead.
  3. Never leak internals: no URLs, hostnames, tokens or stack traces. We do
     NOT forward the backend's free-text `detail`, because it's backend-controlled
     text headed for the model. Messages are built from the stable `code`.
"""

import logging
import re

from mcp.server.mcpserver.exceptions import ToolError

from telco_mcp_lab.mcp_server.clients.telco import GatewayError, GatewayUnavailable
from telco_mcp_lab.mcp_server.security.guard import not_found

log = logging.getLogger(__name__)

_SAFE_TOKEN = re.compile(r"^[A-Z0-9-]{1,40}$")
# Error codes are backend-controlled too: only a plain identifier may reach the model or logs.
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def _safe_code(code: str) -> str:
    return code if _SAFE_CODE.match(code) else "UNRECOGNISED_ERROR"


# Same wording as the tenant guard's refusals (security/guard.py): "doesn't
# exist" and "belongs to another tenant" must be indistinguishable.
_NOT_FOUND_HINTS = {
    "ACCOUNT_NOT_FOUND": not_found("account", "ACC-1001"),
    "SUBSCRIPTION_NOT_FOUND": not_found("subscription", "SUB-1001-01"),
    "SERVICE_NOT_FOUND": not_found("service", "SVC-1001-01"),
    "ORDER_NOT_FOUND": not_found("order", "ORD-000123"),
    "DRAFT_NOT_FOUND": not_found("draft", "DRF-…"),
}


def to_tool_error(exc: GatewayError | GatewayUnavailable) -> ToolError:
    if isinstance(exc, GatewayUnavailable):
        log.warning("gateway unavailable: %s", exc.reason)
        return ToolError(
            "The telecom backend did not respond in time or could not be reached. "
            "This is temporary: you may retry once; if it fails again, tell the user "
            "the service is unavailable right now and to try later."
        )

    code = _safe_code(exc.code)
    if code in _NOT_FOUND_HINTS:
        return ToolError(_NOT_FOUND_HINTS[code])
    if exc.status in (401, 403):
        # Our own service credentials were refused. The model can't fix this,
        # so tell it to stop, and alert operators via the log.
        log.error("gateway rejected service credentials: %s %s", exc.status, code)
        return ToolError(
            "The server is not authorised to reach the telecom backend (server "
            "configuration problem). Do not retry; tell the user the service is unavailable."
        )
    if exc.status == 429:
        return ToolError("The telecom backend is rate limiting requests. Wait briefly, then retry.")
    if exc.status in (400, 409, 422):
        values = [v for v in exc.body.get("valid_values", []) if _SAFE_TOKEN.match(str(v))]
        hint = f" Valid values: {', '.join(values)}." if values else ""
        return ToolError(f"The backend rejected the request ({code}).{hint} Fix the input.")
    log.warning("gateway error: %s %s", exc.status, code)
    return ToolError(
        "The telecom backend failed to process the request. You may retry once; if it "
        "fails again, tell the user the service is unavailable right now."
    )
