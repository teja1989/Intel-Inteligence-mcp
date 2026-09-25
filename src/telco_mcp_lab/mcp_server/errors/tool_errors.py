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

log = logging.getLogger(__name__)

_SAFE_TOKEN = re.compile(r"^[A-Z0-9-]{1,40}$")

_NOT_FOUND_HINTS = {
    "ACCOUNT_NOT_FOUND": "No account exists with that ID. Account IDs look like ACC-1001. "
    "Do not guess IDs: ask the user to confirm their account ID.",
    "SUBSCRIPTION_NOT_FOUND": "No subscription exists with that ID on this account. "
    "Subscription IDs look like SUB-1001-01.",
    "SERVICE_NOT_FOUND": "No service exists with that ID. Service IDs look like SVC-1001-01.",
    "ORDER_NOT_FOUND": "No order exists with that ID. Order IDs look like ORD-000123.",
    "DRAFT_NOT_FOUND": "No draft exists with that ID. Create a new draft first.",
}


def to_tool_error(exc: GatewayError | GatewayUnavailable) -> ToolError:
    if isinstance(exc, GatewayUnavailable):
        log.warning("gateway unavailable: %s", exc.reason)
        return ToolError(
            "The telecom backend did not respond in time or could not be reached. "
            "This is temporary: you may retry once; if it fails again, tell the user "
            "the service is unavailable right now and to try later."
        )

    if exc.code in _NOT_FOUND_HINTS:
        return ToolError(_NOT_FOUND_HINTS[exc.code])
    if exc.status in (401, 403):
        # Our own service credentials were refused. The model can't fix this,
        # so tell it to stop, and alert operators via the log.
        log.error("gateway rejected service credentials: %s %s", exc.status, exc.code)
        return ToolError(
            "The server is not authorised to reach the telecom backend (server "
            "configuration problem). Do not retry; tell the user the service is unavailable."
        )
    if exc.status == 429:
        return ToolError("The telecom backend is rate limiting requests. Wait briefly, then retry.")
    if exc.status in (400, 409, 422):
        values = [v for v in exc.body.get("valid_values", []) if _SAFE_TOKEN.match(str(v))]
        hint = f" Valid values: {', '.join(values)}." if values else ""
        return ToolError(f"The backend rejected the request ({exc.code}).{hint} Fix the input.")
    log.warning("gateway error: %s %s", exc.status, exc.code)
    return ToolError(
        "The telecom backend failed to process the request. You may retry once; if it "
        "fails again, tell the user the service is unavailable right now."
    )
