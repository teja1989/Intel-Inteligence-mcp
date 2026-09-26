"""Which model providers are configured, and how to build one. Shared by CLI and chat UI.

A provider is "configured" when its settings validate from the environment / .env.
Nothing here contacts a provider.
"""

from typing import Literal

from pydantic import ValidationError

from telco_mcp_lab.harness.llm import ChatModel

Provider = Literal["azure", "claude", "gemini"]
PROVIDERS: tuple[Provider, ...] = ("claude", "gemini", "azure")
LABELS = {"claude": "Claude (Anthropic)", "gemini": "Gemini (Google)", "azure": "Azure OpenAI"}


def _settings(provider: Provider):  # noqa: ANN202 - one of three settings classes
    from telco_mcp_lab.harness import settings as s

    if provider == "azure":
        return s.AzureOpenAISettings()  # type: ignore[call-arg]
    if provider == "claude":
        return s.ClaudeSettings()  # type: ignore[call-arg]
    return s.GeminiSettings()  # type: ignore[call-arg]


def provider_status(provider: Provider) -> str | None:
    """None when configured, else a short reason (field names only, never values)."""
    try:
        _settings(provider)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
        return f"not configured ({', '.join(fields)})"
    return None


def configured() -> list[Provider]:
    return [p for p in PROVIDERS if provider_status(p) is None]


def model_name(provider: Provider) -> str:
    st = _settings(provider)
    return getattr(st, "deployment", None) or st.model


def build_chat_model(provider: Provider) -> ChatModel:
    st = _settings(provider)
    if provider == "azure":
        from telco_mcp_lab.harness.llm import AzureChatModel

        return AzureChatModel(st)
    if provider == "claude":
        from telco_mcp_lab.harness.llm_claude import ClaudeChatModel

        return ClaudeChatModel(st)
    from telco_mcp_lab.harness.llm_gemini import GeminiChatModel

    return GeminiChatModel(st)


def resolve(provider: Provider | None) -> Provider:
    """The requested provider, or the first configured one. Raises with a clear message."""
    if provider is not None:
        if (why := provider_status(provider)) is not None:
            raise RuntimeError(f"{LABELS[provider]} is {why}; see .env.example")
        return provider
    available = configured()
    if not available:
        raise RuntimeError(
            "No model provider configured: set CHAT_CLAUDE_API_KEY, CHAT_GEMINI_API_KEY "
            "or AZURE_OPENAI_* in .env (see .env.example)"
        )
    return available[0]
