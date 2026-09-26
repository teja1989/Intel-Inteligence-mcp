"""Harness configuration (env / .env). Nothing secret in code.

Azure OpenAI **v1** endpoint, e.g.
    AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/
with the plain `openai.AsyncOpenAI` client (no api-version). This follows
Microsoft's v1 guidance as recalled; it couldn't be fetched from the build
sandbox, so run `make harness-check` first. If your resource wants the key in
an `api-key` header instead of `Authorization: Bearer`, set
AZURE_OPENAI_AUTH_HEADER=api-key.

Corporate HTTPS proxy: the standard HTTPS_PROXY / SSL_CERT_FILE environment
variables are honoured automatically (httpx2 trust_env). Or set
AZURE_OPENAI_PROXY / AZURE_OPENAI_CA_BUNDLE explicitly here.
"""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AzureOpenAISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AZURE_OPENAI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    endpoint: str
    api_key: SecretStr = Field(min_length=8)
    deployment: str = Field(min_length=1, description="Your deployment name (gpt-5 / gpt-4.x).")
    auth_header: Literal["bearer", "api-key"] = "bearer"
    proxy: str | None = None  # e.g. http://user:pass@proxy.corp:8080 (else HTTPS_PROXY is used)
    ca_bundle: Path | None = None  # PEM with your corporate root CA (TLS-inspecting proxies)
    timeout_s: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    # Sampling knobs are OFF by default: GPT-5-family (reasoning) deployments reject
    # `temperature` and use `max_completion_tokens`. Set only what your model supports.
    temperature: float | None = None
    max_completion_tokens: int | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None

    @field_validator("endpoint")
    @classmethod
    def _v1_https(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("AZURE_OPENAI_ENDPOINT must be an https URL")
        if not parts.path.rstrip("/").endswith("/openai/v1"):
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT must be the v1 base URL, ending in /openai/v1/ "
                "(e.g. https://<resource>.openai.azure.com/openai/v1/)"
            )
        return v if v.endswith("/") else v + "/"

    @field_validator("ca_bundle")
    @classmethod
    def _ca_exists(cls, v: Path | None) -> Path | None:
        if v is not None and not v.is_file():
            raise ValueError(f"AZURE_OPENAI_CA_BUNDLE file not found: {v}")
        return v


class HarnessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HARNESS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    mcp_url: str = "http://127.0.0.1:8090/mcp"
    caller: str = "alice"  # uses MCP_TOKEN_<CALLER> from .env
    # JWT mode: a token from the token service (or `make token`) replaces MCP_TOKEN_<CALLER>.
    bearer_token: SecretStr | None = None
    # JWT mode, customer_context clients: sent as X-Customer-Account-Id by THIS APP's code,
    # i.e. the host decides which customer the conversation is about, never the model.
    customer_account_id: str | None = Field(default=None, pattern=r"^ACC-\d{4}(,ACC-\d{4})*$")
    customer_header: str = "X-Customer-Account-Id"

    @field_validator("bearer_token", "customer_account_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:  # blank .env lines mean "not set"
        return None if isinstance(v, str) and not v.strip() else v

    protocol: Literal["auto", "legacy"] = "auto"
    max_steps: int = Field(default=8, ge=1, le=30)
    confirm_destructive: bool = True
    trace_dir: Path = Path(".data/harness")
    # Layer C: the host's own system prompt (versioned file, eval-gated).
    system_prompt_file: Path = Path("prompts/agent.system.md")
    # Layer B: add the MCP server's `instructions` below the host prompt.
    use_server_instructions: bool = True
    # Layer E (host side): input/output guardrails.
    guardrails_file: Path = Path("config/guardrails.json")
