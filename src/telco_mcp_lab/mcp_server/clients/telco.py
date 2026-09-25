"""Typed async client for the telecom domain APIs, reached through the gateway.

Tools call these methods and never build URLs or handle HTTP themselves. The
client turns every failure into one of two exceptions:

* `GatewayError`: the gateway or backend answered with an error (Problem Details).
* `GatewayUnavailable`: no usable answer (timeout, connection refused, bad JSON).

`errors/` then turns those into actionable tool errors for the model.

Java/Spring equivalent: an `@HttpExchange` interface (or a `RestClient` wrapper)
with a `ResponseErrorHandler` that maps `ProblemDetail` to domain exceptions.
"""

from typing import Any

import httpx

from telco_mcp_lab.gateway_routes import DomainApi, GatewayRoutes
from telco_mcp_lab.mcp_server.clients.gateway import (
    GatewayBearerAuth,
    GatewayClientSettings,
    GatewayUrls,
    TokenProvider,
)


class GatewayError(Exception):
    """The gateway/backend returned an error response."""

    def __init__(self, status: int, code: str, detail: str, body: dict[str, Any]) -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail, self.body = status, code, detail, body


class GatewayUnavailable(Exception):  # noqa: N818 - reads naturally at call sites
    """No usable response: timeout, connection failure, or malformed body."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TelcoApiClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        routes: GatewayRoutes,
    ) -> None:
        self._http = http
        self._urls = GatewayUrls(base_url, routes)

    @classmethod
    def build(
        cls,
        settings: GatewayClientSettings,
        routes: GatewayRoutes,
        tokens: TokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "TelcoApiClient":
        """Create the client with its own pooled `httpx.AsyncClient`.

        `transport` lets tests route calls straight into the mock ASGI app.
        """
        http = httpx.AsyncClient(
            auth=GatewayBearerAuth(tokens),
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            follow_redirects=False,  # a redirect from the gateway is a misconfiguration
            transport=transport,
        )
        return cls(http, settings.base_url, routes)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ domain calls
    async def get_account(self, account_id: str) -> dict[str, Any]:
        return await self._get(self._urls.url(DomainApi.ACCOUNT, "account", account_id))

    async def list_subscriptions(
        self,
        account_id: str,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"account_id": account_id, "limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return await self._get(self._urls.url(DomainApi.SUBSCRIPTION, "subscription"), params)

    async def all_subscriptions(self, account_id: str, max_pages: int = 20) -> list[dict[str, Any]]:
        """Follow cursors to collect every subscription (bounded, never unbounded)."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            page = await self.list_subscriptions(account_id, cursor=cursor)
            items += page["items"]
            cursor = page.get("next_cursor")
            if not cursor:
                return items
        raise GatewayUnavailable("Too many subscription pages; refusing to read further.")

    # ---------------------------------------------------------------- plumbing
    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = await self._http.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise GatewayUnavailable("timeout") from exc
        except httpx.TransportError as exc:
            raise GatewayUnavailable("connection failed") from exc
        return self._decode(resp)

    @staticmethod
    def _decode(resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            raise GatewayUnavailable(f"non-JSON response (HTTP {resp.status_code})") from exc
        if resp.is_success and isinstance(body, dict):
            return body
        if not resp.is_success:
            body = body if isinstance(body, dict) else {}
            raise GatewayError(
                resp.status_code,
                str(body.get("code", "HTTP_ERROR")),
                str(body.get("detail", "")),
                body,
            )
        raise GatewayUnavailable("unexpected response shape")
