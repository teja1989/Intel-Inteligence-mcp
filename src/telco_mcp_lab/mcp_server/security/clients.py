"""Client registry (config/clients.json): which agent clients may call, and as whom.

A client-credentials token proves WHICH AGENT is calling. It says nothing about
which customer the agent is serving. The registry turns a verified token into a
`CallerContext`:

* Unregistered client_id → no caller at all (every tool hidden, every call denied).
  A valid token from the token service is necessary, not sufficient.
* Effective scopes = (token scopes mapped through `scope_map`) ∩ the client's
  `allowed_scopes`. A token scope we don't know is ignored (fail closed), and a
  token service misconfiguration can't grant more than the registry allows.
* mode `bound`: the client only ever sees its tenant's accounts (e.g. a partner
  or a single-business agent). The customer header is ignored.
* mode `customer_context`: the account(s) come from a request header
  (default `X-Customer-Account-Id`) that the AGENT APPLICATION sets from its
  own session after it has identified the customer. The model never chooses it:
  tool arguments only select among these accounts (tenant guard). No header →
  no accounts → every account lookup is refused.

Trust note: the customer header is an ASSERTION by an authenticated, registered
client, not proof that the customer was verified. That's why only clients
registered as `customer_context` may use it, and why it's audited on every call.
For third-party agents, replace it with a server-verified handle (docs/06 §4, B2).

Java/Spring equivalent: a `Converter<Jwt, AbstractAuthenticationToken>` that
looks up the client and builds authorities, plus a request-scoped bean for the
customer context.
"""

import json
import logging
import re
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from starlette.types import ASGIApp, Receive, Scope, Send

from telco_mcp_lab.ids import ACCOUNT_ID
from telco_mcp_lab.mcp_server.security.caller import AccessModel, CallerContext
from telco_mcp_lab.mcp_server.security.environment import ENVIRONMENTS

log = logging.getLogger(__name__)

MAX_CUSTOMER_ACCOUNTS = 10
_ACCOUNT_RE = re.compile(ACCOUNT_ID)

Mode = Literal["bound", "customer_context"]


@dataclass(frozen=True)
class ClientDef:
    client_id: str
    mode: Mode
    allowed_scopes: frozenset[str]
    tenant: str | None = None


class ClientRegistry:
    def __init__(
        self,
        clients: dict[str, ClientDef],
        scope_map: dict[str, str],
        access_model: AccessModel,
    ) -> None:
        for c in clients.values():
            if c.mode == "bound" and c.tenant not in access_model.tenants:
                raise ValueError(f"client {c.client_id!r}: unknown tenant {c.tenant!r}")
            if c.mode == "customer_context" and c.tenant is not None:
                raise ValueError(f"client {c.client_id!r}: customer_context takes no tenant")
        self.clients = clients
        self.scope_map = scope_map
        self.access_model = access_model
        self.problems: list[str] = []  # production guard findings (see load)

    @classmethod
    def load(
        cls, path: Path, access_model: AccessModel, environment: str = "local"
    ) -> "ClientRegistry":
        """Load the registry for ONE environment.

        An entry may carry `"environments": ["dev", "test"]`. Entries not tagged for
        the current environment are skipped. In production every entry must be
        tagged explicitly and include "production"; anything else is reported in
        `problems`, which the production guard (G2) turns into a startup failure.
        That keeps a lower-env client (e.g. the shared developer client) from ever
        being accepted by production, even if the token service shared an issuer.
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        scope_map = {k: v for k, v in raw.get("scope_map", {}).items() if not k.startswith("_")}
        clients: dict[str, ClientDef] = {}
        problems: list[str] = []
        for cid, v in raw["clients"].items():
            if v["mode"] not in ("bound", "customer_context"):
                raise ValueError(f"client {cid!r}: mode must be bound or customer_context")
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
            scopes = frozenset(v["allowed_scopes"])
            clients[cid] = ClientDef(cid, v["mode"], scopes, v.get("tenant"))
        registry = cls(clients, scope_map, access_model)
        registry.problems = problems
        return registry

    def effective_scopes(self, client: ClientDef, token_scopes: Iterable[str]) -> frozenset[str]:
        mapped = {self.scope_map[s] for s in token_scopes if s in self.scope_map}
        return frozenset(mapped) & client.allowed_scopes

    def context_for(
        self, client_id: str, token_scopes: Iterable[str], customer_header: str | None
    ) -> CallerContext | None:
        client = self.clients.get(client_id)
        if client is None:
            log.warning("unregistered client %r refused", client_id)
            return None
        scopes = self.effective_scopes(client, token_scopes)
        if client.mode == "bound":
            assert client.tenant is not None  # noqa: S101 - enforced in __init__
            return CallerContext(
                client_id, client.tenant, self.access_model.tenants[client.tenant], scopes, "http"
            )
        accounts = parse_customer_header(customer_header)
        if accounts is None:
            log.warning("client %r: malformed customer header ignored", client_id)
            accounts = frozenset()
        return CallerContext(
            client_id,
            "customer-context",
            accounts,
            scopes,
            "http",
            customer=",".join(sorted(accounts)) or None,
        )


def parse_customer_header(value: str | None) -> frozenset[str] | None:
    """Empty set when absent; None when present but malformed (all-or-nothing)."""
    if value is None or not value.strip():
        return frozenset()
    parts = [p.strip() for p in value.split(",")]
    if len(parts) > MAX_CUSTOMER_ACCOUNTS or not all(_ACCOUNT_RE.fullmatch(p) for p in parts):
        return None
    return frozenset(parts)


# ------------------------------------------------------------ the header, per request

_customer_header: ContextVar[str | None] = ContextVar("customer_header", default=None)


def current_customer_header() -> str | None:
    return _customer_header.get()


class CustomerHeaderMiddleware:
    """Copies ONE configured request header into a contextvar for the request's duration.

    Same mechanism the SDK uses for the access token (`get_access_token()`).
    Only this header is kept; nothing else from the request is exposed to tools.
    """

    def __init__(self, app: ASGIApp, header: str) -> None:
        self.app = app
        self.header = header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        values = [v for k, v in scope["headers"] if k == self.header]
        # A repeated header is ambiguous (which one did the gateway check?): treat as malformed.
        value = values[0].decode("latin-1") if len(values) == 1 else ("!" if values else None)
        token = _customer_header.set(value)
        try:
            await self.app(scope, receive, send)
        finally:
            _customer_header.reset(token)
