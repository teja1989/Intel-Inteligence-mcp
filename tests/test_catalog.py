"""The published tool catalog must match the live definitions exactly."""

from telco_mcp_lab.mcp_server.catalog import (
    CATALOG_DOC,
    build_catalog_markdown,
    catalog_definitions,
    catalog_hash,
)
from tests.conftest import server_as


async def test_committed_catalog_matches_live_definitions():
    expected = await build_catalog_markdown()
    assert CATALOG_DOC.read_text(encoding="utf-8") == expected, (
        "docs/tool-catalog.md is out of date: run `make catalog` and review the diff. "
        "A changed tool definition is a contract change for external agents."
    )


async def test_every_tool_declares_scope_and_annotations(mock_telco_factory):
    defs = await catalog_definitions(server_as("carol", mock_telco_factory))
    for d in defs:
        assert d["required_scope"], d["name"]
        assert "readOnlyHint" in d["annotations"], d["name"]  # explicit, never defaulted
        assert d["outputSchema"], d["name"]  # structured output everywhere


async def test_hash_changes_when_any_definition_changes(mock_telco_factory):
    defs = await catalog_definitions(server_as("carol", mock_telco_factory))
    before = catalog_hash(defs)
    defs[0]["description"] += " "  # even whitespace in a description is a contract change
    assert catalog_hash(defs) != before and before.startswith("sha256:")


async def test_catalog_ignores_the_callers_scopes(mock_telco_factory):
    """The contract lists every tool, even ones a given caller can't see."""
    defs = await catalog_definitions(server_as("mallory", mock_telco_factory))
    assert len(defs) == 5
