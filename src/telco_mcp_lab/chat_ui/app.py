"""Streamlit chat UI: pick a model and an identity, chat, and see every step.

    make chat-ui        # binds 127.0.0.1 only; see docs/10

LAB RIG: local only, synthetic data only. Everything security-relevant lives in
session.py / safe_render.py (tested without a browser); this file is layout.
"""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

from telco_mcp_lab.chat_ui.safe_render import safe_markdown
from telco_mcp_lab.chat_ui.session import (
    ChatConfig,
    ChatState,
    TurnOutcome,
    explain,
    run_turn,
    usage_totals,
)
from telco_mcp_lab.harness import models
from telco_mcp_lab.harness.__main__ import ENV
from telco_mcp_lab.harness.settings import HarnessSettings
from telco_mcp_lab.mcp_server.security.caller import AccessModel
from telco_mcp_lab.mcp_server.settings import McpServerSettings

LOCAL = ("127.0.0.1", "localhost", "::1")
RESULT_PREVIEW = 4000

st.set_page_config(page_title="Telco MCP lab chat", page_icon="📡", layout="wide")

# --------------------------------------------------------------------------- guard
if st.get_option("server.address") not in LOCAL:
    st.error(
        "This lab UI must be bound to localhost. Start it with `make chat-ui` "
        "(it passes --server.address 127.0.0.1)."
    )
    st.stop()

if "chat" not in st.session_state:
    st.session_state.chat = ChatState()
    st.session_state.fingerprint = None
chat: ChatState = st.session_state.chat


def mock_url() -> str:
    return f"http://{ENV.get('MOCK_HOST') or '127.0.0.1'}:{ENV.get('MOCK_PORT') or '8081'}"


def set_chaos(body: dict[str, Any]) -> str:
    token = ENV.get("MOCK_GATEWAY_TOKEN", "")
    try:
        r = httpx.post(f"{mock_url()}/_admin/chaos", json=body, timeout=5, trust_env=False,
                       headers={"Authorization": f"Bearer {token}"})  # fmt: skip
        return "ok" if r.is_success else f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        return f"mock gateway unreachable ({type(exc).__name__})"


# ------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Setup")
    available = models.configured()
    if not available:
        st.error(
            "No model configured. Set CHAT_CLAUDE_API_KEY, CHAT_GEMINI_API_KEY or "
            "AZURE_OPENAI_* in .env, then restart."
        )
        st.stop()
    provider = st.selectbox(
        "Model",
        available,
        format_func=lambda p: f"{models.LABELS[p]} · {models.model_name(p)}",
    )
    identity = st.radio(
        "Identity",
        ["lab", "shared"],
        format_func=lambda i: {
            "lab": "Lab caller (static token, `make mcp-http`)",
            "shared": "Shared lower-env client (JWT, `make mcp-http-jwt`)",
        }[i],
    )
    if identity == "lab":
        access = AccessModel.load(McpServerSettings(_env_file=None).access_config)
        caller = st.selectbox(
            "Caller",
            sorted(access.callers),
            format_func=lambda c: (
                f"{c}: {access.callers[c].tenant}, "
                f"scopes {', '.join(sorted(access.callers[c].scopes)) or 'none'}"
            ),
        )
        cfg = ChatConfig(provider=provider, identity="lab", caller=caller,
                         mcp_url=st.text_input("MCP URL", HarnessSettings().mcp_url))  # fmt: skip
    else:
        conf = st.text_input("Connector config (docs/09)", ".data/connect/local.env")
        customer = st.text_input(
            "Customer (set by YOU, never by the model)", "ACC-1001", help="e.g. ACC-1001"
        ).strip()
        cfg = ChatConfig(provider=provider, identity="shared", connector_config=Path(conf),
                         customer=customer or None)  # fmt: skip

    if st.button("New conversation", use_container_width=True):
        st.session_state.chat = chat = ChatState()

    with st.expander("Backend chaos (mock gateway)"):
        c1, c2, c3 = st.columns(3)
        if c1.button("Slow"):
            st.caption(set_chaos({"delay_ms": 5000}))
        if c2.button("Fail"):
            st.caption(set_chaos({"fail_rate": 1.0, "fail_status": 503}))
        if c3.button("Off"):
            st.caption(set_chaos({}))

    st.caption(
        f"🔒 Local only · synthetic data only · tool results are sent to "
        f"{models.LABELS[provider]}. Traces: .data/chat-ui/"
    )

