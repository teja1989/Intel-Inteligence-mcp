"""GeminiChatModel against a mocked Gemini Developer API (httpx.MockTransport).

Full agent loop: mocked Gemini deciding, the real MCP server executing. Checks the
request the real `google-genai` SDK sends: explicit key + base URL, tools as JSON
schema, automatic function calling off, the model's content (thought signature)
replayed verbatim, tool results grouped in one role=tool content. No network.
"""

import base64
import io
import json

import httpx
import pytest
from mcp import Client

from telco_mcp_lab.harness.agent import Agent
from telco_mcp_lab.harness.llm_gemini import REFUSED, GeminiChatModel, to_gemini_contents
from telco_mcp_lab.harness.settings import GeminiSettings
from telco_mcp_lab.harness.trace import Tracer
from tests.conftest import server_with

KEY = "gemini-test-key-0123456789"
SIG = base64.b64encode(b"signature-1").decode()


def settings(**kw) -> GeminiSettings:
    return GeminiSettings(_env_file=None, **{"api_key": KEY, **kw})


def response(parts: list[dict], finish: str = "STOP", **extra) -> dict:
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        **extra,
    }


def call(name: str, args: dict, **kw) -> dict:
    return {"functionCall": {"name": name, "args": args}, **kw}


class FakeGemini:
    def __init__(self, *bodies: dict) -> None:
        self.bodies = list(bodies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.bodies.pop(0))

    def body(self, i: int) -> dict:
        return json.loads(self.requests[i].content)

    def model(self, **kw) -> GeminiChatModel:
        return GeminiChatModel(settings(**kw), transport=httpx.MockTransport(self))


async def ask(fake: FakeGemini, prompt: str, mock_telco_factory):
    async with Client(server_with(mock_telco_factory)) as mcp:
        return await Agent(fake.model(), mcp, Tracer(out=io.StringIO())).ask(prompt)


