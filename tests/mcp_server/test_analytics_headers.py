"""Analytics headers on gateway calls (clients/analytics_headers.py).

The gateway must learn which client and tool caused each backend call, from the
VALIDATED identity only: never a header the caller sent, never the caller's token.
"""

import asyncio
from dataclasses import replace

import httpx
import pytest
import respx
from mcp import Client
from pydantic import SecretStr, ValidationError

from telco_mcp.clients.analytics_headers import AnalyticsHeaders
from telco_mcp.clients.gateway import GatewayClientSettings
from telco_mcp.request_context import CallIdentity, bind_call
from telco_mcp_lab.mock_apis.app import create_app
from telco_mcp_lab.mock_apis.settings import MockApiSettings
from tests.conftest import (
    GATEWAY_URL,
    TEST_TOKEN,
    LiveServer,
    http_app_in,
    jwt_token,
    make_telco,
    server_with,
)
from tests.mcp_server.test_observability import call_tool

pytestmark = pytest.mark.security


def settings(**kw) -> GatewayClientSettings:
    return GatewayClientSettings(_env_file=None, token=SecretStr(TEST_TOKEN), **kw)


class TestHeaderNames:
    def test_defaults(self):
        s = settings()
        assert (s.header_client_id, s.header_tool) == ("X-Client-Id", "X-MCP-Tool")

    def test_names_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_HEADER_CLIENT_ID", "X-Consumer-Id")
        monkeypatch.setenv("GATEWAY_HEADER_TOOL", "")
        s = settings()
        assert (s.header_client_id, s.header_tool) == ("X-Consumer-Id", "")

    @pytest.mark.parametrize(
        "name",
        ["Authorization", "authorization", "Proxy-Authorization", "Cookie", "Host",
         "traceparent", "baggage", "Content-Type", "Idempotency-Key", "Sec-Fetch-Site",
         "X Client", "X-Client-Id\r\nX-Evil", "X_Client"],
    )  # fmt: skip
    def test_reserved_or_malformed_names_refused(self, name):
        with pytest.raises(ValidationError, match="GATEWAY_HEADER_CLIENT_ID"):
            settings(header_client_id=name)

    def test_the_two_names_must_differ(self):
        with pytest.raises(ValueError, match="must differ"):
            AnalyticsHeaders("X-Id", "x-id")


class TestValues:
    def test_nothing_outside_a_tool_call(self):
        assert AnalyticsHeaders("X-Client-Id", "X-MCP-Tool").for_current_call() == {}

    def test_values_from_the_call_identity(self):
        with bind_call(CallIdentity("care-agent", "list_orders")):
            h = AnalyticsHeaders("X-Client-Id", "X-MCP-Tool").for_current_call()
        assert h == {"X-Client-Id": "care-agent", "X-MCP-Tool": "list_orders"}

    def test_odd_values_are_not_passed_through(self):
        with bind_call(CallIdentity("bad value\twith\ttabs", "t")):
            h = AnalyticsHeaders("X-Client-Id", "").for_current_call()
        assert h == {"X-Client-Id": "invalid"}

    def test_disabled_header_is_not_sent(self):
        with bind_call(CallIdentity("c", "t")):
            assert AnalyticsHeaders("", "").for_current_call() == {}


# ------------------------------------------------------------------------ end to end
@pytest.fixture
def recording_gateway(tmp_path):
    seen: list[dict[str, str]] = []
    app = create_app(MockApiSettings(_env_file=None, gateway_token=SecretStr(TEST_TOKEN),
                                     db_path=tmp_path / "gw.sqlite3"))  # fmt: skip

    @app.middleware("http")
    async def record(request, call_next):
        seen.append({k.lower(): v for k, v in request.headers.items()})
        return await call_next(request)

    with LiveServer(app) as srv:
        yield srv.url, seen


@pytest.mark.protocol
def test_gateway_gets_validated_identity_never_the_callers_token(recording_gateway, tmp_path):
    gw_url, seen = recording_gateway
    token = jwt_token("agent-a")
    with LiveServer(http_app_in(tmp_path, lambda: make_telco(base_url=gw_url))) as srv:
        # The caller tries to spoof the analytics identity with its own headers.
        r = call_tool(srv.url, token, {"X-Client-Id": "someone-else", "X-MCP-Tool": "fake"})
        assert r.status_code == 200 and not r.json()["result"]["isError"]
    (gw,) = seen
    assert gw["x-client-id"] == "agent-a" and gw["x-mcp-tool"] == "list_subscriptions"
    assert gw["authorization"] == f"Bearer {TEST_TOKEN}"  # the server's own token
    assert all(token not in v and token.split(".")[2] not in v for v in gw.values())


@respx.mock
async def test_concurrent_calls_never_get_each_others_identity():
    """Interleave two clients' calls and check every gateway request carries the identity
    of the call that made it. get_account_summary makes its SECOND gateway call after an
    await, which is exactly where request state kept in a shared place would bleed."""
    seen: list[tuple[str, str]] = []

    def account(account_id: str) -> dict:
        return {"account_id": account_id, "type": "CONSUMER", "status": "ACTIVE",
                "holder_name": "Alex Example", "created_at": "2021-03-14T09:26:53Z"}  # fmt: skip

    async def get_account(request):
        acc = request.url.path.rsplit("/", 1)[-1]
        seen.append((request.headers["x-client-id"], acc))
        await asyncio.sleep(0.01)  # let the other client's call run in between
        return httpx.Response(200, json=account(acc))

    async def list_subs(request):
        seen.append((request.headers["x-client-id"], request.url.params["account_id"]))
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    respx.get(url__regex=rf"{GATEWAY_URL}/boaccount/API/account/.*").mock(side_effect=get_account)
    respx.get(f"{GATEWAY_URL}/bosubscription/API/subscription").mock(side_effect=list_subs)

    async def run(client_id: str, account_id: str) -> None:
        server = server_with(make_telco, "read")
        server.fallback_client = replace(server.fallback_client, client_id=client_id)
        async with Client(server) as c:
            for _ in range(5):
                r = await c.call_tool("get_account_summary", {"account_id": account_id})
                assert not r.is_error, r.content[0].text

    await asyncio.gather(run("agent-a", "ACC-1001"), run("agent-b", "ACC-2001"))
    expected = {"agent-a": "ACC-1001", "agent-b": "ACC-2001"}
    assert len(seen) == 20 and all(expected[c] == acc for c, acc in seen), seen
