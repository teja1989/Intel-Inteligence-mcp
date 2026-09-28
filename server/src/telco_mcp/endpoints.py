"""The gateway endpoint catalogue: every downstream call the server makes, in one place.

Each operation has a path template, e.g.

    get_account   /boaccount/API/account/{account_id}
    list_orders   /boorder/API/order?account_id={account_id}&limit={limit}&cursor={cursor}

The defaults are the lab mock's paths. Each environment overrides what differs with
one environment variable per operation (Cloud Foundry manifest, Kubernetes ConfigMap):

    GATEWAY_ENDPOINT_GET_ACCOUNT=/customer/v2/accounts/{account_id}
    GATEWAY_ENDPOINT_LIST_SUBSCRIPTIONS=/customer/v2/accounts/{account_id}/lines?state={status}&pageSize={limit}&pageToken={cursor}

The host is always GATEWAY_BASE_URL (one gateway per environment).

Rules, checked at startup (a bad template stops the process with a clear message):
* A template is a PATH, never a URL: no scheme, no host, no `//`, `.`/`..` segments,
  `#` or `%`. So configuration can't send requests, or the server's gateway token,
  to another host.
* It must use exactly the placeholders its operation defines, each once. A real API
  that lacks a parameter (e.g. no status filter) is a code change, never a silent drop.
* Query parameters are `name={placeholder}` or `name=literal`; a placeholder whose value
  is None is left out of the request.
* Values are percent-encoded when substituted, so an ID can't escape its segment.
* The HTTP method belongs to the operation in code, not to configuration: it decides
  retry safety (reads only, clients/resilience.py).
* Production requires every template to be set explicitly (startup guard G2): the
  defaults are the mock's paths.

Java/Spring equivalent: `@ConfigurationProperties("gateway.endpoints")` with a
`Map<String, String>` of URI templates, expanded with `UriComponentsBuilder`
(which also encodes path variables).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote

from pydantic import PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DomainApi(StrEnum):
    """Backend APIs. Each gets its own circuit breaker, and names logs and metrics."""

    ACCOUNT = "account"
    SUBSCRIPTION = "subscription"
    SERVICE = "service"
    ORDER = "order"


@dataclass(frozen=True)
class Operation:
    api: DomainApi
    # Reads only today; writes arrive with the order flow (docs/archive/91-…-submit.md).
    method: Literal["GET"]
    params: frozenset[str]  # placeholders the template must use, each exactly once
    default: str  # the lab mock's path


def _op(api: DomainApi, default: str, *params: str) -> Operation:
    return Operation(api, "GET", frozenset(params), default)


OPERATIONS: dict[str, Operation] = {
    "get_account": _op(
        DomainApi.ACCOUNT, "/boaccount/API/account/{account_id}", "account_id"
    ),
    "list_subscriptions": _op(
        DomainApi.SUBSCRIPTION,
        "/bosubscription/API/subscription"
        "?account_id={account_id}&status={status}&limit={limit}&cursor={cursor}",
        "account_id", "status", "limit", "cursor",
    ),
    "get_subscription": _op(
        DomainApi.SUBSCRIPTION,
        "/bosubscription/API/subscription/{subscription_id}",
        "subscription_id",
    ),
    "get_service": _op(DomainApi.SERVICE, "/boservice/API/service/{service_id}", "service_id"),
    "get_order": _op(DomainApi.ORDER, "/boorder/API/order/{order_id}", "order_id"),
    "list_orders": _op(
        DomainApi.ORDER,
        "/boorder/API/order?account_id={account_id}&limit={limit}&cursor={cursor}",
        "account_id", "limit", "cursor",
    ),
}  # fmt: skip

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_PATH_CHARS = re.compile(r"^/[A-Za-z0-9._~\-/:{}]*$")  # no %, spaces, ?, #, @
_QUERY_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_QUERY_LITERAL = re.compile(r"^[A-Za-z0-9_.\-,]{0,64}$")


def env_var(name: str) -> str:
    return f"GATEWAY_ENDPOINT_{name.upper()}"


@dataclass(frozen=True)
class Template:
    """A parsed, validated endpoint template."""

    path: str  # e.g. /customer/v2/accounts/{account_id}
    query: tuple[tuple[str, str | None, str], ...]  # (name, placeholder or None, literal)

    @classmethod
    def parse(cls, name: str, raw: str, expected: frozenset[str]) -> "Template":
        where = f"{env_var(name)}={raw!r}"
        if "://" in raw or raw.startswith("//") or "#" in raw:
            raise ValueError(f"{where}: must be a path; the host comes from GATEWAY_BASE_URL")
        path, _, query = raw.partition("?")
        if not _PATH_CHARS.match(path):
            raise ValueError(
                f"{where}: the path must start with / and use only letters, digits, "
                "'-._~:/' and {placeholders}"
            )
        for segment in path.split("/")[1:]:
            if segment in ("", ".", ".."):
                raise ValueError(f"{where}: empty, '.' or '..' path segments are not allowed")
            if "{" in _PLACEHOLDER.sub("", segment) or "}" in _PLACEHOLDER.sub("", segment):
                raise ValueError(f"{where}: malformed placeholder in {segment!r}")
        used = _PLACEHOLDER.findall(path)
        pairs: list[tuple[str, str | None, str]] = []
        for item in query.split("&") if query else []:
            qname, sep, value = item.partition("=")
            if not sep or not _QUERY_NAME.match(qname):
                raise ValueError(f"{where}: query must be name=value pairs, got {item!r}")
            if m := re.fullmatch(r"\{([a-z_]+)\}", value):
                pairs.append((qname, m[1], ""))
                used.append(m[1])
            elif _QUERY_LITERAL.match(value):
                pairs.append((qname, None, value))
            else:
                raise ValueError(f"{where}: query value {value!r} must be a {{placeholder}} "
                                 "or a plain literal")  # fmt: skip
        if dupes := sorted({p for p in used if used.count(p) > 1}):
            raise ValueError(f"{where}: placeholders used more than once: {dupes}")
        if missing := sorted(expected - set(used)):
            raise ValueError(f"{where}: missing placeholders {missing}")
        if unknown := sorted(set(used) - expected):
            raise ValueError(
                f"{where}: unknown placeholders {unknown}; allowed: {sorted(expected)}"
            )
        return cls(path, tuple(pairs))

    def render(self, values: Mapping[str, Any]) -> tuple[str, list[tuple[str, str]]]:
        """(path with encoded values, query params). None values are left out of the query."""

        def fill(m: re.Match[str]) -> str:
            value = values[m[1]]
            text = "" if value is None else str(value)
            if text in ("", ".", ".."):
                raise ValueError(f"path value for {{{m[1]}}} is empty or a dot segment")
            return quote(text, safe="")  # nothing is safe: '/' and '?' get encoded too

        path = _PLACEHOLDER.sub(fill, self.path)
        params = [
            (qname, literal if placeholder is None else str(values[placeholder]))
            for qname, placeholder, literal in self.query
            if placeholder is None or values[placeholder] is not None
        ]
        return path, params


@dataclass(frozen=True)
class Call:
    """One resolved downstream request."""

    operation: str
    api: DomainApi
    method: str
    path: str  # encoded, with IDs: never log it
    params: list[tuple[str, str]]  # may hold IDs: never log them
    route: str  # the template path, e.g. /boaccount/API/account/{account_id}: safe to log


class GatewayEndpoints(BaseSettings):
    """GATEWAY_ENDPOINT_<OPERATION> overrides; the defaults are the mock's paths."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_ENDPOINT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,  # errors must never echo .env values (secrets)
    )

    get_account: str = OPERATIONS["get_account"].default
    list_subscriptions: str = OPERATIONS["list_subscriptions"].default
    get_subscription: str = OPERATIONS["get_subscription"].default
    get_service: str = OPERATIONS["get_service"].default
    get_order: str = OPERATIONS["get_order"].default
    list_orders: str = OPERATIONS["list_orders"].default

    _templates: dict[str, Template] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _parse_all(self) -> "GatewayEndpoints":
        errors: list[str] = []
        for name, op in OPERATIONS.items():
            try:
                self._templates[name] = Template.parse(name, getattr(self, name), op.params)
            except ValueError as exc:
                errors.append(str(exc))
        if errors:  # report every bad template at once, not one per restart
            raise ValueError("invalid gateway endpoint(s):\n  - " + "\n  - ".join(errors))
        return self

    def resolve(self, operation: str, **values: Any) -> Call:
        op = OPERATIONS[operation]
        if set(values) != op.params:
            raise TypeError(f"{operation} takes {sorted(op.params)}, got {sorted(values)}")
        template = self._templates[operation]
        path, params = template.render(values)
        return Call(operation, op.api, op.method, path, params, template.path)

    def defaulted(self) -> list[str]:
        """Operations still on the lab default (not set by the environment)."""
        return [name for name in OPERATIONS if name not in self.model_fields_set]

    def overrides(self) -> dict[str, str]:
        """Operations set by the environment → their templates (no secrets, no IDs)."""
        return {name: getattr(self, name) for name in OPERATIONS if name in self.model_fields_set}
