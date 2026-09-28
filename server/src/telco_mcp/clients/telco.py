"""Typed async client for the telecom domain APIs, reached through the gateway.

Tools call these methods and never build URLs or handle HTTP themselves. The
client turns every failure into one of two exceptions:

* `GatewayError`: the gateway or backend answered with an error (Problem Details).
* `GatewayUnavailable`: no usable answer (timeout, connection refused, bad JSON).

`errors/` then turns those into actionable tool errors for the model.

Java/Spring equivalent: an `@HttpExchange` interface (or a `RestClient` wrapper)
with a `ResponseErrorHandler` that maps `ProblemDetail` to domain exceptions.
"""

import logging
import time
from typing import Any

import httpx

from telco_mcp.clients.gateway import (
    GatewayBearerAuth,
    GatewayClientSettings,
    TokenProvider,
)
from telco_mcp.clients.resilience import (
    RETRYABLE_STATUS,
    CircuitBreaker,
    CircuitOpen,
    RetryPolicy,
    with_retry,
)
from telco_mcp.endpoints import DomainApi, GatewayEndpoints
from telco_mcp.observability.telemetry import instrument_http_client

log = logging.getLogger("telco_mcp.gateway")


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
        endpoints: GatewayEndpoints,
        retry: RetryPolicy | None = None,
        breakers: dict[DomainApi, CircuitBreaker] | None = None,
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._endpoints = endpoints
        self._retry = retry or RetryPolicy()
        self.breakers = breakers or {api: CircuitBreaker(api.value) for api in DomainApi}

    @classmethod
    def build(
        cls,
        settings: GatewayClientSettings,
        endpoints: GatewayEndpoints,
        tokens: TokenProvider,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
    ) -> "TelcoApiClient":
        """Create the client with its own pooled `httpx.AsyncClient`.

        `transport` lets tests route calls straight into the mock ASGI app.
        """
        http = httpx.AsyncClient(
            auth=GatewayBearerAuth(tokens),
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            follow_redirects=False,  # a redirect from the gateway is a misconfiguration
            transport=transport,
            # Corporate laptops often set HTTP(S)_PROXY. The lab gateway is on
            # localhost and must never be sent through a corporate proxy.
            trust_env=settings.allow_non_local,
        )
        instrument_http_client(http)  # client spans + traceparent towards the gateway
        return cls(http, settings.base_url, endpoints, retry=retry)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ domain calls
    async def get_account(self, account_id: str) -> dict[str, Any]:
        return await self._get("get_account", account_id=account_id)

    async def list_subscriptions(
        self,
        account_id: str,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await self._get(
            "list_subscriptions",
            account_id=account_id, status=status or None, limit=limit, cursor=cursor or None,
        )  # fmt: skip

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

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self._get("get_subscription", subscription_id=subscription_id)

    async def get_service(self, service_id: str) -> dict[str, Any]:
        return await self._get("get_service", service_id=service_id)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._get("get_order", order_id=order_id)

    async def list_orders(
        self, account_id: str, limit: int = 10, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._get(
            "list_orders", account_id=account_id, limit=limit, cursor=cursor or None
        )

    # ---------------------------------------------------------------- plumbing
    async def _get(self, operation: str, **values: Any) -> dict[str, Any]:
        """One GET via the endpoint catalogue (endpoints.py): path, query, encoding."""
        call = self._endpoints.resolve(operation, **values)
        url = self._base + call.path
        breaker = self.breakers[call.api]
        # Log fields name the API and operation, never the URL or params (they carry IDs).
        fields: dict[str, Any] = {"gateway.api": call.api.value, "gateway.operation": operation}
        try:
            breaker.before_call()
        except CircuitOpen as exc:
            # DEBUG: the breaker's own WARNING when it opened is the signal; one line
            # per rejected call during an outage would flood the logs.
            log.debug("gateway call rejected: circuit open", extra={"fields": fields})
            raise GatewayUnavailable("circuit open") from exc

        async def attempt() -> httpx.Response:
            # The route template (no IDs) names the client span (observability/telemetry.py).
            return await self._http.get(
                url, params=call.params, extensions={"telco.route": call.route}
            )

        started = time.perf_counter_ns()
        # Every exit path must report to the breaker, or a half-open trial wedges it.
        try:
            resp = await with_retry(attempt, _retryable_read, self._retry, name=call.api.value)
        except httpx.TimeoutException as exc:
            breaker.on_failure()
            self._log_failure(fields, started, "timeout", exc)
            raise GatewayUnavailable("timeout") from exc
        except httpx.HTTPError as exc:  # transport, protocol, decoding, …
            breaker.on_failure()
            self._log_failure(fields, started, "connection failed", exc)
            raise GatewayUnavailable("connection failed") from exc
        except BaseException:  # cancelled (client went away) or a bug: no health verdict
            breaker.abandon()
            raise
        fields |= {"http.response.status_code": resp.status_code,
                   "event.duration": time.perf_counter_ns() - started}  # fmt: skip
        # 4xx means the backend is healthy and answered; only 5xx counts against it.
        if resp.status_code >= 500:
            breaker.on_failure()
            log.warning("gateway call failed: HTTP %d", resp.status_code, extra={"fields": fields})
        else:
            breaker.on_success()
            # Success at DEBUG: at 5k calls/min the traces (client spans) carry per-call
            # latency; logs are for what needs a human.
            log.debug("gateway call", extra={"fields": fields})
        return self._decode(resp)

    @staticmethod
    def _log_failure(fields: dict[str, Any], started: int, what: str, exc: Exception) -> None:
        log.warning("gateway call failed: %s (%s)", what, type(exc).__name__,
                    extra={"fields": {**fields, "event.duration": time.perf_counter_ns() - started,
                                      "error.type": type(exc).__name__}})  # fmt: skip

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


def _retryable_read(outcome: BaseException | httpx.Response) -> bool:
    if isinstance(outcome, httpx.Response):
        return outcome.status_code in RETRYABLE_STATUS
    # Connect errors: the request never reached the backend. Read timeouts are
    # deliberately NOT retried (see resilience.py).
    return isinstance(outcome, httpx.ConnectError | httpx.ConnectTimeout)
