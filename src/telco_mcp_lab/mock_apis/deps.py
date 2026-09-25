"""Shared FastAPI dependencies: service authentication, store access, pagination."""

import base64
import binascii
import hmac
import json
from typing import Annotated

from fastapi import Depends, Header, Query, Request

from telco_mcp_lab.mock_apis.problems import ApiProblem
from telco_mcp_lab.mock_apis.store import OrderStore


def require_service_key(
    request: Request, x_api_key: Annotated[str | None, Header()] = None
) -> None:
    """Service-to-service auth: only our MCP server holds this key.

    `hmac.compare_digest` is constant-time, so response timing does not leak
    how many leading characters of a guessed key were right.
    """
    expected: str = request.app.state.settings.api_key.get_secret_value()
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise ApiProblem(401, "UNAUTHENTICATED", "Missing or invalid X-Api-Key.")


def get_store(request: Request) -> OrderStore:
    return request.app.state.store


StoreDep = Annotated[OrderStore, Depends(get_store)]


def _bad_cursor() -> ApiProblem:
    return ApiProblem(400, "INVALID_CURSOR", "Cursor is malformed; omit it to restart.")


class Page:
    """Opaque cursor pagination. The cursor is base64url(JSON offset).

    Clients must treat it as opaque. Cursor pagination is also what MCP uses
    for `tools/list`, so the concept carries over.
    """

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ) -> None:
        self.limit = limit
        self.offset = self._decode(cursor) if cursor else 0

    @staticmethod
    def _decode(cursor: str) -> int:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode() + b"=" * (-len(cursor) % 4))
            offset = json.loads(raw)["o"]
        except (binascii.Error, ValueError, KeyError, TypeError) as exc:
            raise _bad_cursor() from exc
        if not isinstance(offset, int) or offset < 0:
            raise _bad_cursor()
        return offset

    @staticmethod
    def encode(offset: int) -> str:
        return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")

    def next_cursor(self, has_more: bool) -> str | None:
        return self.encode(self.offset + self.limit) if has_more else None


PageDep = Annotated[Page, Depends()]
