"""The harness's MCP connection and error diagnosis, used by `make model-check`.

By default (HARNESS_TRANSPORT=stdio) the MCP server is started as a child process, with
the scopes in MCP_STDIO_SCOPES. With HARNESS_TRANSPORT=http it calls a running server
(`make mcp-http`) using the JWT in HARNESS_BEARER_TOKEN (`make token`).
"""

import sys
from collections.abc import Awaitable, Callable

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from telco_mcp_lab.harness.settings import HarnessSettings

STDIO_SERVER = ["-m", "telco_mcp", "--log-level", "WARNING"]


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
    # Most specific first: a proxy's "403" must not be read as the model API's 403.
    hints = {
        "ProxyError": "Proxy refused/failed: check HTTPS_PROXY / CHAT_GEMINI_PROXY "
        "(and proxy credentials, allow-list for generativelanguage.googleapis.com).",
        "CERTIFICATE_VERIFY_FAILED": "TLS: your proxy inspects TLS. Set CHAT_GEMINI_CA_BUNDLE "
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
        "Name or service not known": "DNS: check your network / VPN and CHAT_GEMINI_BASE_URL.",
        "nodename nor servname": "DNS: check your network / VPN and CHAT_GEMINI_BASE_URL.",
        "Connection refused": "Not running? Start the mocks (make mocks) and, for "
        "--transport http, the server (make mcp-http).",
        "401": "401: key rejected. Check CHAT_GEMINI_API_KEY in .env.",
        "403": "403: key valid but not allowed (org/project policy or region).",
        "404": "404: wrong model name: check CHAT_GEMINI_MODEL.",
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
