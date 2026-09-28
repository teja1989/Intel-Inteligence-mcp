"""Before/after: the prompt-injection note, as the backend stores it and as the model sees it.

    make mocks          # terminal 1
    make demo-injection # terminal 2

Runs the MCP server in-process (scope read) against the mock gateway, twice:
once with MCP_UNSAFE_RAW_FREE_TEXT behaviour (before) and once with shaping (after).
"""

import asyncio
import json

import httpx
from mcp import Client

from telco_mcp.clients.gateway import GatewayClientSettings
from telco_mcp.endpoints import GatewayEndpoints
from telco_mcp.security.clients import ClientContext, Scope
from telco_mcp.server import build_server


async def main() -> None:
    gw = GatewayClientSettings()  # type: ignore[call-arg]
    url = gw.base_url + GatewayEndpoints().resolve("get_account", account_id="ACC-1001").path
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            url, headers={"Authorization": f"Bearer {gw.token.get_secret_value()}"}
        )
    raw = resp.json()
    print("1) BACKEND (Account API) stores this free text:\n")
    print("   " + raw["notes"] + "\n")

    local = ClientContext("demo", frozenset({Scope.READ}), "in-process")
    for label, unsafe in (("2) BEFORE: raw passthrough", True), ("3) AFTER: shaped", False)):
        server = build_server(fallback_client=local, unsafe_raw_free_text=unsafe)
        async with Client(server) as c:
            r = await c.call_tool("get_account_summary", {"account_id": "ACC-1001"})
        print(f"{label}: what the MODEL receives in get_account_summary.notes\n")
        print("   " + json.dumps(r.structured_content["notes"], indent=2).replace("\n", "\n   "))
        print()
    print(
        "Heuristic shaping is defence in depth only. The real controls: least-privilege scopes,\n"
        "server-minted drafts and human confirmation for writes (Phases 4-5)."
    )


if __name__ == "__main__":
    asyncio.run(main())
