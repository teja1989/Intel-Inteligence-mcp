"""Guardrail G2 (docs/08): production refuses to start with lab or lower-env settings.

`MCP_ENVIRONMENT` is one of local | dev | test | production. In production every
rule below must hold, or the process exits before serving a single request, with
all violations listed. A config slip (the dev JWKS file, lab tokens, a lower-env
client left in the registry) then fails loudly at deploy time instead of quietly
opening production to lower-env credentials.

The rules don't replace the per-request checks (issuer, audience, signature,
registry). They make sure those checks are configured with production values.

Java/Spring equivalent: a `@Profile("production")` `ApplicationRunner` (or an
`EnvironmentPostProcessor`) that validates the bound properties and fails startup.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

Environment = Literal["local", "dev", "test", "production"]
ENVIRONMENTS: tuple[Environment, ...] = ("local", "dev", "test", "production")

# RFC 6761 / 2606 reserved names and loopback: never valid in production.
_RESERVED_SUFFIXES = (".invalid", ".test", ".example", ".localhost", ".local")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104


class UnsafeProductionConfig(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            "Refusing to start with MCP_ENVIRONMENT=production:\n  - " + "\n  - ".join(problems)
        )
        self.problems = problems


@dataclass(frozen=True)
class ProductionFacts:
    """The settings the guard looks at, gathered from wherever they're configured."""

    transport: str  # "http" | "stdio"
    auth_mode: str  # "static" | "jwt"
    public_url: str
    unsafe_raw_free_text: bool
    legacy_sessions: bool = False
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_jwks_url: str | None = None
    jwt_jwks_file: Path | None = None
    registry_problems: tuple[str, ...] = ()


def _dev_host(url: str) -> str | None:
    """The reason `url` is not a production URL, or None if it could be one."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return "must be https"
    host = (parts.hostname or "").lower()
    if not host:
        return "has no host"
    if host in _LOCAL_HOSTS or host.endswith(_RESERVED_SUFFIXES):
        return f"uses a local/reserved host ({host})"
    return None


def production_problems(f: ProductionFacts) -> list[str]:
    problems: list[str] = []
    if f.transport != "http":
        problems.append("stdio transport acts as one fixed caller with no authentication")
    if f.auth_mode != "jwt":
        problems.append("MCP_AUTH_MODE must be jwt (static lab tokens are for demos)")
    if f.legacy_sessions:
        problems.append("--legacy-sessions keeps sessions in memory (demo; breaks scaling)")
    if f.unsafe_raw_free_text:
        problems.append("MCP_UNSAFE_RAW_FREE_TEXT must be false (lab demo switch)")
    if reason := _dev_host(f.public_url):
        problems.append(f"MCP_PUBLIC_URL {reason}")
    if f.auth_mode == "jwt":
        if f.jwt_jwks_file is not None:
            problems.append("MCP_JWT_JWKS_FILE is for local testing; use MCP_JWT_JWKS_URL")
        for name, value in (
            ("MCP_JWT_ISSUER", f.jwt_issuer),
            ("MCP_JWT_AUDIENCE", f.jwt_audience),
            ("MCP_JWT_JWKS_URL", f.jwt_jwks_url),
        ):
            if value and value.startswith(("http://", "https://")):
                if reason := _dev_host(value):
                    problems.append(f"{name} {reason}")
            elif name == "MCP_JWT_JWKS_URL":
                problems.append("MCP_JWT_JWKS_URL must be set")
    problems.extend(f.registry_problems)
    return problems


def enforce(environment: str, facts: ProductionFacts) -> None:
    if environment == "production" and (problems := production_problems(facts)):
        raise UnsafeProductionConfig(problems)
