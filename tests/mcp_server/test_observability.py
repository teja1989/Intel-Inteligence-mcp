"""Logs and traces (docs/10): ECS JSON shape, trace correlation, redaction, no secrets.

The end-to-end test runs the real HTTP app with a gateway-style `traceparent` and
checks that every log line of the request carries that trace, that the downstream
call carries it on to the gateway (and `baggage` does not), and that neither logs
nor span attributes contain the token, tool arguments or IDs outside the audit line.
"""

import io
import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from telco_mcp.clients.resilience import CircuitBreaker, RetryPolicy, with_retry
from telco_mcp.gateway_routes import GatewayRoutes
from telco_mcp.observability import fields as log_fields
from telco_mcp.observability.logs import EcsJsonFormatter, TextFormatter
from telco_mcp.observability.telemetry import configure_telemetry, redact_url
from telco_mcp.security.scoped_server import _parent_context
from telco_mcp_lab.mock_apis.app import create_app
from telco_mcp_lab.mock_apis.settings import MockApiSettings
from tests.conftest import TEST_TOKEN, LiveServer, http_app_in, jwt_token, make_telco

SERVICE = {"name": "telco-mcp-server", "version": "test", "environment": "local"}
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"
SPANS = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def telemetry():
    configure_telemetry(SERVICE, log_level="DEBUG")  # once per process (global providers)
    provider = trace.get_tracer_provider()
    if not getattr(provider, "_test_exporter", False):
        provider.add_span_processor(SimpleSpanProcessor(SPANS))
        provider._test_exporter = True


@contextmanager
def captured(fmt: logging.Formatter):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(fmt)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


def wait_for_lines(buf: io.StringIO, needle: str, count: int, timeout_s: float = 3.0) -> list:
    """The access line is written right AFTER the response is sent (so it can include the
    full duration), i.e. possibly after the client already has its answer: wait for it."""
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        lines = [json.loads(x) for x in buf.getvalue().splitlines() if needle in x]
        if len(lines) >= count or time.monotonic() > deadline:
            return lines
        time.sleep(0.02)


def record(msg="hello", **kw) -> logging.LogRecord:
    rec = logging.LogRecord("telco_mcp.test", logging.INFO, __file__, 1, msg, (), None)
    for k, v in kw.items():
        setattr(rec, k, v)
    return rec


