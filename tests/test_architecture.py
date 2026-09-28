"""Architecture boundary: the shipped server package must not depend on the lab.

Ships (Docker image):  server/  → package `telco_mcp` (distribution telco-mcp-server)
Never ships:           lab/     → package `telco_mcp_lab`: mock APIs, LLM harness,
                                  dev token service, developer connector, scripts

The server has NO LLM dependency: agents (hosts) bring their own model. A server
that imported an LLM SDK would blur who is responsible for what, and would ship a
dependency (and credentials) it must never need. These tests enforce the boundary
three ways: source imports, declared dependencies, and what actually loads.
"""

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SRC = REPO / "server" / "src" / "telco_mcp"
SERVER_FILES = sorted(SRC.rglob("*.py"))

# Third-party top-level modules the server may import: each must be a declared
# dependency in server/pyproject.toml (import name → distribution name).
ALLOWED_THIRD_PARTY = {
    "httpx": "httpx",
    "jwt": "pyjwt",
    "mcp": "mcp",
    "opentelemetry": "opentelemetry-sdk",  # + api, exporter, asgi/httpx instrumentation
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
}
# Never in the server, whatever the reason.
FORBIDDEN = ("telco_mcp_lab", "openai", "anthropic", "google", "fastapi", "dotenv", "httpx2")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def server_dependencies() -> set[str]:
    project = tomllib.loads((REPO / "server" / "pyproject.toml").read_text())["project"]
    return {d.split("==")[0].split("[")[0].strip().lower() for d in project["dependencies"]}


def test_there_are_server_files_to_check():
    assert len(SERVER_FILES) > 20  # guards against the glob silently matching nothing


@pytest.mark.parametrize("path", SERVER_FILES, ids=lambda p: str(p.relative_to(SRC)))
def test_server_imports_only_stdlib_its_own_package_and_declared_deps(path):
    tops = {m.split(".")[0] for m in imported_modules(path)}
    forbidden = {m for m in tops if m in FORBIDDEN}
    assert not forbidden, f"{path.name} imports {sorted(forbidden)}"
    unknown = tops - set(sys.stdlib_module_names) - {"telco_mcp"} - set(ALLOWED_THIRD_PARTY)
    assert not unknown, f"{path.name} imports undeclared {sorted(unknown)}"


def test_every_allowed_import_is_a_declared_server_dependency():
    missing = set(ALLOWED_THIRD_PARTY.values()) - server_dependencies()
    assert not missing, f"imported but not declared in server/pyproject.toml: {missing}"


def test_server_dependencies_contain_no_lab_packages():
    lab_only = {"openai", "anthropic", "google-genai", "fastapi", "python-dotenv", "telco-mcp-lab"}
    assert not server_dependencies() & lab_only


def test_starting_the_server_loads_no_lab_or_llm_module():
    """Import-time check too (catches indirect imports the AST scan can't see)."""
    code = (
        "import sys, telco_mcp.__main__, telco_mcp.http_app;"
        "bad=[m for m in sys.modules if m.split('.')[0] in "
        "('telco_mcp_lab','openai','anthropic','fastapi') or m.startswith('google.genai')];"
        "print(','.join(sorted(bad)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)  # noqa: S603
    assert out.stdout.strip() == ""
