"""Claude as the host's model, via the official Anthropic SDK (Messages API).

The agent keeps its conversation in the Chat Completions shape (llm.py). This adapter
translates both ways:

    system messages           → the top-level `system` parameter
    assistant + tool_calls    → content blocks: text + `tool_use`
    consecutive role=tool     → ONE user message with all `tool_result` blocks
                                (splitting them teaches Claude to stop parallel calls)
    response `tool_use`       → ToolCall(id, name, json arguments)

Thinking: Claude thinks between tool calls, and its thinking blocks must be sent
back **unchanged** while the tool loop continues. The adapter returns the full
response content as `AssistantTurn.native`; the agent stores it on the assistant
message (`_native`) and it is replayed verbatim on the next call.

Refusals: `stop_reason == "refusal"` is checked before reading content. With
CHAT_CLAUDE_FALLBACKS=true (default) the request opts into Anthropic's server-side
fallback (`fallbacks: "default"`), which re-runs a declined request on the
recommended substitute model inside the same call.

Java/Spring equivalent: Spring AI's `AnthropicChatModel` + `ToolCallback`s.
"""

import json
import ssl
from typing import Any

import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from telco_mcp_lab.harness.llm import AssistantTurn, ToolCall
from telco_mcp_lab.harness.settings import ClaudeSettings

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # pairs with fallbacks="default"
REFUSED = "I can't help with that request."


def to_claude_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat Completions function tools → Anthropic tool definitions."""
    out = []
    for t in tools:
        fn = t["function"]
        out.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def to_claude_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Chat Completions history → (system, Anthropic messages)."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        role = m["role"]
        if role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": m.get("content") or "",
            }
            if m.get("_is_error"):
                block["is_error"] = True
            pending_results.append(block)
            continue
        flush_results()
        if role == "system":
            system_parts.append(m.get("content") or "")
        elif role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            native = m.get("_native")
            if isinstance(native, dict) and native.get("provider") == "anthropic":
                out.append({"role": "assistant", "content": native["content"]})  # verbatim
                continue
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for call in m.get("tool_calls") or []:
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                blocks.append(
                    {"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                     "input": args if isinstance(args, dict) else {}}
                )  # fmt: skip
            out.append({"role": "assistant", "content": blocks or ""})
    flush_results()
    return "\n\n".join(p for p in system_parts if p), out


class ClaudeChatModel:
    def __init__(
        self, settings: ClaudeSettings, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.name = f"claude:{settings.model}"
        self._s = settings
        kwargs: dict[str, Any] = {
            # The key travels in x-api-key, which (unlike Authorization) is not
            # stripped on a cross-host redirect: never follow redirects.
            "follow_redirects": False,
        }
        if transport is not None:
            kwargs["transport"] = transport  # test seam: a mocked Anthropic API
        if settings.proxy:
            kwargs["proxy"] = settings.proxy
        if settings.ca_bundle:
            kwargs["verify"] = ssl.create_default_context(cafile=str(settings.ca_bundle))
        self._client = AsyncAnthropic(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            timeout=settings.timeout_s,
            max_retries=settings.max_retries,
            http_client=DefaultAsyncHttpxClient(**kwargs),
        )

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        system, claude_messages = to_claude_messages(messages)
        params: dict[str, Any] = {
            "model": self._s.model,
            "max_tokens": self._s.max_tokens,
            "messages": claude_messages,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = to_claude_tools(tools)
        if self._s.effort:
            params["output_config"] = {"effort": self._s.effort}
        if self._s.fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        resp = await self._client.beta.messages.create(**params)

        usage = {
            "prompt_tokens": resp.usage.input_tokens,
            "completion_tokens": resp.usage.output_tokens,
        }
        if resp.stop_reason == "refusal":  # check BEFORE reading content
            return AssistantTurn(content=REFUSED, usage=usage)

        text = "".join(b.text for b in resp.content if b.type == "text").strip() or None
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=json.dumps(b.input))
            for b in resp.content
            if b.type == "tool_use"
        ]
        if resp.stop_reason == "max_tokens" and not calls:
            text = (text or "") + "\n\n[answer cut off: CHAT_CLAUDE_MAX_TOKENS reached]"
        # The SDK's own block objects, replayed as-is (the documented pattern): the SDK
        # serializes them for the next request, thinking signatures included.
        native = {"provider": "anthropic", "content": list(resp.content)}
        return AssistantTurn(content=text, tool_calls=calls, usage=usage, native=native)

    async def aclose(self) -> None:
        await self._client.close()
