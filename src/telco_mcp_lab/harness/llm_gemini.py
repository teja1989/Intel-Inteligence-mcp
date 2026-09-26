"""Gemini as the host's model, via the official Google Gen AI SDK (`google-genai`).

Manual function calling (automatic function calling is OFF: the agent loop, with its
guardrails and confirmation step, stays in charge):

    system messages        → `system_instruction`
    user                   → Content(role="user", parts=[text])
    assistant (Gemini)     → the model's own Content, replayed verbatim (it carries
                             thought signatures that must come back unchanged)
    assistant (other)      → text + function_call parts
    consecutive role=tool  → ONE Content(role="tool") with function_response parts
    response function_call → ToolCall (an ID is synthesized when Gemini sends none)

Blocked prompts (`prompt_feedback.block_reason`) and SAFETY stops become a fixed,
safe answer.

Java/Spring equivalent: Spring AI's Vertex AI / Google GenAI chat model + ToolCallbacks.
"""

import json
import ssl
from itertools import count
from typing import Any

import httpx
from google import genai
from google.genai import types

from telco_mcp_lab.harness.llm import AssistantTurn, ToolCall
from telco_mcp_lab.harness.settings import GeminiSettings

REFUSED = "I can't help with that request."
SYNTHETIC_ID = "gemini-call-"  # prefix for IDs we invent (Gemini API often sends none)
_ids = count(1)


def to_gemini_tools(tools: list[dict[str, Any]]) -> list[types.Tool]:
    if not tools:
        return []
    decls = [
        types.FunctionDeclaration(
            name=t["function"]["name"],
            description=t["function"].get("description", ""),
            parameters_json_schema=t["function"].get("parameters")
            or {"type": "object", "properties": {}},
        )
        for t in tools
    ]
    return [types.Tool(function_declarations=decls)]


def to_gemini_contents(messages: list[dict[str, Any]]) -> tuple[str, list[Any]]:
    """Chat Completions history → (system_instruction, contents)."""
    system_parts: list[str] = []
    contents: list[Any] = []
    names: dict[str, str] = {}  # tool_call_id → function name (responses need the name)
    pending: list[types.Part] = []

    def flush() -> None:
        if pending:
            contents.append(types.Content(role="tool", parts=list(pending)))
            pending.clear()

    for m in messages:
        role = m["role"]
        if role == "tool":
            call_id = m["tool_call_id"]
            key = "error" if m.get("_is_error") else "result"
            fr = types.FunctionResponse(
                name=names.get(call_id, "unknown"),
                response={key: m.get("content") or ""},
                id=None if call_id.startswith(SYNTHETIC_ID) else call_id,
            )
            pending.append(types.Part(function_response=fr))
            continue
        flush()
        if role == "system":
            system_parts.append(m.get("content") or "")
        elif role == "user":
            contents.append(
                types.Content(role="user", parts=[types.Part(text=m.get("content") or "")])
            )
        elif role == "assistant":
            for call in m.get("tool_calls") or []:
                names[call["id"]] = call["function"]["name"]
            native = m.get("_native")
            if isinstance(native, dict) and native.get("provider") == "gemini":
                contents.append(native["content"])  # verbatim: thought signatures intact
                continue
            parts: list[types.Part] = []
            if m.get("content"):
                parts.append(types.Part(text=m["content"]))
            for call in m.get("tool_calls") or []:
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                fc = types.FunctionCall(
                    name=call["function"]["name"],
                    args=args if isinstance(args, dict) else {},
                    id=None if call["id"].startswith(SYNTHETIC_ID) else call["id"],
                )
                parts.append(types.Part(function_call=fc))
            if parts:
                contents.append(types.Content(role="model", parts=parts))
    flush()
    return "\n\n".join(p for p in system_parts if p), contents


class GeminiChatModel:
    def __init__(
        self, settings: GeminiSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.name = f"gemini:{settings.model}"
        self._s = settings
        verify: Any = (
            ssl.create_default_context(cafile=str(settings.ca_bundle))
            if settings.ca_bundle
            else True
        )
        self._http = httpx.AsyncClient(
            transport=transport,  # test seam: a mocked Gemini API
            proxy=settings.proxy,
            verify=verify,
            # The key travels in x-goog-api-key, which is not stripped on a
            # cross-host redirect: never follow redirects.
            follow_redirects=False,
            timeout=settings.timeout_s,
        )
        self._client = genai.Client(
            api_key=settings.api_key.get_secret_value(),
            vertexai=False,  # explicit: GOOGLE_GENAI_USE_VERTEXAI must not flip it
            http_options=types.HttpOptions(
                base_url=settings.base_url,  # explicit: not GOOGLE_GEMINI_BASE_URL
                httpx_async_client=self._http,
                timeout=int(settings.timeout_s * 1000),
            ),
        )

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AssistantTurn:
        system, contents = to_gemini_contents(messages)
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            tools=to_gemini_tools(tools) or None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = await self._client.aio.models.generate_content(
            model=self._s.model, contents=contents, config=config
        )
        meta = resp.usage_metadata
        usage = (
            {
                "prompt_tokens": meta.prompt_token_count or 0,
                "completion_tokens": meta.candidates_token_count or 0,
            }
            if meta
            else None
        )
        blocked = resp.prompt_feedback is not None and resp.prompt_feedback.block_reason
        if blocked or not resp.candidates:
            return AssistantTurn(content=REFUSED, usage=usage)
        cand = resp.candidates[0]
        if cand.finish_reason in (
            types.FinishReason.SAFETY,
            types.FinishReason.PROHIBITED_CONTENT,
            types.FinishReason.BLOCKLIST,
            types.FinishReason.SPII,
        ):
            return AssistantTurn(content=REFUSED, usage=usage)
        parts = (cand.content.parts if cand.content else None) or []
        text = "".join(p.text for p in parts if p.text and not p.thought).strip() or None
        calls = [
            ToolCall(
                id=p.function_call.id or f"{SYNTHETIC_ID}{next(_ids)}",
                name=p.function_call.name or "",
                arguments=json.dumps(p.function_call.args or {}),
            )
            for p in parts
            if p.function_call
        ]
        if cand.finish_reason == types.FinishReason.MAX_TOKENS and not calls:
            text = (text or "") + "\n\n[answer cut off: token limit reached]"
        native = {"provider": "gemini", "content": cand.content} if cand.content else None
        return AssistantTurn(content=text, tool_calls=calls, usage=usage, native=native)

    async def aclose(self) -> None:
        await self._http.aclose()
