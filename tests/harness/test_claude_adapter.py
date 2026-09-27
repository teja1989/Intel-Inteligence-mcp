"""ClaudeChatModel against a mocked Anthropic Messages API (httpx2.MockTransport).

Checks the exact request the real `anthropic` SDK sends (system, tools, fallback
opt-in, thinking blocks replayed verbatim, grouped tool results) and runs the full
agent loop: mocked Claude deciding, the real MCP server executing. No network.
"""

import io
import json

import httpx2
import pytest
from mcp import Client

from telco_mcp_lab.harness.agent import Agent
from telco_mcp_lab.harness.llm_claude import (
    FALLBACK_BETA,
    REFUSED,
    ClaudeChatModel,
    to_claude_messages,
)
from telco_mcp_lab.harness.settings import ClaudeSettings
from telco_mcp_lab.harness.trace import Tracer
from tests.conftest import server_with

KEY = "sk-ant-test-0123456789"
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc123"}


def settings(**kw) -> ClaudeSettings:
    return ClaudeSettings(_env_file=None, **{"api_key": KEY, **kw})


def message(content: list[dict], stop: str = "end_turn") -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 8},
    }  # fmt: skip


def tool_use(id_: str, name: str, args: dict) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


class FakeClaude:
    def __init__(self, *bodies: dict) -> None:
        self.bodies = list(bodies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json=self.bodies.pop(0))

    def body(self, i: int) -> dict:
        return json.loads(self.requests[i].content)

    def model(self, **kw) -> ClaudeChatModel:
        return ClaudeChatModel(settings(**kw), transport=httpx2.MockTransport(self))


async def ask(fake: FakeClaude, prompt: str, mock_telco_factory, **kw):
    async with Client(server_with(mock_telco_factory)) as mcp:
        agent = Agent(fake.model(**kw), mcp, Tracer(out=io.StringIO()))
        return await agent.ask(prompt)


