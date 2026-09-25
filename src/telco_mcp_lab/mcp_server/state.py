"""Process-wide resources shared by all tool calls (built once in the lifespan).

This is NOT per-client session state. The server stays stateless: `AppState`
only holds infrastructure (a pooled HTTP client), exactly like a Spring
singleton bean. Nothing about a caller or a conversation is ever stored here.
"""

from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import Context

from telco_mcp_lab.mcp_server.clients.telco import TelcoApiClient


@dataclass(frozen=True)
class AppState:
    telco: TelcoApiClient
    unsafe_raw_free_text: bool = False  # lab demo switch, see shaping/free_text.py


def app_state(ctx: Context[Any, Any]) -> AppState:
    state = ctx.request_context.lifespan_context
    if not isinstance(state, AppState):  # a wiring bug, not a caller error
        raise RuntimeError("Server lifespan did not provide AppState")
    return state
