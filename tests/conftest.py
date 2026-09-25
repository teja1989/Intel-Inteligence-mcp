import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr

from telco_mcp_lab.gateway_routes import GatewayRoutes
from telco_mcp_lab.mcp_server.clients.gateway import GatewayClientSettings, StaticTokenProvider
from telco_mcp_lab.mcp_server.clients.telco import TelcoApiClient
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


@pytest.fixture
def mcp_server_on_mock(app):
    """MCP server whose gateway calls go straight into the in-process mock app."""
    return build_server(lambda: make_telco(transport=httpx.ASGITransport(app=app)))


@pytest.fixture
def live_gateway(tmp_path) -> Iterator[str]:
    """The mock gateway on a real TCP port, for subprocess (stdio) tests."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    mock_settings = MockApiSettings(
        _env_file=None, gateway_token=SecretStr(TEST_TOKEN), db_path=tmp_path / "live.sqlite3"
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(mock_settings, GatewayRoutes(_env_file=None)),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock gateway did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