# A new identity, customer, model or server means a new conversation: the old one
# holds the previous identity's data in the model's context.
if st.session_state.fingerprint != cfg.fingerprint():
    if chat.transcript:
        st.info("Setup changed: started a new conversation (no data carried over).")
    st.session_state.chat = chat = ChatState()
    st.session_state.fingerprint = cfg.fingerprint()


# ------------------------------------------------------------------------ rendering
def render_steps(outcome: TurnOutcome) -> None:
    tokens = usage_totals(outcome.events)
    calls = len(outcome.tool_calls)
    with st.expander(
        f"What happened: {calls} tool call(s) · {tokens['prompt_tokens']}+"
        f"{tokens['completion_tokens']} tokens · {outcome.seconds:.1f}s · {outcome.model}"
    ):
        for ev in outcome.events:
            kind = ev["kind"]
            if kind == "llm":
                n = len(ev.get("tool_calls") or [])
                st.markdown(f"🧠 **model step {ev['step']}** → "
                            f"{f'{n} tool call(s)' if n else 'final answer'}")  # fmt: skip
            elif kind == "tool_call":
                st.markdown(f"🔧 **MCP tools/call** `{ev['name']}`")
                st.json(ev.get("arguments") or {})
            elif kind == "tool_result":
                label = "ERROR" if ev.get("is_error") else "result"
                st.markdown(f"↩️ {label} from `{ev['name']}` (as the model saw it)")
                st.code(str(ev.get("content", ""))[:RESULT_PREVIEW], language="json")
            elif kind == "guardrail":
                st.markdown(f"🛡️ {ev['stage']} guardrail `{ev['rule']}`: "
                            f"{ev['action']} ×{ev['count']}")  # fmt: skip
            elif kind == "host_error":
                st.markdown(f"⛔ host refused `{ev['name']}`: {safe_markdown(ev['message'])}")
            elif kind == "confirmation":
                st.markdown(f"🙋 `{ev['name']}` needs confirmation: declined (not in the UI yet)")
            elif kind == "stopped":
                st.warning(ev["reason"])


st.title("📡 Telco MCP lab chat")
who = (f"lab caller **{cfg.caller}**" if cfg.identity == "lab"
       else f"shared client, customer **{cfg.customer or 'none'}**")  # fmt: skip
st.caption(f"{models.LABELS[provider]} · {who} · synthetic data")

for entry in chat.transcript:
    with st.chat_message(entry["role"]):
        if entry["role"] == "user":
            st.markdown(safe_markdown(entry["text"]))
        elif entry.get("error"):
            st.error(entry["error"])
        else:
            st.markdown(safe_markdown(entry["outcome"].answer or "(no answer)"))
            render_steps(entry["outcome"])

if prompt := st.chat_input("Ask about the account, lines or orders…"):
    chat.transcript.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(safe_markdown(prompt))
    with st.chat_message("assistant"):
        with st.spinner("Thinking and calling tools…"):
            try:
                outcome = asyncio.run(run_turn(cfg, chat, prompt))
            except Exception as exc:  # shown to the local user; never includes secrets
                entry = {"role": "assistant", "error": explain(exc)}
            else:
                entry = {"role": "assistant", "outcome": outcome}
        chat.transcript.append(entry)
        if "error" in entry:
            st.error(entry["error"])
        else:
            st.markdown(safe_markdown(entry["outcome"].answer or "(no answer)"))
            render_steps(entry["outcome"])
