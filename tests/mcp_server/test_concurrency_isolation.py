"""Cross-customer isolation under concurrency: many interleaved requests, 2 replicas.

Sequential tests can't catch the classic bleed bug (request state kept somewhere
shared instead of per request): only interleaving shows it. The canary test
plants exactly that bug and asserts this probe catches it, so the probe can't
silently lose its teeth.
"""

import asyncio
import json
import random
from contextlib import ExitStack

import httpx
import pytest

from telco_mcp_lab.devtools.round_robin_lb import build_app as build_lb
from telco_mcp_lab.devtools.token_issuer import generate_key, jwks_for, mint
from telco_mcp_lab.mcp_server.http_app import build_http_app
from telco_mcp_lab.mcp_server.security import clients as clients_module
from telco_mcp_lab.mcp_server.settings import JwtSettings, McpServerSettings
from tests.conftest import ACCESS_MODEL, LiveServer, make_telco

pytestmark = [pytest.mark.security, pytest.mark.protocol, pytest.mark.slow]

ISS, AUD = "https://ts.invalid", "https://mcp.invalid/mcp"
KEY = generate_key()
CUSTOMERS = ["ACC-1001", "ACC-1002", "ACC-2001"]
TOOLS = [("get_account_summary", {}), ("list_subscriptions", {}), ("list_orders", {})]
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture
def cluster_url(live_gateway, tmp_path):
    (tmp_path / "jwks.json").write_text(json.dumps(jwks_for(KEY)))
    (tmp_path / "clients.json").write_text(json.dumps({
        "scope_map": {"read": "read"},
        "clients": {"care": {"mode": "customer_context", "allowed_scopes": ["read"]}},
    }))  # fmt: skip
    settings = McpServerSettings(
        _env_file=None, auth_mode="jwt", clients_config=tmp_path / "clients.json", public_url=AUD
    )
    js = JwtSettings(_env_file=None, issuer=ISS, audience=AUD, jwks_file=tmp_path / "jwks.json")

    def replica():
        return build_http_app(settings, ACCESS_MODEL, {}, jwt_settings=js,
                              telco_factory=lambda: make_telco(base_url=live_gateway))  # fmt: skip

    with ExitStack() as stack:
        urls = [stack.enter_context(LiveServer(replica())).url for _ in range(2)]
        yield stack.enter_context(LiveServer(build_lb(urls))).url + "/mcp"


async def one(client: httpx.AsyncClient, url: str, i: int) -> tuple[str, str]:
    customer = random.choice(CUSTOMERS)  # noqa: S311
    tool, args = random.choice(TOOLS)  # noqa: S311
    token = mint(KEY, client_id="care", scopes="read", issuer=ISS, audience=AUD)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Customer-Account-Id": customer,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool,
    }  # fmt: skip
    body = {"jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": tool, "arguments": args, "_meta": META}}  # fmt: skip
    try:
        r = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return "transport", type(exc).__name__
    result = r.json()["result"]
    if result.get("isError"):
        return "error", result["content"][0]["text"][:120]
    content = result["structuredContent"]
    seen = {content.get("account_id")} | {
        o.get("account_id") for o in content.get("items", []) if isinstance(o, dict)
    }
    seen.discard(None)
    return ("ok", customer) if seen <= {customer} else ("BLEED", f"{customer} got {sorted(seen)}")


def probe(url: str, n: int) -> list[tuple[str, str]]:
    async def run():
        limits = httpx.Limits(max_connections=100)
        async with httpx.AsyncClient(timeout=30, limits=limits, trust_env=False) as c:
            return await asyncio.gather(*(one(c, url, i) for i in range(n)))

    return asyncio.run(run())


def test_no_cross_customer_bleed_under_concurrency(cluster_url):
    results = probe(cluster_url, 200)
    bleed = [r for r in results if r[0] == "BLEED"]
    errors = [r for r in results if r[0] == "error"]
    assert not bleed, bleed[:3]
    assert not errors, errors[:3]  # every customer here is valid: any tool error is a bug
    # Transport errors say nothing about isolation, but must stay rare (tracked separately).
    assert sum(r[0] == "ok" for r in results) >= 195


class _SharedNotPerRequest:
    """The planted bug: a ContextVar look-alike that is really one shared global."""

    def __init__(self) -> None:
        self.value = None

    def set(self, value):
        self.value = value

    def reset(self, _token) -> None:
        pass

    def get(self):
        return self.value


def test_canary_probe_detects_shared_request_state(cluster_url, monkeypatch):
    monkeypatch.setattr(clients_module, "_customer_header", _SharedNotPerRequest())
    results = probe(cluster_url, 200)
    assert any(r[0] == "BLEED" for r in results), "probe failed to detect a planted bleed bug"
