"""MCP-side gateway client: token injection, safety interlock. URLs: test_endpoints.py."""

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from telco_mcp.clients.gateway import (
    GatewayBearerAuth,
    GatewayClientSettings,
    StaticTokenProvider,
)

TOKEN = SecretStr("client-side-token-0123456789")


def settings(**kw) -> GatewayClientSettings:
    return GatewayClientSettings(_env_file=None, token=TOKEN, **kw)


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
    def test_token_added_to_every_request(self):
        url = "http://127.0.0.1:8081/boaccount/API/account/ACC-1001"
        route = respx.get(url).mock(return_value=httpx.Response(200, json={}))
        with httpx.Client(auth=GatewayBearerAuth(StaticTokenProvider(TOKEN))) as c:
            c.get(url)
        assert route.calls.last.request.headers["Authorization"] == (
            f"Bearer {TOKEN.get_secret_value()}"
        )
