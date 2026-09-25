"""The function-calling loop: the host's job, made visible step by step.

    user prompt
      └─► LLM (sees tool list) ──► text answer? ── yes ──► done
                │ no: tool call(s)
                ▼
          host checks: known tool? valid JSON args? destructive → ask the human
                ▼
          MCP tools/call ──► result appended as role=tool ──► back to the LLM

Guards (all are host responsibilities, not the model's or the server's):
  * max_steps: stops a model that loops forever;
  * unknown tool name (hallucinated): error back to the model, nothing executed;
  * invalid JSON arguments: error back to the model, nothing executed;
  * destructive tools: a human must say yes (y/N) first. A declined call is
    reported to the model as declined, never silently skipped;
  * tool results are data: the system prompt says so, the server shapes them.
"""

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.types import Tool

from telco_mcp_lab.harness.bridge import is_destructive, result_to_message_content, to_openai_tools
from telco_mcp_lab.harness.guardrails import Guardrails
from telco_mcp_lab.harness.llm import ChatModel
from telco_mcp_lab.harness.prompts import compose_system_prompt, load_system_prompt
from telco_mcp_lab.harness.trace import Tracer

Confirm = Callable[[str, dict[str, Any]], Awaitable[bool]]


async def deny_all(_: str, __: dict[str, Any]) -> bool:
    return False


@dataclass
class RunResult:
    answer: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{name, arguments, is_error}]
    steps: int = 0
    stopped_reason: str = "answered"  # answered | max_steps | input_blocked
    messages: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        llm: ChatModel,
        mcp: Client,
        tracer: Tracer,
        *,
        max_steps: int = 8,
        confirm: Confirm = deny_all,
        system_prompt: str | None = None,
        use_server_instructions: bool = True,
        guardrails: Guardrails | None = None,
    ) -> None:
        self.llm, self.mcp, self.tracer = llm, mcp, tracer
        self.max_steps, self.confirm = max_steps, confirm
        self.host_prompt = system_prompt if system_prompt is not None else load_system_prompt()
        self.use_server_instructions = use_server_instructions
        self.guardrails = guardrails or Guardrails.disabled()
        self.history: list[dict[str, Any]] = [{"role": "system", "content": self.host_prompt}]
        self._tools: dict[str, Tool] = {}
        self._openai_tools: list[dict[str, Any]] = []
        self._loaded = False

    async def load_tools(self) -> None:
        tools = (await self.mcp.list_tools()).tools
        self._tools = {t.name: t for t in tools}
        self._openai_tools = to_openai_tools(tools)
        instructions = self.mcp.instructions if self.use_server_instructions else None
        self.history[0]["content"] = compose_system_prompt(
            self.host_prompt, "telco-mcp-lab", instructions
        )
        self.tracer.tools_loaded(tools)
        self.tracer.system_prompt(self.history[0]["content"], bool(instructions))
        self._loaded = True

    async def ask(self, prompt: str) -> RunResult:
        """One user turn. History is kept, so follow-up questions work (REPL)."""
        if not self._loaded:
            await self.load_tools()
        result = RunResult(answer=None, messages=self.history)

        # Guard FIRST, then trace: the trace file must never hold what the
        # guardrail removed (it's a log on disk too).
        guarded = self.guardrails.check_input(prompt)
        blocked = guarded.blocked_message is not None
        self.tracer.user("[message blocked by input guardrail; not recorded]" if blocked
                         else guarded.text)  # fmt: skip
        self.tracer.guardrail("input", guarded.events)
        if blocked:
            # Never sent to the model, and not added to the conversation history.
            result.answer, result.stopped_reason = guarded.blocked_message, "input_blocked"
            self.tracer.final(result.answer)
            return result
        self.history.append({"role": "user", "content": guarded.text})
        grounding: list[str] = []  # tool results seen this turn (for the output guard)

        for step in range(1, self.max_steps + 1):
            result.steps = step
            started = time.perf_counter()
            turn = await self.llm.complete(self.history, self._openai_tools)
            self.tracer.llm_turn(step, turn, time.perf_counter() - started)

            if not turn.tool_calls:
                checked = self.guardrails.check_output(turn.content or "", grounding)
                self.tracer.guardrail("output", checked.events)
                # History keeps what the user actually saw.
                self.history.append({"role": "assistant", "content": checked.text})
                result.answer = checked.text
                self.tracer.final(checked.text)
                return result

            self.history.append(
                {
                    "role": "assistant",
                    "content": turn.content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.arguments},
                        }
                        for c in turn.tool_calls
                    ],
                }
            )
            for call in turn.tool_calls:
                content, record = await self._execute(call.name, call.arguments)
                result.tool_calls.append(record)
                grounding.append(content)
                self.history.append({"role": "tool", "tool_call_id": call.id, "content": content})

        result.stopped_reason = "max_steps"
        self.tracer.stopped(f"max_steps={self.max_steps} reached without a final answer")
        return result

    async def _execute(self, name: str, raw_args: str) -> tuple[str, dict[str, Any]]:
        record: dict[str, Any] = {"name": name, "arguments": raw_args, "is_error": True}
        tool = self._tools.get(name)
        if tool is None:
            msg = f"HOST ERROR: there is no tool named {name!r}. Available: {sorted(self._tools)}"
            self.tracer.host_error(name, msg)
            return msg, record
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            msg = f"HOST ERROR: arguments were not a valid JSON object ({exc}). Call again."
            self.tracer.host_error(name, msg)
            return msg, record
        record["arguments"] = args

        if is_destructive(tool):
            approved = await self.confirm(name, args)
            self.tracer.confirmation(name, args, approved)
            if not approved:
                return "The user DECLINED this action. Do not retry it; tell the user.", record

        self.tracer.tool_call(name, args)
        started = time.perf_counter()
        result = await self.mcp.call_tool(name, args)
        content = result_to_message_content(result)
        record["is_error"] = result.is_error
        self.tracer.tool_result(name, result.is_error, content, time.perf_counter() - started)
        return content, record
