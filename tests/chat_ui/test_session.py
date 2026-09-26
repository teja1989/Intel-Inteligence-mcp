"""A chat turn end to end over real HTTP: scripted model, real MCP server, both identities."""

import json
from pathlib import Path

import pytest

from telco_mcp_lab.chat_ui.session import ChatConfig, ChatState, run_turn, usage_totals
from telco_mcp_lab.mcp_server.http_app import build_http_app
from telco_mcp_lab.mcp_server.settings import McpServerSettings
from tests.conftest import ACCESS_MODEL, CALLER_ENV, LiveServer, make_telco
from tests.connect.test_bridge import stack  # noqa: F401 - pytest fixture
from tests.connect.test_token_and_service import SECRET
from tests.harness.test_agent import ScriptedModel, call, say

REPO = Path(__file__).parents[2]
pytestmark = pytest.mark.protocol


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No repo .env: tokens and paths come from the test only."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_SYSTEM_PROMPT_FILE", str(REPO / "prompts/agent.system.md"))
    monkeypatch.setenv("HARNESS_GUARDRAILS_FILE", str(REPO / "config/guardrails.json"))
    for k, v in CALLER_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def static_mcp(live_gateway):
    app = build_http_app(McpServerSettings(_env_file=None), ACCESS_MODEL, CALLER_ENV,
                         telco_factory=lambda: make_telco(base_url=live_gateway))  # fmt: skip
    with LiveServer(app) as srv:
        yield srv.url + "/mcp"


async def test_lab_caller_turn_with_tool_call_and_follow_up(static_mcp, tmp_path):
    cfg = ChatConfig(provider="claude", identity="lab", caller="alice", mcp_url=static_mcp)
    state = ChatState()
    model = ScriptedModel(call("get_account_summary", {"account_id": "ACC-1001"}),
                          say("Your account is active."), say("Two lines."))  # fmt: skip
    out = await run_turn(cfg, state, "summarise ACC-1001", model=model, trace_dir=tmp_path)
    assert out.answer == "Your account is active."
    (rec,) = out.tool_calls
    assert rec["name"] == "get_account_summary" and not rec["is_error"]
    assert json.loads(rec["result"])["holder_name"] == "A*** E******"  # masked for alice
    assert [e["kind"] for e in out.events if e["kind"] in ("tool_call", "tool_result")] == [
        "tool_call", "tool_result"]  # fmt: skip
    # Follow-up keeps the conversation (history persisted in ChatState).
    await run_turn(cfg, state, "and how many lines?", model=model, trace_dir=tmp_path)
    assert "summarise ACC-1001" in json.dumps(model.seen_messages[-1])


async def test_lab_caller_cannot_reach_other_tenant(static_mcp, tmp_path):
    cfg = ChatConfig(provider="claude", identity="lab", caller="alice", mcp_url=static_mcp)
    model = ScriptedModel(call("get_account_summary", {"account_id": "ACC-2001"}), say("No."))
    out = await run_turn(cfg, ChatState(), "show ACC-2001", model=model, trace_dir=tmp_path)
    assert out.tool_calls[0]["is_error"]


async def test_missing_lab_token_is_a_clear_error(static_mcp, tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_TOKEN_BOB")
    cfg = ChatConfig(provider="claude", identity="lab", caller="bob", mcp_url=static_mcp)
    with pytest.raises(RuntimeError, match="MCP_TOKEN_BOB"):
        await run_turn(cfg, ChatState(), "hi", model=ScriptedModel(say("x")), trace_dir=tmp_path)


def connector_config(tmp_path: Path, env: dict[str, str]) -> Path:
    path = tmp_path / "lower.env"
    lines = [f"{k}={v}" for k, v in env.items()] + [f"TELCO_MCP_CLIENT_SECRET={SECRET}"]
    path.write_text("\n".join(lines) + "\n")
    return path


async def test_shared_client_uses_customer_and_reuses_the_token(stack, tmp_path):  # noqa: F811
    cfg = ChatConfig(provider="gemini", identity="shared", customer="ACC-1002",
                     connector_config=connector_config(tmp_path, stack))  # fmt: skip
    state = ChatState()
    model = ScriptedModel(call("get_account_summary", {}), say("ok"), say("again"))
    out = await run_turn(cfg, state, "my account", model=model, trace_dir=tmp_path)
    assert json.loads(out.tool_calls[0]["result"])["account_id"] == "ACC-1002"
    first_token = state.token_cache
    await run_turn(cfg, state, "again", model=model, trace_dir=tmp_path)
    assert state.token_cache == first_token  # no new token per message


async def test_bad_connector_config_names_fields_not_values(tmp_path):
    bad = tmp_path / "bad.env"
    bad.write_text("TELCO_MCP_URL=http://mcp.corp.example/mcp\nTELCO_MCP_TOKEN_URL=https://t/x\n"
                   f"TELCO_MCP_CLIENT_ID=c\nTELCO_MCP_CLIENT_SECRET={SECRET}\n")  # fmt: skip
    cfg = ChatConfig(provider="claude", identity="shared", connector_config=bad)
    with pytest.raises(RuntimeError) as exc:
        await run_turn(cfg, ChatState(), "hi", model=ScriptedModel(say("x")), trace_dir=tmp_path)
    assert "url" in str(exc.value) and SECRET not in str(exc.value)
    assert "mcp.corp.example" not in str(exc.value)


class TestConfig:
    def test_customer_format_enforced(self):
        with pytest.raises(ValueError, match="ACC-1234"):
            ChatConfig(provider="claude", identity="shared", customer="ACC-1001 OR 1=1",
                       connector_config=Path("x")).validate()  # fmt: skip

    @pytest.mark.parametrize("change", [{"customer": "ACC-2001"}, {"caller": "bob"},
                                        {"provider": "gemini"}, {"identity": "lab"}])  # fmt: skip
    def test_anything_that_changes_who_or_which_model_changes_the_fingerprint(self, change):
        base = ChatConfig(provider="claude", identity="shared", customer="ACC-1001",
                          connector_config=Path("x"))  # fmt: skip
        changed = ChatConfig(**{**base.__dict__, **change})
        assert changed.fingerprint() != base.fingerprint()


def test_usage_totals():
    events = [
        {"kind": "llm", "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        {"kind": "llm", "usage": None},
        {"kind": "llm", "usage": {"prompt_tokens": 1}},
    ]
    assert usage_totals(events) == {"prompt_tokens": 4, "completion_tokens": 2}


def test_errors_are_unwrapped_to_the_real_cause():
    from telco_mcp_lab.chat_ui.session import explain

    inner = RuntimeError("Error code: 401 - invalid key")
    wrapped = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    text = explain(wrapped)
    assert text.startswith("RuntimeError: Error code: 401") and "ExceptionGroup" not in text
    assert "key rejected" in text
