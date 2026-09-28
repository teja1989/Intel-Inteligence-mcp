"""The LLM side of the host, behind a tiny interface so tests don't need a real model.

`ChatModel.complete(messages, tools) -> AssistantTurn`. Messages and tools use the OpenAI
Chat Completions shape as the harness's internal format (the lingua franca that
Spring AI's chat models also map to); the Gemini adapter (llm_gemini.py) converts it.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, exactly as the model produced it (may be invalid!)


@dataclass(frozen=True)
class AssistantTurn:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None
    # Provider-specific message data the provider needs back verbatim on the next call
    # (Gemini thought signatures). Opaque to the agent, which
    # stores it on the assistant message as "_native". Keys starting with "_" are
    # host-internal and never sent to a provider as-is.
    native: Any = None


def outbound(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages without host-internal ("_"-prefixed) keys, for providers that reject extras."""
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


class ChatModel(Protocol):
    name: str

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn: ...
