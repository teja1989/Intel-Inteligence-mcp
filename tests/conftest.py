import json
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr

from telco_mcp.clients.gateway import GatewayClientSettings, StaticTokenProvider
from telco_mcp.clients.telco import TelcoApiClient
from telco_mcp.gateway_routes import GatewayRoutes
from telco_mcp.http_app import build_http_app
from telco_mcp.security.clients import ClientContext, Scope
from telco_mcp.server import build_server
from telco_mcp.settings import JwtSettings, McpServerSettings
from telco_mcp_lab.devtools.token_issuer import generate_key, jwks_for, mint
from telco_mcp_lab.mock_apis.app import create_app
from telco_mcp_lab.mock_apis.settings import MockApiSettings

TEST_TOKEN = "test-gateway-token-0123456789"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


class FakeClock:
    """Controllable time, so draft expiry can be tested without sleeping."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path) -> MockApiSettings:
    # Constructed explicitly with _env_file=None, so a developer's local .env
    # can never leak into (or break) the test run.
    return MockApiSettings(
        _env_file=None,
        gateway_token=SecretStr(TEST_TOKEN),
        db_path=tmp_path / "test.sqlite3",
        draft_ttl_seconds=900,
    )


@pytest.fixture
def routes() -> GatewayRoutes:
    return GatewayRoutes(_env_file=None)  # placeholder defaults, independent of .env


@pytest.fixture
def app(settings, routes, clock):
    return create_app(settings, routes, clock=clock)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, headers=AUTH) as c:
        yield c


@pytest.fixture
def anon_client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- MCP server fixtures
GATEWAY_URL = "http://127.0.0.1:8081"


def gateway_settings(base_url: str = GATEWAY_URL, **kw) -> GatewayClientSettings:
    return GatewayClientSettings(
        _env_file=None, base_url=base_url, token=SecretStr(TEST_TOKEN), **kw
    )


def make_telco(transport=None, **kw) -> TelcoApiClient:
    s = gateway_settings(**kw)
    return TelcoApiClient.build(
        s, GatewayRoutes(_env_file=None), StaticTokenProvider(s.token), transport=transport
    )


# JWT fixtures: HTTP always needs a JWT (from the dev token issuer's code, test keys).
JWT_ISS = "https://token-service.test.invalid"
JWT_AUD = "https://mcp.test.invalid/mcp"
JWT_KEY = generate_key()
TEST_CLIENTS = {
    "scope_map": {"read": "read", "pii:read": "pii:read", "mcp.read": "read"},
    "clients": {
        "agent-a": {"allowed_scopes": ["read"]},
        "care-agent": {"allowed_scopes": ["read", "pii:read"]},
    },
}


def jwt_token(client: str = "agent-a", scopes: str = "read", **kw) -> str:
    kw.setdefault("issuer", JWT_ISS)
    kw.setdefault("audience", JWT_AUD)
    return mint(kw.pop("key", JWT_KEY), client_id=client, scopes=scopes, **kw)


def jwt_settings_in(tmp_path: Path) -> JwtSettings:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(jwks_for(JWT_KEY)), encoding="utf-8")
    return JwtSettings(_env_file=None, issuer=JWT_ISS, audience=JWT_AUD, jwks_file=path)


def http_app_in(tmp_path: Path, telco_factory, *, clients: dict | None = None, **kw):
    """The real HTTP app (JWT auth, test keys, TEST_CLIENTS registry)."""
    path = tmp_path / "clients.json"
    path.write_text(json.dumps(clients or TEST_CLIENTS), encoding="utf-8")
    settings = McpServerSettings(_env_file=None, clients_config=path, public_url=JWT_AUD)
    return build_http_app(
        settings, jwt_settings=jwt_settings_in(tmp_path), telco_factory=telco_factory, **kw
    )


def client_ctx(*scopes: str, client_id: str = "test") -> ClientContext:
    return ClientContext(client_id, frozenset(scopes or (Scope.READ,)), "in-process")


def server_with(telco_factory, *scopes: str, **kw):
    """In-process server as ONE local client (the stdio-style fallback), default scope read."""
    return build_server(telco_factory, fallback_client=client_ctx(*scopes), **kw)


@pytest.fixture
def mock_telco_factory(app):
    return lambda: make_telco(transport=httpx.ASGITransport(app=app))


@pytest.fixture
def mcp_server_on_mock(mock_telco_factory):
    """MCP server (scope read) whose gateway calls go into the in-process mock app."""
    return server_with(mock_telco_factory)


class LiveServer:
    """Serve an ASGI app on a real 127.0.0.1 port in a background thread."""

    def __init__(self, app) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "LiveServer":
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def live_gateway(tmp_path) -> Iterator[str]:
    """The mock gateway on a real TCP port, for subprocess (stdio) and HTTP tests."""
    mock_settings = MockApiSettings(
        _env_file=None, gateway_token=SecretStr(TEST_TOKEN), db_path=tmp_path / "live.sqlite3"
    )
    with LiveServer(create_app(mock_settings, GatewayRoutes(_env_file=None))) as srv:
        yield srv.url
