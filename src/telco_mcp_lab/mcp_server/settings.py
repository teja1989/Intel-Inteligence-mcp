"""MCP server settings (env / .env, prefix MCP_). Tokens are read separately by the verifier."""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Asymmetric only. HS* would mean sharing the token service's signing secret with
# every resource server (anyone holding it can mint tokens); "none" is no signature.
ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


class McpServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # local | dev | test | production. production refuses lab/lower-env settings at
    # startup (guardrail G2, security/environment.py, docs/08).
    environment: Literal["local", "dev", "test", "production"] = "local"
    # static: lab tokens MCP_TOKEN_<CALLER> (demos/tests). jwt: tokens from the token service.
    auth_mode: Literal["static", "jwt"] = "static"
    access_config: Path = Path("config/access.json")
    # jwt mode: which client_ids may call, their mode (bound / customer_context), allowed scopes.
    clients_config: Path = Path("config/clients.json")
    # jwt mode, customer_context clients: the header the AGENT APPLICATION sets (never the model)
    # with the customer's account ID(s), comma-separated.
    customer_header: str = "X-Customer-Account-Id"

    # stdio has no headers, so the process runs as ONE configured caller.
    stdio_caller: str = "alice"

    host: str = "127.0.0.1"  # never 0.0.0.0 in the lab (spec: bind to localhost)
    port: int = Field(default=8090, ge=1, le=65535)
    # The URL clients use; also the OAuth "resource" this server protects (RFC 8707/9728).
    public_url: str = "http://127.0.0.1:8090/mcp"
    # Browser origins allowed to call us (DNS-rebinding protection). Inspector's UI by default.
    allowed_origins: list[str] = ["http://127.0.0.1:6274", "http://localhost:6274"]
    # Host headers accepted (DNS-rebinding protection). Add the internal hostname the
    # gateway uses when deploying, e.g. ["mcp.internal.example:*"].
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*"]

    # LAB DEMO ONLY: pass free text (notes) to the model verbatim, to see the injection risk.
    unsafe_raw_free_text: bool = False


class JwtSettings(BaseSettings):
    """Validation of access tokens from the internal token service (MCP_AUTH_MODE=jwt).

    Every value is what YOUR token service does. The defaults are a guess at a typical
    client-credentials JWT; confirm each against a real (decoded, non-production) token.
    """

    model_config = SettingsConfigDict(
        env_prefix="MCP_JWT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    issuer: str = Field(min_length=1)  # required: exact "iss" value
    audience: str = Field(min_length=1)  # required: exact "aud" restricting tokens to this server
    # Exactly one key source: the token service's JWKS endpoint, or a local JWKS file.
    jwks_url: str | None = None
    jwks_file: Path | None = None
    algorithms: list[str] = ["RS256"]
    # First claim present wins. client_credentials tokens commonly use client_id or azp.
    client_id_claims: list[str] = ["client_id", "azp"]
    scope_claim: str = "scope"  # space-separated string or a JSON list
    required_claims: list[str] = ["exp", "iat", "iss", "aud"]
    leeway_s: int = Field(default=30, ge=0, le=300)  # clock skew allowance
    # Tokens are issued for 2 h: refuse anything claiming a longer life (exp - iat).
    max_lifetime_s: int = Field(default=7200, ge=60)
    jwks_cache_s: int = Field(default=3600, ge=60)
    # Unknown "kid" triggers a refetch (key rotation), at most this often.
    jwks_min_refresh_s: int = Field(default=60, ge=5)
    jwks_timeout_s: float = Field(default=5.0, gt=0)
    # Use HTTP(S)_PROXY / SSL_CERT_FILE from the environment for the JWKS fetch.
    jwks_trust_env: bool = False
    jwks_ca_bundle: Path | None = None

    @field_validator("jwks_url", "jwks_file", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("algorithms")
    @classmethod
    def _asymmetric_only(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if a not in ASYMMETRIC_ALGORITHMS]
        if bad or not v:
            allowed = ", ".join(sorted(ASYMMETRIC_ALGORITHMS))
            raise ValueError(f"unsupported algorithms {bad}: use asymmetric ones ({allowed})")
        return v

    @field_validator("jwks_url")
    @classmethod
    def _https_jwks(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("https://"):
            raise ValueError(
                "MCP_JWT_JWKS_URL must be https:// (keys fetched over plain HTTP can be swapped)"
            )
        return v

    @model_validator(mode="after")
    def _one_key_source(self) -> "JwtSettings":  # no secrets in this model, safe to echo
        if (self.jwks_url is None) == (self.jwks_file is None):
            raise ValueError("set exactly one of MCP_JWT_JWKS_URL or MCP_JWT_JWKS_FILE")
        return self
