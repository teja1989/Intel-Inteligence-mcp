"""MCP-side gateway client: URL building, token injection, safety interlock."""

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from telco_mcp_lab.gateway_routes import DomainApi, GatewayRoutes
from telco_mcp_lab.mcp_server.clients.gateway import (
    GatewayBearerAuth,
    GatewayClientSettings,
    GatewayUrls,
    StaticTokenProvider,
)
from tests.conftest import TEST_TOKEN

TOKEN = SecretStr("client-side-token-0123456789")


def settings(**kw) -> GatewayClientSettings:
    return GatewayClientSettings(_env_file=None, token=TOKEN, **kw)


@pytest.fixture
def urls() -> GatewayUrls:
    return GatewayUrls("http://127.0.0.1:8081/", GatewayRoutes(_env_file=None))


class TestUrls:
    def test_matches_gateway_convention(self, urls):
        assert (
            urls.url(DomainApi.SUBSCRIPTION, "subscription", "SUB-1001-01")
            == "http://127.0.0.1:8081/bosubscription/API/subscription/SUB-1001-01"
        )
        assert (
            urls.url(DomainApi.ORDER_SUBMISSION, "submission")
            == "http://127.0.0.1:8081/boordersubmission/API/submission"
        )

    @pytest.mark.security
    def test_ids_cannot_escape_their_segment(self, urls):
        url = urls.url(DomainApi.ACCOUNT, "account", "../../boorder/API/order")
        assert url.endswith("/boaccount/API/account/..%2F..%2Fboorder%2FAPI%2Forder")
        assert httpx.URL(url).path.startswith("/boaccount/API/account/")

    @pytest.mark.security
    @pytest.mark.parametrize("bad", ["", ".", ".."])
    def test_dot_and_empty_segments_rejected(self, urls, bad):
        with pytest.raises(ValueError):
            urls.url(DomainApi.ACCOUNT, "account", bad)


@pytest.mark.security
class TestSafetyInterlock:
    def test_localhost_allowed(self):
        assert settings(base_url="http://localhost:9000").base_url == "http://localhost:9000"

    def test_non_local_refused_by_default(self):
        with pytest.raises(ValidationError, match="synthetic data"):
            settings(base_url="https://gateway.example.com")

    def test_non_local_requires_https_even_when_allowed(self):
        with pytest.raises(ValidationError, match="https"):
            settings(base_url="http://gateway.example.com", allow_non_local=True)

    def test_non_local_https_with_explicit_opt_in(self):
        s = settings(base_url="https://gateway.example.com", allow_non_local=True)
        assert s.allow_non_local

    @pytest.mark.parametrize("bad", ["ftp://127.0.0.1", "127.0.0.1:8081", "http://"])
    def test_non_http_urls_refused(self, bad):
        with pytest.raises(ValidationError):
            settings(base_url=bad)

    def test_config_errors_never_contain_the_token(self):
        with pytest.raises(ValidationError) as exc:
            settings(base_url="https://gateway.example.com")
        assert TOKEN.get_secret_value() not in str(exc.value)
        assert TOKEN.get_secret_value() not in repr(exc.value.errors())

    def test_token_is_required_and_hidden(self):
        with pytest.raises(ValidationError):
            GatewayClientSettings(_env_file=None)
        s = settings()
        assert "client-side-token" not in repr(s)
        assert "client-side-token" not in repr(StaticTokenProvider(s.token))


class TestBearerAuth:
    @respx.mock
    def test_token_added_to_every_request(self, urls):
        route = respx.get(urls.url(DomainApi.ACCOUNT, "account", "ACC-1001")).mock(
            return_value=httpx.Response(200, json={})
        )
        with httpx.Client(auth=GatewayBearerAuth(StaticTokenProvider(TOKEN))) as c:
            c.get(urls.url(DomainApi.ACCOUNT, "account", "ACC-1001"))
        assert route.calls.last.request.headers["Authorization"] == (
            f"Bearer {TOKEN.get_secret_value()}"
        )

    async def test_client_and_mock_gateway_agree_end_to_end(self, app):
        """Both sides read gateway_routes.py, so paths can't drift. Prove it."""
        urls = GatewayUrls("http://127.0.0.1:8081", GatewayRoutes(_env_file=None))
        auth = GatewayBearerAuth(StaticTokenProvider(SecretStr(TEST_TOKEN)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), auth=auth) as c:
            r = await c.get(urls.url(DomainApi.SERVICE, "service", "SVC-1001-01"))
        assert r.status_code == 200
        assert r.json()["service_id"] == "SVC-1001-01"
