"""The Streamable HTTP ASGI app, built one way for `__main__` and for tests.

Two auth modes (MCP_AUTH_MODE):

* `static`: lab tokens MCP_TOKEN_<CALLER> mapped to callers in config/access.json.
  For demos and tests only.
* `jwt`: access tokens from the internal token service, validated here
  (signature via JWKS, iss, aud, exp, lifetime), then mapped through
  config/clients.json. This is the mode for running internally.
"""

import logging
from collections.abc import Mapping

import httpx
from mcp.server.auth.provider import TokenVerifier
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from telco_mcp_lab.mcp_server.security.caller import AccessModel
from telco_mcp_lab.mcp_server.security.clients import ClientRegistry, CustomerHeaderMiddleware
from telco_mcp_lab.mcp_server.security.environment import ProductionFacts, enforce
from telco_mcp_lab.mcp_server.security.jwt_verifier import JwksCache, JwtTokenVerifier
from telco_mcp_lab.mcp_server.security.verifier import StaticTokenVerifier
from telco_mcp_lab.mcp_server.server import TelcoFactory, build_server, default_telco_factory
from telco_mcp_lab.mcp_server.settings import JwtSettings, McpServerSettings

log = logging.getLogger("telco_mcp")


def build_http_app(
    settings: McpServerSettings,
    model: AccessModel,
    env: Mapping[str, str],
    *,
    legacy_sessions: bool = False,
    telco_factory: TelcoFactory = default_telco_factory,
    jwt_settings: JwtSettings | None = None,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
) -> ASGIApp:
    verifier: TokenVerifier
    registry: ClientRegistry | None = None
    js: JwtSettings | None = None
    issuer_url = "https://auth.telco-mcp-lab.invalid"
    if settings.auth_mode == "jwt":
        js = jwt_settings or JwtSettings()  # type: ignore[call-arg]  # from env / .env
        registry = ClientRegistry.load(settings.clients_config, model, settings.environment)
    # Guardrail G2: before any verifier or route exists (docs/08).
    enforce(
        settings.environment,
        ProductionFacts(
            transport="http",
            auth_mode=settings.auth_mode,
            public_url=settings.public_url,
            unsafe_raw_free_text=settings.unsafe_raw_free_text,
            legacy_sessions=legacy_sessions,
            jwt_issuer=js.issuer if js else None,
            jwt_audience=js.audience if js else None,
            jwt_jwks_url=js.jwks_url if js else None,
            jwt_jwks_file=js.jwks_file if js else None,
            registry_problems=tuple(registry.problems) if registry else (),
        ),
    )
    if js is not None and registry is not None:
        verifier = JwtTokenVerifier(js, JwksCache(js, transport=jwks_transport))
        if js.issuer.startswith("https://"):
            issuer_url = js.issuer
        log.info(
            "http: environment=%s auth=jwt issuer=%s audience=%s clients=%s",
            settings.environment,
            js.issuer,
            js.audience,
            sorted(registry.clients),
        )
    else:
        static = StaticTokenVerifier(model, env)
        verifier = static
        log.info(
            "http: environment=%s auth=static (lab tokens) callers=%s",
            settings.environment,
            static.enabled_callers,
        )

    server = build_server(
        telco_factory,
        access_model=model,
        token_verifier=verifier,
        client_registry=registry,
        issuer_url=issuer_url,
        public_url=settings.public_url,
        unsafe_raw_free_text=settings.unsafe_raw_free_text,
    )
    app = server.streamable_http_app(
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
    if registry is None:
        return app
    return CustomerHeaderMiddleware(app, settings.customer_header)
