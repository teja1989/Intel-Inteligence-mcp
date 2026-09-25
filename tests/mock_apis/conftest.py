from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from telco_mcp_lab.mock_apis.app import create_app
from telco_mcp_lab.mock_apis.settings import MockApiSettings

TEST_KEY = "test-service-key-0123456789"
AUTH = {"X-Api-Key": TEST_KEY}


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
        api_key=SecretStr(TEST_KEY),
        db_path=tmp_path / "test.sqlite3",
        draft_ttl_seconds=900,
    )


@pytest.fixture
def app(settings, clock):
    return create_app(settings, clock=clock)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, headers=AUTH) as c:
        yield c


@pytest.fixture
def anon_client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
