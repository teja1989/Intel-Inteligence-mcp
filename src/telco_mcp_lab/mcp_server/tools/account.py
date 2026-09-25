"""Account tools.

`get_account_summary` is deliberately *task-oriented*: it answers "what does my
account look like?" in one call by combining the Account API and the
Subscription API. It is not a 1:1 wrapper of GET /account/{id}. Fewer,
higher-level tools mean fewer wrong tool choices and fewer round trips for the
model.

Output is an explicit allow-list (`AccountSummary`). Fields we don't list
(email, address, contact MSISDN, free-text notes) can't leak, because they're
never copied. Phase 3 adds masking of the remaining PII (holder name).
"""

from collections import Counter
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from telco_mcp_lab import ids
from telco_mcp_lab.mcp_server.clients.telco import GatewayError, GatewayUnavailable
from telco_mcp_lab.mcp_server.errors.tool_errors import to_tool_error
from telco_mcp_lab.mcp_server.state import app_state

AccountId = Annotated[
    str,
    Field(
        pattern=ids.ACCOUNT_ID,
        description="Customer account ID, format ACC- followed by 4 digits, e.g. ACC-1001.",
        examples=["ACC-1001"],
    ),
]


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
    holder_name: str = Field(description="Account holder (PII; masked from Phase 3).")
    customer_since: str = Field(description="Date the account was opened (YYYY-MM-DD).")
    subscriptions: SubscriptionCounts
    active_plans: list[str] = Field(description="Distinct plan names on ACTIVE lines.")


DESCRIPTION = """\
Get a one-call overview of a single customer account: account status and type,
the holder's name, when it was opened, how many mobile lines (subscriptions) it
has in each status, and which plans the active lines are on.

Use this when the user asks about their account in general, for example
"what's on my account?", "is my account active?", "how many lines do I have?",
"which plans am I on?".

Do NOT use this for details of one specific line (phone number, SIM, roaming,
add-ons) or for orders. It returns counts and plan names only.

Input: account_id such as "ACC-1001". Never guess an ID; ask the user if unknown.
"""


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        name="get_account_summary",
        title="Get account summary",
        description=DESCRIPTION,
        annotations=ToolAnnotations(
            read_only_hint=True,  # changes nothing
            open_world_hint=False,  # closed domain: our own backend, not the internet
        ),
    )
    async def get_account_summary(account_id: AccountId, ctx: Context) -> AccountSummary:
        api = app_state(ctx).telco
        try:
            account = await api.get_account(account_id)
            subs = await api.all_subscriptions(account_id)
        except (GatewayError, GatewayUnavailable) as exc:
            raise to_tool_error(exc) from exc

        by_status = Counter(s["status"] for s in subs)
        return AccountSummary(
            account_id=account["account_id"],
            account_type=account["type"],
            status=account["status"],
            holder_name=account["holder_name"],
            customer_since=account["created_at"][:10],
            subscriptions=SubscriptionCounts(
                active=by_status["ACTIVE"],
                suspended=by_status["SUSPENDED"],
                terminated=by_status["TERMINATED"],
                total=len(subs),
            ),
            active_plans=sorted({s["plan_name"] for s in subs if s["status"] == "ACTIVE"}),
        )
