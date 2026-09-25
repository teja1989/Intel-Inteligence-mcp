"""Protocol tests over a REAL stdio subprocess, the way hosts run MCP servers.

Covers both protocol eras the Python SDK serves:
* 2026-07-28 (modern): `server/discover`, then per-request `_meta`
* 2025-11-25 (legacy): `initialize` handshake, the era Spring AI speaks today
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.conftest import TEST_TOKEN

pytestmark = pytest.mark.protocol

SERVER = ["-m", "telco_mcp_lab.mcp_server", "--log-level", "WARNING"]


ACCESS_CONFIG = str(Path(__file__).parents[2] / "config" / "access.json")
READ_TOOLS = [
    "get_account_summary",
    "list_subscriptions",
    "get_service_details",
    "get_order_status",
    "list_orders",
]


def server_env(gateway_url: str, caller: str = "alice") -> dict[str, str]:
    # Real env vars beat .env in pydantic-settings, so a developer's .env
    # can't point the test server at the wrong gateway or identity.
    return {
        "GATEWAY_BASE_URL": gateway_url,
        "GATEWAY_TOKEN": TEST_TOKEN,
        "MCP_STDIO_CALLER": caller,
        "MCP_ACCESS_CONFIG": ACCESS_CONFIG,
    }


@pytest.mark.parametrize(
    ("mode", "expected_version"), [("auto", "2026-07-28"), ("legacy", "2025-11-25")]
)
async def test_list_and_call_over_stdio(live_gateway, mode, expected_version):
    params = StdioServerParameters(
        command=sys.executable, args=SERVER, env=server_env(live_gateway)
    )
    async with Client(params, mode=mode) as c:
        assert c.session.protocol_version == expected_version
        tools = await c.list_tools()
        assert [t.name for t in tools.tools] == READ_TOOLS  # deterministic order (spec SHOULD)
        r = await c.call_tool("get_account_summary", {"account_id": "ACC-1001"})
    assert not r.is_error
    assert r.structured_content["subscriptions"]["total"] == 3


def test_stdout_carries_only_json_rpc_and_eof_shuts_down(live_gateway):
    """Spec: server MUST NOT write non-MCP output to stdout; SHOULD exit on stdin EOF."""
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_account_summary",
                "arguments": {"account_id": "ACC-1001"},
                "_meta": meta,
            },
        },
    ]
    proc = subprocess.Popen(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "telco_mcp_lab.mcp_server", "--log-level", "DEBUG"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **server_env(live_gateway)},
    )
    assert proc.stdin and proc.stdout and proc.stderr
    for r in requests:
        proc.stdin.write((json.dumps(r) + "\n").encode())
    proc.stdin.flush()
    # Read one line per request. Any non-JSON line on stdout fails json.loads.
    msgs = [json.loads(proc.stdout.readline()) for _ in requests]
    assert all(m["jsonrpc"] == "2.0" for m in msgs)
    by_id = {m["id"]: m for m in msgs}
    assert by_id[1]["result"]["supportedVersions"] == ["2026-07-28"]
    assert by_id[2]["result"]["tools"][0]["name"] == "get_account_summary"
    assert by_id[3]["result"]["isError"] is False

    # Closing stdin is the stdio shutdown signal. Only close after all responses
    # have arrived: the SDK abandons in-flight requests on EOF
    # ("-32000 Connection closed"), which is spec-compliant.
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0
    assert proc.stdout.read() == b""  # nothing else was written to stdout
    assert proc.stderr.read()  # DEBUG logs exist, and they went to stderr


def test_sdk_diverts_stray_prints_away_from_the_protocol_stream():
    """mcp v2 claims fd 1 for the wire and points it at stderr, so print() can't
    corrupt the JSON-RPC stream. (In SDK v1 and in other stacks it would. Don't
    rely on this, and never print() in a stdio server.)"""
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "noisy", "arguments": {}, "_meta": meta},
    }
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "tests.mcp_server.printing_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write((json.dumps(call) + "\n").encode())
    proc.stdin.flush()
    reply = json.loads(proc.stdout.readline())
    proc.stdin.close()
    proc.wait(timeout=10)
    assert reply["result"]["structuredContent"] == {"result": "ok"}
    assert b"STRAY-PRINT" not in proc.stdout.read()
    assert b"STRAY-PRINT-FROM-TOOL" in proc.stderr.read()


@pytest.mark.security
async def test_stdio_runs_as_the_configured_caller(live_gateway):
    """No headers on stdio: identity is MCP_STDIO_CALLER, and the tenant guard still applies."""
    params = StdioServerParameters(
        command=sys.executable, args=SERVER, env=server_env(live_gateway, caller="bob")
    )
    async with Client(params) as c:
        own = await c.call_tool("get_account_summary", {})
        foreign = await c.call_tool("get_account_summary", {"account_id": "ACC-1001"})
    assert own.structured_content["account_id"] == "ACC-2001"
    assert foreign.is_error
