"""JWT mode (MCP_AUTH_MODE=jwt): token validation, client registry, customer context.

Three layers:
* JwtTokenVerifier unit tests: every way a token must be refused, incl. the
  classic JWT attacks (alg=none, HS256 key confusion, foreign key with a known kid).
* JWKS cache: caching, rotation, rate-limited refresh, fetch failure, settings interlocks.
* ClientRegistry + over-the-wire tests: unregistered clients, scope intersection,
  bound vs customer_context, the customer header, audit, health, both protocol eras.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path

import httpx
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from telco_mcp_lab.devtools.token_issuer import generate_key, jwks_for, mint
from telco_mcp_lab.mcp_server.http_app import build_http_app
from telco_mcp_lab.mcp_server.security.audit import AUDIT_LOGGER
from telco_mcp_lab.mcp_server.security.clients import ClientRegistry, parse_customer_header
from telco_mcp_lab.mcp_server.security.guard import NO_CUSTOMER
from telco_mcp_lab.mcp_server.security.jwt_verifier import JwksCache, JwtTokenVerifier
from telco_mcp_lab.mcp_server.settings import JwtSettings, McpServerSettings
from tests.conftest import ACCESS_MODEL, LiveServer, make_telco

pytestmark = pytest.mark.security

ISS = "https://token-service.test.invalid"
AUD = "https://mcp.test.invalid/mcp"
KEY = generate_key()
OTHER_KEY = generate_key()

CLIENTS = {
    "scope_map": {"read": "read", "pii:read": "pii:read", "mcp.read": "read"},
    "clients": {
        "care-agent": {"mode": "customer_context", "allowed_scopes": ["read"]},
        "partner-b": {"mode": "bound", "tenant": "tenant-b", "allowed_scopes": ["read"]},
        "ops-a": {"mode": "bound", "tenant": "tenant-a", "allowed_scopes": ["read", "pii:read"]},
    },
}


def token(client: str = "partner-b", scopes: str = "read", **kw) -> str:
    kw.setdefault("issuer", ISS)
    kw.setdefault("audience", AUD)
    return mint(kw.pop("key", KEY), client_id=client, scopes=scopes, **kw)


@pytest.fixture
def jwks_file(tmp_path):
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(jwks_for(KEY)), encoding="utf-8")
    return path


@pytest.fixture
def jwt_settings(jwks_file) -> JwtSettings:
    return JwtSettings(_env_file=None, issuer=ISS, audience=AUD, jwks_file=jwks_file)


@pytest.fixture
def verifier(jwt_settings) -> JwtTokenVerifier:
    return JwtTokenVerifier(jwt_settings)


def b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


# ------------------------------------------------------------------ verifier: accept
class TestVerifierAccepts:
    async def test_valid_token(self, verifier):
        at = await verifier.verify_token(token("care-agent", "read pii:read"))
        assert at is not None
        assert at.client_id == "care-agent" and at.scopes == ["pii:read", "read"]
        assert at.token == "[redacted]"  # the raw token isn't kept
        assert set(at.claims or {}) <= {"jti", "iss"}

    async def test_scope_as_json_list(self, verifier):
        at = await verifier.verify_token(token(extra={"scope": ["read", "pii:read"]}))
        assert at is not None and at.scopes == ["pii:read", "read"]

    async def test_client_id_from_azp_when_client_id_absent(self, verifier):
        t = mint(KEY, client_id="x", scopes="read", issuer=ISS, audience=AUD,
                 extra={"client_id": None, "azp": "partner-b"})  # fmt: skip
        at = await verifier.verify_token(t)
        assert at is not None and at.client_id == "partner-b"

    async def test_audience_list_containing_ours(self, verifier):
        at = await verifier.verify_token(token(audience=[AUD, "https://other.invalid"]))
        assert at is not None

    async def test_small_clock_skew_tolerated(self, verifier):
        at = await verifier.verify_token(token(now=time.time() - 7200 - 10))  # expired 10 s ago
        assert at is not None


# ------------------------------------------------------------------ verifier: refuse
class TestVerifierRefuses:
    @pytest.mark.parametrize(
        "kw",
        [
            pytest.param({"now": time.time() - 7200 - 120}, id="expired"),
            pytest.param({"audience": "https://domain-api.invalid"}, id="wrong-audience"),
            pytest.param({"issuer": "https://evil.invalid"}, id="wrong-issuer"),
            pytest.param({"key": OTHER_KEY}, id="signed-by-another-key-same-kid"),
            pytest.param({"kid": "no-such-kid"}, id="unknown-kid"),
            pytest.param({"ttl_s": 7200 + 3600}, id="lifetime-longer-than-issued"),
            pytest.param({"now": time.time() + 3600}, id="issued-in-the-future"),
            pytest.param({"extra": {"scope": 42}}, id="unreadable-scope"),
            pytest.param({"extra": {"client_id": None, "sub": None}}, id="no-client-id"),
        ],
    )
    async def test_bad_token(self, verifier, kw):
        assert await verifier.verify_token(token(**kw)) is None

    @pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud"])
    async def test_required_claim_missing(self, verifier, claim):
        claims = jwt.decode(token(), options={"verify_signature": False})
        del claims[claim]
        t = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "dev-key-1"})
        assert await verifier.verify_token(t) is None

    async def test_alg_none(self, verifier, caplog):
        caplog.set_level(logging.WARNING)
        claims = jwt.decode(token(), options={"verify_signature": False})
        t = f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': 'dev-key-1'})}.{b64(claims)}."
        assert await verifier.verify_token(t) is None
        # Refused by OUR allow-list before any key lookup (PyJWT would refuse too:
        # two layers, and this pins the first one).
        assert "algorithm not allowed" in caplog.text

    async def test_hs256_key_confusion(self, verifier, caplog):
        """Classic attack: sign with HMAC using the PUBLIC key as the secret."""
        public_pem = KEY.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        claims = jwt.decode(token(), options={"verify_signature": False})
        header = b64({"alg": "HS256", "typ": "JWT", "kid": "dev-key-1"})
        signing_input = f"{header}.{b64(claims)}".encode()
        sig = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
        t = f"{header}.{b64(claims)}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"
        caplog.set_level(logging.WARNING)
        assert await verifier.verify_token(t) is None
        assert "algorithm not allowed" in caplog.text

    async def test_alg_differs_from_the_keys_declared_alg(self, jwks_file):
        s = JwtSettings(_env_file=None, issuer=ISS, audience=AUD, jwks_file=jwks_file,
                        algorithms=["RS256", "PS256"])  # fmt: skip
        t = token(algorithm="PS256")  # the JWK says alg RS256
        assert await JwtTokenVerifier(s).verify_token(t) is None

    async def test_garbage_and_oversized(self, verifier):
        assert await verifier.verify_token("not.a.jwt") is None
        assert await verifier.verify_token("x" * 20000) is None

    async def test_missing_kid(self, verifier):
        claims = jwt.decode(token(), options={"verify_signature": False})
        assert await verifier.verify_token(jwt.encode(claims, KEY, algorithm="RS256")) is None

    async def test_rejection_logs_reason_never_the_token(self, verifier, caplog):
        caplog.set_level(logging.WARNING)
        t = token(audience="https://domain-api.invalid")
        await verifier.verify_token(t)
        assert "InvalidAudienceError" in caplog.text
        assert t not in caplog.text and t.split(".")[2] not in caplog.text


# ------------------------------------------------------------------------ settings
class TestJwtSettings:
    @pytest.mark.parametrize("algs", [["HS256"], ["none"], ["RS256", "HS512"], []])
    def test_symmetric_or_none_algorithms_refused(self, jwks_file, algs):
        with pytest.raises(ValidationError):
            JwtSettings(_env_file=None, issuer=ISS, audience=AUD, jwks_file=jwks_file,
                        algorithms=algs)  # fmt: skip

    def test_blank_issuer_or_audience_refused(self, jwks_file):
        for kw in ({"issuer": "", "audience": AUD}, {"issuer": ISS, "audience": ""}):
            with pytest.raises(ValidationError):
                JwtSettings(_env_file=None, jwks_file=jwks_file, **kw)

    def test_blank_key_source_counts_as_unset(self, jwks_file):
        s = JwtSettings(_env_file=None, issuer=ISS, audience=AUD, jwks_file=jwks_file, jwks_url="")
        assert s.jwks_url is None

    def test_jwks_url_must_be_https(self):
        with pytest.raises(ValidationError, match="https"):
            JwtSettings(_env_file=None, issuer=ISS, audience=AUD, jwks_url="http://ts/jwks")

    @pytest.mark.parametrize("both", [True, False])
    def test_exactly_one_key_source(self, jwks_file, both):
        kw = {"jwks_url": "https://ts.invalid/jwks", "jwks_file": jwks_file} if both else {}
        with pytest.raises(ValidationError, match="exactly one"):
            JwtSettings(_env_file=None, issuer=ISS, audience=AUD, **kw)


# ---------------------------------------------------------------------- JWKS cache
class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class JwksServer:
    def __init__(self, *keys) -> None:
        self.keys = list(keys)
        self.hits = 0
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hits += 1
        if self.fail:
            return httpx.Response(503)
        docs = [jwks_for(k, kid=f"k{i}")["keys"][0] for i, k in enumerate(self.keys)]
        return httpx.Response(200, json={"keys": docs})


def url_verifier(server: JwksServer, clock: FakeClock) -> JwtTokenVerifier:
    s = JwtSettings(_env_file=None, issuer=ISS, audience=AUD, jwks_url="https://ts.invalid/jwks")
    cache = JwksCache(s, transport=httpx.MockTransport(server.handler), clock=clock)
    return JwtTokenVerifier(s, cache)


class TestJwksCache:
    async def test_cold_start_concurrent_requests_share_one_fetch(self):
        """Review bug 2: requests arriving during the first JWKS fetch must wait, not 401."""
        import asyncio

        srv, clock = JwksServer(KEY), FakeClock()

        async def slow(request):
            await asyncio.sleep(0.1)
            return srv.handler(request)

        s = JwtSettings(
            _env_file=None, issuer=ISS, audience=AUD, jwks_url="https://ts.invalid/jwks"
        )
        v = JwtTokenVerifier(s, JwksCache(s, transport=httpx.MockTransport(slow), clock=clock))
        results = await asyncio.gather(*(v.verify_token(token(kid="k0")) for _ in range(10)))
        assert all(r is not None for r in results)
        assert srv.hits == 1

    async def test_failed_cold_start_retries_quickly(self):
        """No keys at all: retry after a few seconds, not the full rotation rate limit."""
        srv, clock = JwksServer(KEY), FakeClock()
        v = url_verifier(srv, clock)
        srv.fail = True
        assert await v.verify_token(token(kid="k0")) is None
        srv.fail = False
        clock.t += 6
        assert await v.verify_token(token(kid="k0")) is not None

    async def test_fetched_once_and_cached(self):
        srv, clock = JwksServer(KEY), FakeClock()
        v = url_verifier(srv, clock)
        for _ in range(5):
            assert await v.verify_token(token(kid="k0")) is not None
        assert srv.hits == 1

    async def test_unknown_kid_refresh_is_rate_limited(self):
        srv, clock = JwksServer(KEY), FakeClock()
        v = url_verifier(srv, clock)
        await v.verify_token(token(kid="k0"))
        for _ in range(20):  # a flood of tokens with made-up kids
            assert await v.verify_token(token(kid="bogus")) is None
        assert srv.hits == 1

    async def test_key_rotation_picked_up(self):
        srv, clock = JwksServer(KEY), FakeClock()
        v = url_verifier(srv, clock)
        await v.verify_token(token(kid="k0"))
        srv.keys.append(OTHER_KEY)  # the token service publishes a new key as k1
        clock.t += 61
        assert await v.verify_token(token(kid="k1", key=OTHER_KEY)) is not None
        assert srv.hits == 2

    async def test_fetch_failure_without_cached_keys_refuses(self):
        srv, clock = JwksServer(KEY), FakeClock()
        srv.fail = True
        assert await url_verifier(srv, clock).verify_token(token(kid="k0")) is None

    async def test_fetch_failure_keeps_cached_keys(self):
        srv, clock = JwksServer(KEY), FakeClock()
        v = url_verifier(srv, clock)
        await v.verify_token(token(kid="k0"))
        srv.fail = True
        clock.t += 3601  # TTL passed; refresh fails
        assert await v.verify_token(token(kid="k0")) is not None


# ---------------------------------------------------------------------- registry
@pytest.fixture
def clients_file(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps(CLIENTS), encoding="utf-8")
    return path


@pytest.fixture
def registry(clients_file) -> ClientRegistry:
    return ClientRegistry.load(clients_file, ACCESS_MODEL)


class TestRegistry:
    def test_unregistered_client_gets_no_caller(self, registry):
        assert registry.context_for("stranger", ["read"], None) is None

    def test_scopes_are_token_intersect_allowed(self, registry):
        c = registry.context_for("partner-b", ["read", "pii:read", "order:submit"], None)
        assert c is not None and c.scopes == {"read"}

    def test_unknown_token_scopes_ignored_and_mapped_names_work(self, registry):
        c = registry.context_for("partner-b", ["mcp.read", "admin"], None)
        assert c is not None and c.scopes == {"read"}

    def test_bound_client_ignores_customer_header(self, registry):
        c = registry.context_for("partner-b", ["read"], "ACC-1001")
        assert c is not None and c.account_ids == {"ACC-2001"} and c.customer is None

    def test_customer_context_from_header(self, registry):
        c = registry.context_for("care-agent", ["read"], "ACC-1001, ACC-1002")
        assert c is not None and c.account_ids == {"ACC-1001", "ACC-1002"}
        assert c.customer == "ACC-1001,ACC-1002"

    def test_customer_context_without_header_has_no_accounts(self, registry):
        c = registry.context_for("care-agent", ["read"], None)
        assert c is not None and c.account_ids == frozenset()

    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, frozenset()),
            ("", frozenset()),
            ("ACC-1001", {"ACC-1001"}),
            ("ACC-1001,acc-1002", None),
            ("ACC-1001\nX-Injected: 1", None),
            ("ACC-1001;DROP", None),
            (",".join(f"ACC-{1000 + i}" for i in range(11)), None),
            ("!", None),  # what the middleware passes for a repeated header
        ],
    )
    def test_customer_header_parsing(self, value, expected):
        assert parse_customer_header(value) == expected

    def test_bound_client_with_unknown_tenant_refused_at_load(self, tmp_path):
        bad = {"clients": {"x": {"mode": "bound", "tenant": "nope", "allowed_scopes": []}}}
        path = tmp_path / "c.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match="unknown tenant"):
            ClientRegistry.load(path, ACCESS_MODEL)

    def test_repo_clients_config_loads(self):
        ClientRegistry.load(Path(__file__).parents[2] / "config" / "clients.json", ACCESS_MODEL)


# --------------------------------------------------------------- over the wire (HTTP)
@pytest.fixture
def jwt_mcp_url(live_gateway, jwt_settings, clients_file):
    settings = McpServerSettings(
        _env_file=None, auth_mode="jwt", clients_config=clients_file, public_url=AUD
    )
    app = build_http_app(
        settings,
        ACCESS_MODEL,
        {},
        jwt_settings=jwt_settings,
        telco_factory=lambda: make_telco(base_url=live_gateway),
    )
    with LiveServer(app) as srv:
        yield srv.url


def rpc(url: str, method: str, params: dict, headers: dict, name: str | None = None):
    h = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
        **({"Mcp-Name": name} if name else {}),
    }
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta}}
    return httpx.post(url + "/mcp", json=body, headers=[*h.items(), *headers], timeout=10)


def bearer(t: str) -> list[tuple[str, str]]:
    return [("Authorization", f"Bearer {t}")]


def call(url, tool, args, headers):
    r = rpc(url, "tools/call", {"name": tool, "arguments": args}, headers, name=tool)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def text(result) -> str:
    return result["content"][0]["text"]


@pytest.mark.protocol
class TestJwtOverHttp:
    def test_healthz_is_open_and_says_nothing(self, jwt_mcp_url):
        r = httpx.get(jwt_mcp_url + "/healthz")
        assert r.status_code == 200 and r.json() == {"status": "ok"}

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param({"now": time.time() - 9000}, id="expired"),
            pytest.param({"audience": "https://domain-api.invalid"}, id="other-audience"),
            pytest.param({"key": OTHER_KEY}, id="forged"),
        ],
    )
    def test_invalid_tokens_get_401(self, jwt_mcp_url, bad):
        r = rpc(jwt_mcp_url, "tools/list", {}, bearer(token(**bad)))
        assert r.status_code == 401
        assert 'error="invalid_token"' in r.headers["www-authenticate"]

    def test_protected_resource_metadata_names_the_issuer(self, jwt_mcp_url):
        r = httpx.get(jwt_mcp_url + "/.well-known/oauth-protected-resource/mcp")
        assert r.status_code == 200
        body = r.json()
        assert body["resource"] == AUD
        assert ISS in [s.rstrip("/") for s in body["authorization_servers"]]

    def test_bound_client_sees_only_its_tenant(self, jwt_mcp_url):
        h = bearer(token("partner-b"))
        ok = call(jwt_mcp_url, "get_account_summary", {}, h)
        assert not ok["isError"] and ok["structuredContent"]["account_id"] == "ACC-2001"
        denied = call(jwt_mcp_url, "get_account_summary", {"account_id": "ACC-1001"}, h)
        assert denied["isError"] and "No account with that ID" in text(denied)

    def test_bound_client_cannot_widen_itself_with_the_header(self, jwt_mcp_url):
        h = bearer(token("partner-b")) + [("X-Customer-Account-Id", "ACC-1001")]
        r = call(jwt_mcp_url, "get_account_summary", {"account_id": "ACC-1001"}, h)
        assert r["isError"]

    def test_customer_context_client_acts_for_the_header_account_only(self, jwt_mcp_url):
        h = bearer(token("care-agent")) + [("X-Customer-Account-Id", "ACC-1002")]
        ok = call(jwt_mcp_url, "get_account_summary", {}, h)
        assert not ok["isError"] and ok["structuredContent"]["account_id"] == "ACC-1002"
        # The model asking for another customer's account gets "not found".
        other = call(jwt_mcp_url, "get_account_summary", {"account_id": "ACC-1001"}, h)
        assert other["isError"] and "No account with that ID" in text(other)
        order = call(jwt_mcp_url, "get_order_status", {"order_id": "ORD-000456"}, h)
        assert order["isError"]

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param([], id="no-header"),
            pytest.param([("X-Customer-Account-Id", "ACC-1001 OR 1=1")], id="malformed"),
            pytest.param(
                [("X-Customer-Account-Id", "ACC-1001"), ("X-Customer-Account-Id", "ACC-2001")],
                id="repeated",
            ),
        ],
    )
    def test_customer_context_client_without_valid_header_sees_nothing(self, jwt_mcp_url, headers):
        h = bearer(token("care-agent")) + headers
        for tool, args in [
            ("get_account_summary", {"account_id": "ACC-1001"}),
            ("list_orders", {}),
            ("get_order_status", {"order_id": "ORD-000123"}),
        ]:
            r = call(jwt_mcp_url, tool, args, h)
            assert r["isError"] and NO_CUSTOMER in text(r), tool

    def test_unregistered_client_sees_no_tools(self, jwt_mcp_url, caplog):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
        h = bearer(token("stranger"))
        r = rpc(jwt_mcp_url, "tools/list", {}, h)
        assert r.status_code == 200 and r.json()["result"]["tools"] == []
        denied = call(jwt_mcp_url, "list_orders", {}, h)
        assert denied["isError"] and "Unknown tool: list_orders" in text(denied)
        (line,) = [json.loads(r.message) for r in caplog.records if r.name == AUDIT_LOGGER]
        assert line["caller"] == "stranger" and line["tenant"] is None
        assert line["outcome"] == "denied"

    def test_token_without_read_scope_sees_no_tools(self, jwt_mcp_url):
        r = rpc(jwt_mcp_url, "tools/list", {}, bearer(token("partner-b", scopes="profile")))
        assert r.json()["result"]["tools"] == []

    def test_pii_needs_scope_in_token_and_registry(self, jwt_mcp_url):
        masked = call(jwt_mcp_url, "list_subscriptions", {"account_id": "ACC-1001", "limit": 1},
                      bearer(token("ops-a", scopes="read")))  # fmt: skip
        full = call(jwt_mcp_url, "list_subscriptions", {"account_id": "ACC-1001", "limit": 1},
                    bearer(token("ops-a", scopes="read pii:read")))  # fmt: skip
        m = masked["structuredContent"]["items"][0]["msisdn"]
        f = full["structuredContent"]["items"][0]["msisdn"]
        assert "*" in m and "*" not in f

    def test_audit_records_client_and_customer(self, jwt_mcp_url, caplog):
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
        h = bearer(token("care-agent")) + [("X-Customer-Account-Id", "ACC-1002")]
        call(jwt_mcp_url, "get_account_summary", {}, h)
        (line,) = [json.loads(r.message) for r in caplog.records if r.name == AUDIT_LOGGER]
        assert line["caller"] == "care-agent" and line["customer"] == "ACC-1002"
        assert line["outcome"] == "ok"

    @pytest.mark.parametrize("mode", ["auto", "legacy"])
    async def test_sdk_client_both_eras(self, jwt_mcp_url, mode):
        headers = {"Authorization": f"Bearer {token('care-agent')}",
                   "X-Customer-Account-Id": "ACC-1001"}  # fmt: skip
        async with (
            httpx2.AsyncClient(headers=headers) as h,
            Client(streamable_http_client(jwt_mcp_url + "/mcp", http_client=h), mode=mode) as c,
        ):
            tools = {t.name for t in (await c.list_tools()).tools}
            assert "get_account_summary" in tools
            r = await c.call_tool("get_account_summary", {})
            assert not r.is_error and r.structured_content["account_id"] == "ACC-1001"
