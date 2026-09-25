"""Order Submission API: turns a draft into a real order. Idempotent.

Contract (modelled on the IETF `Idempotency-Key` header draft and Stripe):
  * `Idempotency-Key` header is REQUIRED       -> 400 if missing or malformed
  * first successful call                      -> 201 Created
  * same key + same draft again                -> 200 with the ORIGINAL body,
                                                  header `Idempotent-Replayed: true`
  * same key + different draft                 -> 422 IDEMPOTENCY_KEY_REUSED
  * draft already submitted under another key  -> 409 DRAFT_ALREADY_SUBMITTED
  * draft expired                              -> 410 DRAFT_EXPIRED
Only successes are remembered. A failed attempt can be retried with the same
key once the cause is fixed.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from telco_mcp_lab import ids
from telco_mcp_lab.mock_apis.deps import StoreDep
from telco_mcp_lab.mock_apis.problems import ApiProblem
from telco_mcp_lab.mock_apis.store import StoreError

router = APIRouter(prefix="/submission", tags=["Order Submission API"])

_KEY_RE = re.compile(ids.IDEMPOTENCY_KEY)


class SubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: str = Field(pattern=ids.DRAFT_ID)


@router.post("", status_code=201)
def submit_order(
    req: SubmissionRequest,
    response: Response,
    store: StoreDep,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict:
    if idempotency_key is None or not _KEY_RE.fullmatch(idempotency_key):
        raise ApiProblem(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Header Idempotency-Key is required: 8-128 chars of [A-Za-z0-9_-] (a UUID works).",
        )
    try:
        result = store.submit(req.draft_id, idempotency_key)
    except StoreError as err:
        raise ApiProblem(err.status, err.code, err.detail, **err.extra) from err
    if result.replayed:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
    return result.body
