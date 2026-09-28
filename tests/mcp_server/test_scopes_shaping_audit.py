"""Scopes (tool visibility + enforcement), PII masking, injection neutralising, audit."""

import json
import logging

import pytest
from mcp import Client

from telco_mcp.security.audit import AUDIT_LOGGER
from telco_mcp.shaping.pii import mask_msisdn, mask_name
from telco_mcp_lab.mock_apis.data import INJECTED_NOTE
from tests.conftest import server_with

READ_TOOLS = {
    "get_account_summary",
    "list_subscriptions",
    "get_service_details",
    "get_order_status",
    "list_orders",
}


# ----------------------------------------------------------------------------------- scopes
@pytest.mark.security
class TestScopes:
    async def test_reader_sees_read_tools(self, mock_telco_factory):
        async with Client(server_with(mock_telco_factory)) as c:
            assert {t.name for t in (await c.list_tools()).tools} == READ_TOOLS

    async def test_no_scopes_means_no_tools(self, mock_telco_factory):
        async with Client(server_with(mock_telco_factory, "profile")) as c:
            assert (await c.list_tools()).tools == []

    @pytest.mark.parametrize("tool", sorted(READ_TOOLS))
    async def test_hidden_tool_cannot_be_called_and_looks_nonexistent(
        self, tool, mock_telco_factory
    ):
        async with Client(server_with(mock_telco_factory, "profile")) as c:
            hidden = await c.call_tool(tool, {})
            missing = await c.call_tool("no_such_tool", {})
        assert hidden.is_error
        assert hidden.content[0].text == f"Unknown tool: {tool}"
        assert missing.content[0].text == "Unknown tool: no_such_tool"  # same shape

    async def test_tool_without_declared_scope_is_hidden_from_everyone(self, mock_telco_factory):
        server = server_with(mock_telco_factory, "read", "pii:read")

        @server.tool(name="forgot_scope")
        async def forgot_scope() -> str:
            return "should never run"

        async with Client(server) as c:
            assert "forgot_scope" not in {t.name for t in (await c.list_tools()).tools}
            assert (await c.call_tool("forgot_scope", {})).is_error


# ---------------------------------------------------------------------------------- masking
class TestPii:
    def test_mask_functions(self):
        assert mask_msisdn("+447700900111") == "+44*******111"
        assert mask_name("Alex Example") == "A*** E******"

    @pytest.mark.security
    async def test_masked_by_default_unmasked_with_pii_scope(self, mock_telco_factory):
        args = {"account_id": "ACC-1001", "status": "ACTIVE"}
        async with Client(server_with(mock_telco_factory)) as c:
            masked = (await c.call_tool("list_subscriptions", args)).structured_content
        async with Client(server_with(mock_telco_factory, "read", "pii:read")) as c:
            clear = (await c.call_tool("list_subscriptions", args)).structured_content
        assert masked["items"][0]["msisdn"] == "+44*******111"
        assert clear["items"][0]["msisdn"] == "+447700900111"

    @pytest.mark.security
    async def test_line_details_never_expose_imsi_or_iccid(self, mock_telco_factory):
        async with Client(server_with(mock_telco_factory, "read", "pii:read")) as c:
            r = await c.call_tool("get_service_details", {"subscription_id": "SUB-1001-01"})
        wire = json.dumps(r.structured_content)
        assert "00101" not in wire and "8900101" not in wire


# ------------------------------------------------------------------- free text (notes)
@pytest.mark.security
class TestNotes:
    """Structural control: free-text notes never reach the model, only `has_notes`.
    (Replaced a keyword filter that a paraphrase could bypass, 2026-09-28.)"""

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("get_account_summary", {"account_id": "ACC-1001"}),
            ("get_service_details", {"subscription_id": "SUB-1001-01"}),
        ],
    )
    async def test_note_text_never_returned(self, mock_telco_factory, tool, args):
        async with Client(server_with(mock_telco_factory, "read", "pii:read")) as c:
            r = await c.call_tool(tool, args)
        wire = json.dumps(r.model_dump(mode="json"))
        assert "ignore previous" not in wire and "notes" not in r.structured_content
        assert isinstance(r.structured_content["has_notes"], bool)

    async def test_has_notes_reflects_the_backend(self, mock_telco_factory):
        async with Client(server_with(mock_telco_factory)) as c:
            r = await c.call_tool("get_account_summary", {"account_id": "ACC-1001"})
        assert INJECTED_NOTE and r.structured_content["has_notes"] is True


# ------------------------------------------------------------------------------------ audit
@pytest.mark.security
class TestAudit:
    async def test_one_line_per_call_with_metadata_only(self, caplog, mock_telco_factory):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
        async with Client(server_with(mock_telco_factory)) as c:
            await c.call_tool("get_account_summary", {"account_id": "ACC-1001"})  # ok
            await c.call_tool("get_order_status", {"order_id": "ORD-999999"})  # tool_error
            await c.call_tool("get_order_status", {"order_id": "bad ORD-1"})  # tool_error (args)
        lines = [json.loads(r.message) for r in caplog.records if r.name == AUDIT_LOGGER]
        assert [(e["tool"], e["outcome"], e["resources"]) for e in lines] == [
            ("get_account_summary", "ok", {"account_id": "ACC-1001"}),
            ("get_order_status", "tool_error", {"order_id": "ORD-999999"}),
            ("get_order_status", "tool_error", {}),  # malformed IDs are never recorded
        ]
        for e in lines:
            assert set(e) == {
                "event", "tool", "client", "via", "resources", "outcome", "latency_ms"
            }  # fmt: skip
            assert e["client"] == "test" and e["via"] == "in-process"
        raw = " ".join(r.message for r in caplog.records if r.name == AUDIT_LOGGER)
        for payload in ("Alex", "ignore previous", "+44", "Standard", "bad ORD"):
            assert payload not in raw  # results and rejected values are never logged

    async def test_hidden_tool_attempt_is_audited_as_denied(self, caplog, mock_telco_factory):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
        async with Client(server_with(mock_telco_factory, "profile")) as c:
            await c.call_tool("list_orders", {"account_id": "ACC-1001"})
        (line,) = [json.loads(r.message) for r in caplog.records if r.name == AUDIT_LOGGER]
        assert line["outcome"] == "denied" and line["client"] == "test"
        assert line["resources"] == {"account_id": "ACC-1001"}  # probing is visible too


class TestFriendlyValidation:
    async def test_arg_errors_name_the_field_and_rule_not_the_value(self, mock_telco_factory):
        async with Client(server_with(mock_telco_factory)) as c:
            r = await c.call_tool("get_order_status", {"order_id": "order one two three"})
        msg = r.content[0].text
        assert r.is_error
        assert "order_id" in msg and "ORD-" in msg
        assert "order one two three" not in msg  # the rejected value is never echoed
        assert "errors.pydantic.dev" not in msg


class TestCrashesAreNotBlamedOnTheClient:
    async def test_bug_in_tool_is_audited_as_error_not_invalid_arguments(
        self, caplog, mock_telco_factory
    ):
        from pydantic import BaseModel

        server = server_with(mock_telco_factory)

        class Out(BaseModel):
            n: int

        server.require_scope("buggy", "read")

        @server.tool(name="buggy")
        async def buggy() -> Out:
            return Out.model_validate({"n": "not a number"})  # our bug, not the client's

        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
        async with Client(server) as c:
            r = await c.call_tool("buggy", {})
        assert r.is_error
        assert "Invalid arguments" not in r.content[0].text
        (line,) = [json.loads(x.message) for x in caplog.records if x.name == AUDIT_LOGGER]
        assert line["outcome"] == "error"
