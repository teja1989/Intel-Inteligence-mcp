"""Architecture boundary: the shippable MCP server must not depend on the lab rig.

Shippable (what you'd deploy):  telco_mcp_lab.mcp_server + the shared contract
modules it uses (ids, gateway_routes, the package __init__).
Lab rig (never deployed):       harness (LLM test client), mock_apis, devtools, scripts,
                                connect (developer-side token connector).

The server has NO LLM dependency: agents (hosts) bring their own model. A
server that imported an LLM SDK would blur who is responsible for what, and
would ship a dependency (and credentials) it must never need.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src" / "telco_mcp_lab"
SERVER_FILES = sorted((SRC / "mcp_server").rglob("*.py"))

FORBIDDEN = (
    "openai",
    "httpx2",  # only as an MCP *client* transport; the server's downstream client is httpx
    "telco_mcp_lab.harness",
    "telco_mcp_lab.mock_apis",
    "telco_mcp_lab.devtools",
    "telco_mcp_lab.connect",  # developer-side token connector (lower envs only)
    "tests",
    "scripts",
)
# Modules outside mcp_server/ the server may use: the shared contract, and the version.
ALLOWED_SHARED = ("telco_mcp_lab.ids", "telco_mcp_lab.gateway_routes")
ALLOWED_EXACT = ("telco_mcp_lab", "telco_mcp_lab.__version__")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def test_there_are_server_files_to_check():
    assert len(SERVER_FILES) > 10  # guards against the glob silently matching nothing


@pytest.mark.parametrize("path", SERVER_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_server_never_imports_the_lab_rig_or_an_llm_sdk(path):
    bad = {
        m for m in imported_modules(path)
        if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)
    }  # fmt: skip
    assert not bad, f"{path.name} imports {sorted(bad)}"


@pytest.mark.parametrize("path", SERVER_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_server_only_uses_shared_contract_modules_outside_its_package(path):
    def allowed(m: str) -> bool:
        return (
            m.startswith("telco_mcp_lab.mcp_server")
            or m in ALLOWED_EXACT
            or any(m == a or m.startswith(a + ".") for a in ALLOWED_SHARED)
        )

    outside = {
        m for m in imported_modules(path) if m.startswith("telco_mcp_lab") and not allowed(m)
    }
    assert not outside, f"{path.name} reaches outside the server: {sorted(outside)}"


def test_starting_the_server_loads_no_llm_sdk():
    """Import-time check too (catches indirect imports the AST scan can't see)."""
    lab = "('telco_mcp_lab.harness', 'telco_mcp_lab.mock_apis', 'telco_mcp_lab.devtools')"
    code = (
        "import sys, telco_mcp_lab.mcp_server.__main__, telco_mcp_lab.mcp_server.http_app;"
        f"bad=[m for m in sys.modules if m.split('.')[0] == 'openai' or m.startswith({lab})];"
        "print(','.join(sorted(bad)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)  # noqa: S603
    assert out.stdout.strip() == ""
