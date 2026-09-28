"""Entry point.

    uv run python -m telco_mcp                      # stdio (MCP_STDIO_SCOPES)
    uv run python -m telco_mcp --transport http     # stateless Streamable HTTP
    uv run python -m telco_mcp --transport http --port 8091 --legacy-sessions

stdio golden rule: stdout belongs to the protocol, so stdio logs go to stderr. Over
HTTP, logs go to stdout (ECS JSON in production, docs/10) for the platform to ship.

`--legacy-sessions` exists ONLY to demonstrate the sticky-session problem:
it turns off `stateless_http`, so pre-2026 clients get an in-memory
`Mcp-Session-Id` that other replicas don't know. See docs/04.
"""

import argparse
import logging
import sys

import uvicorn

from telco_mcp import __version__
from telco_mcp.endpoints import GatewayEndpoints
from telco_mcp.http_app import build_http_app
from telco_mcp.observability.logs import configure_logging
from telco_mcp.observability.telemetry import configure_telemetry
from telco_mcp.security.clients import ClientContext
from telco_mcp.security.environment import (
    ProductionFacts,
    UnsafeProductionConfig,
    enforce,
)
from telco_mcp.server import build_server
from telco_mcp.settings import McpServerSettings

log = logging.getLogger("telco_mcp")


def main() -> None:
    _main()


def _main() -> None:
    parser = argparse.ArgumentParser(prog="telco-mcp-server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, help="override MCP_PORT (e.g. for a 2nd replica)")
    parser.add_argument("--legacy-sessions", action="store_true", help="demo only, see docstring")
    parser.add_argument("--log-level", help="override MCP_LOG_LEVEL")
    args = parser.parse_args()

    settings = McpServerSettings()
    level = (args.log_level or settings.log_level).upper()
    service = {
        "name": "telco-mcp-server",
        "version": __version__,
        "environment": settings.environment,
    }
    configure_logging(
        level=level,
        fmt=settings.log_format,
        service=service,
        stream=sys.stderr if args.transport == "stdio" else sys.stdout,
    )
    shutdown_telemetry = configure_telemetry(service, log_level=level)
    exit_code = 0
    try:
        _run(args, settings, level)
        log.info("stopped")
    except UnsafeProductionConfig as exc:
        log.critical("%s", exc)
        exit_code = 2
    except (ValueError, OSError) as exc:  # bad configuration: say what, not a traceback
        log.critical("startup failed: %s: %s", type(exc).__name__, exc)
        exit_code = 2
    finally:
        shutdown_telemetry()  # flush batched spans / log records (incl. the lines above)
    if exit_code:
        sys.exit(exit_code)


def _run(args: argparse.Namespace, settings: McpServerSettings, level: str) -> None:
    log.info(
        "starting telco-mcp-server %s: transport=%s environment=%s log_format=%s log_level=%s",
        __version__, args.transport, settings.environment, settings.log_format, level,
    )  # fmt: skip
    # Validate the endpoint catalogue now: a bad GATEWAY_ENDPOINT_* stops startup with
    # one clear line, instead of a traceback from the server's lifespan later.
    endpoints = GatewayEndpoints()
    if settings.unsafe_raw_free_text:
        log.warning("MCP_UNSAFE_RAW_FREE_TEXT is ON: free text reaches the model verbatim (demo)")

    if args.transport == "stdio":
        # Guardrail G2: stdio has no authentication, so never in production.
        enforce(
            settings.environment,
            ProductionFacts(
                transport="stdio",
                log_format=settings.log_format,
                public_url=settings.public_url,
                unsafe_raw_free_text=settings.unsafe_raw_free_text,
            ),
        )
        client = ClientContext("stdio", frozenset(settings.stdio_scopes.split()), "stdio")
        log.info("stdio: local client, scopes %s", sorted(client.scopes))
        server = build_server(
            fallback_client=client,
            unsafe_raw_free_text=settings.unsafe_raw_free_text,
        )
        server.run(transport="stdio")
        return

    port = args.port or settings.port
    app = build_http_app(
        settings, legacy_sessions=args.legacy_sessions, gateway_endpoints=endpoints
    )
    if args.legacy_sessions:
        log.warning("--legacy-sessions: legacy clients get in-memory sessions (NOT scalable)")
    log.info("http: listening on http://%s:%s/mcp (stateless)", settings.host, port)
    uvicorn.run(
        app,
        host=settings.host,
        port=port,
        log_config=None,  # keep our handlers/format (observability/logs.py)
        access_log=False,  # our own access log (observability/access.py) replaces it
        server_header=False,  # don't advertise the server software
        timeout_graceful_shutdown=settings.shutdown_grace_s,
    )


if __name__ == "__main__":
    main()
