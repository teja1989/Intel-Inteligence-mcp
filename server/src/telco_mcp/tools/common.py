"""Shared input types and helpers for tools."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from pydantic import Field

from telco_mcp import ids
from telco_mcp.clients.telco import GatewayError, GatewayUnavailable
from telco_mcp.errors.tool_errors import to_tool_error

AccountIdArg = Annotated[
    str,
    Field(
        pattern=ids.ACCOUNT_ID,
        description="The account ID, e.g. ACC-1001. Use the ID the user gave you or one "
        "returned by a tool; never guess or invent one.",
        examples=["ACC-1001"],
    ),
]
SubscriptionIdArg = Annotated[
    str,
    Field(
        pattern=ids.SUBSCRIPTION_ID,
        description="Subscription (mobile line) ID, e.g. SUB-1001-01. Get it from "
        "list_subscriptions; never guess.",
        examples=["SUB-1001-01"],
    ),
]
OrderIdArg = Annotated[
    str,
    Field(
        pattern=ids.ORDER_ID,
        description="Order ID: 'ORD-' + 6 digits, e.g. ORD-000123. If the user says 'order 123', "
        "pad to 6 digits: ORD-000123.",
        examples=["ORD-000123"],
    ),
]
LimitArg = Annotated[int, Field(ge=1, le=20, description="Page size, 1-20. Default 10.")]
CursorArg = Annotated[
    str | None,
    Field(
        pattern=r"^[A-Za-z0-9_-]{1,200}$",
        description="Opaque next_cursor from the previous page. Omit for the first page.",
    ),
]


@asynccontextmanager
async def gateway_errors() -> AsyncIterator[None]:
    """Translate client exceptions into model-facing tool errors."""
    try:
        yield
    except (GatewayError, GatewayUnavailable) as exc:
        raise to_tool_error(exc) from exc
