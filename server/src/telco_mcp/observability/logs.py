"""Logging setup: ECS JSON for ELK (production), readable text for local runs.

One line per event, to stdout (HTTP) or stderr (stdio, where stdout is the protocol).
The platform ships it to ELK (Cloud Foundry loggregator, or Fluent Bit / Filebeat on
EKS). With OTLP configured, the same records also go to the OpenTelemetry collector
(telemetry.py).

JSON lines follow the Elastic Common Schema (ECS), so Kibana understands them without
custom parsing:

    {"@timestamp":"2026-09-28T09:15:02.123Z","log.level":"info","message":"request",
     "ecs.version":"8.11.0","log":{"logger":"telco_mcp.access"},
     "service":{"name":"telco-mcp-server","version":"0.4.0","environment":"dev"},
     "trace":{"id":"4bf92f35..."},"span":{"id":"00f067aa..."},
     "http":{"request":{"method":"POST"},"response":{"status_code":200}},
     "event":{"duration":12345678},"mcp":{"method":"tools/call","tool":"list_orders",...}}

Structured fields are passed as `extra={"fields": {"dotted.name": value}}` and nested
on output. Custom (non-ECS) fields live under `mcp.*` and `gateway.*`.

Rules for every log call in this service: never log tokens, secrets, tool arguments
or results, or backend response bodies. IDs only where docs/10 says so.

Java/Spring equivalent: logback + `co.elastic.logging:logback-ecs-encoder`, with the
OpenTelemetry Java agent adding trace.id/span.id to the MDC.
"""

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

from opentelemetry import trace

from telco_mcp.observability import fields as log_fields

ECS_VERSION = "8.11.0"
LogFormat = Literal["json", "text"]

# Loggers that are too chatty at INFO, or log things we must not (httpx logs every
# downstream URL, and URLs carry account IDs; in real APIs often MSISDNs).
# mcp.server.sse (legacy transport, unused here) logs whole messages at DEBUG: held at
# WARNING so MCP_LOG_LEVEL=DEBUG can never put tool arguments/results in the logs.
_QUIET = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "uvicorn.access": logging.WARNING,
    "mcp.server.sse": logging.WARNING,
}


def _nest(target: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    node = target
    for p in parents:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):  # a scalar already sits here: keep both, don't crash
            return
    node[leaf] = value


def _trace_ids() -> tuple[str | None, str | None]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    extra = getattr(record, "fields", None)
    return {**log_fields.current(), **(extra if isinstance(extra, dict) else {})}


class EcsJsonFormatter(logging.Formatter):
    def __init__(self, service: dict[str, str]) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "@timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "log.level": record.levelname.lower(),
            "message": getattr(record, "ecs_message", None) or record.getMessage(),
            "ecs.version": ECS_VERSION,
            "log": {"logger": record.name},
            "service": dict(self._service),
            "process": {"pid": record.process},
        }
        trace_id, span_id = _trace_ids()
        if trace_id:
            doc["trace"], doc["span"] = {"id": trace_id}, {"id": span_id}
        for key, value in _record_fields(record).items():
            _nest(doc, key, value)
        if record.exc_info and record.exc_info[0] is not None:
            etype, evalue, tb = record.exc_info
            doc["error"] = {
                "type": etype.__name__,
                "message": str(evalue),
                "stack_trace": "".join(traceback.format_exception(etype, evalue, tb)),
            }
        return json.dumps(doc, ensure_ascii=False, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Local runs: `time LEVEL logger: message key=value … trace=<first 8>`."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        # Records with an ecs_message (the audit line) already carry their facts in the text.
        own = {} if getattr(record, "ecs_message", None) else _record_fields(record)
        extras = " ".join(f"{k}={v}" for k, v in own.items() if v is not None)
        trace_id, _ = _trace_ids()
        tail = " ".join(x for x in (extras, f"trace={trace_id[:8]}" if trace_id else "") if x)
        return f"{line} {tail}" if tail else line


def configure_logging(
    *,
    level: str,
    fmt: LogFormat,
    service: dict[str, str],
    stream: TextIO | None = None,
) -> logging.Handler:
    """Replace the root handlers with one formatted handler. Returns it (for tests)."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(EcsJsonFormatter(service) if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name, quiet in _QUIET.items():
        logging.getLogger(name).setLevel(max(quiet, root.level))
    # uvicorn's own loggers: propagate to root so they get the same format.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    return handler
