"""The harness over REAL Streamable HTTP with bearer auth, plus CLI diagnostics."""

import io

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from telco_mcp_lab.harness.agent import Agent
from telco_mcp_lab.harness.runtime import diagnose, mcp_client
from telco_mcp_lab.harness.settings import HarnessSettings
from telco_mcp_lab.harness.trace import Tracer
from tests.harness.test_agent import ScriptedModel, call, say
from tests.mcp_server.test_http_protocol import auth, mcp_servers


@pytest.mark.protocol
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_harness_over_http(live_gateway, mode):
    model = ScriptedModel(
        call("list_subscriptions", {"account_id": "ACC-2001", "status": "SUSPENDED"}),
        say("One suspended line."),
    )
    with mcp_servers(live_gateway) as (url,):
        async with (
            httpx2.AsyncClient(headers=auth(), trust_env=False) as http,
            Client(streamable_http_client(url + "/mcp", http_client=http), mode=mode) as mcp,
        ):
            result = await Agent(model, mcp, Tracer(out=io.StringIO())).ask("suspended lines?")
    assert result.answer == "One suspended line."
    tool_msg = [m for m in model.seen_messages[-1] if m["role"] == "tool"][0]["content"]
    assert "SUB-2001-05" in tool_msg and "ACC-2001" in tool_msg


@pytest.mark.protocol
async def test_harness_as_scope_less_client_offers_no_tools(live_gateway):
    model = ScriptedModel(say("I can't access account data."))
    with mcp_servers(live_gateway) as (url,):
        async with (
            httpx2.AsyncClient(headers=auth("profile"), trust_env=False) as http,
            Client(streamable_http_client(url + "/mcp", http_client=http)) as mcp,
        ):
            await Agent(model, mcp, Tracer(out=io.StringIO())).ask("show my account")
    assert model.seen_tools[0] == []  # the LLM never even learns the tools exist


class TestDiagnose:
    @pytest.mark.parametrize(
        ("error", "hint"),
        [
            ("Error code: 401 - unauthorized", "CHAT_GEMINI_API_KEY"),
            ("404 NOT_FOUND model", "CHAT_GEMINI_MODEL"),
            ("400 API_KEY_INVALID", "AI Studio key"),
            ("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate", "CA_BUNDLE"),
            ("ProxyError: 407 Proxy Authentication Required", "HTTPS_PROXY"),
            ("[Errno 111] Connection refused", "make mcp-http"),
            # a PROXY's 403 must be blamed on the proxy, not on the model API
            ("APIConnectionError <- ProxyError: 403 Forbidden", "Proxy refused"),
        ],
    )
    def test_actionable_hints(self, error, hint):
        assert hint in diagnose(RuntimeError(error))

    def test_walks_the_cause_chain(self):
        try:
            try:
                raise OSError("[Errno -2] Name or service not known")
            except OSError as inner:
                raise RuntimeError("Connection error.") from inner
        except RuntimeError as outer:
            msg = diagnose(outer)
        assert "Name or service not known" in msg and "DNS" in msg


class TestMcpClient:
    def test_stdio_is_the_default_and_needs_no_token(self):
        hs = HarnessSettings(_env_file=None)
        assert hs.transport == "stdio" and hs.bearer_token is None
        mcp_client(hs)  # builds without a token

    def test_http_without_a_token_stops_with_a_hint(self):
        with pytest.raises(SystemExit, match="make token"):
            mcp_client(HarnessSettings(_env_file=None, transport="http"))

    def test_blank_env_values_mean_unset(self):
        hs = HarnessSettings(_env_file=None, bearer_token=" ")
        assert hs.bearer_token is None
