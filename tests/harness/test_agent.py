"""The host's function-calling loop, with a scripted LLM (offline, deterministic).

The MCP side is REAL: the in-process MCP server acting as alice, backed by the
real mock gateway. Only the model's decisions are scripted.
"""

import io
import json

import pytest
from mcp import Client
from mcp.types import ToolAnnotations

from telco_mcp_lab.harness.agent import Agent
from telco_mcp_lab.harness.llm import AssistantTurn, ToolCall
from telco_mcp_lab.harness.trace import Tracer
from tests.conftest import server_as


class ScriptedModel:
    """Plays back AssistantTurns and records what the host sent each time."""

    name = "scripted"

    def __init__(self, *turns: AssistantTurn) -> None:
        self.turns = list(turns)
        self.seen_messages: list[list[dict]] = []
        self.seen_tools: list[list[dict]] = []

    async def complete(self, messages, tools):
        self.seen_messages.append(json.loads(json.dumps(messages)))
        self.seen_tools.append(tools)
        return self.turns.pop(0)


def call(name: str, args: dict | str, id_: str = "c1") -> AssistantTurn:
    raw = args if isinstance(args, str) else json.dumps(args)
    return AssistantTurn(content=None, tool_calls=[ToolCall(id_, name, raw)])


def say(text: str) -> AssistantTurn:
    return AssistantTurn(content=text)


def last_tool_message(model: ScriptedModel) -> str:
    return [m for m in model.seen_messages[-1] if m["role"] == "tool"][-1]["content"]


@pytest.fixture
def tracer():
    return Tracer(out=io.StringIO())


async def run(server, model, tracer, prompt="q", **kw):
    async with Client(server) as mcp:
        agent = Agent(model, mcp, tracer, **kw)
        return await agent.ask(prompt), agent


