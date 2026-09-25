"""Run the harness.

    uv run python -m telco_mcp_lab.harness --check                 # connectivity: MCP + Azure
    uv run python -m telco_mcp_lab.harness "what plans am I on?"   # one question
    uv run python -m telco_mcp_lab.harness                         # interactive chat (REPL)
    uv run python -m telco_mcp_lab.harness --caller bob "list my lines"

Needs: `make mocks` + `make mcp-http` running, and AZURE_OPENAI_* in .env.
The harness logs in to the MCP server as HARNESS_CALLER using MCP_TOKEN_<CALLER>.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Any

import httpx2
from dotenv import dotenv_values
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from telco_mcp_lab.harness.agent import Agent, deny_all
from telco_mcp_lab.harness.guardrails import Guardrails
from telco_mcp_lab.harness.llm import AzureChatModel
from telco_mcp_lab.harness.prompts import load_system_prompt
from telco_mcp_lab.harness.settings import AzureOpenAISettings, HarnessSettings
from telco_mcp_lab.harness.trace import Tracer

ENV = {**{k: v for k, v in dotenv_values(".env").items() if v is not None}, **os.environ}


async def ask_human(tool: str, args: dict[str, Any]) -> bool:
    prompt = (
        f"\n⚠️  The assistant wants to run a state-changing tool:\n   {tool}({args})\n"
        "   Allow? [y/N] "
    )
    answer = await asyncio.to_thread(input, prompt)
    return answer.strip().lower() in ("y", "yes")


def mcp_client(hs: HarnessSettings) -> tuple[Client, httpx2.AsyncClient]:
    token = ENV.get(f"MCP_TOKEN_{hs.caller.upper()}")
    if not token:
        sys.exit(f"No MCP_TOKEN_{hs.caller.upper()} in .env (run: make env-tokens)")
    # trust_env=False: the MCP server is local; only Azure traffic may use the
    # corporate proxy (HTTPS_PROXY), so don't let it capture localhost calls.
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=30, trust_env=False
    )
    return Client(streamable_http_client(hs.mcp_url, http_client=http), mode=hs.protocol), http


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
        "Name or service not known": "DNS: check the resource name in AZURE_OPENAI_ENDPOINT.",
        "nodename nor servname": "DNS: check the resource name in AZURE_OPENAI_ENDPOINT.",
        "Connection refused": "MCP server not running? Start: make mocks && make mcp-http",
        "401": "401: key rejected. Check AZURE_OPENAI_API_KEY; "
        "try AZURE_OPENAI_AUTH_HEADER=api-key.",
        "403": "403: key valid but not allowed (network rules / RBAC on the resource?).",
        "404": "404: wrong AZURE_OPENAI_ENDPOINT (must end /openai/v1/) or deployment name.",
        "ConnectError": "Cannot connect: VPN/proxy? Is HTTPS_PROXY needed on this network?",
        "APIConnectionError": "Cannot reach Azure: check endpoint, VPN and HTTPS_PROXY.",
    }
    for needle, hint in hints.items():
        if needle in text:
            return f"{text}\n   → {hint}"
    return text


async def check(hs: HarnessSettings) -> int:
    ok = True
    print(f"[1/3] MCP server {hs.mcp_url} as {hs.caller!r} …")
    client, http = mcp_client(hs)
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
        await http.aclose()

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

    print("[2/3] Azure OpenAI settings …")
    try:
        az = AzureOpenAISettings()  # type: ignore[call-arg]
        proxy = "set" if az.proxy or ENV.get("HTTPS_PROXY") else "none"
        ca = az.ca_bundle or ENV.get("SSL_CERT_FILE") or "system default"
        print(f"      ✅ endpoint {az.endpoint}, deployment {az.deployment!r}")
        print(f"         auth header {az.auth_header}, proxy {proxy}, CA bundle {ca}")
    except ValidationError as exc:
        print(f"      ❌ {exc}")
        return 1

    print("[3/3] Azure OpenAI round trip (one tiny completion, no tools) …")
    llm = AzureChatModel(az)
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
    llm = AzureChatModel(AzureOpenAISettings())  # type: ignore[call-arg]
    tracer = Tracer(jsonl=hs.trace_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    client, http = mcp_client(hs)
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
        await http.aclose()


def main() -> None:
    p = argparse.ArgumentParser(prog="harness")
    p.add_argument("prompt", nargs="?")
    p.add_argument("--check", action="store_true", help="test MCP + Azure connectivity")
    p.add_argument("--caller", help="override HARNESS_CALLER (alice, bob, carol, mallory)")
    p.add_argument("--mcp-url", help="override HARNESS_MCP_URL")
    p.add_argument("--protocol", choices=["auto", "legacy"])
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    overrides = {k: v for k, v in {"caller": a.caller, "mcp_url": a.mcp_url,
                                   "protocol": a.protocol}.items() if v}  # fmt: skip
    hs = HarnessSettings(**overrides)
    sys.exit(asyncio.run(check(hs) if a.check else chat(hs, a.prompt)))


if __name__ == "__main__":
    main()
