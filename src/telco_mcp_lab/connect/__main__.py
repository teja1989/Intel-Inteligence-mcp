"""Connector CLI (lower environments).

    python -m telco_mcp_lab.connect headers --config FILE  # JSON headers (Claude headersHelper)
    python -m telco_mcp_lab.connect bridge  --config FILE  # stdio MCP server → remote HTTP
    python -m telco_mcp_lab.connect check   --config FILE  # token + tools/list, no secrets

--config FILE is a dotenv file of NON-secret TELCO_MCP_* values. Real environment
variables win. stdout belongs to the protocol (bridge) or the headers (headers):
all logging goes to stderr.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx2
import jwt
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from telco_mcp_lab.connect.bridge import run_stdio
from telco_mcp_lab.connect.settings import ConnectSettings, is_local
from telco_mcp_lab.connect.token import ClientCredentials, TokenError


def load_settings(config: Path | None, customer: str | None) -> ConnectSettings:
    overrides = {"customer": customer} if customer else {}
    try:
        return ConnectSettings(_env_file=config, **overrides)  # type: ignore[call-arg]
    except ValidationError as exc:
        # Field names and messages only: never echo input values (could be a secret).
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'config'}: {e['msg']}"
                             for e in exc.errors())  # fmt: skip
        sys.exit(f"connect: invalid configuration: {problems}")


async def headers(s: ConnectSettings) -> dict[str, str]:
    token = await ClientCredentials(s).token()
    out = {"Authorization": f"Bearer {token}"}
    if s.customer:
        out[s.customer_header] = s.customer
    return out


async def check(s: ConnectSettings) -> None:
    """Get a token, then list tools through the real MCP client. Prints no secrets."""
    h = await headers(s)
    claims = jwt.decode(h["Authorization"][7:], options={"verify_signature": False})
    shown = {k: claims[k] for k in ("iss", "aud", "client_id", "azp", "scope") if k in claims}
    print(f"token ok: {json.dumps(shown)}")
    verify = str(s.ca_bundle) if s.ca_bundle else True
    trust_env = s.trust_env and not is_local(s.url)
    async with (
        httpx2.AsyncClient(
            headers=h, verify=verify, trust_env=trust_env, timeout=s.timeout_s
        ) as http,
        Client(streamable_http_client(s.url, http_client=http)) as c,
    ):
        tools = [t.name for t in (await c.list_tools()).tools]
        version = c.session.protocol_version
    print(f"MCP ok: {s.url} -> protocol {version}, tools {tools}")


def main() -> None:
    p = argparse.ArgumentParser(prog="telco-mcp-connect", description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["headers", "bridge", "check"])
    p.add_argument("--config", type=Path, help="dotenv file with non-secret TELCO_MCP_* values")
    p.add_argument("--customer", help="override TELCO_MCP_CUSTOMER (ACC-1234[,ACC-…])")
    p.add_argument("--log-level", default="WARNING")
    a = p.parse_args()
    logging.basicConfig(stream=sys.stderr, level=a.log_level.upper(),
                        format="%(asctime)s %(levelname)s connect: %(message)s")  # fmt: skip
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it would log URLs per request
    if a.config is not None and not a.config.is_file():
        sys.exit(f"connect: config file not found: {a.config}")
    s = load_settings(a.config, a.customer)
    try:
        if a.command == "headers":
            print(json.dumps(asyncio.run(headers(s))))
        elif a.command == "check":
            asyncio.run(check(s))
        else:
            asyncio.run(run_stdio(s))
    except TokenError as exc:
        sys.exit(f"connect: {exc}")


if __name__ == "__main__":
    main()
