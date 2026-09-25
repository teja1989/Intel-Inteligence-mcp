"""AzureChatModel against a mocked Azure v1 endpoint (httpx2.MockTransport).

Checks the exact HTTP request the real `openai` SDK sends (URL, auth header,
body) and that a real SDK response with tool_calls is parsed correctly. Then a
full loop: mocked Azure deciding, the real MCP server executing.
"""

import io
import json

import httpx2
import pytest
from mcp import Client
from pydantic import ValidationError

from telco_mcp_lab.harness.agent import Agent
from telco_mcp_lab.harness.llm import AzureChatModel
from telco_mcp_lab.harness.settings import AzureOpenAISettings
from telco_mcp_lab.harness.trace import Tracer
from tests.conftest import server_as

ENDPOINT = "https://lab-resource.openai.azure.com/openai/v1/"
KEY = "azure-test-key-0123456789"


def settings(**kw) -> AzureOpenAISettings:
    base = {"endpoint": ENDPOINT, "api_key": KEY, "deployment": "gpt-5-lab"}
    return AzureOpenAISettings(_env_file=None, **{**base, **kw})


def completion(message: dict, finish: str = "stop") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


class FakeAzure:
    """Records requests; answers with scripted Chat Completions bodies."""

    def __init__(self, *bodies: dict) -> None:
        self.bodies = list(bodies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json=self.bodies.pop(0))

    def client(self, s: AzureOpenAISettings) -> AzureChatModel:
        # The adapter builds its real http client (incl. the api-key hook); only
        # the network transport is the mock. An explicit transport also means
        # environment proxies are not used, so this never touches the network.
        return AzureChatModel(s, transport=httpx2.MockTransport(self))


class TestSettings:
    def test_v1_endpoint_required(self):
        with pytest.raises(ValidationError, match="openai/v1"):
            settings(endpoint="https://lab-resource.openai.azure.com/")

    def test_https_required(self):
        with pytest.raises(ValidationError, match="https"):
            settings(endpoint="http://lab-resource.openai.azure.com/openai/v1/")

    def test_trailing_slash_normalised(self):
        assert settings(endpoint=ENDPOINT.rstrip("/")).endpoint == ENDPOINT

    @pytest.mark.security
    def test_key_hidden(self):
        assert KEY not in repr(settings())

    def test_missing_ca_bundle_is_a_clear_error(self, tmp_path):
        with pytest.raises(ValidationError, match="CA_BUNDLE file not found"):
            settings(ca_bundle=tmp_path / "nope.pem")


class TestRequestShape:
    async def test_bearer_mode_request(self):
        fake = FakeAzure(completion({"role": "assistant", "content": "OK"}))
        llm = fake.client(settings())
        turn = await llm.complete([{"role": "user", "content": "hi"}], [])
        req = fake.requests[0]
        assert str(req.url) == ENDPOINT + "chat/completions"
        assert "api-version" not in str(req.url)  # v1: no api-version
        assert req.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(req.content)
        assert body["model"] == "gpt-5-lab"  # the DEPLOYMENT name goes in `model`
        # Reasoning models reject these, so they're not sent unless configured.
        assert "temperature" not in body and "max_tokens" not in body
        assert turn.content == "OK" and turn.usage == {"prompt_tokens": 11, "completion_tokens": 7}

    @pytest.mark.security
    async def test_api_key_mode_sends_key_once_in_api_key_header(self):
        fake = FakeAzure(completion({"role": "assistant", "content": "OK"}))
        llm = fake.client(settings(auth_header="api-key"))
        await llm.complete([{"role": "user", "content": "hi"}], [])
        req = fake.requests[0]
        assert req.headers["api-key"] == KEY
        assert "authorization" not in req.headers

    async def test_optional_knobs_sent_only_when_set(self):
        fake = FakeAzure(completion({"role": "assistant", "content": "OK"}))
        llm = fake.client(settings(max_completion_tokens=256, reasoning_effort="low"))
        tool = {"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}
        await llm.complete([{"role": "user", "content": "hi"}], [tool])
        body = json.loads(fake.requests[0].content)
        assert body["max_completion_tokens"] == 256 and body["reasoning_effort"] == "low"
        assert body["tool_choice"] == "auto" and body["tools"][0]["function"]["name"] == "t"

    async def test_tool_calls_parsed(self):
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_orders", "arguments": '{"account_id":"ACC-1001"}'},
                }
            ],
        }
        fake = FakeAzure(completion(msg, "tool_calls"))
        turn = await fake.client(settings()).complete([{"role": "user", "content": "x"}], [])
        (tc,) = turn.tool_calls
        assert (tc.id, tc.name, json.loads(tc.arguments)) == (
            "call_1",
            "list_orders",
            {"account_id": "ACC-1001"},
        )


class TestEndToEndWithMockedAzure:
    async def test_mocked_azure_drives_real_mcp_tools(self, mock_telco_factory):
        tool_turn = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_order_status",
                        "arguments": json.dumps({"order_id": "ORD-000123"}),
                    },
                }
            ],
        }
        fake = FakeAzure(
            completion(tool_turn, "tool_calls"),
            completion({"role": "assistant", "content": "Your order is COMPLETED."}),
        )
        llm = fake.client(settings())
        async with Client(server_as("alice", mock_telco_factory)) as mcp:
            result = await Agent(llm, mcp, Tracer(out=io.StringIO())).ask("status of order 123?")
        assert result.answer == "Your order is COMPLETED."
        # Second request carried the tool result back to "Azure" as a role=tool message.
        second = json.loads(fake.requests[1].content)["messages"]
        tool_msg = [m for m in second if m["role"] == "tool"][0]
        assert tool_msg["tool_call_id"] == "call_1"
        assert json.loads(tool_msg["content"])["status"] == "COMPLETED"
