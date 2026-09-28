"""Model selection: Gemini only. No network."""

import pytest

from telco_mcp_lab.harness import models

KEYS = ("CHAT_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "CHAT_GEMINI_MODEL")


@pytest.fixture
def no_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def test_missing_key_is_a_clear_error_naming_fields_not_values(no_key):
    with pytest.raises(RuntimeError, match="CHAT_GEMINI_API_KEY") as exc:
        models.resolve()
    assert "api_key" in str(exc.value) or "CHAT_GEMINI_API_KEY" in str(exc.value)


def test_configured_returns_the_model_name(no_key, monkeypatch):
    monkeypatch.setenv("CHAT_GEMINI_API_KEY", "gemini-key-0123456789")
    assert models.resolve() == "gemini-flash-latest"
    monkeypatch.setenv("CHAT_GEMINI_MODEL", "gemini-2.5-pro")
    assert models.resolve() == "gemini-2.5-pro"


def test_standard_key_name_works_too(no_key, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-0123456789")
    assert models.resolve()
