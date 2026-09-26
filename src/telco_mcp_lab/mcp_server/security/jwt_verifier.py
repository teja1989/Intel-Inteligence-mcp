"""JWT access-token validation for tokens from the internal token service.

Implements the SDK's `TokenVerifier` protocol, the same seam as the lab's
`StaticTokenVerifier`, so nothing downstream changes. The checks are in order,
and every failure returns None, which the SDK turns into `401 invalid_token`:

1. Header: `alg` must be on the configured allow-list (asymmetric only, so
   `none` and HS256 key-confusion tokens are refused before any crypto), and
   `kid` must name a key in the token service's JWKS.
2. Signature, with that key and ONLY that algorithm.
3. Claims: `iss` exact match, `aud` must contain this server's audience
   (RFC 8707: a token minted for another API is useless here), `exp`/`nbf`
   with a small leeway, required claims present, and a lifetime (exp - iat)
   no longer than the token service issues (2 h). A token claiming a longer
   life wasn't minted by the normal flow.
4. Identity: the client ID from the first configured claim present.

Scopes are passed through as the token states them. Mapping them to server
permissions, and cutting them down to what the client is registered for,
happens in `ClientRegistry` (security/clients.py), not here.

The reason for a rejection is logged (never the token itself); the caller only
sees the generic 401.

Java/Spring equivalent: `NimbusJwtDecoder.withJwkSetUri(...)` plus
`JwtValidators.createDefaultWithIssuer(...)` and an audience validator, via
`spring-boot-starter-oauth2-resource-server`.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

from telco_mcp_lab.mcp_server.settings import JwtSettings

log = logging.getLogger(__name__)

MAX_TOKEN_BYTES = 8192  # far above any real access token; bounds parsing work


class JwksUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SigningKey:
    jwk: jwt.PyJWK
    declared_alg: str | None  # the JWK's own "alg", if the token service states one


class JwksCache:
    """The token service's public keys, by `kid`, refreshed on a TTL and on unknown kids.

    Fetched with our own httpx client (not PyJWKClient, which uses urllib and
    silently honours proxy environment variables) so the proxy and CA settings
    are explicit. A refresh caused by an unknown `kid` is rate-limited, so a
    stream of garbage tokens can't make us hammer the token service.

    Refreshes are single-flight: concurrent requests (typically right after a
    deploy or restart) wait for the one fetch in progress instead of seeing an
    empty cache and failing with 401.
    """

    COLD_RETRY_S = 5.0  # with NO keys at all, retry a failed fetch this soon

    def __init__(
        self,
        settings: JwtSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s = settings
        self._transport = transport
        self._clock = clock
        self._keys: dict[str, SigningKey] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def _load_document(self) -> dict[str, Any]:
        if self._s.jwks_file is not None:
            return json.loads(self._s.jwks_file.read_text(encoding="utf-8"))
        verify: Any = str(self._s.jwks_ca_bundle) if self._s.jwks_ca_bundle else True
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._s.jwks_timeout_s,
            trust_env=self._s.jwks_trust_env,
            verify=verify,
            follow_redirects=False,
        ) as client:
            response = await client.get(self._s.jwks_url or "")
            response.raise_for_status()
            return response.json()

    async def _refresh(self) -> None:
        self._fetched_at = self._clock()  # set first: a failing fetch is rate-limited too
        try:
            document = await self._load_document()
            keys: dict[str, SigningKey] = {}
            for entry in document.get("keys", []):
                kid = entry.get("kid")
                if not kid or entry.get("use", "sig") != "sig":
                    continue
                try:
                    keys[kid] = SigningKey(jwt.PyJWK(entry), entry.get("alg"))
                except jwt.PyJWKError as exc:  # unsupported key type: skip, keep the rest
                    log.warning("jwks: skipping key %r: %s", kid, exc)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            kind = type(exc).__name__
            log.error("jwks: fetch failed (%s); keeping %d cached keys", kind, len(self._keys))
            if not self._keys:
                raise JwksUnavailable from exc
            return
        self._keys = keys
        log.info("jwks: loaded %d signing keys", len(keys))

    def _needs_refresh(self, kid: str) -> bool:
        if self._fetched_at is None:
            return True
        age = self._clock() - self._fetched_at
        if age >= self._s.jwks_cache_s:
            return True
        if kid in self._keys:
            return False
        # Unknown kid: possibly a key rotation. Rate-limited; sooner while we have no keys.
        wait = (
            self._s.jwks_min_refresh_s
            if self._keys
            else min(self.COLD_RETRY_S, self._s.jwks_min_refresh_s)
        )
        return age >= wait

    async def key_for(self, kid: str) -> "SigningKey | None":
        # Also wait when a fetch is already in flight and we don't know this kid yet:
        # it may be exactly the fetch that brings it.
        if self._needs_refresh(kid) or (kid not in self._keys and self._lock.locked()):
            async with self._lock:
                if self._needs_refresh(kid):  # re-check: a waiter may find it already done
                    await self._refresh()
        return self._keys.get(kid)


def parse_scopes(value: Any) -> list[str] | None:
    """`scope` as an RFC 8693/9068 space-separated string, or a JSON list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [s for v in value for s in v.split()]
    return None


