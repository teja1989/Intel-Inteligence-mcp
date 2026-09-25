"""Before/after: the prompt-injection note, as the backend stores it and as the model sees it.

    make mocks          # terminal 1
    make demo-injection # terminal 2

Runs the MCP server in-process (as alice) against the mock gateway, twice:
once with MCP_UNSAFE_RAW_FREE_TEXT behaviour (before) and once with shaping (after).
"""

import asyncio
import json

import httpx
from mcp import Client

from telco_mcp_lab.gateway_routes import DomainApi, GatewayRoutes
from telco_mcp_lab.mcp_server.clients.gateway import GatewayClientSettings, GatewayUrls
from telco_mcp_lab.mcp_server.security.caller import AccessModel
from telco_mcp_lab.mcp_server.server import build_server
from telco_mcp_lab.mcp_server.settings import McpServerSettings


async def main() -> None:
    gw = GatewayClientSettings()  # type: ignore[call-arg]
    url = GatewayUrls(gw.base_url, GatewayRoutes()).url(DomainApi.ACCOUNT, "account", "ACC-1001")
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            url, headers={"Authorization": f"Bearer {gw.token.get_secret_value()}"}
        )
    raw = resp.json()
    print("1) BACKEND (Account API) stores this free text:\n")
    print("   " + raw["notes"] + "\n")

    model = AccessModel.load(McpServerSettings().access_config)
    alice = model.context_for("alice", via="in-process")
    for label, unsafe in (("2) BEFORE: raw passthrough", True), ("3) AFTER: shaped", False)):
        server = build_server(
            access_model=model, fallback_caller=alice, unsafe_raw_free_text=unsafe
        )
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
