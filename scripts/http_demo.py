"""Walk through Phase 3 over real HTTP: identities, scopes, tenant guard, masking, injection.

    make mocks            # terminal 1
    make mcp-http         # terminal 2 (or: make mcp-cluster for 2 replicas + LB on :8099)
    make demo-http        # terminal 3   (URL=http://127.0.0.1:8099/mcp for the cluster)

Tokens come from .env (MCP_TOKEN_*). Nothing here bypasses the server's security.
"""

import argparse
import asyncio
import json
import os

import httpx2
from dotenv import dotenv_values
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ENV = {**dotenv_values(".env"), **os.environ}


def short(obj: object, n: int = 260) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


async def as_caller(url: str, caller: str, mode: str, calls: list[tuple[str, dict]]) -> None:
    token = ENV.get(f"MCP_TOKEN_{caller.upper()}")
    if not token:
        print(f"\n### {caller}: no MCP_TOKEN_{caller.upper()} in .env (run: make env-tokens)")
        return
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as h,
        Client(streamable_http_client(url, http_client=h), mode=mode) as c,
    ):
        tools = [t.name for t in (await c.list_tools()).tools]
        print(f"\n### {caller} ({mode}, protocol {c.session.protocol_version})")
        print(f"    tools/list -> {tools}")
        for name, args in calls:
            r = await c.call_tool(name, args)
            shown = f"ERROR {r.content[0].text}" if r.is_error else short(r.structured_content)
            print(f"    {name}({args})\n      -> {shown}")


async def main(url: str) -> None:
    await as_caller(
        url,
        "alice",
        "auto",
        [
            ("get_account_summary", {}),  # two accounts: must ask
            ("get_account_summary", {"account_id": "ACC-1001"}),  # masked name, notes withheld
            ("get_account_summary", {"account_id": "ACC-2001"}),  # tenant B: refused
            ("get_order_status", {"order_id": "ORD-000456"}),  # tenant B's order: refused
            ("get_order_status", {"order_id": "ORD-999999"}),  # nonexistent: SAME message
        ],
    )
    await as_caller(url, "bob", "legacy", [("list_subscriptions", {"limit": 2})])
    await as_caller(
        url, "carol", "auto", [("list_subscriptions", {"account_id": "ACC-1001", "limit": 1})]
    )
    await as_caller(url, "mallory", "auto", [("list_orders", {})])


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("URL", "http://127.0.0.1:8090/mcp"))
    asyncio.run(main(p.parse_args().url))
