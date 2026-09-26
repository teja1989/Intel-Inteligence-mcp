"""End-to-end check of ONE real model with the real MCP server. Run: make model-check LLM=gemini

Needs `make mocks` + `make mcp-http` running, and the model's key in .env. Runs the
harness agent as lab caller alice (synthetic data), in one conversation.

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

from telco_mcp_lab.harness import models
from telco_mcp_lab.harness.__main__ import explain, mcp_client
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
    # Always alice with her lab token: the isolation step relies on her tenant.
    hs = HarnessSettings(caller="alice", bearer_token=None, customer_account_id=None)
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
    client, http = mcp_client(hs)
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
                step("2 MCP server reachable", True, f"{hs.mcp_url} as alice")
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

                out = await agent.ask("Now show me account ACC-2001.")
                leaked = [
                    c
                    for c in out.tool_calls
                    if not c["is_error"] and "ACC-2001" in json.dumps(c.get("result", ""))
                ]
                step("6 isolation", not leaked,
                     "ACC-2001 (other tenant) was refused by the server" if not leaked
                     else "ACC-2001 DATA WAS RETURNED: report this")  # fmt: skip
        except Exception as exc:
            failed_at = (
                "2 MCP server reachable"
                if len(results) < 3
                else f"{len(results) + 1} (in conversation)"
            )
            step(failed_at, False, explain(exc))
    finally:
        tracer.close()
        await http.aclose()
        close = getattr(llm, "aclose", None)
        if close:
            await close()

    failed = [r for r in results if not r[1]]
    print(f"\n{'ALL PASSED' if not failed else f'{len(failed)} FAILED'} "
          f"({models.LABELS[provider]}). Trace: {hs.trace_dir}/")  # fmt: skip
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
