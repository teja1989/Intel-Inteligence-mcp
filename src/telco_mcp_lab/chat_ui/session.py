"""One chat turn, independent of Streamlit (so it can be tested without a browser).

Each turn opens a fresh MCP connection: the server is stateless, and Streamlit
re-runs the script on every interaction, so nothing long-lived is kept except the
conversation history (and a still-valid token for the shared client).

Identity comes from the sidebar, never from the model:
* lab: a lab caller's static token (MCP_TOKEN_<CALLER> from .env) against a
  static-mode server (`make mcp-http`);
* shared: the shared lower-env client via the connector config (client
  credentials) plus the chosen customer header, against a JWT-mode server.
"""

import os
import re
import time
from dataclasses import astuple, dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx2
from dotenv import dotenv_values
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from telco_mcp_lab.connect.settings import ConnectSettings, is_local
from telco_mcp_lab.connect.token import ClientCredentials
from telco_mcp_lab.harness.agent import Agent, deny_all
from telco_mcp_lab.harness.guardrails import Guardrails
from telco_mcp_lab.harness.llm import ChatModel
from telco_mcp_lab.harness.models import Provider, build_chat_model
from telco_mcp_lab.harness.prompts import load_system_prompt
from telco_mcp_lab.harness.settings import HarnessSettings
from telco_mcp_lab.harness.trace import Tracer

CUSTOMER_RE = re.compile(r"^ACC-\d{4}(,ACC-\d{4})*$")
Identity = Literal["lab", "shared"]


@dataclass(frozen=True)
class ChatConfig:
    provider: Provider
    identity: Identity
    mcp_url: str = "http://127.0.0.1:8090/mcp"  # lab mode; shared mode uses the connector's
    caller: str = "alice"
    connector_config: Path | None = None
    customer: str | None = None

    def fingerprint(self) -> tuple[Any, ...]:
        """Anything that changes WHO is asking, or which model sees the data. A change
        must start a new conversation: the old one holds the previous identity's or
        customer's data in the model's context."""
        return astuple(self)

    def validate(self) -> None:
        if self.customer and not CUSTOMER_RE.fullmatch(self.customer):
            raise ValueError("Customer must look like ACC-1234 (comma-separated for several)")
        if self.identity == "shared" and self.connector_config is None:
            raise ValueError("Shared-client mode needs a connector config (docs/09)")


@dataclass
class TurnOutcome:
    answer: str | None
    stopped_reason: str
    tool_calls: list[dict[str, Any]]
    events: list[dict[str, Any]]
    seconds: float
    model: str


@dataclass
class ChatState:
    """What survives Streamlit reruns."""

    history: list[dict[str, Any]] | None = None  # agent history incl. system prompt
    token_cache: tuple[str, float] | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)  # what the UI shows


def _env() -> dict[str, str]:
    return {**{k: v for k, v in dotenv_values(".env").items() if v is not None}, **os.environ}


async def _mcp_headers(cfg: ChatConfig, state: ChatState) -> tuple[str, dict[str, str], Any]:
    """(mcp_url, headers, tls verify) for this identity."""
    if cfg.identity == "lab":
        token = _env().get(f"MCP_TOKEN_{cfg.caller.upper()}")
        if not token:
            raise RuntimeError(f"No MCP_TOKEN_{cfg.caller.upper()} in .env (run: make env-tokens)")
        return cfg.mcp_url, {"Authorization": f"Bearer {token}"}, True
    if not cfg.connector_config or not cfg.connector_config.is_file():
        raise RuntimeError(
            f"Connector config not found: {cfg.connector_config} (make connect-local)"
        )
    try:
        s = ConnectSettings(_env_file=cfg.connector_config)  # type: ignore[call-arg]
    except ValidationError as exc:  # field names only: never echo values (could be secrets)
        fields = ", ".join(sorted({".".join(map(str, e["loc"])) or "config" for e in exc.errors()}))
        raise RuntimeError(f"Connector config invalid ({fields}); see docs/09") from None
    tokens = ClientCredentials(s)
    tokens.seed(state.token_cache)
    token = await tokens.token()
    state.token_cache = tokens.export()
    headers = {"Authorization": f"Bearer {token}"}
    if cfg.customer:
        headers[s.customer_header] = cfg.customer
    verify: Any = str(s.ca_bundle) if s.ca_bundle else True
    return s.url, headers, verify


async def run_turn(
    cfg: ChatConfig,
    state: ChatState,
    prompt: str,
    *,
    model: ChatModel | None = None,
    trace_dir: Path | None = None,
) -> TurnOutcome:
    cfg.validate()
    hs = HarnessSettings()
    url, headers, verify = await _mcp_headers(cfg, state)
    llm = model or build_chat_model(cfg.provider)
    trace_dir = trace_dir or hs.trace_dir.parent / "chat-ui"
    tracer = Tracer(out=None, jsonl=trace_dir / f"{time.strftime('%Y%m%d')}.jsonl")
    started = time.perf_counter()
    http = httpx2.AsyncClient(
        headers=headers, timeout=60, verify=verify, trust_env=not is_local(url)
    )
    try:
        async with Client(streamable_http_client(url, http_client=http)) as mcp:
            agent = Agent(
                llm, mcp, tracer, max_steps=hs.max_steps,
                confirm=deny_all,  # no y/N dialog in the UI yet: non-read-only tools are declined
                system_prompt=load_system_prompt(hs.system_prompt_file),
                use_server_instructions=hs.use_server_instructions,
                guardrails=Guardrails.load(hs.guardrails_file),
            )  # fmt: skip
            if state.history:
                agent.history = state.history
            await agent.load_tools()
            result = await agent.ask(prompt)
            state.history = agent.history
    finally:
        tracer.close()
        await http.aclose()
        if model is None:
            await llm.aclose()
    return TurnOutcome(
        answer=result.answer,
        stopped_reason=result.stopped_reason,
        tool_calls=result.tool_calls,
        events=tracer.events,
        seconds=time.perf_counter() - started,
        model=llm.name,
    )


def usage_totals(events: list[dict[str, Any]]) -> dict[str, int]:
    total = {"prompt_tokens": 0, "completion_tokens": 0}
    for ev in events:
        for k, v in (ev.get("usage") or {}).items():
            if k in total and isinstance(v, int):
                total[k] += v
    return total


def explain(exc: BaseException) -> str:
    """A readable error: unwrap async ExceptionGroups to the real cause, then add a hint."""
    from telco_mcp_lab.harness.__main__ import diagnose

    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return diagnose(exc)
