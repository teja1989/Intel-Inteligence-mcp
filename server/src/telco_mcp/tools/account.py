"""Account tools.

`get_account_summary` is deliberately *task-oriented*: it answers "what does this
account look like?" in one call by combining the Account API and the
Subscription API. It is not a 1:1 wrapper of GET /account/{id}. Fewer,
higher-level tools mean fewer wrong tool choices and fewer round trips for the
model.

Output is an explicit allow-list (`AccountSummary`). Email, address and the
contact MSISDN are never copied. The holder name is masked unless the client
has `pii:read`. Free-text notes are never returned, only whether one exists
(`has_notes`): a structural answer to prompt injection, not a filter that can be bypassed.
"""

from collections import Counter
from typing import Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from telco_mcp.errors.tool_errors import not_found
from telco_mcp.security.clients import Scope
from telco_mcp.security.scoped_server import ScopedMCPServer, current_client
from telco_mcp.shaping.pii import PiiPolicy
from telco_mcp.state import app_state
from telco_mcp.tools.common import AccountIdArg, gateway_errors


class SubscriptionCounts(BaseModel):
    active: int
    suspended: int
    terminated: int
    total: int


class AccountSummary(BaseModel):
    """What the model gets back. Every field here is a deliberate decision."""

    account_id: str
    account_type: Literal["CONSUMER", "BUSINESS"]
    status: str
    holder_name: str = Field(description="Account holder; masked unless the client may see PII.")
    customer_since: str = Field(description="Date the account was opened (YYYY-MM-DD).")
    subscriptions: SubscriptionCounts
    active_plans: list[str] = Field(description="Distinct plan names on ACTIVE lines.")
    has_notes: bool = Field(
        description="True if people left free-text notes here. The text is never returned: "
        "it is untrusted and could carry instructions. You may tell the user a note exists."
    )


DESCRIPTION = """\
Get a one-call overview of a customer account: status and type, the
holder's name, when it was opened, how many mobile lines (subscriptions) it
has in each status, which plans the active lines are on, and whether it has notes.

Use this when the user asks about their account in general, for example
"what's on my account?", "is my account active?", "how many lines do I have?",
"which plans am I on?".

Do NOT use this for the list of individual lines or phone numbers (use
list_subscriptions), SIM/roaming/add-on details of one line (use
get_service_details), or orders (use get_order_status / list_orders).

Needs the account_id (e.g. ACC-1001). If the user hasn't given it, ask for it.
"""


def register(mcp: ScopedMCPServer) -> None:
    mcp.require_scope("get_account_summary", Scope.READ)

    @mcp.tool(
        name="get_account_summary",
        title="Get account summary",
        description=DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_account_summary(ctx: Context, account_id: AccountIdArg) -> AccountSummary:
        state = app_state(ctx)
        async with gateway_errors():
            account = await state.telco.get_account(account_id)
            if account.get("account_id") != account_id:  # backend inconsistency: don't trust it
                raise ToolError(not_found("account", "ACC-1001"))
            subs = await state.telco.all_subscriptions(account_id)
        # Belt and braces, like the list tools: never count another account's rows,
        # even if the backend ignored the filter.
        subs = [s for s in subs if s.get("account_id") == account_id]

        pii = PiiPolicy(current_client())
        by_status = Counter(s["status"] for s in subs)
        return AccountSummary(
            account_id=account["account_id"],
            account_type=account["type"],
            status=account["status"],
            holder_name=pii.name(account["holder_name"]),
            customer_since=account["created_at"][:10],
            subscriptions=SubscriptionCounts(
                active=by_status["ACTIVE"],
                suspended=by_status["SUSPENDED"],
                terminated=by_status["TERMINATED"],
                total=len(subs),
            ),
            active_plans=sorted({s["plan_name"] for s in subs if s["status"] == "ACTIVE"}),
            has_notes=bool(account.get("notes")),
        )
