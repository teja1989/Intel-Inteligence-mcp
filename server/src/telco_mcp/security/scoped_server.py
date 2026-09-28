"""MCPServer with per-client tool visibility, enforcement, audit and clean arg errors.

We override the two *public* MCPServer methods every request goes through:

* `list_tools()`: return only the tools the client's scopes allow. The
  2026-07-28 spec explicitly permits this: "The set MAY vary by the
  authorization presented on the request". The SDK's default
  `cacheScope: "private"` stops the filtered list being shared across clients.
* `call_tool()`: enforce the SAME rule. Filtering the list is UX, not
  security: a client can call a tool name it was never shown. A hidden tool
  answers exactly like a nonexistent one ("Unknown tool: …"), so its existence
  isn't revealed.

`call_tool()` is also the one choke point for the audit log, for turning
Pydantic argument errors into short, model-friendly messages, and for the
`tools/call <name>` span + log fields (tool, client) that every log line inside
the call carries (docs/operations.md §3).

(The SDK also offers `middleware=[…]`, but documents it as provisional, so we
build on the stable public methods instead.)

Java/Spring equivalent: filtering the `ToolCallback` list per request, plus
`@PreAuthorize("hasAuthority('SCOPE_read')")` on each tool method, plus an
aspect for auditing.
"""

import time
from contextvars import ContextVar
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared._otel import extract_trace_context
from mcp.types import CallToolResult, InputRequiredResult
from mcp.types import Tool as MCPTool
from opentelemetry import trace
from pydantic import ValidationError

from telco_mcp.observability import fields as log_fields
from telco_mcp.request_context import CallIdentity, bind_call
from telco_mcp.security.audit import audit_tool_call, resource_ids
from telco_mcp.security.clients import ClientContext, ClientRegistry, resolve_client

_tracer = trace.get_tracer("telco_mcp")
_current_client: ContextVar[ClientContext | None] = ContextVar("current_client", default=None)


def current_client() -> ClientContext:
    """The client of the tool call in progress. Tools use this and nothing else."""
    client = _current_client.get()
    if client is None:  # can't happen via call_tool(); fail closed if it ever does
        raise ToolError("Not authorised.")
    return client


class ScopedMCPServer(MCPServer[Any]):
    def __init__(
        self,
        *args: Any,
        client_registry: ClientRegistry | None = None,
        fallback_client: ClientContext | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.client_registry = client_registry
        self.fallback_client = fallback_client
        self.tool_scopes: dict[str, str] = {}

    def require_scope(self, tool_name: str, scope: str) -> None:
        self.tool_scopes[tool_name] = scope

    def _client(self) -> ClientContext | None:
        return resolve_client(self.client_registry, self.fallback_client)

    def _allowed(self, client: ClientContext | None, tool_name: str) -> bool:
        scope = self.tool_scopes.get(tool_name)  # a tool without a declared scope: nobody
        return client is not None and scope is not None and client.has(scope)

    async def list_tools(self) -> list[MCPTool]:
        client = self._client()
        return [t for t in await super().list_tools() if self._allowed(client, t.name)]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        client = self._client()
        client_id = client.client_id if client else _unresolved_client()
        with (
            _tracer.start_as_current_span(
                f"tools/call {name}",
                context=_parent_context(context),
                attributes={"mcp.tool.name": name, "mcp.client_id": client_id or ""},
            ),
            log_fields.bind(**{"mcp.tool": name, "mcp.client_id": client_id}),
        ):
            return await self._call_tool(name, arguments, context, client, client_id)

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None,
        client: ClientContext | None,
        client_id: str | None,
    ) -> CallToolResult | InputRequiredResult:
        started = time.perf_counter()
        outcome = "error"
        try:
            if not self._allowed(client, name):
                outcome = "denied"
                raise ToolError(f"Unknown tool: {name}")  # same text as a truly unknown tool
            token = _current_client.set(client)
            try:
                # Validated identity for downstream use (analytics headers to the gateway).
                with bind_call(CallIdentity(client.client_id, name)):  # type: ignore[union-attr]
                    result = await super().call_tool(name, arguments, context)
            finally:
                _current_client.reset(token)
            outcome = "ok"
            return result
        except UnexpectedToolError:
            # A crash inside the tool (a bug), not a client mistake. The SDK
            # already hides the details from the model; audit it as "error".
            outcome = "error"
            raise
        except ToolError as exc:
            if outcome != "denied":
                outcome = "tool_error"
            validation = _find_cause(exc, ValidationError)
            if validation is not None:
                raise ToolError(_friendly_validation(name, validation)) from None
            raise
        finally:
            audit_tool_call(
                tool=name,
                client=client_id,
                via=client.via if client else None,
                resources=resource_ids(arguments),
                outcome=outcome,
                started=started,
            )


def _parent_context(context: Context[Any, Any] | None) -> Any:
    """Parent for the tool span: the HTTP request's span (inbound `traceparent` from
    the gateway) if there is one, else a `traceparent` the MCP client put in `_meta`
    (SEP-414; the only carrier over stdio)."""
    if trace.get_current_span().get_span_context().is_valid:
        return None  # ambient context wins
    meta = getattr(getattr(context, "request_context", None), "meta", None)
    if meta is not None and hasattr(meta, "model_dump"):
        meta = meta.model_dump(by_alias=True, exclude_none=True)
    return extract_trace_context(meta if isinstance(meta, dict) else None)


def _unresolved_client() -> str | None:
    """Who presented a VALID token but got no client (e.g. unregistered), for the audit."""
    token = get_access_token()
    return token.client_id if token is not None else None


def _find_cause[E: BaseException](exc: BaseException, kind: type[E]) -> E | None:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, kind):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


def _friendly_validation(tool: str, err: ValidationError) -> str:
    """Field + rule, never the rejected value (it may be PII or an injection)."""
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in err.errors()
    )
    return f"Invalid arguments for {tool}: {problems}. Correct them and call the tool again."
