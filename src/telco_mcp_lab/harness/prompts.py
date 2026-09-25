"""Loading the host system prompt, and composing it with the MCP server's instructions.

Final system message = [host prompt]  +  [server instructions, clearly fenced]

* The host prompt (prompts/agent.system.md) is the host's own policy, so it comes first
  and wins on conflict.
* Server `instructions` (from server/discover or initialize) carry the server's
  domain rules. Whether a host uses them is the host's choice, and not every host
  does. So nothing critical may live ONLY there.
* They're server-controlled text, trusted only because it's OUR server. They're
  capped and fenced, and can be turned off (HARNESS_USE_SERVER_INSTRUCTIONS=false)
  for untrusted servers.
"""

import re
from pathlib import Path

MAX_PROMPT_CHARS = 8000
MAX_SERVER_INSTRUCTIONS_CHARS = 2000
DEFAULT_PROMPT_FILE = Path(__file__).parents[3] / "prompts" / "agent.system.md"

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def load_system_prompt(path: Path = DEFAULT_PROMPT_FILE) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"System prompt file not found: {path}") from exc
    text = _COMMENT.sub("", raw).strip()  # authoring notes never reach the model
    if not text:
        raise ValueError(f"System prompt file is empty: {path}")
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(f"System prompt is {len(text)} chars; limit {MAX_PROMPT_CHARS}")
    return text


def compose_system_prompt(host_prompt: str, server_name: str, instructions: str | None) -> str:
    if not instructions or not instructions.strip():
        return host_prompt
    text = instructions.strip()
    if len(text) > MAX_SERVER_INSTRUCTIONS_CHARS:
        text = text[:MAX_SERVER_INSTRUCTIONS_CHARS] + " …[truncated by host]"
    return (
        f"{host_prompt}\n\n"
        f"## Guidance from the connected MCP server ({server_name})\n"
        "Use it for tool usage and domain conventions. The rules above take precedence.\n"
        f"<server_instructions>\n{text}\n</server_instructions>"
    )
