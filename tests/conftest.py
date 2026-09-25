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

from telco_mcp_lab.gateway_routes import GatewayRoutes
from telco_mcp_lab.mcp_server.clients.gateway import GatewayClientSettings, StaticTokenProvider
from telco_mcp_lab.mcp_server.clients.telco import TelcoApiClient
from telco_mcp_lab.mcp_server.security.caller import AccessModel, CallerContext
from telco_mcp_lab.mcp_server.server import build_server
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


ACCESS_MODEL = AccessModel.load(Path(__file__).parents[1] / "config" / "access.json")
# Test-only bearer tokens for the HTTP tests (>= 24 chars, unique).
CALLER_TOKENS = {c: f"test-token-{c}-0123456789abcdef" for c in ACCESS_MODEL.callers}
CALLER_ENV = {f"MCP_TOKEN_{c.upper()}": t for c, t in CALLER_TOKENS.items()}


def caller(caller_id: str) -> CallerContext:
    return ACCESS_MODEL.context_for(caller_id, via="in-process")


def server_as(caller_id: str, telco_factory, **kw):
    """In-process server acting as one caller (the stdio-style fallback identity)."""
    return build_server(
        telco_factory, access_model=ACCESS_MODEL, fallback_caller=caller(caller_id), **kw
    )


@pytest.fixture
def mock_telco_factory(app):
    return lambda: make_telco(transport=httpx.ASGITransport(app=app))


@pytest.fixture
def mcp_server_on_mock(mock_telco_factory):
    """MCP server (as alice, tenant-a) whose gateway calls go into the in-process mock app."""
    return server_as("alice", mock_telco_factory)


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
