"""Account API: account profile, including PII and a free-text notes field."""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from telco_mcp_lab.mock_apis import data, ids
from telco_mcp_lab.mock_apis.problems import ApiProblem

router = APIRouter(prefix="/account", tags=["Account API"])

AccountIdPath = Annotated[str, Path(pattern=ids.ACCOUNT_ID)]
AccountIdQuery = Annotated[str, Query(pattern=ids.ACCOUNT_ID)]


def load_account(account_id: str) -> dict:
    account = data.ACCOUNTS.get(account_id)
    if account is None:
        raise ApiProblem(404, "ACCOUNT_NOT_FOUND", f"No account with id {account_id}.")
    return account


@router.get("/{account_id}")
def get_account(account_id: AccountIdPath) -> dict:
    return load_account(account_id)
