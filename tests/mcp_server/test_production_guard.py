"""Guardrail G2 (docs/08): production refuses lab and lower-env configuration at startup."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from telco_mcp_lab.mcp_server.http_app import build_http_app
from telco_mcp_lab.mcp_server.security.clients import ClientRegistry
from telco_mcp_lab.mcp_server.security.environment import (
    ProductionFacts,
    UnsafeProductionConfig,
    enforce,
    production_problems,
)
from telco_mcp_lab.mcp_server.settings import JwtSettings, McpServerSettings
from tests.conftest import ACCESS_MODEL, make_telco

pytestmark = pytest.mark.security

REPO = Path(__file__).parents[2]
PROD = ProductionFacts(
    transport="http",
    auth_mode="jwt",
    public_url="https://mcp.corp-telco.com/mcp",
    unsafe_raw_free_text=False,
    jwt_issuer="https://token.corp-telco.com",
    jwt_audience="https://mcp.corp-telco.com/mcp",
    jwt_jwks_url="https://token.corp-telco.com/jwks",
)


def with_(**changes) -> ProductionFacts:
    return ProductionFacts(**{**PROD.__dict__, **changes})


class TestRules:
    def test_a_production_shaped_config_passes(self):
        assert production_problems(PROD) == []

    @pytest.mark.parametrize(
        "changes, expected",
        [
            ({"transport": "stdio"}, "stdio"),
            ({"auth_mode": "static"}, "MCP_AUTH_MODE"),
            ({"legacy_sessions": True}, "legacy-sessions"),
            ({"unsafe_raw_free_text": True}, "UNSAFE_RAW"),
            ({"public_url": "http://mcp.corp-telco.com/mcp"}, "MCP_PUBLIC_URL must be https"),
            ({"public_url": "https://127.0.0.1:8090/mcp"}, "MCP_PUBLIC_URL uses a local"),
            ({"jwt_issuer": "https://token-service.dev.invalid"}, "MCP_JWT_ISSUER"),
            ({"jwt_audience": "https://mcp.test/mcp"}, "MCP_JWT_AUDIENCE"),
            ({"jwt_jwks_url": "https://localhost/jwks"}, "MCP_JWT_JWKS_URL"),
            ({"jwt_jwks_url": None}, "MCP_JWT_JWKS_URL must be set"),
            ({"jwt_jwks_file": Path("x.json")}, "MCP_JWT_JWKS_FILE"),
            ({"registry_problems": ("client 'x' is not tagged",)}, "not tagged"),
        ],
    )
    def test_each_violation_is_reported(self, changes, expected):
        problems = production_problems(with_(**changes))
        assert any(expected in p for p in problems), problems

    def test_all_violations_reported_at_once(self):
        bad = with_(auth_mode="static", transport="stdio", unsafe_raw_free_text=True)
        with pytest.raises(UnsafeProductionConfig) as exc:
            enforce("production", bad)
        assert len(exc.value.problems) >= 3

    @pytest.mark.parametrize("environment", ["local", "dev", "test"])
    def test_lower_environments_are_not_blocked(self, environment):
        enforce(environment, with_(auth_mode="static", transport="stdio"))

    def test_non_url_issuer_and_audience_are_allowed(self):
        """e.g. Azure AD audiences like api://… or plain issuer names."""
        assert production_problems(with_(jwt_issuer="corp-ts", jwt_audience="api://mcp")) == []


# ------------------------------------------------------------ registry environment tags
def registry_file(tmp_path, clients: dict) -> Path:
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"scope_map": {"read": "read"}, "clients": clients}))
    return path


SHARED = {"mode": "customer_context", "allowed_scopes": ["read"], "environments": ["dev", "test"]}
PROD_APP = {"mode": "customer_context", "allowed_scopes": ["read"], "environments": ["production"]}
UNTAGGED = {"mode": "bound", "tenant": "tenant-a", "allowed_scopes": ["read"]}


class TestRegistryTags:
    def test_lower_env_client_loaded_where_tagged(self, tmp_path):
        r = ClientRegistry.load(registry_file(tmp_path, {"shared": SHARED}), ACCESS_MODEL, "dev")
        assert "shared" in r.clients and r.problems == []

    def test_lower_env_client_skipped_elsewhere(self, tmp_path):
        r = ClientRegistry.load(registry_file(tmp_path, {"shared": SHARED}), ACCESS_MODEL, "local")
        assert "shared" not in r.clients

    def test_production_reports_lower_env_and_untagged_entries(self, tmp_path):
        path = registry_file(tmp_path, {"shared": SHARED, "old": UNTAGGED, "app": PROD_APP})
        r = ClientRegistry.load(path, ACCESS_MODEL, "production")
        assert set(r.clients) == {"app"}
        assert any("'shared'" in p for p in r.problems)
        assert any("'old'" in p for p in r.problems)

    def test_unknown_environment_tag_rejected(self, tmp_path):
        bad = {**SHARED, "environments": ["staging"]}
        with pytest.raises(ValueError, match="bad environments"):
            ClientRegistry.load(registry_file(tmp_path, {"x": bad}), ACCESS_MODEL, "dev")

    def test_repo_sample_registry_would_not_pass_production(self):
        r = ClientRegistry.load(REPO / "config" / "clients.json", ACCESS_MODEL, "production")
        assert r.problems  # the lab sample is not a production registry


# --------------------------------------------------------------- wired into the entry points
class TestWiring:
    def _app(self, tmp_path, clients: dict, **settings):
        jwks = tmp_path / "jwks.json"
        jwks.write_text('{"keys": []}')
        s = McpServerSettings(
            _env_file=None,
            auth_mode="jwt",
            clients_config=registry_file(tmp_path, clients),
            **{"environment": "production", "public_url": PROD.public_url, **settings},
        )
        js_kw = {"jwks_url": PROD.jwt_jwks_url} if "jwks_file" not in settings else {}
        js = JwtSettings(_env_file=None, issuer=PROD.jwt_issuer, audience=PROD.jwt_audience,
                         **js_kw)  # fmt: skip
        return build_http_app(s, ACCESS_MODEL, {}, jwt_settings=js, telco_factory=make_telco)

    def test_http_app_builds_with_production_config(self, tmp_path):
        self._app(tmp_path, {"app": PROD_APP})

    def test_http_app_refuses_lower_env_client_in_production(self, tmp_path):
        with pytest.raises(UnsafeProductionConfig, match="'shared'"):
            self._app(tmp_path, {"app": PROD_APP, "shared": SHARED})

    def test_http_app_refuses_lab_public_url_in_production(self, tmp_path):
        with pytest.raises(UnsafeProductionConfig, match="MCP_PUBLIC_URL"):
            self._app(tmp_path, {"app": PROD_APP}, public_url="http://127.0.0.1:8090/mcp")

    def test_static_lab_tokens_refused_in_production(self):
        s = McpServerSettings(_env_file=None, environment="production", public_url=PROD.public_url)
        with pytest.raises(UnsafeProductionConfig, match="MCP_AUTH_MODE"):
            build_http_app(s, ACCESS_MODEL, {}, telco_factory=make_telco)

    def test_stdio_process_exits_in_production(self):
        env = {**os.environ, "MCP_ENVIRONMENT": "production"}
        r = subprocess.run(  # noqa: S603 - fixed argv, our own module
            [sys.executable, "-m", "telco_mcp_lab.mcp_server"],
            cwd=REPO, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
            text=True,
        )  # fmt: skip
        assert r.returncode == 2
        assert "Refusing to start with MCP_ENVIRONMENT=production" in r.stderr
        assert "stdio transport" in r.stderr
