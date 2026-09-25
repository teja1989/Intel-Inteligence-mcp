"""Artificial latency and failures, so we can watch timeouts and retries.

Phase 3 uses this to show that a slow backend turns into a clean, actionable
MCP tool error instead of a hung model turn.
"""

import asyncio
import random
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from telco_mcp_lab.mock_apis.problems import problem_response

_EXEMPT_PREFIXES = ("/health", "/_admin")


@dataclass
class ChaosConfig:
    delay_ms: int = 0
    fail_rate: float = 0.0
    fail_status: int = 503


class ChaosMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)
        chaos: ChaosConfig = request.app.state.chaos
        if chaos.delay_ms:
            await asyncio.sleep(chaos.delay_ms / 1000)
        if chaos.fail_rate and random.random() < chaos.fail_rate:  # noqa: S311 - not crypto
            return problem_response(
                chaos.fail_status,
                "INJECTED_FAILURE",
                "Chaos switch injected this failure. The backend is temporarily unavailable.",
            )
        return await call_next(request)


class TrailingSlashMiddleware:
    """Treat `/x/` and `/x` as the same route, as many API gateways do.

    Without this, Starlette answers `/bosubscription/API/subscription/` with a
    307 redirect, and httpx does not follow redirects by default.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and len(scope["path"]) > 1 and scope["path"].endswith("/"):
            scope = dict(scope, path=scope["path"].rstrip("/") or "/")
        await self.app(scope, receive, send)
