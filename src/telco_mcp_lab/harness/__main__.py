"""Run the harness.

    uv run python -m telco_mcp_lab.harness --check                 # connectivity: MCP + model
    uv run python -m telco_mcp_lab.harness "what plans am I on?"   # one question
    uv run python -m telco_mcp_lab.harness                         # interactive chat (REPL)
    uv run python -m telco_mcp_lab.harness --transport http "list lines on ACC-2001"

Needs: `make mocks` running and a model key in .env (CHAT_CLAUDE_*, CHAT_GEMINI_* or
AZURE_OPENAI_*). By default (HARNESS_TRANSPORT=stdio) the harness starts the MCP
server itself, with the scopes in MCP_STDIO_SCOPES. With --transport http it calls a
running server (`make mcp-http`) using the JWT in HARNESS_BEARER_TOKEN (`make token`).
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
from dotenv import dotenv_values
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from telco_mcp_lab.harness import models
from telco_mcp_lab.harness.agent import Agent, deny_all
from telco_mcp_lab.harness.guardrails import Guardrails
from telco_mcp_lab.harness.prompts import load_system_prompt
from telco_mcp_lab.harness.settings import AzureOpenAISettings, HarnessSettings
from telco_mcp_lab.harness.trace import Tracer


async def ask_human(tool: str, args: dict[str, Any]) -> bool:
    prompt = (
        f"\n⚠️  The assistant wants to run a state-changing tool:\n   {tool}({args})\n"
        "   Allow? [y/N] "
    )
    answer = await asyncio.to_thread(input, prompt)
    return answer.strip().lower() in ("y", "yes")


# Only for diagnostics (proxy / CA settings shown by --check).
ENV = {**{k: v for k, v in dotenv_values(".env").items() if v is not None}, **os.environ}
STDIO_SERVER = ["-m", "telco_mcp_lab.mcp_server", "--log-level", "WARNING"]


async def _nothing() -> None:
    return None


def describe(hs: HarnessSettings) -> str:
    if hs.transport == "stdio":
        return "a local stdio server (scopes from MCP_STDIO_SCOPES)"
    return f"{hs.mcp_url} with a bearer token"


def mcp_client(hs: HarnessSettings) -> tuple[Client, Callable[[], Awaitable[None]]]:
    """The MCP client for hs.transport, and what to close afterwards."""
    if hs.transport == "stdio":
        # The child inherits this environment and reads .env itself (GATEWAY_*, MCP_*).
        params = StdioServerParameters(command=sys.executable, args=STDIO_SERVER)
        return Client(params, mode=hs.protocol), _nothing
    if hs.bearer_token is None:
        sys.exit("HARNESS_TRANSPORT=http needs HARNESS_BEARER_TOKEN (get one: make token)")
    headers = {"Authorization": f"Bearer {hs.bearer_token.get_secret_value()}"}
    # trust_env=False: the MCP server is local; only model traffic may use the
    # corporate proxy (HTTPS_PROXY), so don't let it capture localhost calls.
    http = httpx2.AsyncClient(headers=headers, timeout=30, trust_env=False)
    client = Client(streamable_http_client(hs.mcp_url, http_client=http), mode=hs.protocol)
    return client, http.aclose


def diagnose(exc: BaseException) -> str:
    # SDKs wrap the real reason ("Connection error.") - walk the cause chain.
    chain, seen = [], exc
    while seen is not None and len(chain) < 6:
        chain.append(f"{type(seen).__name__}: {seen}")
        seen = seen.__cause__ or seen.__context__
    text = " <- ".join(chain)
    # Most specific first: a proxy's "403" must not be read as Azure's 403.
    hints = {
        "ProxyError": "Proxy refused/failed: check HTTPS_PROXY / AZURE_OPENAI_PROXY "
        "(and proxy credentials, allow-list for *.openai.azure.com).",
        "CERTIFICATE_VERIFY_FAILED": "TLS: your proxy inspects TLS. Set AZURE_OPENAI_CA_BUNDLE "
        "(or SSL_CERT_FILE) to your corporate root CA PEM.",
        # Gemini (google-genai) error codes, before the generic HTTP-status hints below.
        "API_KEY_INVALID": "Gemini rejected the key: check CHAT_GEMINI_API_KEY / GEMINI_API_KEY "
        "(an AI Studio key for the Gemini Developer API, not a Google Cloud key).",
        "RESOURCE_EXHAUSTED": "Quota/rate limit reached for this key or model: wait, or use "
        "another CHAT_GEMINI_MODEL / project quota.",
        "is not found for API version": "Unknown model: set CHAT_GEMINI_MODEL to a model your key "
        "can use.",
        "INVALID_ARGUMENT": "The model API rejected the request. With Gemini this is usually the "
        "model name or a tool schema: send the full message so the adapter can be fixed.",
        "PERMISSION_DENIED": "Key valid but not allowed for this model/region (org policy?).",
        "Name or service not known": "DNS: check the resource name in AZURE_OPENAI_ENDPOINT.",
        "nodename nor servname": "DNS: check the resource name in AZURE_OPENAI_ENDPOINT.",
        "Connection refused": "Not running? Start the mocks (make mocks) and, for "
        "--transport http, the server (make mcp-http).",
        "401": "401: key rejected. Check the provider's key in .env: CHAT_CLAUDE_API_KEY, "
        "CHAT_GEMINI_API_KEY or AZURE_OPENAI_API_KEY "
        "(Azure: try AZURE_OPENAI_AUTH_HEADER=api-key).",
        "403": "403: key valid but not allowed (org/project policy, region, or Azure RBAC).",
        "404": "404: wrong model name (CHAT_CLAUDE_MODEL / CHAT_GEMINI_MODEL) or, for Azure, "
        "AZURE_OPENAI_ENDPOINT (must end /openai/v1/) / deployment name.",
        "ConnectError": "Cannot connect: VPN/proxy? Is HTTPS_PROXY needed on this network?",
        "APIConnectionError": "Cannot reach the model API: check VPN, HTTPS_PROXY and base URL.",
    }
    for needle, hint in hints.items():
        if needle in text:
            return f"{text}\n   → {hint}"
    return text


def explain(exc: BaseException) -> str:
    """diagnose(), after unwrapping async ExceptionGroups down to the real cause."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return diagnose(exc)


