"""The Streamable HTTP ASGI app, built one way for `__main__` and for tests.

HTTP always needs a JWT access token from the token service, validated here
(signature via JWKS, iss, aud, exp, lifetime), then mapped through
config/clients.json to a client and its scopes. Locally the token service is
`make dev-token-service` and the keys are `make dev-keys` (docs/07, docs/09).
"""

import logging

import httpx
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from telco_mcp_lab.mcp_server.security.clients import ClientRegistry
from telco_mcp_lab.mcp_server.security.environment import ProductionFacts, enforce
from telco_mcp_lab.mcp_server.security.jwt_verifier import JwksCache, JwtTokenVerifier
from telco_mcp_lab.mcp_server.server import TelcoFactory, build_server, default_telco_factory
from telco_mcp_lab.mcp_server.settings import JwtSettings, McpServerSettings

log = logging.getLogger("telco_mcp")


def build_http_app(
    settings: McpServerSettings,
    *,
    legacy_sessions: bool = False,
    telco_factory: TelcoFactory = default_telco_factory,
    jwt_settings: JwtSettings | None = None,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
) -> ASGIApp:
    js = jwt_settings or JwtSettings()  # type: ignore[call-arg]  # from env / .env
    registry = ClientRegistry.load(settings.clients_config, settings.environment)
    # Guardrail G2: before any verifier or route exists (docs/08).
    enforce(
        settings.environment,
        ProductionFacts(
            transport="http",
            public_url=settings.public_url,
            unsafe_raw_free_text=settings.unsafe_raw_free_text,
            legacy_sessions=legacy_sessions,
            jwt_issuer=js.issuer,
            jwt_audience=js.audience,
            jwt_jwks_url=js.jwks_url,
            jwt_jwks_file=js.jwks_file,
            registry_problems=tuple(registry.problems),
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
        unsafe_raw_free_text=settings.unsafe_raw_free_text,
    )
    return server.streamable_http_app(
        # The key switch for horizontal scaling. The 2026-07-28 path is
        # stateless regardless; this makes the LEGACY (initialize) path build a
        # throwaway per-request session instead of an in-memory one that only
        # the replica which created it knows about.
        stateless_http=not legacy_sessions,
        json_response=True,  # plain JSON bodies instead of SSE for request/response
        host=settings.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )
