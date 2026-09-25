"""Order read tools: status of one order, and the order history."""

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from telco_mcp_lab.mcp_server.security.caller import Scope
from telco_mcp_lab.mcp_server.security.guard import ensure_owned, resolve_account
from telco_mcp_lab.mcp_server.security.scoped_server import ScopedMCPServer, current_caller
from telco_mcp_lab.mcp_server.state import app_state
from telco_mcp_lab.mcp_server.tools.common import (
    AccountIdArg,
    CursorArg,
    LimitArg,
    OrderIdArg,
    gateway_errors,
)


class Order(BaseModel):
    order_id: str
    status: str = Field(description="e.g. SUBMITTED, IN_PROGRESS, COMPLETED, CANCELLED.")
    action: str = Field(description="CHANGE_PLAN, ADD_ADDON or REMOVE_ADDON.")
    target: str = Field(description="Plan or add-on code the order applies.")
    subscription_id: str
    created_at: str


class OrderPage(BaseModel):
    account_id: str
    items: list[Order]
    next_cursor: str | None


STATUS_DESCRIPTION = """\
Get the current status of ONE order by its ID (read-only; it never changes or
cancels anything).

Use this for "what's the status of order 123?", "has my plan change gone
through?", when the user gives an order number.

Order IDs look like ORD-000123. If the user says "order 123", use ORD-000123.
If the user doesn't know the ID, use list_orders instead.
"""

LIST_DESCRIPTION = """\
List the user's orders, newest first (read-only). Paginated.

Use this for "what orders do I have?", "did I change anything recently?", or
to find an order ID. For one known order, prefer get_order_status.
Pagination: pass next_cursor as cursor to get older orders.
"""


def _order(o: dict) -> Order:
    return Order(
        order_id=o["order_id"],
        status=o["status"],
        action=o["action"],
        target=o["target_code"],
        subscription_id=o["subscription_id"],
        created_at=o["created_at"],
    )


def register(mcp: ScopedMCPServer) -> None:
    mcp.require_scope("get_order_status", Scope.READ)
    mcp.require_scope("list_orders", Scope.READ)

    @mcp.tool(
        name="get_order_status",
        title="Get order status",
        description=STATUS_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_order_status(ctx: Context, order_id: OrderIdArg) -> Order:
        caller = current_caller()
        async with gateway_errors():
            order = await app_state(ctx).telco.get_order(order_id)
        return _order(ensure_owned(caller, order, "order", "ORD-000123"))

    @mcp.tool(
        name="list_orders",
        title="List orders",
        description=LIST_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def list_orders(
        ctx: Context,
        account_id: AccountIdArg = None,
        limit: LimitArg = 10,
        cursor: CursorArg = None,
    ) -> OrderPage:
        caller = current_caller()
        account_id = resolve_account(caller, account_id)
        async with gateway_errors():
            page = await app_state(ctx).telco.list_orders(account_id, limit, cursor)
        return OrderPage(
            account_id=account_id,
            items=[_order(o) for o in page["items"] if o.get("account_id") in caller.account_ids],
            next_cursor=page.get("next_cursor"),
        )