# ------------------------------------------------------------------------- formatters
class TestEcsJson:
    def test_core_fields_and_nested_extras(self):
        doc = json.loads(EcsJsonFormatter(SERVICE).format(
            record(fields={"http.response.status_code": 200, "mcp.tool": "list_orders"})
        ))  # fmt: skip
        assert doc["log.level"] == "info" and doc["message"] == "hello"
        assert doc["@timestamp"].endswith("Z") and doc["ecs.version"]
        assert doc["service"] == SERVICE and doc["log"]["logger"] == "telco_mcp.test"
        assert doc["http"]["response"]["status_code"] == 200
        assert doc["mcp"]["tool"] == "list_orders"
        assert "trace" not in doc  # no active span

    def test_trace_ids_from_the_active_span(self):
        with trace.get_tracer("t").start_as_current_span("s") as span:
            doc = json.loads(EcsJsonFormatter(SERVICE).format(record()))
        ctx = span.get_span_context()
        assert doc["trace"]["id"] == format(ctx.trace_id, "032x")
        assert doc["span"]["id"] == format(ctx.span_id, "016x")

    def test_bound_request_fields_are_included(self):
        with log_fields.bind(**{"mcp.client_id": "agent-a"}):
            doc = json.loads(EcsJsonFormatter(SERVICE).format(record()))
        assert doc["mcp"]["client_id"] == "agent-a"
        assert "mcp" not in json.loads(EcsJsonFormatter(SERVICE).format(record()))

    def test_exception_becomes_ecs_error(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = record(exc_info=sys.exc_info())
        doc = json.loads(EcsJsonFormatter(SERVICE).format(rec))
        assert doc["error"]["type"] == "ValueError" and "boom" in doc["error"]["stack_trace"]

    def test_conflicting_field_names_do_not_crash(self):
        rec = record(fields={"mcp": "scalar", "mcp.tool": "x"})
        assert json.loads(EcsJsonFormatter(SERVICE).format(rec))["mcp"] == "scalar"

    def test_ecs_message_replaces_the_text_message(self):
        doc = json.loads(EcsJsonFormatter(SERVICE).format(record('{"x":1}', ecs_message="short")))
        assert doc["message"] == "short"


def test_text_format_is_one_readable_line():
    line = TextFormatter().format(record(fields={"mcp.tool": "list_orders"}))
    assert "INFO telco_mcp.test: hello mcp.tool=list_orders" in line and "\n" not in line


# -------------------------------------------------------------------------- redaction
@pytest.mark.security
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://gw/boaccount/API/account/ACC-1001", "https://gw/boaccount/API/account/{id}"),
        ("https://gw/bosubscription/API/subscription?account_id=ACC-1001&limit=5",
         "https://gw/bosubscription/API/subscription"),
        ("https://gw/x/API/msisdn/+447700900111/usage", "https://gw/x/API/msisdn/{id}/usage"),
        ("http://127.0.0.1:8081/boorder/API/order/ORD-000123", "http://127.0.0.1:8081/boorder/API/order/{id}"),
    ],
)  # fmt: skip
def test_redact_url_removes_ids_and_query(url, expected):
    assert redact_url(url) == expected


def test_meta_traceparent_is_used_when_there_is_no_ambient_trace():
    ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"traceparent": TRACEPARENT}))
    parent = _parent_context(ctx)
    assert format(trace.get_current_span(parent).get_span_context().trace_id, "032x") == TRACE_ID
    assert _parent_context(SimpleNamespace(request_context=SimpleNamespace(meta=None))) is None


# ------------------------------------------------------------------ resilience events
def test_breaker_transitions_are_logged(caplog):
    caplog.set_level(logging.INFO, logger="telco_mcp.resilience")
    now = [0.0]
    b = CircuitBreaker("account", failure_threshold=2, cooldown_s=10, clock=lambda: now[0])
    b.on_failure()
    b.on_failure()  # → open
    now[0] = 11
    b.before_call()  # → half_open
    b.on_success()  # → closed
    msgs = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert msgs == [
        ("WARNING", "circuit breaker closed -> open"),
        ("INFO", "circuit breaker open -> half_open"),
        ("INFO", "circuit breaker half_open -> closed"),
    ]
    assert caplog.records[0].fields["gateway.api"] == "account"


async def test_retries_are_logged_with_reason(caplog):
    caplog.set_level(logging.WARNING, logger="telco_mcp.resilience")
    responses = iter([httpx.Response(503), httpx.Response(200)])

    async def call():
        return next(responses)

    async def no_sleep(_):
        return None

    r = await with_retry(call, lambda x: getattr(x, "status_code", 0) == 503,
                         RetryPolicy(max_attempts=2), sleep=no_sleep, name="order")  # fmt: skip
    assert r.status_code == 200
    (rec,) = caplog.records
    assert rec.getMessage() == "retrying order after HTTP 503 (attempt 1/2)"


# ----------------------------------------------------------------------- end to end
@pytest.fixture
def recording_gateway(tmp_path):
    seen: list[dict[str, str]] = []
    app = create_app(
        MockApiSettings(_env_file=None, gateway_token=SecretStr(TEST_TOKEN),
                        db_path=tmp_path / "gw.sqlite3"),
        GatewayRoutes(_env_file=None),
    )  # fmt: skip

    @app.middleware("http")
    async def record_headers(request, call_next):
        seen.append({k.lower(): v for k, v in request.headers.items()})
        return await call_next(request)

    with LiveServer(app) as srv:
        yield srv.url, seen


