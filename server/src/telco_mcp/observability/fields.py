"""Per-request log fields, carried in a ContextVar so every log line of a request has them.

    with bind(**{"mcp.tool": "list_orders", "mcp.client_id": "care-agent"}):
        ...  # any log line in here, in any module, carries both fields

A ContextVar is per request (per asyncio task context), never shared between
concurrent requests; the concurrency test checks exactly this for the client.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_fields: ContextVar[dict[str, Any]] = ContextVar("log_fields", default={})  # noqa: B039 - never mutated


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    token = _fields.set({**_fields.get(), **fields})
    try:
        yield
    finally:
        _fields.reset(token)


def current() -> dict[str, Any]:
    return _fields.get()
