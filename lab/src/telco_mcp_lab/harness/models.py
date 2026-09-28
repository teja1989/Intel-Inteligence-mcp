"""The model behind the harness: Gemini (Gemini Developer API). Nothing here contacts it.

One provider on purpose (trimmed 2026-09-28): the harness exists to check that a real
model uses our tools correctly (`make model-check`), not to compare providers.
"""

from pydantic import ValidationError

from telco_mcp_lab.harness.llm import ChatModel
from telco_mcp_lab.harness.settings import GeminiSettings

LABEL = "Gemini (Google)"


def resolve() -> str:
    """The configured model's name, or a clear error naming missing FIELDS (never values)."""
    try:
        return GeminiSettings().model  # type: ignore[call-arg]
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
        raise RuntimeError(
            f"Gemini is not configured ({', '.join(fields)}): set CHAT_GEMINI_API_KEY "
            "in .env (see .env.example)"
        ) from None


def build_chat_model() -> ChatModel:
    from telco_mcp_lab.harness.llm_gemini import GeminiChatModel

    return GeminiChatModel(GeminiSettings())  # type: ignore[call-arg]
