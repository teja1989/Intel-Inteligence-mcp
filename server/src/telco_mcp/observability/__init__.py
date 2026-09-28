"""Logs, traces and request correlation (docs/operations.md §3).

* `logs`      : logging setup; ECS JSON (production) or readable text (local).
* `fields`    : per-request log fields (client, tool) carried in a ContextVar.
* `access`    : one access-log line per HTTP request.
* `telemetry` : OpenTelemetry: W3C trace context in/out, spans, OTLP export.
"""