async def check(hs: HarnessSettings) -> int:
    ok = True
    print(f"[1/3] MCP server: {describe(hs)} …")
    client, close = mcp_client(hs)
    try:
        async with client as c:
            tools = (await c.list_tools()).tools
            print(
                f"      ✅ protocol {c.session.protocol_version}, tools: {[t.name for t in tools]}"
            )
    except Exception as exc:
        ok = False
        print(f"      ❌ {diagnose(exc)}")
    finally:
        await close()

    print("      prompts/guardrails …")
    try:
        prompt = load_system_prompt(hs.system_prompt_file)
        rails = Guardrails.load(hs.guardrails_file)
        print(f"      ✅ system prompt {hs.system_prompt_file} ({len(prompt)} chars), server "
              f"instructions {'on' if hs.use_server_instructions else 'off'}, guardrails "
              f"{hs.guardrails_file} ({len(rails.input_rules)} input / "
              f"{len(rails.output_rules)} output rules)")  # fmt: skip
    except (ValueError, OSError) as exc:
        ok = False
        print(f"      ❌ {exc}")

    print("[2/3] Model provider settings …")
    try:
        provider = models.resolve(hs.llm)
    except RuntimeError as exc:
        print(f"      ❌ {exc}")
        return 1
    print(f"      ✅ {models.LABELS[provider]}, model {models.model_name(provider)!r}")
    if provider == "azure":
        az = AzureOpenAISettings()  # type: ignore[call-arg]
        proxy = "set" if az.proxy or ENV.get("HTTPS_PROXY") else "none"
        ca = az.ca_bundle or ENV.get("SSL_CERT_FILE") or "system default"
        print(
            f"         endpoint {az.endpoint}, auth header {az.auth_header}, proxy {proxy}, CA {ca}"
        )

    print(f"[3/3] {models.LABELS[provider]} round trip (one tiny request, no tools) …")
    llm = models.build_chat_model(provider)
    try:
        started = time.perf_counter()
        turn = await llm.complete([{"role": "user", "content": "Reply with exactly: OK"}], [])
        print(
            f"      ✅ {turn.content!r} in {time.perf_counter() - started:.1f}s, usage {turn.usage}"
        )
    except Exception as exc:
        ok = False
        print(f"      ❌ {diagnose(exc)}")
    finally:
        await llm.aclose()
    return 0 if ok else 1


async def chat(hs: HarnessSettings, prompt: str | None) -> int:
    try:
        provider = models.resolve(hs.llm)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 1
    llm = models.build_chat_model(provider)
    print(f"model: {models.LABELS[provider]} ({models.model_name(provider)})", file=sys.stderr)
    tracer = Tracer(jsonl=hs.trace_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    client, close = mcp_client(hs)
    try:
        async with client as mcp:
            agent = Agent(
                llm, mcp, tracer, max_steps=hs.max_steps,
                confirm=ask_human if hs.confirm_destructive else deny_all,
                system_prompt=load_system_prompt(hs.system_prompt_file),
                use_server_instructions=hs.use_server_instructions,
                guardrails=Guardrails.load(hs.guardrails_file),
            )  # fmt: skip
            await agent.load_tools()
            if prompt:
                await agent.ask(prompt)
                return 0
            print("Interactive chat. Empty line or Ctrl-D to quit.")
            while True:
                try:
                    line = await asyncio.to_thread(input, "\nyou> ")
                except EOFError:
                    return 0
                if not line.strip():
                    return 0
                await agent.ask(line)
    finally:
        tracer.close()
        await llm.aclose()
        await close()


def main() -> None:
    p = argparse.ArgumentParser(prog="harness")
    p.add_argument("prompt", nargs="?")
    p.add_argument("--check", action="store_true", help="test MCP + model connectivity")
    p.add_argument("--transport", choices=["stdio", "http"], help="override HARNESS_TRANSPORT")
    p.add_argument("--mcp-url", help="override HARNESS_MCP_URL (http)")
    p.add_argument("--protocol", choices=["auto", "legacy"])
    p.add_argument("--llm", choices=["claude", "gemini", "azure"], help="override HARNESS_LLM")
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    given = {
        "transport": a.transport,
        "mcp_url": a.mcp_url,
        "protocol": a.protocol,
        "llm": a.llm,
    }
    overrides = {k: v for k, v in given.items() if v}
    hs = HarnessSettings(**overrides)
    sys.exit(asyncio.run(check(hs) if a.check else chat(hs, a.prompt)))


if __name__ == "__main__":
    main()
