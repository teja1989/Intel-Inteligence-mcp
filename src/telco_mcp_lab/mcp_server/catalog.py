"""The tool catalog: the server's public contract for external agents.

Generated from the live tool definitions (never hand-written), so the document
integrators read can't drift from what `tools/list` returns. The catalog hash is a
SHA-256 over the canonical JSON of every definition (name, title, description,
schemas, annotations, required scope). Any change to what an agent sees changes the
hash. E3 will expose it in `_meta` and gate it with a version bump; agents can pin it.

    uv run python -m telco_mcp_lab.mcp_server.catalog        # regenerate docs/tool-catalog.md
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from telco_mcp_lab.mcp_server.security.scoped_server import ScopedMCPServer

CATALOG_DOC = Path(__file__).parents[3] / "docs" / "tool-catalog.md"


async def catalog_definitions(server: ScopedMCPServer) -> list[dict[str, Any]]:
    """Every registered tool, regardless of caller, in registration order."""
    tools = await MCPServer.list_tools(server)  # bypass per-caller filtering on purpose
    return [
        {
            "name": t.name,
            "title": t.title,
            "required_scope": server.tool_scopes.get(t.name),
            "annotations": t.annotations.model_dump(by_alias=True, exclude_none=True)
            if t.annotations
            else {},
            "description": t.description,
            "inputSchema": t.input_schema,
            "outputSchema": t.output_schema,
        }
        for t in tools
    ]


def catalog_hash(definitions: list[dict[str, Any]]) -> str:
    canonical = json.dumps(definitions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _params(schema: dict[str, Any]) -> list[str]:
    required = set(schema.get("required", []))
    rows = []
    for name, prop in schema.get("properties", {}).items():
        variants = prop.get("anyOf", [prop])
        types = [v.get("type", "?") for v in variants if v.get("type") != "null"]
        extras = [f"pattern `{v['pattern']}`" for v in variants if "pattern" in v] + [
            f"one of {', '.join(v['enum'])}" for v in variants if "enum" in v
        ]
        if "minimum" in prop or "maximum" in prop:
            extras.append(f"{prop.get('minimum', '…')}–{prop.get('maximum', '…')}")
        flag = "**required**" if name in required else "optional"
        detail = "; ".join(extras)
        rows.append(
            f"| `{name}` | {'/'.join(types)} | {flag} | {detail} | "
            f"{prop.get('description', '').replace('|', '/')} |"
        )
    return rows


def _output_fields(schema: dict[str, Any] | None) -> list[str]:
    return list((schema or {}).get("properties", {}))


def _summary(description: str | None) -> str:
    """First sentence of the first paragraph (descriptions wrap across lines)."""
    para = " ".join((description or "").strip().split("\n\n")[0].split())
    return para.split(". ")[0].rstrip(".") + "."


def render_markdown(definitions: list[dict[str, Any]], server_version: str) -> str:
    out = [
        "# Tool catalog (generated, do not edit)",
        "",
        "> Generated from the live tool definitions by `make catalog`. A test fails if",
        "> this file is out of date. This is the contract external agents integrate against.",
        "",
        f"* Server version: `{server_version}`",
        f"* Catalog hash: `{catalog_hash(definitions)}`",
        f"* Tools: {len(definitions)}",
        "",
        "| Tool | Scope | Read-only | Summary |",
        "|---|---|---|---|",
    ]
    for d in definitions:
        summary = _summary(d["description"])
        ro = "yes" if d["annotations"].get("readOnlyHint") else "**no**"
        out.append(
            f"| [`{d['name']}`](#{d['name']}) | `{d['required_scope']}` | {ro} | {summary} |"
        )
    for d in definitions:
        out += [
            "",
            f"## {d['name']}",
            "",
            f"**{d['title']}** · scope `{d['required_scope']}` · annotations "
            f"`{json.dumps(d['annotations'], sort_keys=True)}`",
            "",
            "```text",
            (d["description"] or "").strip(),
            "```",
            "",
            "| Parameter | Type | | Constraints | Description |",
            "|---|---|---|---|---|",
            *(_params(d["inputSchema"]) or ["| *(none)* | | | | |"]),
            "",
            "Output fields: " + ", ".join(f"`{k}`" for k in _output_fields(d["outputSchema"])),
        ]
    return "\n".join(out) + "\n"


async def build_catalog_markdown() -> str:
    from telco_mcp_lab import __version__
    from telco_mcp_lab.mcp_server.security.caller import AccessModel
    from telco_mcp_lab.mcp_server.server import build_server
    from telco_mcp_lab.mcp_server.settings import McpServerSettings

    access = AccessModel.load(McpServerSettings(_env_file=None).access_config)
    server = build_server(access_model=access)  # no lifespan runs: no backend needed
    return render_markdown(await catalog_definitions(server), __version__)


if __name__ == "__main__":
    import asyncio

    CATALOG_DOC.write_text(asyncio.run(build_catalog_markdown()), encoding="utf-8")
    print(f"wrote {CATALOG_DOC}")
