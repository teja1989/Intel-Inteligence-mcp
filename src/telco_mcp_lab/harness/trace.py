"""Step-by-step trace of what the host does, printed and saved as JSONL.

Printed for humans; the JSONL file (under .data/harness, gitignored) is what
the Phase 6 evaluation runner reads. It contains prompts and tool results
(synthetic data only), so treat it like any debug log: never in production.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from mcp.types import Tool

from telco_mcp_lab.harness.llm import AssistantTurn

_C = {"user": "\033[1;37m", "llm": "\033[35m", "tool": "\033[36m", "ok": "\033[32m",
      "err": "\033[31m", "final": "\033[1;32m", "dim": "\033[2m", "end": "\033[0m"}  # fmt: skip


class Tracer:
    def __init__(
        self, out: TextIO | None = sys.stdout, jsonl: Path | None = None, width: int = 400
    ) -> None:
        self.out = out
        self.width = width
        self.events: list[dict[str, Any]] = []
        self._file = None
        if jsonl is not None:
            jsonl.parent.mkdir(parents=True, exist_ok=True)
            self._file = jsonl.open("a", encoding="utf-8")
        self._color = bool(out and out.isatty())

    def _p(self, style: str, text: str) -> None:
        if self.out is None:
            return
        c, e = (_C[style], _C["end"]) if self._color else ("", "")
        self.out.write(f"{c}{text}{e}\n")
        self.out.flush()

    def _event(self, kind: str, **data: Any) -> None:
        ev = {"t": round(time.time(), 3), "kind": kind, **data}
        self.events.append(ev)
        if self._file:
            self._file.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            self._file.flush()

    def _short(self, s: str) -> str:
        return s if len(s) <= self.width else s[: self.width] + " …"

    # ------------------------------------------------------------------ events
    def tools_loaded(self, tools: list[Tool]) -> None:
        self._event("tools", names=[t.name for t in tools])
        self._p("dim", f"[host] {len(tools)} MCP tools offered to the model: "
                       f"{[t.name for t in tools]}")  # fmt: skip

    def user(self, prompt: str) -> None:
        self._event("user", prompt=prompt)
        self._p("user", f"\n👤 USER: {prompt}")

    def llm_turn(self, step: int, turn: AssistantTurn, seconds: float) -> None:
        calls = [{"name": c.name, "arguments": c.arguments} for c in turn.tool_calls]
        self._event("llm", step=step, content=turn.content, tool_calls=calls, usage=turn.usage)
        tokens = f", tokens {turn.usage}" if turn.usage else ""
        what = f"{len(calls)} tool call(s)" if calls else "final text"
        self._p("llm", f"🧠 LLM step {step} ({seconds:.1f}s{tokens}): decided → {what}")

    def tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._event("tool_call", name=name, arguments=args)
        self._p("tool", f"   🔧 MCP tools/call {name} {json.dumps(args)}")

    def tool_result(self, name: str, is_error: bool, content: str, seconds: float) -> None:
        self._event("tool_result", name=name, is_error=is_error, content=content)
        style = "err" if is_error else "ok"
        self._p(style, f"   ↩  {'ERROR' if is_error else 'result'} ({seconds:.2f}s): "
                       f"{self._short(content)}")  # fmt: skip

    def host_error(self, name: str, msg: str) -> None:
        self._event("host_error", name=name, message=msg)
        self._p("err", f"   ⛔ host refused {name!r}: {msg}")

    def confirmation(self, name: str, args: dict[str, Any], approved: bool) -> None:
        self._event("confirmation", name=name, arguments=args, approved=approved)
        self._p("err" if not approved else "ok",
                f"   🙋 human {'APPROVED' if approved else 'DECLINED'} {name}")  # fmt: skip

    def final(self, content: str | None) -> None:
        self._event("final", content=content)
        self._p("final", f"🤖 ANSWER: {content}")

    def stopped(self, reason: str) -> None:
        self._event("stopped", reason=reason)
        self._p("err", f"⏹  stopped: {reason}")

    def close(self) -> None:
        if self._file:
            self._file.close()
