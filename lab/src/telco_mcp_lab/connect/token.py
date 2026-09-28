"""OAuth 2.0 client-credentials token source (RFC 6749 §4.4) with caching and early refresh.

* The token is cached in memory only (never on disk) and refreshed `refresh_margin_s`
  before it expires. `expires_in` from the response wins; otherwise the JWT's `exp`.
* One refresh at a time: concurrent callers wait for the fetch in progress.
* `token(force=True)` after a 401 gets a new token even if the cached one looks valid
  (revoked, rotated key, clock skew).
* Errors never contain the secret or the token.

Java/Spring equivalent: `OAuth2AuthorizedClientManager` with a `client_credentials`
provider (it caches and refreshes the same way).
"""

import asyncio
import logging
import shlex
import subprocess
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt

from telco_mcp_lab.connect.settings import ConnectSettings, is_local

log = logging.getLogger(__name__)

FALLBACK_LIFETIME_S = 300  # when neither expires_in nor exp is available: be conservative


class TokenError(RuntimeError):
    """A token couldn't be obtained. The message is safe to show and log."""


def http_client(s: ConnectSettings, target: str, **kw: Any) -> httpx.AsyncClient:
    """An httpx client with the connector's TLS/proxy policy for `target`."""
    verify: Any = str(s.ca_bundle) if s.ca_bundle else True
    return httpx.AsyncClient(
        timeout=s.timeout_s,
        verify=verify,
        # A corporate proxy must never capture localhost traffic (docs/05).
        trust_env=s.trust_env and not is_local(target),
        follow_redirects=False,  # a redirect could carry the Authorization header elsewhere
        **kw,
    )


def read_secret(s: ConnectSettings) -> str:
    if s.client_secret is not None:
        return s.client_secret.get_secret_value()
    assert s.secret_command is not None  # noqa: S101 - enforced by settings
    try:
        out = subprocess.run(  # noqa: S603 - the user's own configured command, no shell
            shlex.split(s.secret_command), capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TokenError(
            f"TELCO_MCP_SECRET_COMMAND failed ({type(exc).__name__}); check the keychain entry"
        ) from None
    secret = out.stdout.strip()
    if not secret:
        raise TokenError("TELCO_MCP_SECRET_COMMAND printed nothing")
    return secret


class ClientCredentials:
    def __init__(
        self,
        settings: ConnectSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._s = settings
        self._transport = transport
        self._clock = clock
        self._secret: str | None = None
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.fetches = 0  # observable for tests and `check`

    def _fresh(self) -> bool:
        return (
            self._token is not None and self._clock() < self._expires_at - self._s.refresh_margin_s
        )

    async def token(self, *, force: bool = False) -> str:
        if not force and self._fresh():
            return self._token  # type: ignore[return-value]
        stale = self._token
        async with self._lock:
            # Re-check: another caller may have refreshed while we waited. With force,
            # accept only a token different from the one that was just rejected.
            if self._fresh() and (not force or self._token != stale):
                return self._token  # type: ignore[return-value]
            await self._fetch()
            return self._token  # type: ignore[return-value]

    async def _fetch(self) -> None:
        s = self._s
        if self._secret is None:
            self._secret = read_secret(s)
        data = {"grant_type": "client_credentials"}
        if s.scope:
            data["scope"] = s.scope
        auth = None
        if s.client_auth == "basic":
            auth = httpx.BasicAuth(s.client_id, self._secret)
        else:
            data |= {"client_id": s.client_id, "client_secret": self._secret}
        try:
            async with http_client(s, s.token_url, transport=self._transport) as client:
                r = await client.post(s.token_url, data=data, auth=auth)
        except httpx.HTTPError as exc:
            raise TokenError(f"token service unreachable ({type(exc).__name__})") from None
        body = _json(r)
        if r.status_code != 200 or not isinstance(body.get("access_token"), str):
            error = str(body.get("error", "unknown"))[:60]
            raise TokenError(f"token service refused the request: HTTP {r.status_code} {error}")
        self.fetches += 1
        self._token = body["access_token"]
        self._expires_at = self._clock() + _lifetime(body, self._token)
        log.info("token obtained (valid %d s)", int(self._expires_at - self._clock()))


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _lifetime(body: dict[str, Any], token: str) -> float:
    expires_in = body.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        return float(expires_in)
    try:  # not validating: the server does that; we only read when to refresh
        claims = jwt.decode(token, options={"verify_signature": False})
        return max(0.0, float(claims["exp"]) - time.time())
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return FALLBACK_LIFETIME_S
