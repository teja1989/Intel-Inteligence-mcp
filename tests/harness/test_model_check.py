"""lab/scripts/model_check.py end to end: scripted model, real MCP server over HTTP."""

import importlib.util
from pathlib import Path

import pytest

from telco_mcp_lab.harness import models
from telco_mcp_lab.harness.__main__ import explain
from tests.conftest import LiveServer, http_app_in, jwt_token, make_telco
from tests.harness.test_agent import ScriptedModel, call, say

REPO = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("model_check", REPO / "lab/scripts/model_check.py")
model_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model_check)

pytestmark = pytest.mark.protocol


@pytest.fixture
def mcp_url(live_gateway, monkeypatch, tmp_path):
    app = http_app_in(tmp_path, lambda: make_telco(base_url=live_gateway))
    monkeypatch.chdir(tmp_path)  # no repo .env
    monkeypatch.setenv("HARNESS_SYSTEM_PROMPT_FILE", str(REPO / "lab/prompts/agent.system.md"))
    monkeypatch.setenv("HARNESS_GUARDRAILS_FILE", str(REPO / "lab/config/guardrails.json"))
    monkeypatch.setenv("HARNESS_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("HARNESS_TRANSPORT", "http")
    monkeypatch.setenv("HARNESS_BEARER_TOKEN", jwt_token())
    with LiveServer(app) as srv:
        monkeypatch.setenv("HARNESS_MCP_URL", srv.url + "/mcp")
        yield srv.url + "/mcp"


def use_model(monkeypatch, model: ScriptedModel) -> None:
    monkeypatch.setattr(models, "resolve", lambda _: "gemini")
    monkeypatch.setattr(models, "model_name", lambda _: "scripted")
    monkeypatch.setattr(models, "build_chat_model", lambda _: model)
    model_check.results.clear()


async def test_all_six_steps_pass(mcp_url, monkeypatch, capsys):
    use_model(monkeypatch, ScriptedModel(
        say("OK"),
        call("get_account_summary", {"account_id": "ACC-1001"}), say("Active, 2 lines."),
        say("Standard 50GB."),
        call("get_account_summary", {"account_id": "ACC-9999"}, "c2"), say("Not found."),
    ))  # fmt: skip
    assert await model_check.main() == 0
    out = capsys.readouterr().out
    assert "ALL PASSED" in out and out.count("✅") == 6


async def test_a_model_that_ignores_the_error_case_fails_step_6(mcp_url, monkeypatch, capsys):
    use_model(monkeypatch, ScriptedModel(
        say("OK"),
        call("get_account_summary", {"account_id": "ACC-1001"}), say("Active."),
        say("Standard 50GB."),
        say("ACC-9999 is active."),  # made up, no tool call
    ))  # fmt: skip
    assert await model_check.main() == 1
    assert "expected a not-found tool call" in capsys.readouterr().out


async def test_a_model_that_skips_the_tool_fails_step_4(mcp_url, monkeypatch, capsys):
    use_model(monkeypatch, ScriptedModel(say("OK"), say("I think it's fine."), say("x"), say("y")))
    assert await model_check.main() == 1
    assert "answered without calling the tool" in capsys.readouterr().out


def test_errors_are_unwrapped_to_the_real_cause():
    inner = RuntimeError("Error code: 401 - invalid key")
    text = explain(ExceptionGroup("outer", [ExceptionGroup("inner", [inner])]))
    assert text.startswith("RuntimeError: Error code: 401") and "ExceptionGroup" not in text
    assert "key rejected" in text
