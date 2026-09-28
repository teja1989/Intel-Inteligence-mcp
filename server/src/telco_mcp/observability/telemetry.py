"""OpenTelemetry: W3C trace context in and out, spans, and OTLP export.

Always on (cheap, in-process): a TracerProvider, so every log line of a request
carries the `trace.id` of the inbound `traceparent` (from the API gateway) and can be
joined with the gateway's and the backends' logs in ELK.

Exported only when configured, through the standard OpenTelemetry variables, so the
same image works on Cloud Foundry and EKS without code changes:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318   # traces + logs over OTLP/HTTP
    OTEL_EXPORTER_OTLP_HEADERS=...                           # if the collector needs auth
    OTEL_TRACES_SAMPLER=parentbased_traceidratio             # default: parentbased_always_on
    OTEL_TRACES_SAMPLER_ARG=0.1
    OTEL_LOGS_EXPORTER=none                                  # keep logs on stdout only

Propagation is W3C `traceparent`/`tracestate` only. Baggage is deliberately NOT
accepted: callers are untrusted, and baggage would be forwarded to the backends.

Privacy: outbound span URLs are rewritten so IDs in paths (account, line, order,
phone numbers) and query strings never reach the tracing backend.

Java/Spring equivalent: the OpenTelemetry Java agent (or Micrometer Tracing with the
OTLP exporter) configured by the same OTEL_* variables.
"""

import logging
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.types import ASGIApp

log = logging.getLogger(__name__)
_configured = False


def _enabled(signal: str) -> bool:
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return False
    if os.environ.get(f"OTEL_{signal.upper()}_EXPORTER", "otlp").lower() == "none":
        return False
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    )


def configure_telemetry(service: dict[str, str], *, log_level: str) -> Callable[[], None]:
    """Set up tracing (always) and OTLP export (if configured). Returns a shutdown hook."""
    global _configured
    if _configured:  # the global providers can only be set once per process
        return lambda: None
    _configured = True
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", service["name"]),
            "service.version": service["version"],
            "deployment.environment.name": service["environment"],
        }
    )
    tracer_provider = TracerProvider(resource=resource)  # sampler: OTEL_TRACES_SAMPLER
    if _enabled("traces"):
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    shutdowns: list[Callable[[], Any]] = [tracer_provider.shutdown]
    if _enabled("logs"):
        shutdowns.append(_otlp_logs(resource, log_level))
    log.info(
        "telemetry: traces export=%s, logs export=%s",
        "otlp" if _enabled("traces") else "off",
        "otlp" if _enabled("logs") else "off",
    )

    def shutdown() -> None:
        for fn in shutdowns:
            fn()  # flushes batched spans / log records

    return shutdown


def _otlp_logs(resource: Resource, log_level: str) -> Callable[[], Any]:
    # The Python logs SDK still lives under `_logs` (experimental API) in 1.45:
    # kept to this one function so a future rename touches one place.
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    logging.getLogger().addHandler(
        LoggingHandler(level=log_level.upper(), logger_provider=provider)
    )
    return provider.shutdown


def instrument_asgi(app: ASGIApp) -> ASGIApp:
    """Server span per HTTP request, parented on the inbound `traceparent`."""
    return OpenTelemetryMiddleware(app, excluded_urls="healthz", exclude_spans=["receive", "send"])


# A path segment holding an identifier: anything with a digit or a '+' (phone numbers).
_ID_SEGMENT = re.compile(r"^[^/]*[\d+][^/]*$")


def redact_url(url: str) -> str:
    """https://gw/boaccount/API/account/ACC-1001?x=1 → https://gw/boaccount/API/account/{id}"""
    parts = urlsplit(url)
    path = "/".join("{id}" if _ID_SEGMENT.match(s) else s for s in parts.path.split("/"))
    return f"{parts.scheme}://{parts.netloc}{path}"


def _redact_span(span: Any, request: Any) -> None:
    if span is None or not span.is_recording():
        return
    # Prefer the endpoint's route template (endpoints.py: no IDs, low cardinality);
    # fall back to a heuristic redaction for calls made without one.
    route = (getattr(request, "extensions", None) or {}).get("telco.route")
    parts = urlsplit(str(request.url))
    safe = f"{parts.scheme}://{parts.netloc}{route}" if route else redact_url(str(request.url))
    for key in ("url.full", "http.url"):
        span.set_attribute(key, safe)
    method = request.method
    method = method.decode() if isinstance(method, bytes) else method
    span.update_name(f"{method} {urlsplit(safe).path}")


async def _redact_span_async(span: Any, request: Any) -> None:
    _redact_span(span, request)


def instrument_http_client(client: httpx.AsyncClient) -> None:
    """Client span per downstream call + `traceparent` injected towards the gateway."""
    # Must be a coroutine function: for an AsyncClient the instrumentation silently
    # ignores a sync hook (found by test_one_request_one_trace_no_secrets).
    HTTPXClientInstrumentor.instrument_client(client, request_hook=_redact_span_async)
