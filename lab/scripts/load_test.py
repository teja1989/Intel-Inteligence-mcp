"""Load test: N tools/call requests over HTTP with C in flight, JWT from the dev keys.

    make load-test                       # 2000 calls, 20 concurrent, http://127.0.0.1:8090/mcp
    make load-test N=5000 C=50 URL=http://127.0.0.1:8099/mcp   # e.g. the 2-replica cluster

Needs `make mocks`, `make mcp-http` (or mcp-cluster / docker-run) and `make dev-keys`.
Prints throughput, error count and latency percentiles. Numbers from the mock gateway
show the MCP server's own overhead only: repeat against the real lower-env APIs.
"""

import argparse
import asyncio
import random
import time

import httpx

from telco_mcp_lab.devtools.token_issuer import DEV_AUDIENCE, DEV_ISSUER, load_key, mint

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
TOOLS = ["get_account_summary", "list_subscriptions", "list_orders"]
ACCOUNTS = ["ACC-1001", "ACC-1002", "ACC-2001"]


async def run(url: str, n: int, concurrency: int) -> None:
    token = mint(load_key(), client_id="lowerenv-shared", scopes="read",
                 issuer=DEV_ISSUER, audience=DEV_AUDIENCE)  # fmt: skip
    latencies: list[float] = []
    errors = 0
    gate = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(timeout=30, trust_env=False, limits=limits) as client:

        async def one(i: int) -> None:
            nonlocal errors
            tool, account = random.choice(TOOLS), random.choice(ACCOUNTS)  # noqa: S311
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": tool,
            }
            params = {"name": tool, "arguments": {"account_id": account}, "_meta": META}
            body = {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": params}
            async with gate:
                started = time.perf_counter()
                try:
                    r = await client.post(url, json=body, headers=headers)
                    if r.status_code != 200 or r.json()["result"].get("isError"):
                        errors += 1
                except httpx.HTTPError:
                    errors += 1
                latencies.append(time.perf_counter() - started)

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(n)))
        elapsed = time.perf_counter() - t0
    latencies.sort()

    def pct(p: float) -> float:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] * 1000

    rps = n / elapsed
    print(f"{n} calls, {concurrency} concurrent, {elapsed:.1f}s: {rps:.0f} req/s "
          f"(= {rps * 60:.0f}/min), errors={errors}, "
          f"p50={pct(0.5):.0f}ms p95={pct(0.95):.0f}ms p99={pct(0.99):.0f}ms")  # fmt: skip


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8090/mcp")
    p.add_argument("-n", type=int, default=2000)
    p.add_argument("-c", type=int, default=20)
    a = p.parse_args()
    asyncio.run(run(a.url, a.n, a.c))
