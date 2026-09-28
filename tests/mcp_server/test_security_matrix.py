"""Security matrix: every tool × {existing, nonexistent, malformed} IDs × scopes.

There is no customer boundary (decision 2026-09-27, docs/architecture-security.md §3): a client with
`read` can read any account by ID. What IS proven here:
  1. Every tool declares a scope; a client without it sees no tools and every
     call is refused.
  2. With `read`, every tool works for any existing ID.
  3. A nonexistent ID gives the same "not found" shape for every tool (no
     backend text passes through).
  4. A malformed ID (path traversal, injection, wrong case) is refused by the
     schema BEFORE any backend call, so it can never reach the API URL.
  5. The matrix covers EVERY registered tool; adding a tool without a row fails.

Runs against the real mock gateway (in-process), not mocks of it.
"""

import pytest
import respx
from mcp import Client

from tests.conftest import GATEWAY_URL, make_telco, server_with

pytestmark = pytest.mark.security

# tool -> (args for existing data, nonexistent data, id field)
MATRIX: dict[str, tuple[dict, dict, str]] = {
    "get_account_summary": ({"account_id": "ACC-2001"}, {"account_id": "ACC-9999"}, "account_id"),
    "list_subscriptions": ({"account_id": "ACC-1001"}, {"account_id": "ACC-9999"}, "account_id"),
    "get_service_details": (
        {"subscription_id": "SUB-2001-01"},
        {"subscription_id": "SUB-9999-01"},
        "subscription_id",
    ),
    "get_order_status": ({"order_id": "ORD-000456"}, {"order_id": "ORD-999999"}, "order_id"),
    "list_orders": ({"account_id": "ACC-2001"}, {"account_id": "ACC-9999"}, "account_id"),
}

MALFORMED = [
    "../../admin",
    "ACC-1001/../ACC-2001",
    "ACC-1001?x=1",
    "ACC-1001%2F",
    "acc-1001",
    "ACC-1001 ",
    "ACC-1001\nX: 1",
    "",
    "A" * 500,
]


def text(r) -> str:
    return r.content[0].text


async def test_matrix_covers_every_registered_tool(mock_telco_factory):
    """A new tool must come with a row, or this fails."""
    server = server_with(mock_telco_factory, "read", "pii:read", "order:submit")
    registered = {t.name for t in await super(type(server), server).list_tools()}
    read_tools = {n for n, scope in server.tool_scopes.items() if scope == "read"}
    assert read_tools <= set(MATRIX), f"missing matrix rows: {read_tools - set(MATRIX)}"
    assert registered == set(server.tool_scopes), "every tool must declare a scope"


@pytest.mark.parametrize("tool", sorted(MATRIX))
async def test_read_scope_can_read_any_existing_id(tool, mock_telco_factory):
    existing, _, _ = MATRIX[tool]
    async with Client(server_with(mock_telco_factory, "read")) as c:
        r = await c.call_tool(tool, existing)
    assert not r.is_error, text(r)


@pytest.mark.parametrize("tool", sorted(MATRIX))
async def test_nonexistent_id_is_a_clean_not_found(tool, mock_telco_factory):
    _, missing, _ = MATRIX[tool]
    async with Client(server_with(mock_telco_factory, "read")) as c:
        r = await c.call_tool(tool, missing)
    assert r.is_error and " was found" in text(r) and "Do not guess IDs" in text(r)


@pytest.mark.parametrize("tool", sorted(MATRIX))
async def test_without_read_scope_nothing_is_visible_or_callable(tool, mock_telco_factory):
    existing, _, _ = MATRIX[tool]
    async with Client(server_with(mock_telco_factory, "pii:read")) as c:
        assert (await c.list_tools()).tools == []
        r = await c.call_tool(tool, existing)
    assert r.is_error and f"Unknown tool: {tool}" in text(r)


@pytest.mark.parametrize("tool", sorted(MATRIX))
async def test_malformed_ids_never_reach_the_backend(tool, app, mock_telco_factory):
    _, _, field = MATRIX[tool]
    calls: list[str] = []

    @app.middleware("http")
    async def record(request, call_next):
        calls.append(request.url.path)
        return await call_next(request)

    async with Client(server_with(mock_telco_factory, "read")) as c:
        for bad in MALFORMED:
            r = await c.call_tool(tool, {field: bad})
            assert r.is_error, (tool, bad)
        assert calls == []
        # Positive control: the recorder does see a valid call.
        await c.call_tool(tool, MATRIX[tool][0])
    assert calls


@pytest.mark.parametrize("tool", ["get_account_summary", "list_subscriptions", "list_orders"])
async def test_account_id_is_required(tool, mock_telco_factory):
    async with Client(server_with(mock_telco_factory, "read")) as c:
        r = await c.call_tool(tool, {})
    assert r.is_error and "account_id" in text(r)


# ------------------------------------------------ a backend that ignores the account filter
@pytest.mark.parametrize(
    ("tool", "path", "row_fields"),
    [
        ("list_subscriptions", "/bosubscription/API/subscription", {
            "msisdn": "+447700900111", "plan_name": "Standard 50GB",
            "status": "ACTIVE", "started_at": "2022-01-01",
        }),
        ("list_orders", "/boorder/API/order", {
            "order_id": "ORD-000123", "status": "COMPLETED", "action": "CHANGE_PLAN",
            "target_code": "PLAN-M", "created_at": "2026-09-01T10:00:00Z",
        }),
    ],
)  # fmt: skip
@respx.mock
async def test_rows_of_other_accounts_are_dropped(tool, path, row_fields):
    """Belt and braces: even if the backend ignores ?account_id=, only rows of the
    requested account come back (no customer boundary makes this the last filter)."""

    def row(account: str, sub: str) -> dict:
        return {**row_fields, "account_id": account, "subscription_id": sub}

    respx.get(GATEWAY_URL + path).respond(json={
        "items": [row("ACC-1001", "SUB-1001-01"), row("ACC-2001", "SUB-2001-01")],
        "next_cursor": None,
    })  # fmt: skip
    async with Client(server_with(make_telco)) as c:
        r = await c.call_tool(tool, {"account_id": "ACC-1001"})
    assert not r.is_error, text(r)
    items = r.structured_content["items"]
    assert [i["subscription_id"] for i in items] == ["SUB-1001-01"]
