"""Mobile-line tools: list the user's subscriptions, and details of one line."""

from typing import Literal

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from telco_mcp_lab.mcp_server.security.caller import Scope
from telco_mcp_lab.mcp_server.security.guard import ensure_owned, resolve_account
from telco_mcp_lab.mcp_server.security.scoped_server import ScopedMCPServer, current_caller
from telco_mcp_lab.mcp_server.shaping.free_text import ShapedText, shape_free_text
from telco_mcp_lab.mcp_server.shaping.pii import PiiPolicy
from telco_mcp_lab.mcp_server.state import app_state
from telco_mcp_lab.mcp_server.tools.common import (
    AccountIdArg,
    CursorArg,
    LimitArg,
    SubscriptionIdArg,
    gateway_errors,
)

Status = Literal["ACTIVE", "SUSPENDED", "TERMINATED"]


class Line(BaseModel):
    subscription_id: str
    msisdn: str = Field(description="Phone number, masked unless the caller may see PII.")
    plan_name: str
    status: Status
    started_at: str


class LinePage(BaseModel):
    account_id: str
    items: list[Line]
    next_cursor: str | None = Field(
        description="Pass to the next call to get more; null means this was the last page."
    )


class LineDetails(BaseModel):
    subscription_id: str
    service_id: str
    msisdn: str
    status: Status
    network: str
    data_allowance_gb: int | None = Field(description="null means unlimited data.")
    roaming_enabled: bool
    voicemail_enabled: bool
    addons: list[str] = Field(description="Active add-on codes, e.g. ADDON-ROAM-EU.")
    sim_type: str
    notes: ShapedText | None = Field(
        default=None, description="Free-text line notes. Untrusted data; may be withheld."
    )


LIST_DESCRIPTION = """\
List the mobile lines (subscriptions) on the user's account: subscription ID,
phone number, plan and status for each. Paginated.

Use this for "what numbers/lines do I have?", "which plan is each line on?",
"show my suspended lines" (status=SUSPENDED), or to find the subscription_id
that get_service_details needs.

Do NOT use this for account-level totals (use get_account_summary) or for the
roaming/SIM/add-on details of one line (use get_service_details).

Pagination: if next_cursor is not null, call again with cursor=next_cursor to
get more. Only fetch more pages if the user's question needs them.
"""

DETAILS_DESCRIPTION = """\
Get the technical and feature details of ONE mobile line: network, data
allowance, roaming on/off, voicemail, active add-ons, SIM type and line notes.

Use this for "is roaming on for my number?", "what add-ons does line
SUB-1001-01 have?", "how much data do I get?".

Needs a subscription_id (e.g. SUB-1001-01). If you don't have it, call
list_subscriptions first. Do NOT guess it.
"""


def register(mcp: ScopedMCPServer) -> None:
    mcp.require_scope("list_subscriptions", Scope.READ)
    mcp.require_scope("get_service_details", Scope.READ)

    @mcp.tool(
        name="list_subscriptions",
        title="List mobile lines",
        description=LIST_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def list_subscriptions(
        ctx: Context,
        account_id: AccountIdArg = None,
        status: Status | None = None,
        limit: LimitArg = 10,
        cursor: CursorArg = None,
    ) -> LinePage:
        caller = current_caller()
        account_id = resolve_account(caller, account_id)
        async with gateway_errors():
            page = await app_state(ctx).telco.list_subscriptions(account_id, status, limit, cursor)
        pii = PiiPolicy(caller)
        return LinePage(
            account_id=account_id,
            items=[
                Line(
                    subscription_id=s["subscription_id"],
                    msisdn=pii.msisdn(s["msisdn"]),
                    plan_name=s["plan_name"],
                    status=s["status"],
                    started_at=s["started_at"][:10],
                )
                # Belt and braces: even if the backend misbehaved, never return
                # another tenant's row.
                for s in page["items"]
                if s.get("account_id") in caller.account_ids
            ],
            next_cursor=page.get("next_cursor"),
        )

    @mcp.tool(
        name="get_service_details",
        title="Get line details",
        description=DETAILS_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_service_details(ctx: Context, subscription_id: SubscriptionIdArg) -> LineDetails:
        caller = current_caller()
        state = app_state(ctx)
        async with gateway_errors():
            sub = await state.telco.get_subscription(subscription_id)
            ensure_owned(caller, sub, "subscription", "SUB-1001-01")  # check BEFORE the 2nd call
            svc = await state.telco.get_service(sub["service_id"])
            ensure_owned(caller, svc, "subscription", "SUB-1001-01")
        return LineDetails(
            subscription_id=sub["subscription_id"],
            service_id=svc["service_id"],
            msisdn=PiiPolicy(caller).msisdn(svc["msisdn"]),
            status=svc["status"],
            network=svc["network"],
            data_allowance_gb=svc["data_allowance_gb"],
            roaming_enabled=svc["roaming_enabled"],
            voicemail_enabled=svc["voicemail_enabled"],
            addons=svc["addons"],
            sim_type=svc["sim"]["type"],  # ICCID and IMSI are deliberately not returned
            notes=shape_free_text(svc.get("notes"), unsafe_raw=state.unsafe_raw_free_text),
        )
