"""The MCP server's side of the API gateway: where to call, and with which token.

Design (Option A, "service token"):
    The MCP server calls the gateway with ITS OWN token. It never forwards the
    token its caller presented. The MCP spec (2026-07-28, Authorization ·
    Security Considerations) says: "The MCP server MUST NOT pass through the
    token it received from the MCP client." The tenant boundary is therefore
    enforced inside the MCP server (security/, Phase 3).

The seam: tools and clients depend on `TokenProvider`, never on where the
token comes from. Today that's `StaticTokenProvider` (from `.env`). Later an
OAuth2 client-credentials provider (fetch, cache, refresh before expiry) can
replace it without changing any tool code.

Java/Spring equivalent: `OAuth2AuthorizedClientManager` (client_credentials)
plugged into a `RestClient` via `OAuth2ClientHttpRequestInterceptor`.
"""

from collections.abc import Generator
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from telco_mcp_lab.gateway_routes import DomainApi, GatewayRoutes

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class GatewayClientSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Safety interlock: this lab must only ever talk to the MOCK gateway
    # (synthetic data only). Pointing it anywhere else requires opting in.
    # Declared before base_url so the validator below can read it.
    allow_non_local: bool = False
    base_url: str = "http://127.0.0.1:8081"
    token: SecretStr = Field(min_length=16)

    # A *field* validator, not a model validator, on purpose: pydantic puts the
    # validated input into the error message. For a model validator that input
    # is the whole settings dict, raw token included. Here it's just the URL.
    @field_validator("base_url")
    @classmethod
    def _guard_target(cls, url: str, info: ValidationInfo) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("GATEWAY_BASE_URL must be an http(s) URL with a host.")
        if parts.hostname in _LOCAL_HOSTS:
            return url
        if not info.data.get("allow_non_local", False):
            raise ValueError(
                "GATEWAY_BASE_URL points at a non-local host. This lab uses synthetic data "
                "and must only call the mock gateway. Set GATEWAY_ALLOW_NON_LOCAL=true only "
                "if you really mean it."
            )
        if parts.scheme != "https":
            raise ValueError("Non-local gateways must use https (the token would travel in clear).")
        return url


class TokenProvider(Protocol):
    """Anything that can hand out the current gateway access token."""

    def get_token(self) -> str: ...


class StaticTokenProvider:
    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def get_token(self) -> str:
        return self._token.get_secret_value()

    def __repr__(self) -> str:  # never print the secret, even in debug logs
        return "StaticTokenProvider(token=***)"


class GatewayBearerAuth(httpx.Auth):
    """httpx auth hook: adds `Authorization: Bearer …` to every request.

    Works for both `httpx.Client` and `httpx.AsyncClient`. The token is fetched
    per request, so a refreshing provider takes effect immediately.
    """

    def __init__(self, provider: TokenProvider) -> None:
        self._provider = provider

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {self._provider.get_token()}"
        yield request


class GatewayUrls:
    """Builds `/{microservice}/API/{resource}/{id}` URLs safely.

    Every dynamic segment is percent-encoded with no safe characters, so a
    value like `../../boorder/API/order` can never escape its segment. Tools
    validate IDs against strict patterns as well; this is defence in depth.
    """

    def __init__(self, base_url: str, routes: GatewayRoutes) -> None:
        self._base = base_url.rstrip("/")
        self._routes = routes

    def url(self, api: DomainApi, resource: str, *ids: str) -> str:
        if any(i in ("", ".", "..") for i in ids):
            raise ValueError("Empty or dot path segments are not allowed.")
        path = "/".join([resource, *(quote(i, safe="") for i in ids)])
        return f"{self._base}{self._routes.prefix(api)}/{path}"
