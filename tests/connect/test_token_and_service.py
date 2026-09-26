"""The developer connector's token source, its settings, and the dev token service."""

import asyncio
import json
import sys

import httpx
import pytest
from pydantic import ValidationError

from telco_mcp_lab.connect.__main__ import load_settings
from telco_mcp_lab.connect.settings import ConnectSettings
from telco_mcp_lab.connect.token import ClientCredentials, TokenError
from telco_mcp_lab.devtools.token_issuer import generate_key, jwks_for, mint
from telco_mcp_lab.devtools.token_service import create_app
from telco_mcp_lab.mcp_server.security.jwt_verifier import JwtTokenVerifier
from telco_mcp_lab.mcp_server.settings import JwtSettings

pytestmark = pytest.mark.security

KEY = generate_key()
SECRET = "dev-shared-secret-0123456789abcdef"
AUD = "http://127.0.0.1:8090/mcp"
ISS = "https://token-service.dev.invalid"


def service(**kw) -> httpx.ASGITransport:
    app = create_app(KEY, client_id="lowerenv-shared", client_secret=SECRET, audience=AUD, **kw)
    return httpx.ASGITransport(app=app)


def settings(**kw) -> ConnectSettings:
    base = {
        "url": "http://127.0.0.1:8090/mcp",
        "token_url": "http://127.0.0.1:8095/oauth/token",
        "client_id": "lowerenv-shared",
        "client_secret": SECRET,
    }
    return ConnectSettings(_env_file=None, **{**base, **kw})


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------------ dev token service
class TestDevTokenService:
    @pytest.mark.parametrize("client_auth", ["basic", "post"])
    async def test_issues_a_token_the_mcp_server_accepts(self, tmp_path, client_auth):
        cc = ClientCredentials(settings(client_auth=client_auth), transport=service())
        token = await cc.token()
        (tmp_path / "jwks.json").write_text(json.dumps(jwks_for(KEY)))
        js = JwtSettings(_env_file=None, issuer=ISS, audience=AUD, jwks_file=tmp_path / "jwks.json")
        at = await JwtTokenVerifier(js).verify_token(token)
        assert at is not None and at.client_id == "lowerenv-shared" and at.scopes == ["read"]

    async def test_wrong_secret_is_invalid_client_and_secret_never_echoed(self):
        wrong = "not-the-secret-0123456789abcdef"
        cc = ClientCredentials(settings(client_secret=wrong), transport=service())
        with pytest.raises(TokenError, match="HTTP 401 invalid_client") as exc:
            await cc.token()
        assert wrong not in str(exc.value) and SECRET not in str(exc.value)

    async def test_scope_beyond_the_client_is_refused(self):
        cc = ClientCredentials(settings(scope="read admin"), transport=service())
        with pytest.raises(TokenError, match="invalid_scope"):
            await cc.token()

    async def test_unsupported_grant_and_no_store(self):
        async with httpx.AsyncClient(transport=service(), base_url="http://ts") as c:
            bad = await c.post("/oauth/token", data={"grant_type": "password"},
                               auth=("lowerenv-shared", SECRET))  # fmt: skip
            good = await c.post("/oauth/token", data={"grant_type": "client_credentials"},
                                auth=("lowerenv-shared", SECRET))  # fmt: skip
        assert bad.status_code == 400 and bad.json()["error"] == "unsupported_grant_type"
        assert good.headers["cache-control"] == "no-store"

    def test_short_secret_refused(self):
        with pytest.raises(ValueError, match="24"):
            create_app(KEY, client_id="x", client_secret="short")


# ---------------------------------------------------------------------- token source
class CountingTS:
    """A token endpoint that counts calls, optionally slow, with a chosen lifetime."""

    def __init__(self, expires_in: int | None = 7200, delay: float = 0.0) -> None:
        self.calls = 0
        self.expires_in = expires_in
        self.delay = delay

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await asyncio.sleep(self.delay)
        token = mint(KEY, client_id="lowerenv-shared", scopes="read", audience=AUD, ttl_s=7200)
        body = {"access_token": token, "token_type": "Bearer"}
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return httpx.Response(200, json=body)


