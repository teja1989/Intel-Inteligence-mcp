"""Retry and circuit-breaker behaviour of the gateway client, and timeouts end to end."""

import httpx
import pytest
import respx
from mcp import Client

from telco_mcp_lab.gateway_routes import DomainApi
from telco_mcp_lab.mcp_server.clients.resilience import BreakerState, CircuitBreaker, RetryPolicy
from telco_mcp_lab.mcp_server.clients.telco import GatewayError, GatewayUnavailable
from tests.conftest import GATEWAY_URL, TEST_TOKEN, make_telco, server_as

ORDER_URL = f"{GATEWAY_URL}/boorder/API/order/ORD-000123"
NO_WAIT = RetryPolicy(max_attempts=2, base_delay_s=0.0, max_delay_s=0.0)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class TestCircuitBreakerUnit:
    def test_opens_after_threshold_then_half_opens_after_cooldown(self):
        clock = FakeClock()
        b = CircuitBreaker("order", failure_threshold=3, cooldown_s=30, clock=clock)
        for _ in range(3):
            b.before_call()
            b.on_failure()
        assert b.state is BreakerState.OPEN
        with pytest.raises(Exception, match="order"):
            b.before_call()  # fail fast while open
        clock.t += 31
        b.before_call()  # the single half-open trial is allowed
        assert b.state is BreakerState.HALF_OPEN
        with pytest.raises(Exception, match="order"):
            b.before_call()  # ...but only one at a time
        b.on_success()
        assert b.state is BreakerState.CLOSED

    def test_failed_trial_reopens(self):
        clock = FakeClock()
        b = CircuitBreaker("order", failure_threshold=1, cooldown_s=5, clock=clock)
        b.before_call()
        b.on_failure()
        clock.t += 6
        b.before_call()
        b.on_failure()
        assert b.state is BreakerState.OPEN


