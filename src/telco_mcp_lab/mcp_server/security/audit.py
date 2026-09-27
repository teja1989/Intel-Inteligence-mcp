"""Audit log: one structured line per tool call. Metadata only, never payloads.

Recorded: tool, client, transport, the resource IDs the call named (account /
line / order: validated identifiers only), outcome, latency.
Never recorded: other arguments, results, tokens. Results hold customer data;
an audit trail that copied them would become the biggest PII store in the system.

With no customer boundary (docs/08 §1), the resource IDs are what lets you answer
"which client read which account, when", so they are always recorded.

Outcomes:
  ok          the tool returned a result
  tool_error  an anticipated failure the model can act on (not found, backend down)
  denied      a tool the client's scopes don't allow (or no valid client at all)
  error       an unexpected crash (bug)

Java/Spring equivalent: an `@Around` aspect (or Micrometer Observation) on
tool methods, writing JSON through a dedicated SLF4J logger/appender.
"""

import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

from telco_mcp_lab import ids

AUDIT_LOGGER = "telco_mcp.audit"
_audit = logging.getLogger(AUDIT_LOGGER)

# Argument name → strict format. Anything else (or a malformed value) isn't recorded.
_RESOURCE_ARGS = {
    "account_id": re.compile(ids.ACCOUNT_ID),
    "subscription_id": re.compile(ids.SUBSCRIPTION_ID),
    "order_id": re.compile(ids.ORDER_ID),
}


def resource_ids(arguments: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        name: value
        for name, pattern in _RESOURCE_ARGS.items()
        if isinstance(value := (arguments or {}).get(name), str) and pattern.fullmatch(value)
    }


def audit_tool_call(
    *,
    tool: str,
    client: str | None,
    via: str | None,
    resources: Mapping[str, str],
    outcome: str,
    started: float,
) -> None:
    _audit.info(
        json.dumps(
            {
                "event": "tool_call",
                "tool": tool,
                "client": client,
                "via": via,
                "resources": dict(resources),
                "outcome": outcome,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
            separators=(",", ":"),
        )
    )
