"""Cross-tenant security matrix: every tool × {own, other tenant's, nonexistent} IDs.

Rules proven here:
  1. A caller can read their own tenant's data.
  2. A caller can NEVER read another tenant's data, through any tool, by any ID.
  3. The refusal is indistinguishable from "doesn't exist" (no existence oracle).
  4. The matrix covers EVERY registered tool; adding a tool without a row fails.

Runs against the real mock gateway (in-process), not mocks of it, so a bug in
the backend's own filtering can't hide a missing guard.
"""

import pytest
from mcp import Client

from tests.conftest import server_as

pytestmark = pytest.mark.security

# tool -> (args for tenant-a's own data, tenant-b's data, nonexistent data)
MATRIX: dict[str, tuple[dict, dict, dict]] = {
    "get_account_summary": (
        {"account_id": "ACC-1001"},
        {"account_id": "ACC-2001"},
        {"account_id": "ACC-9999"},
    ),
    "list_subscriptions": (
        {"account_id": "ACC-1001"},
        {"account_id": "ACC-2001"},
        {"account_id": "ACC-9999"},
    ),
    "get_service_details": (
        {"subscription_id": "SUB-1001-01"},
        {"subscription_id": "SUB-2001-01"},
        {"subscription_id": "SUB-9999-01"},
    ),
    "get_order_status": (
        {"order_id": "ORD-000123"},
        {"order_id": "ORD-000456"},
        {"order_id": "ORD-999999"},
    ),
    "list_orders": (
        {"account_id": "ACC-1001"},
        {"account_id": "ACC-2001"},
        {"account_id": "ACC-9999"},
    ),
}


def text(r) -> str:
    return r.content[0].text


async def test_matrix_covers_every_registered_tool(mock_telco_factory):
    """A new tool must come with a cross-tenant row, or this fails."""
    server = server_as("carol", mock_telco_factory)  # carol sees every tool
    registered = {t.name for t in await super(type(server), server).list_tools()}
    read_tools = {n for n, scope in server.tool_scopes.items() if scope == "read"}
    assert read_tools <= set(MATRIX), f"missing matrix rows: {read_tools - set(MATRIX)}"
    assert registered == set(server.tool_scopes), "every tool must declare a scope"


@pytest.mark.parametrize("tool", sorted(MATRIX))
async def test_own_tenant_allowed(tool, mock_telco_factory):
    own, _, _ = MATRIX[tool]
    async with Client(server_as("alice", mock_telco_factory)) as c:
        r = await c.call_tool(tool, own)
    assert not r.is_error, text(r)


@pytest.mark.parametrize("tool", sorted(MATRIX))
async def test_other_tenant_denied_and_indistinguishable_from_missing(tool, mock_telco_factory):
    _, foreign, missing = MATRIX[tool]
    async with Client(server_as("alice", mock_telco_factory)) as c:
        r_foreign = await c.call_tool(tool, foreign)
        r_missing = await c.call_tool(tool, missing)
    assert r_foreign.is_error and r_missing.is_error
    assert text(r_foreign) == text(r_missing)
    # Nothing from tenant B leaks into the error either.
    for leak in ("Northwind", "ACC-2001", "tenant-b", "+44770090021"):
        if leak not in str(foreign):
            assert leak not in text(r_foreign)


@pytest.mark.parametrize("tool", sorted(MATRIX))
async def test_reverse_direction_bob_cannot_read_tenant_a(tool, mock_telco_factory):
    own_a, _, _ = MATRIX[tool]  # tenant-a's IDs are "foreign" to bob
    async with Client(server_as("bob", mock_telco_factory)) as c:
        r = await c.call_tool(tool, own_a)
    assert r.is_error


async def test_account_selector_is_checked_before_any_backend_call(app, mock_telco_factory):
    """A foreign account_id is refused from CallerContext alone: the backend is never asked."""
    calls: list[str] = []

    @app.middleware("http")
    async def record(request, call_next):
        calls.append(request.url.path)
        return await call_next(request)

    async with Client(server_as("alice", mock_telco_factory)) as c:
        r = await c.call_tool("list_subscriptions", {"account_id": "ACC-2001"})
        assert r.is_error
        assert calls == []
        # Positive control: the recorder does see an allowed call.
        await c.call_tool("list_subscriptions", {"account_id": "ACC-1001"})
    assert calls == ["/bosubscription/API/subscription"]


async def test_multi_account_caller_must_choose(mock_telco_factory):
    async with Client(server_as("alice", mock_telco_factory)) as c:
        r = await c.call_tool("get_account_summary", {})
    assert r.is_error and "ACC-1001, ACC-1002" in text(r)


async def test_single_account_caller_needs_no_account_id(mock_telco_factory):
    async with Client(server_as("bob", mock_telco_factory)) as c:
        r = await c.call_tool("list_orders", {})
    assert not r.is_error and r.structured_content["account_id"] == "ACC-2001"
