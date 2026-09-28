"""Per-request isolation under concurrency: many interleaved requests, 2 replicas.

With no customer boundary, what must never bleed between requests is:
  * the CLIENT (and so its scopes): a read-only client must never get unmasked PII
    because a pii:read client's request was in flight at the same time;
  * the ACCOUNT: every result is for the account that request asked for.

Sequential tests can't catch the classic bleed bug (request state kept somewhere
shared instead of per request): only interleaving shows it. The canary test
plants exactly that bug and asserts this probe catches it, so the probe can't
silently lose its teeth.
"""

import asyncio
import random
from contextlib import ExitStack

import httpx
import pytest

from telco_mcp.security import scoped_server
from tests.conftest import LiveServer, http_app_in, jwt_token, make_telco
from tests.round_robin_lb import build_app as build_lb

pytestmark = [pytest.mark.security, pytest.mark.protocol, pytest.mark.slow]

ACCOUNTS = ["ACC-1001", "ACC-1002", "ACC-2001"]
# client -> may it see unmasked PII (see TEST_CLIENTS in conftest)
CLIENTS = {"agent-a": ("read", False), "care-agent": ("read pii:read", True)}
TOOLS = ["get_account_summary", "list_subscriptions", "list_orders"]
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
TOKENS = {c: jwt_token(c, scopes) for c, (scopes, _) in CLIENTS.items()}


@pytest.fixture
def cluster_url(live_gateway, tmp_path):
    def replica(i: int):
        d = tmp_path / f"r{i}"
        d.mkdir()
        return http_app_in(d, lambda: make_telco(base_url=live_gateway))

    with ExitStack() as stack:
        urls = [stack.enter_context(LiveServer(replica(i))).url for i in range(2)]
        yield stack.enter_context(LiveServer(build_lb(urls))).url + "/mcp"


def pii_values(content: dict) -> list[str]:
    values = [content.get("holder_name")]
    values += [s.get("msisdn") for s in content.get("subscriptions", []) if isinstance(s, dict)]
    values += [s.get("msisdn") for s in content.get("items", []) if isinstance(s, dict)]
    return [v for v in values if isinstance(v, str)]


async def one(client: httpx.AsyncClient, url: str, i: int) -> tuple[str, str]:
    who = random.choice(list(CLIENTS))  # noqa: S311
    account = random.choice(ACCOUNTS)  # noqa: S311
    tool = random.choice(TOOLS)  # noqa: S311
    headers = {
        "Authorization": f"Bearer {TOKENS[who]}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool,
    }
    params = {"name": tool, "arguments": {"account_id": account}, "_meta": META}
    body = {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": params}
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
    if seen != {account}:
        return "BLEED", f"asked {account}, got {sorted(seen)}"
    unmasked_expected = CLIENTS[who][1]
    for v in pii_values(content):
        if ("*" not in v) != unmasked_expected:
            return "BLEED", f"{who} got {'unmasked' if '*' not in v else 'masked'} PII"
    return "ok", who


def probe(url: str, n: int) -> list[tuple[str, str]]:
    async def run():
        limits = httpx.Limits(max_connections=100)
        async with httpx.AsyncClient(timeout=30, limits=limits, trust_env=False) as c:
            return await asyncio.gather(*(one(c, url, i) for i in range(n)))

    return asyncio.run(run())


def test_no_client_or_account_bleed_under_concurrency(cluster_url):
    results = probe(cluster_url, 200)
    bleed = [r for r in results if r[0] == "BLEED"]
    errors = [r for r in results if r[0] == "error"]
    assert not bleed, bleed[:3]
    assert not errors, errors[:3]  # every account here is valid: any tool error is a bug
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
    monkeypatch.setattr(scoped_server, "_current_client", _SharedNotPerRequest())
    results = probe(cluster_url, 200)
    assert any(r[0] == "BLEED" for r in results), "probe failed to detect a planted bleed bug"
