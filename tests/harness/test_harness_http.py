"""The harness over REAL Streamable HTTP with bearer auth, plus CLI diagnostics."""

import io

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from telco_mcp_lab.harness.__main__ import diagnose, mcp_headers
from telco_mcp_lab.harness.agent import Agent
from telco_mcp_lab.harness.settings import HarnessSettings
from telco_mcp_lab.harness.trace import Tracer
from tests.harness.test_agent import ScriptedModel, call, say
from tests.mcp_server.test_http_protocol import auth, mcp_servers


@pytest.mark.protocol
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_harness_over_http_as_bob(live_gateway, mode):
    model = ScriptedModel(
        call("list_subscriptions", {"status": "SUSPENDED"}),
        say("One suspended line."),
    )
    with mcp_servers(live_gateway) as (url,):
        async with (
            httpx2.AsyncClient(headers=auth("bob"), trust_env=False) as http,
            Client(streamable_http_client(url + "/mcp", http_client=http), mode=mode) as mcp,
        ):
            result = await Agent(model, mcp, Tracer(out=io.StringIO())).ask("suspended lines?")
    assert result.answer == "One suspended line."
    tool_msg = [m for m in model.seen_messages[-1] if m["role"] == "tool"][0]["content"]
    assert "SUB-2001-05" in tool_msg and "ACC-2001" in tool_msg  # bob's own data only


@pytest.mark.protocol
async def test_harness_as_scope_less_caller_offers_no_tools(live_gateway):
    model = ScriptedModel(say("I can't access account data."))
    with mcp_servers(live_gateway) as (url,):
        async with (
            httpx2.AsyncClient(headers=auth("mallory"), trust_env=False) as http,
            Client(streamable_http_client(url + "/mcp", http_client=http)) as mcp,
        ):
            await Agent(model, mcp, Tracer(out=io.StringIO())).ask("show my account")
    assert model.seen_tools[0] == []  # the LLM never even learns the tools exist


class TestDiagnose:
    @pytest.mark.parametrize(
        ("error", "hint"),
        [
            ("Error code: 401 - unauthorized", "AZURE_OPENAI_AUTH_HEADER"),
            ("Error code: 404 - DeploymentNotFound", "/openai/v1/"),
            ("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate", "CA_BUNDLE"),
            ("ProxyError: 407 Proxy Authentication Required", "HTTPS_PROXY"),
            ("[Errno 111] Connection refused", "make mcp-http"),
            # a PROXY's 403 must be blamed on the proxy, not on Azure RBAC
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


class TestMcpHeaders:
    """JWT mode: the host app (not the model) sets the token and the customer header."""

    def test_bearer_token_replaces_caller_token_and_customer_header_is_added(self):
        hs = HarnessSettings(
            _env_file=None, bearer_token="jwt-abc", customer_account_id="ACC-1001,ACC-1002"
        )
        assert mcp_headers(hs) == {
            "Authorization": "Bearer jwt-abc",
            "X-Customer-Account-Id": "ACC-1001,ACC-1002",
        }

    def test_no_customer_header_unless_configured(self):
        assert set(mcp_headers(HarnessSettings(_env_file=None, bearer_token="t"))) == {
            "Authorization"
        }

    @pytest.mark.parametrize("bad", ["ACC-1", "ACC-1001\r\nX-Evil: 1", "ACC-1001;ACC-1002"])
    def test_customer_id_format_enforced(self, bad):
        with pytest.raises(ValidationError):
            HarnessSettings(_env_file=None, customer_account_id=bad)

    def test_blank_env_values_mean_unset(self):
        hs = HarnessSettings(_env_file=None, bearer_token="", customer_account_id=" ")
        assert hs.bearer_token is None and hs.customer_account_id is None
