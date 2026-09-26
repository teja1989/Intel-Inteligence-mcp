"""The stdio ↔ HTTP bridge: per-message headers, 401 refresh, relaying, and end to end."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from telco_mcp_lab.connect.bridge import Bridge
from telco_mcp_lab.connect.token import ClientCredentials
from telco_mcp_lab.devtools.token_issuer import DEV_ISSUER, jwks_for
from telco_mcp_lab.devtools.token_service import create_app as create_token_service
from telco_mcp_lab.mcp_server.http_app import build_http_app
from telco_mcp_lab.mcp_server.settings import JwtSettings, McpServerSettings
from tests.conftest import ACCESS_MODEL, LiveServer, make_telco
from tests.connect.test_token_and_service import KEY, SECRET, settings

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
REPO = Path(__file__).parents[2]


class FakeTokens(ClientCredentials):
    """Hands out tok-1, tok-2, … : a new one on every force."""

    def __init__(self) -> None:
        self.n = 1
        self.forced = 0

    async def token(self, *, force: bool = False) -> str:
        if force:
            self.n += 1
            self.forced += 1
        return f"tok-{self.n}"


def make_bridge(handler, **kw):
    written: list[dict] = []
    tokens = FakeTokens()
    b = Bridge(settings(**kw), tokens, written.append, transport=httpx.MockTransport(handler))
    return b, tokens, written


async def run(b: Bridge, *msgs: dict) -> None:
    for m in msgs:
        b.handle(m)
    await b.drain(5)
    await asyncio.sleep(0)  # let notification tasks finish too
    await b.aclose()


def call(name: str, args: dict, i: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": name, "arguments": args, "_meta": META}}  # fmt: skip


class TestHeaders:
    def test_modern_tools_call_carries_the_protocol_headers(self):
        b, _, _ = make_bridge(lambda r: httpx.Response(200), customer="ACC-1001")
        h = b.headers_for(call("get_account_summary", {}))
        assert h["mcp-protocol-version"] == "2026-07-28"
        assert h["mcp-method"] == "tools/call" and h["mcp-name"] == "get_account_summary"
        assert h["X-Customer-Account-Id"] == "ACC-1001"
        assert "authorization" not in {k.lower() for k in h}  # added per attempt, not here

    async def test_legacy_version_learned_from_initialize(self):
        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                                 "result": {"protocolVersion": "2025-11-25"}},
                                      headers={"mcp-session-id": "s-123"})  # fmt: skip
            return httpx.Response(202)

        b, _, _ = make_bridge(handler)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        assert "mcp-protocol-version" not in b.headers_for(init)
        b.handle(init)
        await b.drain(5)
        h = b.headers_for({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert h["mcp-protocol-version"] == "2025-11-25" and h["mcp-session-id"] == "s-123"
        await b.aclose()


class TestForwarding:
    async def test_401_refreshes_the_token_and_retries_once(self):
        seen: list[str] = []

        def handler(request):
            seen.append(request.headers["authorization"])
            if len(seen) == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

        b, tokens, written = make_bridge(handler)
        await run(b, call("list_orders", {}))
        assert seen == ["Bearer tok-1", "Bearer tok-2"] and tokens.forced == 1
        assert written == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}]

    async def test_second_401_becomes_a_clear_error_not_a_loop(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401, text="nope")

        b, _, written = make_bridge(handler)
        await run(b, call("list_orders", {}))
        assert len(calls) == 2
        (err,) = written
        assert err["id"] == 1 and "registered for this environment" in err["error"]["message"]
        assert "tok-" not in json.dumps(written)  # never leak the token

    async def test_sse_events_are_relayed_in_order(self):
        progress = '{"jsonrpc":"2.0","method":"notifications/progress","params":{}}'
        result = '{"jsonrpc":"2.0","id":1,"result":{}}'
        sse = f"event: message\ndata: {progress}\n\nevent: message\ndata: {result}\n\n"
        b, _, written = make_bridge(
            lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        )
        await run(b, call("list_orders", {}))
        assert [m.get("method", "result") for m in written] == ["notifications/progress", "result"]

    async def test_notification_accepted_writes_nothing(self):
        b, _, written = make_bridge(lambda r: httpx.Response(202))
        await run(b, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert written == []

    async def test_modern_cancel_aborts_the_request_instead_of_posting(self):
        posts: list[str] = []

        async def handler(request):
            posts.append(json.loads(request.content).get("method"))
            await asyncio.sleep(30)

        b, _, written = make_bridge(handler)
        b.handle(call("list_orders", {}, i=7))
        await asyncio.sleep(0.05)
        b.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                  "params": {"requestId": 7, "_meta": META}})  # fmt: skip
        await b.drain(2)
        await b.aclose()
        assert posts == ["tools/call"] and written == []


# ------------------------------------------------------------------------- end to end
AUD = "https://mcp.lowerenv.test/mcp"  # the audience needn't equal the URL: it's a name


@pytest.fixture
def stack(live_gateway, tmp_path):
    """Dev token service + MCP server (JWT mode, `dev` environment) + mock gateway."""
    (tmp_path / "jwks.json").write_text(json.dumps(jwks_for(KEY)))
    (tmp_path / "clients.json").write_text(json.dumps({
        "scope_map": {"read": "read"},
        "clients": {"lowerenv-shared": {"mode": "customer_context", "allowed_scopes": ["read"],
                                        "environments": ["local", "dev", "test"]}},
    }))  # fmt: skip
    mcp_app = build_http_app(
        McpServerSettings(_env_file=None, environment="dev", auth_mode="jwt",
                          clients_config=tmp_path / "clients.json", public_url=AUD),
        ACCESS_MODEL, {},
        jwt_settings=JwtSettings(_env_file=None, issuer=DEV_ISSUER, audience=AUD,
                                 jwks_file=tmp_path / "jwks.json"),
        telco_factory=lambda: make_telco(base_url=live_gateway),
    )  # fmt: skip
    ts_app = create_token_service(KEY, client_id="lowerenv-shared", client_secret=SECRET,
                                  audience=AUD)  # fmt: skip
    with LiveServer(ts_app) as ts, LiveServer(mcp_app) as mcp:
        yield {
            "TELCO_MCP_URL": mcp.url + "/mcp",
            "TELCO_MCP_TOKEN_URL": ts.url + "/oauth/token",
            "TELCO_MCP_CLIENT_ID": "lowerenv-shared",
        }


def bridge_params(env: dict[str, str], **extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "telco_mcp_lab.connect", "bridge"],
        env={**env, "TELCO_MCP_CLIENT_SECRET": SECRET, **extra},
    )


@pytest.mark.protocol
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_sdk_client_through_the_bridge(stack, mode):
    async with Client(bridge_params(stack, TELCO_MCP_CUSTOMER="ACC-1001"), mode=mode) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert "get_account_summary" in tools
        ok = await c.call_tool("get_account_summary", {})
        assert not ok.is_error and ok.structured_content["account_id"] == "ACC-1001"
        other = await c.call_tool("get_account_summary", {"account_id": "ACC-2001"})
        assert other.is_error  # the customer header bounds the data, not the model


@pytest.mark.protocol
async def test_secret_from_keychain_style_command(stack):
    cmd = f"{sys.executable} -c \"print('{SECRET}')\""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "telco_mcp_lab.connect", "bridge"],
        env={**stack, "TELCO_MCP_SECRET_COMMAND": cmd, "TELCO_MCP_CUSTOMER": "ACC-2001"},
    )  # fmt: skip
    async with Client(params) as c:
        r = await c.call_tool("get_account_summary", {})
        assert r.structured_content["account_id"] == "ACC-2001"


def cli(command: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, our own module
        [sys.executable, "-m", "telco_mcp_lab.connect", command],
        env={**os.environ, **env, "TELCO_MCP_CLIENT_SECRET": SECRET},
        capture_output=True, text=True, timeout=60, cwd=REPO,
    )  # fmt: skip


@pytest.mark.protocol
def test_headers_command_prints_json_for_claude_code(stack):
    r = cli("headers", {**stack, "TELCO_MCP_CUSTOMER": "ACC-1001"})
    assert r.returncode == 0, r.stderr
    h = json.loads(r.stdout)
    assert h["Authorization"].startswith("Bearer ") and h["X-Customer-Account-Id"] == "ACC-1001"


@pytest.mark.protocol
def test_check_command_prints_no_secrets(stack):
    r = cli("check", stack)
    assert r.returncode == 0, r.stderr
    assert "MCP ok" in r.stdout and '"client_id": "lowerenv-shared"' in r.stdout
    assert SECRET not in r.stdout + r.stderr and "eyJ" not in r.stdout + r.stderr


@pytest.mark.protocol
def test_wrong_secret_fails_cleanly(stack):
    env = {**stack}
    r = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "telco_mcp_lab.connect", "headers"],
        env={**os.environ, **env, "TELCO_MCP_CLIENT_SECRET": "wrong-secret-0123456789abcdef"},
        capture_output=True, text=True, timeout=60, cwd=REPO,
    )  # fmt: skip
    assert r.returncode != 0 and "invalid_client" in r.stderr and "wrong-secret" not in r.stderr
