"""The Streamable HTTP ASGI app, built one way for `__main__` and for tests."""

import logging
from collections.abc import Mapping

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from telco_mcp_lab.mcp_server.security.caller import AccessModel
from telco_mcp_lab.mcp_server.security.verifier import StaticTokenVerifier
from telco_mcp_lab.mcp_server.server import TelcoFactory, build_server, default_telco_factory
from telco_mcp_lab.mcp_server.settings import McpServerSettings

log = logging.getLogger("telco_mcp")


def build_http_app(
    settings: McpServerSettings,
    model: AccessModel,
    env: Mapping[str, str],
    *,
    legacy_sessions: bool = False,
    telco_factory: TelcoFactory = default_telco_factory,
) -> Starlette:
    verifier = StaticTokenVerifier(model, env)
    log.info("http: callers with tokens configured: %s", verifier.enabled_callers)
    server = build_server(
        telco_factory,
        access_model=model,
        token_verifier=verifier,
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
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
            allowed_origins=settings.allowed_origins,
        ),
    )
