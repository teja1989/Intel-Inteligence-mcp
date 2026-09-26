"""Walk through JWT mode over real HTTP: token checks, client registry, customer context.

    make dev-keys         # once: local RSA key + JWKS (stand-in for the token service)
    make mocks            # terminal 1
    make mcp-http-jwt     # terminal 2: server in JWT mode, trusting the dev JWKS
    make demo-jwt         # terminal 3

Tokens are minted locally with the dev key. Client IDs come from config/clients.json.
"""

import argparse
import asyncio
import json
import os
import time

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from telco_mcp_lab.devtools.token_issuer import DEV_AUDIENCE, DEV_ISSUER, load_key, mint

KEY = load_key()


def tok(client: str, scopes: str = "read", **kw) -> str:
    return mint(KEY, client_id=client, scopes=scopes, issuer=DEV_ISSUER, audience=DEV_AUDIENCE,
                **kw)  # fmt: skip


def short(obj: object, n: int = 200) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


def raw_status(url: str, token: str) -> int:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    return httpx.post(url, json=body, headers=h, timeout=10, trust_env=False).status_code


async def session(url: str, title: str, headers: dict, calls: list[tuple[str, dict]]) -> None:
    print(f"\n### {title}")
    async with (
        httpx2.AsyncClient(headers=headers, trust_env=False) as h,
        Client(streamable_http_client(url, http_client=h)) as c,
    ):
        print(f"    tools/list -> {[t.name for t in (await c.list_tools()).tools]}")
        for name, args in calls:
            r = await c.call_tool(name, args)
            shown = f"ERROR {r.content[0].text}" if r.is_error else short(r.structured_content)
            print(f"    {name}({args})\n      -> {shown}")


def refused_tokens(url: str) -> None:
    base = url.removesuffix("/mcp")
    print(f"healthz -> {httpx.get(base + '/healthz', trust_env=False).json()}")

    print("\n### tokens the server must refuse (expect 401 each)")
    for label, t in [
        ("expired", tok("ops-dashboard", now=time.time() - 3 * 3600)),
        ("wrong audience", mint(KEY, client_id="ops-dashboard", scopes="read",
                                issuer=DEV_ISSUER, audience="https://domain-api.invalid")),
        ("wrong issuer", mint(KEY, client_id="ops-dashboard", scopes="read",
                              issuer="https://evil.invalid", audience=DEV_AUDIENCE)),
        ("24 h lifetime", tok("ops-dashboard", ttl_s=24 * 3600)),
    ]:  # fmt: skip
        print(f"    {label:<15} -> HTTP {raw_status(url, t)}")


async def main(url: str) -> None:
    auth = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731

    await session(url, "ops-dashboard (bound to tenant-a, scope read)",
                  auth(tok("ops-dashboard")),
                  [("get_account_summary", {"account_id": "ACC-1001"}),
                   ("get_account_summary", {"account_id": "ACC-2001"})])  # fmt: skip

    await session(url, "care-agent-internal, customer ACC-1002 set by the agent app",
                  {**auth(tok("care-agent-internal")), "X-Customer-Account-Id": "ACC-1002"},
                  [("get_account_summary", {}),
                   ("get_account_summary", {"account_id": "ACC-1001"})])  # fmt: skip

    await session(url, "care-agent-internal WITHOUT the customer header",
                  auth(tok("care-agent-internal")), [("list_orders", {})])  # fmt: skip

    await session(url, "care-agent-internal asking for pii:read + order:submit (only pii allowed)",
                  {**auth(tok("care-agent-internal", "read pii:read order:submit")),
                   "X-Customer-Account-Id": "ACC-1001"},
                  [("list_subscriptions", {"limit": 1})])  # fmt: skip

    await session(url, "unregistered client with a VALID token",
                  auth(tok("someone-else")), [("list_orders", {})])  # fmt: skip


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("URL", "http://127.0.0.1:8090/mcp"))
    target = p.parse_args().url
    refused_tokens(target)
    asyncio.run(main(target))