class TestRetry:
    @respx.mock
    async def test_503_then_success_is_retried(self):
        route = respx.get(ORDER_URL)
        route.side_effect = [httpx.Response(503, json={"code": "X"}), httpx.Response(200, json={})]
        client = make_telco()
        client._retry = NO_WAIT
        assert await client.get_order("ORD-000123") == {}
        assert route.call_count == 2

    @respx.mock
    async def test_connect_error_is_retried(self):
        route = respx.get(ORDER_URL)
        route.side_effect = [httpx.ConnectError("refused"), httpx.Response(200, json={})]
        client = make_telco()
        client._retry = NO_WAIT
        await client.get_order("ORD-000123")
        assert route.call_count == 2

    @respx.mock
    async def test_read_timeout_is_not_retried(self):
        route = respx.get(ORDER_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        client = make_telco()
        client._retry = NO_WAIT
        with pytest.raises(GatewayUnavailable, match="timeout"):
            await client.get_order("ORD-000123")
        assert route.call_count == 1

    @respx.mock
    async def test_4xx_is_not_retried_and_does_not_trip_breaker(self):
        route = respx.get(ORDER_URL).respond(404, json={"code": "ORDER_NOT_FOUND"})
        client = make_telco()
        client._retry = NO_WAIT
        for _ in range(10):
            with pytest.raises(GatewayError):
                await client.get_order("ORD-000123")
        assert route.call_count == 10  # no retries
        assert client.breakers[DomainApi.ORDER].state is BreakerState.CLOSED


class TestBreakerIntegration:
    @respx.mock
    async def test_breaker_opens_and_fails_fast_without_calling_backend(self):
        route = respx.get(ORDER_URL).respond(500, json={"code": "BOOM"})
        client = make_telco()
        client._retry = NO_WAIT
        for _ in range(5):
            with pytest.raises(GatewayError):
                await client.get_order("ORD-000123")
        assert client.breakers[DomainApi.ORDER].state is BreakerState.OPEN
        calls_before = route.call_count
        with pytest.raises(GatewayUnavailable, match="circuit open"):
            await client.get_order("ORD-000123")
        assert route.call_count == calls_before  # failed fast
        # Other APIs have their own breaker and are unaffected.
        assert client.breakers[DomainApi.ACCOUNT].state is BreakerState.CLOSED


@pytest.mark.slow
class TestTimeoutEndToEnd:
    async def test_slow_backend_becomes_clean_tool_error(self, live_gateway):
        """Chaos delay 1.5 s vs client read timeout 0.3 s: the tool must fail fast and cleanly.

        Uses a REAL socket on purpose: httpx's in-process ASGITransport does not
        enforce timeouts (they live in the network layer), so an in-process test
        would pass the slow call straight through.
        """
        async with httpx.AsyncClient() as admin:
            resp = await admin.post(
                f"{live_gateway}/_admin/chaos",
                json={"delay_ms": 1500},
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
            )
            resp.raise_for_status()
        factory = lambda: make_telco(base_url=live_gateway, read_timeout_s=0.3)  # noqa: E731
        async with Client(server_as("alice", factory)) as c:
            r = await c.call_tool("get_order_status", {"order_id": "ORD-000123"})
        assert r.is_error
        assert "did not respond in time" in r.content[0].text


class TestCorporateProxyIsolation:
    async def test_local_gateway_calls_ignore_proxy_env(self, live_gateway, monkeypatch):
        """HTTP(S)_PROXY on a corporate laptop must not capture localhost gateway calls."""
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # a dead proxy
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        client = make_telco(base_url=live_gateway)
        order = await client.get_order("ORD-000123")
        assert order["order_id"] == "ORD-000123"
        await client.aclose()


class TestBreakerNeverWedges:
    """Review bug 1: a half-open trial that never reports back must not block the API forever."""

    async def _open_then_half_open(self, client, clock):
        breaker = client.breakers[DomainApi.ORDER]
        breaker.clock, breaker.failure_threshold, breaker.cooldown_s = clock, 1, 5
        breaker.before_call()
        breaker.on_failure()
        clock.t += 6  # cooldown over: the next call is the single trial
        return breaker

    @respx.mock
    async def test_cancelled_trial_frees_the_slot(self):
        import asyncio

        async def hang(_request):
            await asyncio.sleep(30)

        client, clock = make_telco(), FakeClock()
        client._retry = NO_WAIT
        breaker = await self._open_then_half_open(client, clock)
        respx.get(ORDER_URL).mock(side_effect=hang)
        task = asyncio.create_task(client.get_order("ORD-000123"))
        await asyncio.sleep(0.05)
        task.cancel()  # e.g. the MCP client disconnected mid-call
        with pytest.raises(asyncio.CancelledError):
            await task
        # A cancellation says nothing about backend health: the next call is a new trial.
        respx.get(ORDER_URL).respond(json={"order_id": "ORD-000123"})
        assert (await client.get_order("ORD-000123"))["order_id"] == "ORD-000123"
        assert breaker.state is BreakerState.CLOSED

    @respx.mock
    async def test_unexpected_http_error_counts_as_failure_not_a_wedge(self):
        client, clock = make_telco(), FakeClock()
        client._retry = NO_WAIT
        breaker = await self._open_then_half_open(client, clock)
        respx.get(ORDER_URL).mock(side_effect=httpx.DecodingError("bad gzip"))
        with pytest.raises(GatewayUnavailable):
            await client.get_order("ORD-000123")
        assert breaker.state is BreakerState.OPEN  # failed trial re-opens...
        clock.t += 6
        respx.get(ORDER_URL).respond(json={"order_id": "ORD-000123"})
        await client.get_order("ORD-000123")  # ...and recovers after the cooldown
        assert breaker.state is BreakerState.CLOSED


def test_abandon_frees_trial_without_changing_state():
    clock = FakeClock()
    b = CircuitBreaker("order", failure_threshold=1, cooldown_s=5, clock=clock)
    b.before_call()
    b.on_failure()
    clock.t += 6
    b.before_call()  # trial in flight
    b.abandon()
    assert b.state is BreakerState.HALF_OPEN
    b.before_call()  # a new trial is allowed
