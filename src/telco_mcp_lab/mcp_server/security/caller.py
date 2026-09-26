"""CallerContext: the single source of truth for "who is calling and what may they touch".

Tools receive a `CallerContext` and never derive identity, tenant or scopes
from their arguments. That's the whole point of this module.

    HTTP  : Authorization: Bearer <token> -> TokenVerifier -> AccessToken -> CallerContext
    stdio : no headers exist, so a FIXED identity from config (MCP_STDIO_CALLER),
            because whoever can spawn the process already has that user's trust.

Java/Spring equivalent: `SecurityContextHolder.getContext().getAuthentication()`,
with a `JwtAuthenticationConverter` mapping claims to authorities (= scopes).
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from mcp.server.auth.middleware.auth_context import get_access_token

if TYPE_CHECKING:
    from telco_mcp_lab.mcp_server.security.clients import ClientRegistry

log = logging.getLogger(__name__)


class Scope:
    READ: Final = "read"
    ORDER_SUBMIT: Final = "order:submit"
    PII_READ: Final = "pii:read"


@dataclass(frozen=True)
class CallerContext:
    caller_id: str
    tenant: str
    account_ids: frozenset[str]
    scopes: frozenset[str]
    via: str  # "http" | "stdio" | "in-process"
    # customer_context clients only: the account(s) the agent app said it acts for (audited).
    customer: str | None = None

    def has(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class CallerDef:
    caller_id: str
    tenant: str
    scopes: frozenset[str]


class AccessModel:
    """Tenants -> accounts and callers -> (tenant, scopes). Loaded from config/access.json.

    Non-secret. In production the tenant and scopes arrive as signed JWT claims,
    and tenant -> accounts comes from a customer/entitlement service.
    """

    def __init__(self, tenants: dict[str, frozenset[str]], callers: dict[str, CallerDef]) -> None:
        for c in callers.values():
            if c.tenant not in tenants:
                raise ValueError(f"caller {c.caller_id!r} references unknown tenant {c.tenant!r}")
        self.tenants = tenants
        self.callers = callers

    @classmethod
    def load(cls, path: Path) -> "AccessModel":
        raw = json.loads(path.read_text(encoding="utf-8"))
        tenants = {t: frozenset(v["accounts"]) for t, v in raw["tenants"].items()}
        callers = {
            name: CallerDef(name, v["tenant"], frozenset(v["scopes"]))
            for name, v in raw["callers"].items()
        }
        return cls(tenants, callers)

    def context_for(self, caller_id: str, via: str) -> CallerContext:
        c = self.callers[caller_id]
        return CallerContext(c.caller_id, c.tenant, self.tenants[c.tenant], c.scopes, via)


def resolve_caller(
    model: AccessModel,
    fallback: CallerContext | None,
    registry: "ClientRegistry | None" = None,
) -> CallerContext | None:
    """The caller for the current request, or None (which means deny everything).

    The bearer token always wins. `fallback` is only ever set for stdio and
    in-process runs; the HTTP entry point never sets it, so a request without a
    valid token can't fall through to a default identity.
    """
    token = get_access_token()
    if token is not None and registry is not None:  # jwt mode: clients.json decides
        from telco_mcp_lab.mcp_server.security.clients import current_customer_header

        return registry.context_for(token.client_id, token.scopes, current_customer_header())
    if token is not None:
        caller_id = token.client_id
        if caller_id not in model.callers:  # verifier and model out of sync: fail closed
            log.error("token for unknown caller %r rejected", caller_id)
            return None
        # Scopes come from the verified token (what was granted), not from config.
        base = model.context_for(caller_id, via="http")
        return CallerContext(
            base.caller_id, base.tenant, base.account_ids, frozenset(token.scopes), "http"
        )
    return fallback
