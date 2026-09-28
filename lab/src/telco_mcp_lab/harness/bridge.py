"""MCP ⇄ LLM translation: this is where "routing" is decided. There's no router.

1. `tools/list` → OpenAI `tools=[{"type": "function", "function": {name, description,
   parameters}}]`. The model chooses a tool **only** from these names,
   descriptions and schemas. That's why Phase 6 measures descriptions.
2. The model's tool call → MCP `tools/call(name, arguments)`.
3. The MCP result → a `role: "tool"` message. Structured content is sent as JSON;
   tool errors are marked so the model can self-correct.

Everything in (1) and (3) is sent to the LLM provider. That's why the server
shapes and masks its output: this is the data-leaves-the-building point.

Java/Spring AI equivalent: `SyncMcpToolCallbackProvider` turns MCP tools into
`ToolCallback`s that `ChatClient` hands to the model; Spring AI runs this loop
for you (internal tool execution). The harness does it by hand so you can see it.
"""

import json
from typing import Any

from mcp.types import CallToolResult, Tool

MAX_RESULT_CHARS = 8000  # protect the context window from a runaway tool result


def to_openai_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def result_to_message_content(result: CallToolResult) -> str:
    if result.is_error:
        text = " ".join(c.text for c in result.content if c.type == "text")
        return f"TOOL ERROR: {text}"
    if result.structured_content is not None:
        body = json.dumps(result.structured_content, ensure_ascii=False)
    else:
        body = " ".join(c.text for c in result.content if c.type == "text")
    if len(body) > MAX_RESULT_CHARS:
        body = body[:MAX_RESULT_CHARS] + " …[truncated by host]"
    return body


def is_destructive(tool: Tool) -> bool:
    """Spec defaults: readOnlyHint=false and destructiveHint=true when absent.

    So a tool that doesn't say it's read-only counts as destructive. Hints come
    from the server; we trust *our* server, and the spec says clients MUST treat
    annotations from untrusted servers as untrusted.
    """
    a = tool.annotations
    if a is None:
        return True
    if a.read_only_hint:
        return False
    return a.destructive_hint is not False
