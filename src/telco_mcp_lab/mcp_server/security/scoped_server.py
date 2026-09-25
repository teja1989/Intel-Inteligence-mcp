"""MCPServer with per-caller tool visibility, enforcement, audit and clean arg errors.

We override the two *public* MCPServer methods every request goes through:

* `list_tools()`: return only the tools the caller's scopes allow. The
  2026-07-28 spec explicitly permits this: "The set MAY vary by the
  authorization presented on the request". The SDK's default
  `cacheScope: "private"` stops the filtered list being shared across callers.
* `call_tool()`: enforce the SAME rule. Filtering the list is UX, not
  security: a client can call a tool name it was never shown. A hidden tool
  answers exactly like a nonexistent one ("Unknown tool: …"), so its existence
  isn't revealed.

`call_tool()` is also the one choke point for the audit log and for turning
Pydantic argument errors into short, model-friendly messages.

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
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult
from mcp.types import Tool as MCPTool
from pydantic import ValidationError

from telco_mcp_lab.mcp_server.security.audit import audit_tool_call
from telco_mcp_lab.mcp_server.security.caller import AccessModel, CallerContext, resolve_caller
from telco_mcp_lab.mcp_server.security.guard import AccessDenied

_current_caller: ContextVar[CallerContext | None] = ContextVar("current_caller", default=None)


def current_caller() -> CallerContext:
    """The caller of the tool call in progress. Tools use this and nothing else."""
    caller = _current_caller.get()
    if caller is None:  # can't happen via call_tool(); fail closed if it ever does
        raise AccessDenied("Not authorised.")
    return caller


class ScopedMCPServer(MCPServer[Any]):
    def __init__(
        self,
        *args: Any,
        access_model: AccessModel,
        fallback_caller: CallerContext | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.access_model = access_model
        self.fallback_caller = fallback_caller
        self.tool_scopes: dict[str, str] = {}

    def require_scope(self, tool_name: str, scope: str) -> None:
        self.tool_scopes[tool_name] = scope

    def _caller(self) -> CallerContext | None:
        return resolve_caller(self.access_model, self.fallback_caller)

    def _allowed(self, caller: CallerContext | None, tool_name: str) -> bool:
        scope = self.tool_scopes.get(tool_name)  # a tool without a declared scope: nobody
        return caller is not None and scope is not None and caller.has(scope)

    async def list_tools(self) -> list[MCPTool]:
        caller = self._caller()
        return [t for t in await super().list_tools() if self._allowed(caller, t.name)]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        started = time.perf_counter()
        caller = self._caller()
        outcome = "error"
        try:
            if not self._allowed(caller, name):
                outcome = "denied"
                raise ToolError(f"Unknown tool: {name}")  # same text as a truly unknown tool
            token = _current_caller.set(caller)
            try:
                result = await super().call_tool(name, arguments, context)
            finally:
                _current_caller.reset(token)
            outcome = "ok"
            return result
        except UnexpectedToolError:
            # A crash inside the tool (a bug), not a caller mistake. The SDK
            # already hides the details from the model; audit it as "error".
            outcome = "error"
            raise
        except ToolError as exc:
            if outcome != "denied":
                outcome = "denied" if _caused_by(exc, AccessDenied) else "tool_error"
            validation = _find_cause(exc, ValidationError)
            if validation is not None:
                raise ToolError(_friendly_validation(name, validation)) from None
            raise
        finally:
            audit_tool_call(
                tool=name,
                caller=caller.caller_id if caller else None,
                tenant=caller.tenant if caller else None,
                via=caller.via if caller else None,
                outcome=outcome,
                started=started,
            )


def _find_cause[E: BaseException](exc: BaseException, kind: type[E]) -> E | None:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, kind):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


def _caused_by(exc: BaseException, kind: type[BaseException]) -> bool:
    return _find_cause(exc, kind) is not None


def _friendly_validation(tool: str, err: ValidationError) -> str:
    """Field + rule, never the rejected value (it may be PII or an injection)."""
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in err.errors()
    )
    return f"Invalid arguments for {tool}: {problems}. Correct them and call the tool again."
