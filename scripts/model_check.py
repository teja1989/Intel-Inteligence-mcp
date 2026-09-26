"""End-to-end check of ONE real model with the real MCP server. Run: make model-check LLM=gemini

Needs `make mocks` + `make mcp-http` running, and the model's key in .env. Uses the chat
UI's own code path (chat_ui.session.run_turn), as lab caller alice (synthetic data).

Steps, each PASS/FAIL with a hint:
  1 provider configured      which model will be used
  2 MCP server reachable     as alice
  3 plain reply              key, model name, network/proxy
  4 tool call                the model accepts our tool schemas, calls a tool, and gets
                             an answer after the result (Claude thinking / Gemini
                             thought signatures replayed on the second model call)
  5 follow-up turn           the conversation history is accepted on the next turn
  6 isolation                another tenant's account never comes back as data
"""

import asyncio
import json
import sys
import time

from telco_mcp_lab.chat_ui.session import ChatConfig, ChatState, explain, run_turn
from telco_mcp_lab.harness import models
from telco_mcp_lab.harness.settings import HarnessSettings

results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str) -> bool:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}: {detail}")
    return ok


async def main() -> int:
    hs = HarnessSettings()
    try:
        provider = models.resolve(hs.llm)
    except RuntimeError as exc:
        step("1 provider configured", False, str(exc))
        return 1
    step("1 provider configured", True, f"{models.LABELS[provider]}, model "
         f"{models.model_name(provider)!r}")  # fmt: skip

    cfg = ChatConfig(provider=provider, identity="lab", caller="alice", mcp_url=hs.mcp_url)
    state = ChatState()

    llm = models.build_chat_model(provider)
    try:
        started = time.perf_counter()
        turn = await llm.complete([{"role": "user", "content": "Reply with exactly: OK"}], [])
        step("3 plain reply", bool(turn.content), f"{turn.content!r} in "
             f"{time.perf_counter() - started:.1f}s")  # fmt: skip
    except Exception as exc:
        step("3 plain reply", False, explain(exc))
        return 1
    finally:
        await llm.aclose()

    async def ask(label: str, prompt: str):
        try:
            return await run_turn(cfg, state, prompt)
        except Exception as exc:
            step(label, False, explain(exc))
            return None

    out = await ask("4 tool call", "Summarise account ACC-1001.")
    if out is None:
        return 1
    step("2 MCP server reachable", True, f"{hs.mcp_url} as alice")
    called = [c for c in out.tool_calls if c["name"] == "get_account_summary"]
    ok = bool(called) and not called[0]["is_error"] and bool(out.answer)
    detail = (f"called {[c['name'] for c in out.tool_calls]}, answer: "
              f"{(out.answer or '')[:120]!r}")  # fmt: skip
    if not called:
        detail += " (the model answered without calling the tool)"
    step("4 tool call", ok, detail)

    out2 = await ask("5 follow-up turn", "Which plans are active on it?")
    if out2 is not None:
        step("5 follow-up turn", bool(out2.answer), f"answer: {(out2.answer or '')[:120]!r}")

    out3 = await ask("6 isolation", "Now show me account ACC-2001.")
    if out3 is not None:
        leaked = [
            c for c in out3.tool_calls
            if not c["is_error"] and "ACC-2001" in json.dumps(c.get("result", ""))
        ]  # fmt: skip
        step("6 isolation", not leaked, "ACC-2001 (other tenant) was refused by the server"
             if not leaked else "ACC-2001 DATA WAS RETURNED: report this")  # fmt: skip

    failed = [r for r in results if not r[1]]
    print(f"\n{'ALL PASSED' if not failed else f'{len(failed)} FAILED'} "
          f"({models.LABELS[provider]}). Trace: .data/chat-ui/")  # fmt: skip
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
