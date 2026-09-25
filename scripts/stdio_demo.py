"""Scripted MCP client over stdio: list tools, call one, show a tool error.

    uv run python scripts/stdio_demo.py                 # 2026-07-28 (stateless) protocol
    uv run python scripts/stdio_demo.py --legacy        # pre-2026 initialize handshake
    uv run python scripts/stdio_demo.py --trace         # also show the raw JSON-RPC wire

Requires the mock gateway (`make mocks`). The client launches the server as a
subprocess, which is exactly what Claude Desktop, an IDE or Inspector does.
"""

import argparse
import asyncio
import json
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

SERVER = [sys.executable, "-m", "telco_mcp_lab.mcp_server"]


def show(title: str, obj: object) -> None:
    print(f"\n=== {title}")
    dump = obj.model_dump(by_alias=True, exclude_none=True, mode="json")  # type: ignore[attr-defined]
    print(json.dumps(dump, indent=2)[:2500])


async def main(legacy: bool, trace: bool) -> None:
    cmd = [sys.executable, "scripts/stdio_trace.py", *SERVER] if trace else SERVER
    params = StdioServerParameters(command=cmd[0], args=cmd[1:])
    async with Client(params, mode="legacy" if legacy else "auto") as client:
        print(f"Negotiated protocol version: {client.session.protocol_version}")
        show("tools/list", await client.list_tools())
        show(
            "tools/call get_account_summary ACC-1001",
            await client.call_tool("get_account_summary", {"account_id": "ACC-1001"}),
        )
        show(
            "tools/call with a bad ID (tool error the model can fix)",
            await client.call_tool("get_account_summary", {"account_id": "ACC-9999"}),
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--legacy", action="store_true", help="force the pre-2026 initialize handshake")
    p.add_argument("--trace", action="store_true", help="route through stdio_trace.py")
    a = p.parse_args()
    asyncio.run(main(a.legacy, a.trace))
