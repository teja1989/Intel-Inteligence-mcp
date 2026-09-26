"""Entry point.

    uv run python -m telco_mcp_lab.mcp_server                      # stdio (as MCP_STDIO_CALLER)
    uv run python -m telco_mcp_lab.mcp_server --transport http     # stateless Streamable HTTP
    uv run python -m telco_mcp_lab.mcp_server --transport http --port 8091 --legacy-sessions

stdio golden rule: stdout belongs to the protocol. All logging goes to stderr.

`--legacy-sessions` exists ONLY to demonstrate the sticky-session problem:
it turns off `stateless_http`, so pre-2026 clients get an in-memory
`Mcp-Session-Id` that other replicas don't know. See docs/04.
"""

import argparse
import logging
import os
import sys

import uvicorn
from dotenv import dotenv_values

from telco_mcp_lab.mcp_server.http_app import build_http_app
from telco_mcp_lab.mcp_server.security.caller import AccessModel
from telco_mcp_lab.mcp_server.security.environment import (
    ProductionFacts,
    UnsafeProductionConfig,
    enforce,
)
from telco_mcp_lab.mcp_server.server import build_server
from telco_mcp_lab.mcp_server.settings import McpServerSettings

log = logging.getLogger("telco_mcp")


def main() -> None:
    try:
        _main()
    except UnsafeProductionConfig as exc:
        log.critical("%s", exc)
        sys.exit(2)


def _main() -> None:
    parser = argparse.ArgumentParser(prog="telco-mcp-server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, help="override MCP_PORT (e.g. for a 2nd replica)")
    parser.add_argument("--legacy-sessions", action="store_true", help="demo only, see docstring")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every downstream URL at INFO, and URLs carry identifiers
    # (and in real systems often MSISDNs). Keep them out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = McpServerSettings()
    model = AccessModel.load(settings.access_config)
    if settings.unsafe_raw_free_text:
        log.warning("MCP_UNSAFE_RAW_FREE_TEXT is ON: free text reaches the model verbatim (demo)")

    if args.transport == "stdio":
        # Guardrail G2: stdio has no authentication, so never in production.
        enforce(
            settings.environment,
            ProductionFacts(
                transport="stdio",
                auth_mode=settings.auth_mode,
                public_url=settings.public_url,
                unsafe_raw_free_text=settings.unsafe_raw_free_text,
            ),
        )
        caller = model.context_for(settings.stdio_caller, via="stdio")
        log.info("stdio: acting as caller %r (tenant %s)", caller.caller_id, caller.tenant)
        server = build_server(
            access_model=model,
            fallback_caller=caller,
            unsafe_raw_free_text=settings.unsafe_raw_free_text,
        )
        server.run(transport="stdio")
        return

    port = args.port or settings.port
    # Real environment variables win over .env, as with pydantic-settings.
    env = {**{k: v for k, v in dotenv_values(".env").items() if v is not None}, **os.environ}
    app = build_http_app(settings, model, env, legacy_sessions=args.legacy_sessions)
    if args.legacy_sessions:
        log.warning("--legacy-sessions: legacy clients get in-memory sessions (NOT scalable)")
    log.info("http: listening on http://%s:%s/mcp (stateless)", settings.host, port)
    uvicorn.run(app, host=settings.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
