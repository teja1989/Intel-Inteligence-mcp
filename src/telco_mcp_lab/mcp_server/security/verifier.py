"""Bearer-token verification: THE seam for real authentication.

`StaticTokenVerifier` implements the SDK's `TokenVerifier` protocol
(`verify_token(token) -> AccessToken | None`). To go to production, write a
`JwtTokenVerifier` with the same method: validate signature (JWKS), `iss`,
`exp`, and `aud` (this MCP server's URL, RFC 8707), then map claims to
`AccessToken(client_id=…, scopes=…, claims=…)`. Nothing else changes: not the
tools, not the tenant guard, not the tests that use CallerContext.

Lab tokens live only in the environment / `.env` as `MCP_TOKEN_<CALLER>`
(e.g. MCP_TOKEN_ALICE). They are held as SHA-256 digests, so the plaintext
isn't kept around in a dict for a heap dump or a debug print to reveal.

Java/Spring equivalent: `spring-boot-starter-oauth2-resource-server` with a
`JwtDecoder` (issuer + audience validators). The static variant would be a
custom `AuthenticationProvider`.
"""

import hashlib
import hmac
import logging
from collections.abc import Mapping

from mcp.server.auth.provider import AccessToken

from telco_mcp_lab.mcp_server.security.caller import AccessModel

log = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 24


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class StaticTokenVerifier:
    def __init__(self, model: AccessModel, env: Mapping[str, str]) -> None:
        self._model = model
        self._by_digest: dict[bytes, str] = {}
        for caller_id in model.callers:
            token = env.get(f"MCP_TOKEN_{caller_id.upper()}", "")
            if not token:
                log.warning(
                    "no MCP_TOKEN_%s set: caller %r cannot log in", caller_id.upper(), caller_id
                )
                continue
            if len(token) < MIN_TOKEN_LENGTH:
                raise ValueError(
                    f"MCP_TOKEN_{caller_id.upper()} must be at least {MIN_TOKEN_LENGTH} chars"
                )
            digest = _digest(token)
            if digest in self._by_digest:
                raise ValueError("two callers share the same token")
            self._by_digest[digest] = caller_id

    @property
    def enabled_callers(self) -> list[str]:
        return sorted(self._by_digest.values())

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = _digest(token)
        caller_id = None
        # Compare against every digest with compare_digest, so timing doesn't
        # reveal which (if any) entry matched.
        for digest, cid in self._by_digest.items():
            if hmac.compare_digest(presented, digest):
                caller_id = cid
        if caller_id is None:
            return None
        caller = self._model.callers[caller_id]
        return AccessToken(
            token="[redacted]",  # noqa: S106 - placeholder: never keep the raw token around
            client_id=caller_id,
            subject=caller_id,
            scopes=sorted(caller.scopes),
            claims={"tenant": caller.tenant},
        )
