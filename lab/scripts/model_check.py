"""End-to-end check of ONE real model with the real MCP server. Run: make model-check LLM=gemini

Needs `make mocks` running and the model's key in .env. Uses the harness's MCP
transport (stdio by default: it starts the server itself), synthetic data, one
conversation.

Steps, each PASS/FAIL with a hint:
  1 provider configured      which model will be used
  2 MCP server reachable     stdio, or HTTP with HARNESS_BEARER_TOKEN
  3 plain reply              key, model name, network/proxy
  4 tool call                the model accepts our tool schemas, calls a tool, and gets
                             an answer after the result (Claude thinking / Gemini
                             thought signatures replayed on the second model call)
  5 follow-up turn           the conversation history is accepted on the next turn
  6 tool error handled       a not-found tool result is accepted and answered (error
                             results replayed correctly to the provider)
"""

import asyncio
import sys
import time

from telco_mcp_lab.harness import models
from telco_mcp_lab.harness.__main__ import describe, explain, mcp_client
from telco_mcp_lab.harness.agent import Agent, deny_all
from telco_mcp_lab.harness.guardrails import Guardrails
from telco_mcp_lab.harness.prompts import load_system_prompt
from telco_mcp_lab.harness.settings import HarnessSettings
from telco_mcp_lab.harness.trace import Tracer

results: list[tuple[str, bool]] = []


def step(name: str, ok: bool, detail: str) -> bool:
    results.append((name, ok))
    print(f"{'✅' if ok else '❌'} {name}: {detail}")
    return ok


async def main() -> int:
    hs = HarnessSettings()
    try:
        provider = models.resolve(hs.llm)
    except RuntimeError as exc:
        step("1 provider configured", False, str(exc))
        return 1
    step("1 provider configured", True,
         f"{models.LABELS[provider]}, model {models.model_name(provider)!r}")  # fmt: skip

    llm = models.build_chat_model(provider)
    tracer = Tracer(
        out=None, jsonl=hs.trace_dir / f"model-check-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    client, close = mcp_client(hs)
    try:
        try:
            started = time.perf_counter()
            turn = await llm.complete([{"role": "user", "content": "Reply with exactly: OK"}], [])
            step("3 plain reply", bool(turn.content),
                 f"{turn.content!r} in {time.perf_counter() - started:.1f}s")  # fmt: skip
        except Exception as exc:
            step("3 plain reply", False, explain(exc))
            return 1

        try:
            async with client as mcp:
                step("2 MCP server reachable", True, describe(hs))
                agent = Agent(
                    llm, mcp, tracer, max_steps=hs.max_steps, confirm=deny_all,
                    system_prompt=load_system_prompt(hs.system_prompt_file),
                    use_server_instructions=hs.use_server_instructions,
                    guardrails=Guardrails.load(hs.guardrails_file),
                )  # fmt: skip

                out = await agent.ask("Summarise account ACC-1001.")
                called = [c for c in out.tool_calls if c["name"] == "get_account_summary"]
                ok = bool(called) and not called[0]["is_error"] and bool(out.answer)
                names = [c["name"] for c in out.tool_calls]
                detail = f"called {names}, answer: {(out.answer or '')[:120]!r}"
                if not called:
                    detail += " (the model answered without calling the tool)"
                step("4 tool call", ok, detail)

                out = await agent.ask("Which plans are active on it?")
                step("5 follow-up turn", bool(out.answer), f"answer: {(out.answer or '')[:120]!r}")

                out = await agent.ask("Now summarise account ACC-9999.")
                errors = [c for c in out.tool_calls if c["is_error"]]
                ok = bool(errors) and bool(out.answer)
                detail = f"answer: {(out.answer or '')[:120]!r}"
                if not errors:
                    detail += " (expected a not-found tool call for ACC-9999)"
                step("6 tool error handled", ok, detail)
        except Exception as exc:
            failed_at = (
                "2 MCP server reachable"
                if len(results) < 3
                else f"{len(results) + 1} (in conversation)"
            )
            step(failed_at, False, explain(exc))
    finally:
        tracer.close()
        await close()
        close = getattr(llm, "aclose", None)
        if close:
            await close()

    failed = [r for r in results if not r[1]]
    print(f"\n{'ALL PASSED' if not failed else f'{len(failed)} FAILED'} "
          f"({models.LABELS[provider]}). Trace: {hs.trace_dir}/")  # fmt: skip
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