class TestSettings:
    def test_claude_code_environment_does_not_leak_in(self, monkeypatch):
        """Claude Code exports these into its shells; none may change our settings."""
        monkeypatch.setenv("CLAUDE_EFFORT", "max")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.claude-code.invalid")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-someone-elses-key")
        s = settings()
        assert s.effort is None and s.model == "claude-opus-5"
        assert s.base_url == "https://api.anthropic.com"
        assert s.api_key.get_secret_value() == KEY

    async def test_requests_go_to_the_configured_base_url_despite_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.claude-code.invalid")
        fake = FakeClaude(message([{"type": "text", "text": "ok"}]))
        await fake.model().complete([{"role": "user", "content": "hi"}], [])
        assert fake.requests[0].url.host == "api.anthropic.com"

    def test_standard_key_name_accepted_chat_name_wins(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-standard-name-123")
        assert ClaudeSettings(_env_file=None).api_key.get_secret_value().endswith("name-123")
        monkeypatch.setenv("CHAT_CLAUDE_API_KEY", KEY)
        assert ClaudeSettings(_env_file=None).api_key.get_secret_value() == KEY

    def test_api_key_required(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CHAT_CLAUDE_API_KEY", raising=False)
        with pytest.raises(ValueError):
            ClaudeSettings(_env_file=None)

    def test_base_url_must_be_https(self):
        with pytest.raises(ValueError, match="https"):
            settings(base_url="http://gateway.corp/anthropic")


class TestLoop:
    async def test_tool_loop_replays_thinking_and_returns_the_answer(self, mock_telco_factory):
        fake = FakeClaude(
            message(
                [
                    THINKING,
                    {"type": "text", "text": "Checking."},
                    tool_use("toolu_1", "get_order_status", {"order_id": "ORD-000123"}),
                ],
                stop="tool_use",
            ),  # fmt: skip
            message([{"type": "text", "text": "Your order is COMPLETED."}]),
        )
        result = await ask(fake, "status of order 123?", mock_telco_factory)
        assert result.answer == "Your order is COMPLETED."

        first, second = fake.body(0), fake.body(1)
        req = fake.requests[0]
        assert req.headers["x-api-key"] == KEY and "authorization" not in req.headers
        assert FALLBACK_BETA in req.headers["anthropic-beta"] and first["fallbacks"] == "default"
        assert first["model"] == "claude-opus-5" and "temperature" not in first
        assert "<server_instructions>" in first["system"]  # host prompt + fenced server rules
        assert all(m["role"] in ("user", "assistant") for m in first["messages"])
        tool = next(t for t in first["tools"] if t["name"] == "get_order_status")
        assert tool["input_schema"]["properties"]["order_id"]["pattern"]

        assistant = second["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["content"][0] == THINKING  # verbatim, signature intact
        assert assistant["content"][2]["id"] == "toolu_1"
        results = second["messages"][2]
        assert results["role"] == "user"
        (block,) = results["content"]
        assert block["type"] == "tool_result" and block["tool_use_id"] == "toolu_1"
        assert json.loads(block["content"])["status"] == "COMPLETED"
        assert "is_error" not in block
        raw = fake.requests[1].content.decode()
        assert "_native" not in raw and "_is_error" not in raw  # host-internal keys stay home

    async def test_parallel_tool_results_go_back_in_one_message(self, mock_telco_factory):
        fake = FakeClaude(
            message(
                [
                    tool_use("t1", "get_order_status", {"order_id": "ORD-000123"}),
                    tool_use("t2", "list_orders", {"account_id": "ACC-1001"}),
                ],
                stop="tool_use",
            ),  # fmt: skip
            message([{"type": "text", "text": "Done."}]),
        )
        await ask(fake, "orders?", mock_telco_factory)
        msgs = fake.body(1)["messages"]
        user_with_results = [
            m for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
        ]
        (only,) = user_with_results
        assert [b["tool_use_id"] for b in only["content"]] == ["t1", "t2"]

    async def test_tool_error_is_flagged(self, mock_telco_factory):
        fake = FakeClaude(
            message(
                [tool_use("t1", "get_order_status", {"order_id": "ORD-999999"})], stop="tool_use"
            ),  # fmt: skip
            message([{"type": "text", "text": "Not found."}]),
        )
        await ask(fake, "order 456?", mock_telco_factory)
        (block,) = fake.body(1)["messages"][2]["content"]
        assert block["is_error"] is True

    async def test_refusal_is_checked_before_content(self, mock_telco_factory):
        fake = FakeClaude(message([tool_use("t1", "list_orders", {})], stop="refusal"))
        result = await ask(fake, "something disallowed", mock_telco_factory)
        assert result.answer == REFUSED and result.tool_calls == []

    async def test_max_tokens_is_made_visible(self, mock_telco_factory):
        fake = FakeClaude(message([{"type": "text", "text": "Partial"}], stop="max_tokens"))
        result = await ask(fake, "long answer", mock_telco_factory)
        assert "cut off" in result.answer

    async def test_fallbacks_can_be_disabled(self, mock_telco_factory):
        fake = FakeClaude(message([{"type": "text", "text": "Hi"}]))
        await ask(fake, "hi", mock_telco_factory, fallbacks=False, effort="low")
        body, req = fake.body(0), fake.requests[0]
        assert "fallbacks" not in body and FALLBACK_BETA not in req.headers.get(
            "anthropic-beta", ""
        )
        assert body["output_config"] == {"effort": "low"}


@pytest.mark.security
async def test_redirects_are_not_followed():
    """x-api-key isn't stripped on cross-host redirects, so never follow one."""
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.host)
        return httpx2.Response(307, headers={"location": "https://evil.example/v1/messages"})

    llm = ClaudeChatModel(settings(max_retries=0), transport=httpx2.MockTransport(handler))
    with pytest.raises(Exception):  # noqa: B017 - any SDK error is fine; the point is below
        await llm.complete([{"role": "user", "content": "hi"}], [])
    assert "evil.example" not in seen


class TestTranslation:
    def test_history_from_another_provider_becomes_tool_use_blocks(self):
        system, msgs = to_claude_messages([
            {"role": "system", "content": "S"},
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "list_orders", "arguments": '{"limit": 2}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "R"},
        ])  # fmt: skip
        assert system == "S"
        assert msgs[1]["content"] == [
            {"type": "tool_use", "id": "c1", "name": "list_orders", "input": {"limit": 2}}
        ]
        assert msgs[2]["content"][0]["tool_use_id"] == "c1"

    def test_invalid_json_arguments_from_another_provider_become_empty_input(self):
        _, msgs = to_claude_messages([
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "x", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{oops"}}]},
        ])  # fmt: skip
        assert msgs[1]["content"][1]["input"] == {}
