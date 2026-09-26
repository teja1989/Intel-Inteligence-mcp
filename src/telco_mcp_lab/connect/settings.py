"""Connector settings: environment variables TELCO_MCP_*, optionally from a --config file.

The config file holds NON-secret values (URLs, client ID, customer). The secret comes
from TELCO_MCP_SECRET_COMMAND (e.g. the macOS Keychain) or, for CI, the environment
variable TELCO_MCP_CLIENT_SECRET injected by the pipeline. Never write it to a file.
"""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOCAL_HOSTS


class ConnectSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TELCO_MCP_", extra="ignore")

    url: str  # the MCP endpoint, e.g. https://<lower-env-gateway>/<path>/mcp
    token_url: str  # the lower-env token service's token endpoint
    client_id: str = Field(min_length=1)
    client_secret: SecretStr | None = None  # CI only; people use secret_command
    secret_command: str | None = None  # e.g. security find-generic-password -s telco-mcp -w
    scope: str | None = None  # space-separated; omitted when the token service decides
    client_auth: Literal["basic", "post"] = "basic"  # RFC 6749 client_secret_basic / _post
    customer: str | None = Field(default=None, pattern=r"^ACC-\d{4}(,ACC-\d{4})*$")
    customer_header: str = "X-Customer-Account-Id"
    ca_bundle: Path | None = None  # corporate root CA, if TLS is intercepted
    trust_env: bool = True  # honour HTTP(S)_PROXY / NO_PROXY for non-local targets
    refresh_margin_s: int = Field(default=300, ge=0, le=3600)  # refresh this long before expiry
    timeout_s: float = Field(default=30.0, gt=0, le=300)

    @field_validator("url", "token_url")
    @classmethod
    def _https_unless_local(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http(s) URL")
        if parts.scheme != "https" and not is_local(v):
            raise ValueError("must be https (a bearer token or secret would travel in clear)")
        return v

    @field_validator("client_secret", "secret_command", "customer", "scope", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _one_secret_source(self) -> "ConnectSettings":
        if (self.client_secret is None) == (self.secret_command is None):
            raise ValueError(
                "set exactly one of TELCO_MCP_SECRET_COMMAND or TELCO_MCP_CLIENT_SECRET"
            )
        return self