class TestLoop:
    async def test_tools_are_offered_in_openai_function_format(self, mock_telco_factory, tracer):
        model = ScriptedModel(say("hi"))
        await run(server_as("alice", mock_telco_factory), model, tracer)
        tools = model.seen_tools[0]
        names = [t["function"]["name"] for t in tools]
        assert names[0] == "get_account_summary" and len(names) == 5
        assert all(t["type"] == "function" and "parameters" in t["function"] for t in tools)

    async def test_single_tool_call_then_answer(self, mock_telco_factory, tracer):
        model = ScriptedModel(
            call("list_subscriptions", {"account_id": "ACC-1001", "status": "ACTIVE"}),
            say("You have one active line on Standard 50GB."),
        )
        result, _ = await run(server_as("alice", mock_telco_factory), model, tracer)
        assert result.answer.startswith("You have one active line")
        assert result.steps == 2
        assert [c["name"] for c in result.tool_calls] == ["list_subscriptions"]
        tool_msg = json.loads(last_tool_message(model))
        assert tool_msg["items"][0]["msisdn"] == "+44*******111"  # masked data reached the LLM

    async def test_multi_step_chain(self, mock_telco_factory, tracer):
        model = ScriptedModel(
            call("list_subscriptions", {"account_id": "ACC-1001", "status": "ACTIVE"}, "c1"),
            call("get_service_details", {"subscription_id": "SUB-1001-01"}, "c2"),
            say("Roaming is on."),
        )
        result, _ = await run(server_as("alice", mock_telco_factory), model, tracer)
        assert [c["name"] for c in result.tool_calls] == [
            "list_subscriptions",
            "get_service_details",
        ]
        assert '"roaming_enabled": true' in last_tool_message(model)

    async def test_tool_error_is_fed_back_and_model_can_self_correct(
        self, mock_telco_factory, tracer
    ):
        model = ScriptedModel(
            call("get_order_status", {"order_id": "123"}, "c1"),  # wrong format
            call("get_order_status", {"order_id": "ORD-000123"}, "c2"),  # corrected
            say("Order ORD-000123 is COMPLETED."),
        )
        result, _ = await run(server_as("alice", mock_telco_factory), model, tracer)
        first_tool_msg = [m for m in model.seen_messages[1] if m["role"] == "tool"][0]
        assert first_tool_msg["content"].startswith("TOOL ERROR:")
        assert "ORD-" in first_tool_msg["content"]  # the error tells the model the format
        assert [c["is_error"] for c in result.tool_calls] == [True, False]

    async def test_parallel_tool_calls_in_one_turn(self, mock_telco_factory, tracer):
        turn = AssistantTurn(
            content=None,
            tool_calls=[
                ToolCall("a", "get_order_status", json.dumps({"order_id": "ORD-000123"})),
                ToolCall("b", "get_order_status", json.dumps({"order_id": "ORD-000124"})),
            ],
        )
        model = ScriptedModel(turn, say("done"))
        await run(server_as("alice", mock_telco_factory), model, tracer)
        tool_msgs = [m for m in model.seen_messages[1] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["a", "b"]  # every call answered, in order

    async def test_history_supports_follow_up_questions(self, mock_telco_factory, tracer):
        model = ScriptedModel(say("first"), say("second"))
        async with Client(server_as("alice", mock_telco_factory)) as mcp:
            agent = Agent(model, mcp, tracer)
            await agent.ask("q1")
            await agent.ask("q2")
        roles = [m["role"] for m in model.seen_messages[1]]
        assert roles == ["system", "user", "assistant", "user"]


class TestHostGuards:
    async def test_hallucinated_tool_is_not_executed(self, mock_telco_factory, tracer):
        model = ScriptedModel(call("delete_account", {"account_id": "ACC-1001"}), say("sorry"))
        result, _ = await run(server_as("alice", mock_telco_factory), model, tracer)
        assert last_tool_message(model).startswith("HOST ERROR: there is no tool named")
        assert result.tool_calls[0]["is_error"]

    async def test_invalid_json_arguments_are_not_executed(self, mock_telco_factory, tracer):
        model = ScriptedModel(call("list_orders", "{not json"), say("sorry"))
        await run(server_as("alice", mock_telco_factory), model, tracer)
        assert "not a valid JSON object" in last_tool_message(model)

    async def test_max_steps_stops_a_looping_model(self, mock_telco_factory, tracer):
        model = ScriptedModel(*[call("list_orders", {"account_id": "ACC-1001"})] * 3)
        result, _ = await run(server_as("alice", mock_telco_factory), model, tracer, max_steps=3)
        assert result.stopped_reason == "max_steps" and result.answer is None

    @pytest.mark.security
    async def test_injected_note_does_not_reach_the_model(self, mock_telco_factory, tracer):
        model = ScriptedModel(call("get_account_summary", {"account_id": "ACC-1001"}), say("ok"))
        await run(server_as("alice", mock_telco_factory), model, tracer)
        everything_sent = json.dumps(model.seen_messages)
        assert "ignore previous instructions" not in everything_sent
        assert "withheld" in everything_sent


@pytest.mark.security
class TestDestructiveConfirmation:
    """Needs a destructive tool; Phase 4's will be real. Here: a test-only one."""

    @pytest.fixture
    def server_with_write(self, mock_telco_factory):
        server = server_as("alice", mock_telco_factory)
        server.require_scope("reset_voicemail_pin", "read")
        self.executed = []

        @server.tool(
            name="reset_voicemail_pin",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
        )
        async def reset_voicemail_pin(subscription_id: str) -> str:
            self.executed.append(subscription_id)
            return "reset"

        return server

    async def test_declined_action_is_not_executed_and_model_is_told(
        self, server_with_write, tracer
    ):
        model = ScriptedModel(call("reset_voicemail_pin", {"subscription_id": "SUB-1001-01"}),
                              say("ok"))  # fmt: skip

        async def human_says_no(name, args):
            return False

        await run(server_with_write, model, tracer, confirm=human_says_no)
        assert self.executed == []
        assert "DECLINED" in last_tool_message(model)

    async def test_approved_action_runs(self, server_with_write, tracer):
        model = ScriptedModel(call("reset_voicemail_pin", {"subscription_id": "SUB-1001-01"}),
                              say("ok"))  # fmt: skip
        asked = []

        async def human_says_yes(name, args):
            asked.append((name, args))
            return True

        await run(server_with_write, model, tracer, confirm=human_says_yes)
        assert asked == [("reset_voicemail_pin", {"subscription_id": "SUB-1001-01"})]
        assert self.executed == ["SUB-1001-01"]

    async def test_default_is_deny(self, server_with_write, tracer):
        model = ScriptedModel(call("reset_voicemail_pin", {"subscription_id": "SUB-1001-01"}),
                              say("ok"))  # fmt: skip
        await run(server_with_write, model, tracer)  # no confirm callback given
        assert self.executed == []

    async def test_read_only_tools_never_prompt(self, mock_telco_factory, tracer):
        model = ScriptedModel(call("list_orders", {"account_id": "ACC-1001"}), say("ok"))

        async def must_not_be_called(name, args):
            raise AssertionError("read-only tool triggered a confirmation")

        await run(server_as("alice", mock_telco_factory), model, tracer,
                  confirm=must_not_be_called)  # fmt: skip


class TestTrace:
    async def test_trace_records_every_step(self, mock_telco_factory, tmp_path):
        jsonl = tmp_path / "t.jsonl"
        tracer = Tracer(out=io.StringIO(), jsonl=jsonl)
        model = ScriptedModel(call("list_orders", {"account_id": "ACC-1001"}), say("two orders"))
        await run(server_as("alice", mock_telco_factory), model, tracer, prompt="my orders?")
        tracer.close()
        kinds = [json.loads(line)["kind"] for line in jsonl.read_text().splitlines()]
        assert kinds == [
            "tools",
            "system_prompt",
            "user",
            "llm",
            "tool_call",
            "tool_result",
            "llm",
            "final",
        ]
        printed = tracer.out.getvalue()
        for marker in ("USER: my orders?", "tools/call list_orders", "ANSWER: two orders"):
            assert marker in printed