class JwtTokenVerifier:
    def __init__(self, settings: JwtSettings, keys: JwksCache | None = None) -> None:
        self._s = settings
        self._keys = keys or JwksCache(settings)

    def _reject(self, reason: str, **fields: Any) -> None:
        extra = "".join(f" {k}={v!r}" for k, v in fields.items())
        log.warning("token rejected: %s%s", reason, extra)

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > MAX_TOKEN_BYTES:
            self._reject("too large")
            return None
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            self._reject("malformed")
            return None
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in self._s.algorithms:
            self._reject("algorithm not allowed", alg=alg)
            return None
        if not isinstance(kid, str) or not kid:
            self._reject("no kid")
            return None
        try:
            key = await self._keys.key_for(kid)
        except JwksUnavailable:
            self._reject("signing keys unavailable")
            return None
        if key is None:
            self._reject("unknown kid", kid=kid)
            return None
        if key.declared_alg is not None and key.declared_alg != alg:
            self._reject("alg does not match the key", alg=alg, kid=kid)
            return None

        try:
            claims = jwt.decode(
                token,
                key=key.jwk.key,
                algorithms=[alg],
                audience=self._s.audience,
                issuer=self._s.issuer,
                leeway=self._s.leeway_s,
                options={"require": list(self._s.required_claims)},
            )
        except jwt.ExpiredSignatureError:
            self._reject("expired")
            return None
        except jwt.PyJWTError as exc:
            self._reject(type(exc).__name__)
            return None

        iat, exp = claims.get("iat"), claims.get("exp")
        if isinstance(iat, int | float) and isinstance(exp, int | float):
            if exp - iat > self._s.max_lifetime_s:
                self._reject("lifetime too long", lifetime_s=int(exp - iat))
                return None
            if iat > time.time() + self._s.leeway_s:
                self._reject("issued in the future")
                return None

        client_id = next(
            (claims[c] for c in self._s.client_id_claims if isinstance(claims.get(c), str)), None
        )
        if not client_id:
            self._reject("no client id claim", looked_for=self._s.client_id_claims)
            return None
        scopes = parse_scopes(claims.get(self._s.scope_claim))
        if scopes is None:
            self._reject("unreadable scope claim", client=client_id)
            return None

        return AccessToken(
            token="[redacted]",  # noqa: S106 - placeholder: never keep the raw token around
            client_id=client_id,
            subject=claims.get("sub") if isinstance(claims.get("sub"), str) else None,
            scopes=sorted(set(scopes)),
            expires_at=int(exp) if isinstance(exp, int | float) else None,
            # Only non-sensitive claims useful for audit/debugging.
            claims={k: claims[k] for k in ("jti", "iss") if k in claims},
        )
