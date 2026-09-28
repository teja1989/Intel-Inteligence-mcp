"""The Streamable HTTP ASGI app, built one way for `__main__` and for tests.

HTTP always needs a JWT access token from the token service, validated here
(signature via JWKS, iss, aud, exp, lifetime), then mapped through
config/clients.json to a client and its scopes (docs/architecture-security.md §2).
Locally, `make dev-keys` stands in for the token service's keys.
"""

import logging

import httpx
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from telco_mcp.endpoints import GatewayEndpoints
from telco_mcp.observability.access import AccessLogMiddleware
from telco_mcp.observability.telemetry import instrument_asgi
from telco_mcp.security.clients import ClientRegistry
from telco_mcp.security.environment import ProductionFacts, enforce
from telco_mcp.security.jwt_verifier import JwksCache, JwtTokenVerifier
from telco_mcp.server import TelcoFactory, build_server, default_telco_factory
from telco_mcp.settings import JwtSettings, McpServerSettings

log = logging.getLogger("telco_mcp")


def build_http_app(
    settings: McpServerSettings,
    *,
    telco_factory: TelcoFactory = default_telco_factory,
    jwt_settings: JwtSettings | None = None,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
    gateway_endpoints: GatewayEndpoints | None = None,
) -> ASGIApp:
    js = jwt_settings or JwtSettings()  # type: ignore[call-arg]  # from env / .env
    registry = ClientRegistry.load(settings.clients_config, settings.environment)
    defaulted: tuple[str, ...] = ()
    if settings.environment == "production":  # elsewhere the mock defaults are fine
        defaulted = tuple((gateway_endpoints or GatewayEndpoints()).defaulted())
    # (The server's lifespan builds its own GatewayEndpoints from the same environment.)
    # Guardrail G2: before any verifier or route exists (docs/architecture-security.md §6).
    enforce(
        settings.environment,
        ProductionFacts(
            transport="http",
            log_format=settings.log_format,
            public_url=settings.public_url,
            jwt_issuer=js.issuer,
            jwt_audience=js.audience,
            jwt_jwks_url=js.jwks_url,
            jwt_jwks_file=js.jwks_file,
            registry_problems=tuple(registry.problems),
            gateway_endpoints_defaulted=defaulted,
        ),
    )
    verifier = JwtTokenVerifier(js, JwksCache(js, transport=jwks_transport))
    log.info(
        "http: environment=%s issuer=%s audience=%s clients=%s",
        settings.environment,
        js.issuer,
        js.audience,
        sorted(registry.allowed),
    )
    server = build_server(
        telco_factory,
        token_verifier=verifier,
        client_registry=registry,
        issuer_url=js.issuer if js.issuer.startswith("https://") else "https://auth.invalid",
        public_url=settings.public_url,
    )
    app = server.streamable_http_app(
        # The key switch for horizontal scaling. The 2026-07-28 path is
        # stateless regardless; this makes the LEGACY (initialize) path build a
        # throwaway per-request session instead of an in-memory one that only
        # the replica which created it knows about (tests/…/test_http_protocol.py).
        stateless_http=True,
        json_response=True,  # plain JSON bodies instead of SSE for request/response
        host=settings.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )
    # Outermost: the OTel server span (parented on the inbound traceparent), so the
    # access log line and everything inside carry the same trace.id.
    return instrument_asgi(AccessLogMiddleware(app))