class TestClientCredentials:
    async def test_cached_until_refresh_margin(self):
        ts, clock = CountingTS(expires_in=7200), FakeClock()
        cc = ClientCredentials(settings(), transport=httpx.MockTransport(ts.handler), clock=clock)
        for _ in range(5):
            await cc.token()
        assert ts.calls == 1
        clock.t += 7200 - 300 - 1  # just inside the 5-minute margin: still cached
        await cc.token()
        assert ts.calls == 1
        clock.t += 2  # now within the margin: refresh early
        await cc.token()
        assert ts.calls == 2

    async def test_concurrent_callers_share_one_fetch(self):
        ts = CountingTS(delay=0.1)
        cc = ClientCredentials(settings(), transport=httpx.MockTransport(ts.handler))
        tokens = await asyncio.gather(*(cc.token() for _ in range(10)))
        assert ts.calls == 1 and len(set(tokens)) == 1

    async def test_force_after_401_fetches_once_for_concurrent_callers(self):
        ts = CountingTS(delay=0.05)
        cc = ClientCredentials(settings(), transport=httpx.MockTransport(ts.handler))
        await cc.token()
        await asyncio.gather(*(cc.token(force=True) for _ in range(5)))
        assert ts.calls == 2  # one initial, ONE forced refresh (not five)

    async def test_lifetime_from_jwt_exp_when_expires_in_missing(self):
        ts, clock = CountingTS(expires_in=None), FakeClock()
        cc = ClientCredentials(settings(), transport=httpx.MockTransport(ts.handler), clock=clock)
        await cc.token()
        await cc.token()
        assert ts.calls == 1  # exp (2 h) used, not the 5-minute conservative fallback

    async def test_secret_from_command(self):
        cmd = f"{sys.executable} -c \"print('{SECRET}')\""
        cc = ClientCredentials(
            settings(client_secret=None, secret_command=cmd), transport=service()
        )
        assert await cc.token()

    @pytest.mark.parametrize(
        "cmd",
        [
            f"{sys.executable} -c 'import sys; sys.exit(3)'",
            f"{sys.executable} -c 'print()'",
            "no-such-command-xyz",
        ],
    )
    async def test_failing_secret_command_is_a_clean_error(self, cmd):
        cc = ClientCredentials(
            settings(client_secret=None, secret_command=cmd), transport=service()
        )
        with pytest.raises(TokenError, match="SECRET_COMMAND"):
            await cc.token()

    async def test_unreachable_token_service(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        cc = ClientCredentials(settings(), transport=httpx.MockTransport(boom))
        with pytest.raises(TokenError, match="unreachable"):
            await cc.token()


# -------------------------------------------------------------------------- settings
class TestSettings:
    @pytest.mark.parametrize("field", ["url", "token_url"])
    def test_plain_http_only_for_localhost(self, field):
        with pytest.raises(ValidationError, match="https"):
            settings(**{field: "http://mcp.lowerenv.corp/mcp"})
        settings(**{field: "https://mcp.lowerenv.corp/mcp"})

    def test_exactly_one_secret_source(self):
        with pytest.raises(ValidationError, match="exactly one"):
            settings(secret_command="security find-generic-password -w")
        with pytest.raises(ValidationError, match="exactly one"):
            settings(client_secret=None)

    def test_blank_values_mean_unset(self):
        s = settings(secret_command=" ", customer="", scope="")
        assert s.secret_command is None and s.customer is None and s.scope is None

    def test_config_errors_never_echo_values(self, tmp_path, monkeypatch):
        cfg = tmp_path / "lower.env"
        cfg.write_text("TELCO_MCP_URL=http://mcp.lowerenv.corp/mcp\n"
                       "TELCO_MCP_TOKEN_URL=https://ts.lowerenv.corp/token\n"
                       "TELCO_MCP_CLIENT_ID=lowerenv-shared\n")  # fmt: skip
        monkeypatch.setenv("TELCO_MCP_CLIENT_SECRET", SECRET)
        with pytest.raises(SystemExit) as exc:
            load_settings(cfg, None)
        assert "url" in str(exc.value) and SECRET not in str(exc.value)
        assert "mcp.lowerenv.corp" not in str(exc.value)
