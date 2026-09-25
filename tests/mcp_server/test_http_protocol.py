"""Protocol + security tests over REAL Streamable HTTP (uvicorn on a real port).

Covers: bearer auth (401 + RFC 9728 metadata pointer), DNS-rebinding/Origin
protection, both protocol eras, per-caller tools/list over the wire, and the
scaling experiment: two replicas behind a round-robin load balancer.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from telco_mcp_lab.devtools.round_robin_lb import build_app as build_lb
from telco_mcp_lab.mcp_server.http_app import build_http_app
from telco_mcp_lab.mcp_server.settings import McpServerSettings
from tests.conftest import (
    ACCESS_MODEL,
    CALLER_ENV,
    CALLER_TOKENS,
    TEST_TOKEN,
    LiveServer,
    make_telco,
)

pytestmark = pytest.mark.protocol

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def mcp_app(gateway_url: str, *, legacy_sessions: bool = False):
    settings = McpServerSettings(_env_file=None)
    return build_http_app(
        settings,
        ACCESS_MODEL,
        CALLER_ENV,
        legacy_sessions=legacy_sessions,
        telco_factory=lambda: make_telco(base_url=gateway_url),
    )


@contextmanager
def mcp_servers(gateway_url: str, n: int = 1, **kw) -> Iterator[list[str]]:
    with ExitStack() as stack:
        yield [stack.enter_context(LiveServer(mcp_app(gateway_url, **kw))).url for _ in range(n)]


@pytest.fixture
def mcp_url(live_gateway) -> Iterator[str]:
    with mcp_servers(live_gateway) as (url,):
        yield url + "/mcp"


def auth(caller: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {CALLER_TOKENS[caller]}"}


def modern_post(url: str, method: str, params: dict, headers: dict, name: str | None = None):
    h = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
        **headers,
    }
    if name:
        h["Mcp-Name"] = name
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": META}}
    return httpx.post(url, json=body, headers=h, timeout=10)


async def run_client(url: str, caller: str, mode: str, served: list | None = None):
    hooks = {"response": [lambda r: _record(r, served)]} if served is not None else {}
    async with (
        httpx2.AsyncClient(headers=auth(caller), event_hooks=hooks) as h,
        Client(streamable_http_client(url, http_client=h), mode=mode) as c,
    ):
        tools = [t.name for t in (await c.list_tools()).tools]
        results = [await c.call_tool("list_orders", {"account_id": "ACC-1001"}) for _ in range(3)]
        return c.session.protocol_version, tools, results


async def _record(resp, served: list) -> None:
    served.append((resp.headers.get("x-served-by"), resp.status_code))


# ------------------------------------------------------------------------------- auth
@pytest.mark.security
class TestHttpAuth:
    def test_no_token_is_401_with_protected_resource_metadata(self, mcp_url):
        r = modern_post(mcp_url, "tools/list", {}, {})
        assert r.status_code == 401
        challenge = r.headers["www-authenticate"]
        assert challenge.startswith("Bearer") and 'error="invalid_token"' in challenge
        assert "resource_metadata=" in challenge  # RFC 9728: where to learn how to get a token

    def test_wrong_token_is_401(self, mcp_url):
        r = modern_post(mcp_url, "tools/list", {}, {"Authorization": "Bearer " + "x" * 40})
        assert r.status_code == 401

    def test_gateway_token_is_not_an_mcp_token(self, mcp_url):
        """Audience separation: the server's OWN downstream token doesn't open the front door."""
        r = modern_post(mcp_url, "tools/list", {}, {"Authorization": f"Bearer {TEST_TOKEN}"})
        assert r.status_code == 401

    def test_foreign_origin_is_rejected(self, mcp_url):
        """DNS-rebinding protection: a web page on evil.example can't drive a local server."""
        r = modern_post(
            mcp_url, "tools/list", {}, {**auth("alice"), "Origin": "http://evil.example"}
        )
        assert r.status_code == 403

    def test_valid_token_lists_private_uncacheable_tools(self, mcp_url):
        r = modern_post(mcp_url, "tools/list", {}, auth("alice"))
        assert r.status_code == 200
        result = r.json()["result"]
        assert len(result["tools"]) == 5
        # Filtered per caller, so it must never be served from a shared cache.
        assert result["cacheScope"] == "private"

    def test_no_scope_caller_sees_empty_list_over_the_wire(self, mcp_url):
        r = modern_post(mcp_url, "tools/list", {}, auth("mallory"))
        assert r.json()["result"]["tools"] == []

    def test_cross_tenant_over_http(self, mcp_url):
        args = {"name": "get_order_status", "arguments": {"order_id": "ORD-000456"}}
        r = modern_post(mcp_url, "tools/call", args, auth("alice"), name="get_order_status")
        assert r.json()["result"]["isError"] is True


# ------------------------------------------------------------------------------ both eras
@pytest.mark.parametrize(("mode", "version"), [("auto", "2026-07-28"), ("legacy", "2025-11-25")])
async def test_sdk_client_over_http_both_eras(mcp_url, mode, version):
    negotiated, tools, results = await run_client(mcp_url, "alice", mode)
    assert negotiated == version
    assert len(tools) == 5
    assert all(not r.is_error for r in results)


# ----------------------------------------------------------- scaling: 2 replicas + round robin
class TestHorizontalScaling:
    """Round-robin LB, no stickiness: what Cloud Foundry's gorouter does by default."""

    @contextmanager
    def cluster(self, live_gateway, legacy_sessions: bool) -> Iterator[str]:
        with (
            mcp_servers(live_gateway, n=2, legacy_sessions=legacy_sessions) as urls,
            LiveServer(build_lb(urls)) as lb,
        ):
            yield lb.url + "/mcp"

    @pytest.mark.parametrize("mode", ["auto", "legacy"])
    async def test_stateless_replicas_serve_both_eras(self, live_gateway, mode):
        served: list = []
        with self.cluster(live_gateway, legacy_sessions=False) as url:
            _, tools, results = await run_client(url, "alice", mode, served)
        assert len(tools) == 5 and all(not r.is_error for r in results)
        replicas = {port for port, _ in served}
        assert len(replicas) == 2, "requests really were spread over both replicas"

    async def test_modern_clients_scale_even_with_legacy_sessions_on(self, live_gateway):
        """2026-07-28 never uses sessions, so the switch doesn't matter to it."""
        with self.cluster(live_gateway, legacy_sessions=True) as url:
            version, _, results = await run_client(url, "alice", "auto")
        assert version == "2026-07-28" and all(not r.is_error for r in results)

    async def test_legacy_sessions_break_behind_round_robin(self, live_gateway):
        """The failure stateless_http prevents: replica B doesn't know replica A's session."""
        served: list = []
        with (
            self.cluster(live_gateway, legacy_sessions=True) as url,
            pytest.raises(Exception),  # noqa: B017 - SDK wraps it in an ExceptionGroup
        ):
            await run_client(url, "alice", "legacy", served)
        # served = (replica, status) in completion order. The replica that answered
        # `initialize` minted the session; every 404 comes from the OTHER one.
        session_owner = served[0][0]
        not_found = [replica for replica, status in served if status == 404]
        assert not_found, served
        assert session_owner not in not_found, served