def call_tool(url: str, token: str, extra_headers: dict[str, str]) -> httpx.Response:
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {}}  # fmt: skip
    args = {"account_id": "ACC-1001", "limit": 1}
    params = {"name": "list_subscriptions", "arguments": args, "_meta": meta}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "list_subscriptions",
        **extra_headers,
    }
    return httpx.post(url + "/mcp", json=body, headers=headers, timeout=10, trust_env=False)


@pytest.mark.security
@pytest.mark.protocol
def test_one_request_one_trace_no_secrets(recording_gateway, tmp_path):
    gw_url, seen = recording_gateway
    app = http_app_in(tmp_path, lambda: make_telco(base_url=gw_url))
    token = jwt_token()
    SPANS.clear()
    with LiveServer(app) as srv, captured(EcsJsonFormatter(SERVICE)) as buf:
        r = call_tool(srv.url, token, {"traceparent": TRACEPARENT, "baggage": "evil=1"})
        assert r.status_code == 200 and not r.json()["result"]["isError"]
        wait_for_lines(buf, "telco_mcp.access", 1)
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    ours = [d for d in lines if d["log"]["logger"].startswith("telco_mcp")]
    by_logger = {d["log"]["logger"]: d for d in ours}

    # 1. Every line of the request carries the gateway's trace.
    assert ours and all(d.get("trace", {}).get("id") == TRACE_ID for d in ours), ours
    # 2. Access line: the protocol facts and the client, nothing else.
    access = by_logger["telco_mcp.access"]
    assert access["http"]["response"]["status_code"] == 200
    assert access["mcp"] == {"method": "tools/call", "tool": "list_subscriptions",
                             "protocol_version": "2026-07-28", "client_id": "agent-a"}  # fmt: skip
    # 3. Audit line: the only place the account ID appears.
    audit = by_logger["telco_mcp.audit"]
    assert audit["mcp"]["resources"] == {"account_id": "ACC-1001"}
    assert audit["event"]["outcome"] == "success"
    for d in ours:
        if d is not audit:
            assert "ACC-1001" not in json.dumps(d), d["log"]["logger"]
    # 4. No token, no PII anywhere in the logs.
    everything = buf.getvalue()
    assert token not in everything and token.split(".")[2] not in everything
    assert "+447700900111" not in everything and "+44*******111" not in everything
    # 5. The gateway got the same trace (new parent span), and never the baggage.
    (gw,) = [h for h in seen if "subscription" in h.get("host", "") or h]
    assert gw["traceparent"].split("-")[1] == TRACE_ID and "baggage" not in gw
    assert gw["authorization"] == f"Bearer {TEST_TOKEN}"  # the server's own token, not the agent's
    # 6. Span attributes never carry IDs or query strings.
    exported = json.dumps(
        [dict(s.attributes or {}) | {"name": s.name} for s in SPANS.get_finished_spans()]
    )
    assert "ACC-1001" not in exported and "account_id=" not in exported
    assert "GET /bosubscription/API/subscription" in exported


@pytest.mark.protocol
def test_unauthenticated_request_is_logged_without_client(tmp_path, live_gateway):
    app = http_app_in(tmp_path, lambda: make_telco(base_url=live_gateway))
    with LiveServer(app) as srv, captured(EcsJsonFormatter(SERVICE)) as buf:
        call_tool(srv.url, "x" * 40, {"Mcp-Name": "bad name; DROP"})
        wait_for_lines(buf, "telco_mcp.access", 1)
        httpx.get(srv.url + "/healthz", trust_env=False)
        denied, health = wait_for_lines(buf, "telco_mcp.access", 2)
    assert denied["http"]["response"]["status_code"] == 401 and "client_id" not in denied["mcp"]
    assert denied["mcp"]["tool"] == "invalid"  # odd header values are never echoed
    assert health["log.level"] == "debug"
