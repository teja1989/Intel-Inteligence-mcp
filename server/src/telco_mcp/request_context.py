"""Who is calling and which tool is running, for the request in progress.

Set once per tool call by `security/scoped_server.py`, AFTER the caller's token was
validated and the client resolved from the registry. Read by anything downstream that
needs to know (e.g. the analytics headers towards the gateway).

A ContextVar, so each concurrent request sees only its own value (asyncio copies the
context per task; the concurrency tests prove there is no bleed). Values here are
trusted: they come from our validation, never from headers the caller sent.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class CallIdentity:
    client_id: str  # validated and registered (or "stdio" for the local transport)
    tool: str  # a registered tool name


_call: ContextVar[CallIdentity | None] = ContextVar("call_identity", default=None)


@contextmanager
def bind_call(identity: CallIdentity) -> Iterator[None]:
    token = _call.set(identity)
    try:
        yield
    finally:
        _call.reset(token)


def current_call() -> CallIdentity | None:
    return _call.get()
