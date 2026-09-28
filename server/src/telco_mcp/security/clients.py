"""Who is calling, and what may they do: the client and its scopes.

There is ONE identity concept: the **client**, i.e. the agent application calling us.

    HTTP  : Authorization: Bearer <JWT> → jwt_verifier → client_id + token scopes
            → config/clients.json → ClientContext(client_id, effective scopes)
    stdio : no headers exist; the process runs with scopes from MCP_STDIO_SCOPES
            (whoever can start the process already has local trust)

Rules:
* A valid token is necessary, not sufficient: an unregistered client_id gets
  nothing (every tool hidden, every call refused).
* Effective scopes = (token scopes mapped through `scope_map`) ∩ the client's
  `allowed_scopes`. Unknown token scopes are ignored (fail closed).
* An entry may be limited to environments (`"environments": ["dev", "test"]`);
  production refuses to start with entries not tagged for production (G2).

**No customer boundary (decision 2026-09-27):** the account comes from the tool
argument and is passed to the domain API in the URL, like the APIs themselves
work. Any client with `read` can read any account. Accepted risk, compensated by
scopes, PII masking, strict ID formats and an audit record of every account /
line / order touched (docs/08 §1).

Java/Spring equivalent: a `Converter<Jwt, AbstractAuthenticationToken>` that
looks up the client and builds authorities from its scopes.
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from mcp.server.auth.middleware.auth_context import get_access_token

from telco_mcp.security.environment import ENVIRONMENTS

log = logging.getLogger(__name__)


class Scope:
    READ: Final = "read"
    PII_READ: Final = "pii:read"
    ORDER_SUBMIT: Final = "order:submit"


@dataclass(frozen=True)
class ClientContext:
    """The client of the request in progress. Tools use this and nothing else."""

    client_id: str
    scopes: frozenset[str]
    via: str  # "http" | "stdio" | "in-process"

    def has(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass
class ClientRegistry:
    allowed: dict[str, frozenset[str]]  # client_id → allowed scopes
    scope_map: dict[str, str] = field(default_factory=dict)  # token scope → our scope
    problems: list[str] = field(default_factory=list)  # production guard findings

    @classmethod
    def load(cls, path: Path, environment: str = "local") -> "ClientRegistry":
        """Load the entries enabled for ONE environment (see module docstring)."""
        if not path.is_file():
            raise ValueError(
                f"client registry {str(path)!r} not found (MCP_CLIENTS_CONFIG). Locally: "
                "server/config/clients.json; in the image: config/clients.json"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        scope_map = {k: v for k, v in raw.get("scope_map", {}).items() if not k.startswith("_")}
        allowed: dict[str, frozenset[str]] = {}
        problems: list[str] = []
        for cid, v in raw["clients"].items():
            envs = v.get("environments")
            if envs is not None:
                unknown = set(envs) - set(ENVIRONMENTS)
                if unknown or not envs:
                    raise ValueError(f"client {cid!r}: bad environments {sorted(unknown)}")
            if environment == "production" and (envs is None or "production" not in envs):
                problems.append(
                    f"client registry entry {cid!r} is not tagged for production "
                    f"(environments={envs}); remove it or tag it after review"
                )
                continue
            if envs is not None and environment not in envs:
                log.info("client %r not enabled in %s; skipped", cid, environment)
                continue
            allowed[cid] = frozenset(v["allowed_scopes"])
        return cls(allowed, scope_map, problems)

    def context_for(self, client_id: str, token_scopes: Iterable[str]) -> ClientContext | None:
        if client_id not in self.allowed:
            log.warning("unregistered client %r refused", client_id)
            return None
        mapped = {self.scope_map[s] for s in token_scopes if s in self.scope_map}
        return ClientContext(client_id, frozenset(mapped) & self.allowed[client_id], "http")


def resolve_client(
    registry: ClientRegistry | None, fallback: ClientContext | None
) -> ClientContext | None:
    """The client of the current request, or None (which means: refuse everything).

    A verified bearer token always wins. `fallback` is only set for stdio and
    in-process runs; the HTTP entry point never sets it, so a request without a
    valid token can't fall through to a default identity.
    """
    token = get_access_token()
    if token is not None:
        return registry.context_for(token.client_id, token.scopes) if registry else None
    return fallback
