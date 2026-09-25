"""The LLM side of the host, behind a tiny interface so tests don't need Azure.

`ChatModel.complete(messages, tools) -> AssistantTurn`, where messages and tools
use the OpenAI Chat Completions format (the lingua franca: Azure, OpenAI and
Spring AI's OpenAI/Azure chat models all speak it).

Why Chat Completions and not the newer Responses API? It works the same on
GPT-4.x and GPT-5 deployments, and it maps 1:1 to Spring AI's `ChatModel` +
`ToolCallback`. Responses is the natural next step, and only this file changes.
"""

import ssl
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from telco_mcp_lab.harness.settings import AzureOpenAISettings


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


class ChatModel(Protocol):
    name: str

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn: ...


class AzureChatModel:
    """Azure OpenAI (v1 endpoint) via the official `openai` SDK."""

    def __init__(
        self, settings: AzureOpenAISettings, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.name = f"azure:{settings.deployment}"
        self._s = settings
        key = settings.api_key.get_secret_value()
        headers: dict[str, str] = {}
        if settings.auth_header == "api-key":
            headers["api-key"] = key
        self._client = AsyncOpenAI(
            api_key=key,
            base_url=settings.endpoint,
            default_headers=headers,
            timeout=settings.timeout_s,
            max_retries=settings.max_retries,
            http_client=self._http_client(settings, transport),
        )

    @staticmethod
    def _http_client(s: AzureOpenAISettings, transport: httpx2.AsyncBaseTransport | None) -> Any:
        """`transport` is a test seam (mock Azure). Real runs pass None: httpx2 then
        honours HTTPS_PROXY / SSL_CERT_FILE from the environment (trust_env)."""
        kwargs: dict[str, Any] = {}
        if transport is not None:
            kwargs["transport"] = transport
        if s.proxy:
            kwargs["proxy"] = s.proxy
        if s.ca_bundle:
            kwargs["verify"] = ssl.create_default_context(cafile=str(s.ca_bundle))
        if s.auth_header == "api-key":
            # Send the key ONCE, in `api-key`: drop the SDK's Bearer header.
            async def strip_bearer(request: httpx2.Request) -> None:
                request.headers.pop("Authorization", None)

            kwargs["event_hooks"] = {"request": [strip_bearer]}
        return DefaultAsyncHttpx2Client(**kwargs)

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        params: dict[str, Any] = {"model": self._s.deployment, "messages": messages}
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        if self._s.temperature is not None:
            params["temperature"] = self._s.temperature
        if self._s.max_completion_tokens is not None:
            params["max_completion_tokens"] = self._s.max_completion_tokens
        if self._s.reasoning_effort is not None:
            params["reasoning_effort"] = self._s.reasoning_effort

        resp = await self._client.chat.completions.create(**params)
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
            if tc.type == "function"
        ]
        usage = None
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }
        return AssistantTurn(content=msg.content, tool_calls=calls, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()
