"""Provider selection: which models are configured, and picking one. No network."""

import pytest

from telco_mcp_lab.harness import models

ALL_KEYS = ("CHAT_CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "CHAT_GEMINI_API_KEY", "GEMINI_API_KEY",
            "GOOGLE_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_DEPLOYMENT")  # fmt: skip


@pytest.fixture
def no_providers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_nothing_configured_is_a_clear_error(no_providers):
    assert models.configured() == []
    with pytest.raises(RuntimeError, match="No model provider configured"):
        models.resolve(None)


def test_first_configured_wins_in_order_claude_gemini_azure(no_providers, monkeypatch):
    monkeypatch.setenv("CHAT_GEMINI_API_KEY", "gemini-key-0123456789")
    assert models.resolve(None) == "gemini"
    monkeypatch.setenv("CHAT_CLAUDE_API_KEY", "sk-ant-0123456789")
    assert models.resolve(None) == "claude"
    assert models.model_name("claude") == "claude-opus-5"


def test_explicit_but_unconfigured_provider_names_fields_not_values(no_providers, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "super-secret-azure-key")
    with pytest.raises(RuntimeError) as exc:
        models.resolve("azure")
    assert "endpoint" in str(exc.value) and "super-secret" not in str(exc.value)
