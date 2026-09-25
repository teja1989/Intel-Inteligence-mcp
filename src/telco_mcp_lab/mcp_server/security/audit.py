"""Audit log: one structured line per tool call. Metadata only, never payloads.

Recorded: tool, caller, tenant, outcome, latency, transport.
Never recorded: arguments, results, tokens. Arguments can hold PII, and
results hold customer data. An audit trail that copies them becomes the
biggest PII store in the system.

Outcomes:
  ok          the tool returned a result
  tool_error  an anticipated failure the model can act on (not found, backend down)
  denied      tenant/scope refusal (AccessDenied) or a hidden tool
  error       an unexpected crash (bug)

Java/Spring equivalent: an `@Around` aspect (or Micrometer Observation) on
tool methods, writing JSON through a dedicated SLF4J logger/appender.
"""

import json
import logging
import time

AUDIT_LOGGER = "telco_mcp.audit"
_audit = logging.getLogger(AUDIT_LOGGER)


def audit_tool_call(
    *,
    tool: str,
    caller: str | None,
    tenant: str | None,
    via: str | None,
    outcome: str,
    started: float,
) -> None:
    _audit.info(
        json.dumps(
            {
                "event": "tool_call",
                "tool": tool,
                "caller": caller,
                "tenant": tenant,
                "via": via,
                "outcome": outcome,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
            separators=(",", ":"),
        )
    )