class TestSettings:
    @pytest.mark.parametrize("name", ["GEMINI_API_KEY", "GOOGLE_API_KEY"])
    def test_standard_key_names_accepted_chat_name_wins(self, monkeypatch, name):
        for n in ("CHAT_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        monkeypatch.setenv(name, "standard-name-key-123")
        assert GeminiSettings(_env_file=None).api_key.get_secret_value() == "standard-name-key-123"
        monkeypatch.setenv("CHAT_GEMINI_API_KEY", KEY)
        assert GeminiSettings(_env_file=None).api_key.get_secret_value() == KEY

    def test_google_environment_does_not_leak_in(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "someone-elses-key-123")
        monkeypatch.setenv("GOOGLE_API_KEY", "someone-elses-key-456")
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://elsewhere.invalid/")
        s = settings()
        assert s.api_key.get_secret_value() == KEY
        assert s.base_url == "https://generativelanguage.googleapis.com/"

    async def test_requests_use_the_explicit_key_and_host(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "someone-elses-key-456")
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://elsewhere.invalid/")
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")  # would switch to Vertex AI
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        fake = FakeGemini(response([{"text": "ok"}]))
        await fake.model().complete([{"role": "user", "content": "hi"}], [])
        req = fake.requests[0]
        assert req.url.host == "generativelanguage.googleapis.com"
        assert req.headers["x-goog-api-key"] == KEY
        # Gemini Developer API path, not Vertex AI's /v1beta1/publishers/google/models/…
        assert req.url.path == "/v1beta/models/gemini-flash-latest:generateContent"


class TestLoop:
    async def test_tool_loop_replays_model_content_and_answers(self, mock_telco_factory):
        fake = FakeGemini(
            response([call("get_order_status", {"order_id": "ORD-000123"}, thoughtSignature=SIG)]),
            response([{"text": "Your order is COMPLETED."}]),
        )
        result = await ask(fake, "status of order 123?", mock_telco_factory)
        assert result.answer == "Your order is COMPLETED."

        first, second = fake.body(0), fake.body(1)
        assert "server_instructions" in json.dumps(first["systemInstruction"])
        decl = first["tools"][0]["functionDeclarations"]
        tool = next(d for d in decl if d["name"] == "get_order_status")
        # The SDK sends the proto field name; the API's protobuf JSON accepts either spelling.
        schema = tool.get("parametersJsonSchema") or tool["parameters_json_schema"]
        assert schema["properties"]["order_id"]["pattern"]
        assert "automaticFunctionCalling" not in first  # SDK-side setting, not sent

        model_turn = second["contents"][1]
        assert model_turn["role"] == "model"
        assert model_turn["parts"][0]["thoughtSignature"] == SIG  # verbatim
        tool_turn = second["contents"][2]
        assert tool_turn["role"] == "tool"
        (part,) = tool_turn["parts"]
        fr = part["functionResponse"]
        assert fr["name"] == "get_order_status" and "id" not in fr  # Gemini sent no id
        assert json.loads(fr["response"]["result"])["status"] == "COMPLETED"
        raw = fake.requests[1].content.decode()
        assert "_native" not in raw and "_is_error" not in raw

    async def test_parallel_calls_and_errors(self, mock_telco_factory):
        fake = FakeGemini(
            response(
                [
                    call("get_order_status", {"order_id": "ORD-999999"}),
                    call("list_orders", {"account_id": "ACC-1001"}),
                ]
            ),  # fmt: skip
            response([{"text": "Done."}]),
        )
        await ask(fake, "orders?", mock_telco_factory)
        tool_turns = [c for c in fake.body(1)["contents"] if c["role"] == "tool"]
        (only,) = tool_turns
        first, second = (p["functionResponse"] for p in only["parts"])
        assert "error" in first["response"]  # ORD-999999 doesn't exist: not found
        assert "result" in second["response"]

    async def test_blocked_prompt(self, mock_telco_factory):
        fake = FakeGemini({"promptFeedback": {"blockReason": "SAFETY"}})
        result = await ask(fake, "something disallowed", mock_telco_factory)
        assert result.answer == REFUSED

    async def test_safety_stop(self, mock_telco_factory):
        fake = FakeGemini(response([{"text": "partial"}], finish="SAFETY"))
        result = await ask(fake, "x", mock_telco_factory)
        assert result.answer == REFUSED

    async def test_thought_parts_are_not_shown(self, mock_telco_factory):
        fake = FakeGemini(response([{"text": "internal musing", "thought": True},
                                    {"text": "Hello."}]))  # fmt: skip
        result = await ask(fake, "hi", mock_telco_factory)
        assert result.answer == "Hello."


@pytest.mark.security
async def test_redirects_are_not_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(307, headers={"location": "https://evil.example/x"})

    llm = GeminiChatModel(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(Exception):  # noqa: B017 - any SDK error; the point is below
        await llm.complete([{"role": "user", "content": "hi"}], [])
    assert "evil.example" not in seen


def test_history_from_another_provider_becomes_function_calls():
    system, contents = to_gemini_contents([
        {"role": "system", "content": "S"},
        {"role": "user", "content": "Q"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "toolu_1", "type": "function",
             "function": {"name": "list_orders", "arguments": '{"limit": 2}'}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "R", "_is_error": True},
    ])  # fmt: skip
    assert system == "S"
    fc = contents[1].parts[0].function_call
    assert (fc.name, fc.args, fc.id) == ("list_orders", {"limit": 2}, "toolu_1")
    fr = contents[2].parts[0].function_response
    assert contents[2].role == "tool" and fr.name == "list_orders" and fr.response == {"error": "R"}


def test_empty_chat_key_line_does_not_hide_the_standard_key(tmp_path, monkeypatch):
    """`make env-update` writes empty CHAT_GEMINI_API_KEY=; GEMINI_API_KEY must still work."""
    for n in ("CHAT_GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(n, raising=False)
    env = tmp_path / ".env"
    env.write_text("CHAT_GEMINI_API_KEY=\nCHAT_GEMINI_MODEL=gemini-flash-latest   # a comment\n")
    monkeypatch.setenv("GEMINI_API_KEY", "standard-name-key-123")
    s = GeminiSettings(_env_file=env)
    assert s.api_key.get_secret_value() == "standard-name-key-123"
    assert s.model == "gemini-flash-latest"
