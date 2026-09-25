"""Order API: read orders, and create *draft* orders (reversible, nothing executes).

A draft is a quote: it prices the change and expires. Only the Order Submission
API turns a draft into a real order. That split is the backbone of the
"prepare, then confirm" pattern for destructive MCP tools.
"""

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, ConfigDict, Field

from telco_mcp_lab import ids
from telco_mcp_lab.mock_apis import data
from telco_mcp_lab.mock_apis.deps import PageDep, StoreDep
from telco_mcp_lab.mock_apis.problems import ApiProblem
from telco_mcp_lab.mock_apis.routers.accounts import AccountIdQuery, load_account
from telco_mcp_lab.mock_apis.routers.subscriptions import load_subscription

router = APIRouter(prefix="/order", tags=["Order API"])


class OrderAction(StrEnum):
    CHANGE_PLAN = "CHANGE_PLAN"
    ADD_ADDON = "ADD_ADDON"
    REMOVE_ADDON = "REMOVE_ADDON"


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # reject unknown fields outright

    account_id: str = Field(pattern=ids.ACCOUNT_ID)
    subscription_id: str = Field(pattern=ids.SUBSCRIPTION_ID)
    action: OrderAction
    target_code: str = Field(pattern=r"^(PLAN|ADDON)-[A-Z0-9-]{1,20}$")


@router.get("")
def list_orders(account_id: AccountIdQuery, page: PageDep, store: StoreDep) -> dict:
    load_account(account_id)
    items, has_more = store.list_orders(account_id, page.offset, page.limit)
    return {"items": items, "next_cursor": page.next_cursor(has_more)}


@router.get("/{order_id}")
def get_order(order_id: Annotated[str, Path(pattern=ids.ORDER_ID)], store: StoreDep) -> dict:
    order = store.get_order(order_id)
    if order is None:
        raise ApiProblem(404, "ORDER_NOT_FOUND", f"No order with id {order_id}.")
    return order


@router.post("/draft", status_code=status.HTTP_201_CREATED)
def create_draft(req: DraftRequest, store: StoreDep) -> dict:
    load_account(req.account_id)
    sub = load_subscription(req.subscription_id)
    if sub["account_id"] != req.account_id:
        # Same message as "not found": don't reveal that the subscription
        # exists under some other account.
        raise ApiProblem(
            404, "SUBSCRIPTION_NOT_FOUND", f"No subscription with id {req.subscription_id}."
        )
    if sub["status"] != "ACTIVE":
        raise ApiProblem(
            409,
            "SUBSCRIPTION_NOT_ACTIVE",
            f"Subscription is {sub['status']}; only ACTIVE subscriptions can be changed.",
        )
    _validate_target(sub, req.action, req.target_code)
    return store.create_draft(sub, req.action.value, req.target_code)


@router.get("/draft/{draft_id}")
def get_draft(draft_id: Annotated[str, Path(pattern=ids.DRAFT_ID)], store: StoreDep) -> dict:
    draft = store.get_draft(draft_id)
    if draft is None:
        raise ApiProblem(404, "DRAFT_NOT_FOUND", f"No draft with id {draft_id}.")
    return draft


def _validate_target(sub: dict, action: OrderAction, target: str) -> None:
    current_addons = data.SERVICES[sub["service_id"]]["addons"]
    if action is OrderAction.CHANGE_PLAN:
        if target not in data.PLANS:
            raise ApiProblem(
                422, "UNKNOWN_PLAN", f"Unknown plan {target}.", valid_values=sorted(data.PLANS)
            )
        if target == sub["plan_code"]:
            raise ApiProblem(409, "NO_CHANGE", f"Subscription is already on {target}.")
        return
    if target not in data.ADDONS:
        raise ApiProblem(
            422, "UNKNOWN_ADDON", f"Unknown add-on {target}.", valid_values=sorted(data.ADDONS)
        )
    if action is OrderAction.ADD_ADDON and target in current_addons:
        raise ApiProblem(409, "NO_CHANGE", f"Add-on {target} is already active.")
    if action is OrderAction.REMOVE_ADDON and target not in current_addons:
        raise ApiProblem(409, "NO_CHANGE", f"Add-on {target} is not active on this line.")
