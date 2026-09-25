"""Subscription API: the lines (MSISDN/IMSI + plan) under an account."""

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Path, Query

from telco_mcp_lab.mock_apis import data, ids
from telco_mcp_lab.mock_apis.deps import PageDep
from telco_mcp_lab.mock_apis.problems import ApiProblem
from telco_mcp_lab.mock_apis.routers.accounts import AccountIdPath, load_account

router = APIRouter(tags=["Subscription API"])


class SubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"


@router.get("/accounts/{account_id}/subscriptions")
def list_subscriptions(
    account_id: AccountIdPath,
    page: PageDep,
    status: Annotated[SubscriptionStatus | None, Query()] = None,
) -> dict:
    load_account(account_id)
    subs = [
        s
        for s in data.SUBSCRIPTIONS.values()
        if s["account_id"] == account_id and (status is None or s["status"] == status)
    ]
    window = subs[page.offset : page.offset + page.limit]
    return {
        "items": window,
        "next_cursor": page.next_cursor(page.offset + page.limit < len(subs)),
    }


@router.get("/subscriptions/{subscription_id}")
def get_subscription(
    subscription_id: Annotated[str, Path(pattern=ids.SUBSCRIPTION_ID)],
) -> dict:
    return load_subscription(subscription_id)


def load_subscription(subscription_id: str) -> dict:
    sub = data.SUBSCRIPTIONS.get(subscription_id)
    if sub is None:
        raise ApiProblem(
            404, "SUBSCRIPTION_NOT_FOUND", f"No subscription with id {subscription_id}."
        )
    return sub
