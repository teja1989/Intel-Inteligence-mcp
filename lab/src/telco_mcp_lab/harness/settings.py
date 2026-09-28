"""Harness configuration (env / .env). Nothing secret in code.

The model is Gemini via the Gemini Developer API (API key). Corporate HTTPS proxy: the
standard HTTPS_PROXY / SSL_CERT_FILE variables are honoured, or set CHAT_GEMINI_PROXY /
CHAT_GEMINI_CA_BUNDLE explicitly.
"""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _existing_file(v: Path | None, name: str) -> Path | None:
    if v is not None and not v.is_file():
        raise ValueError(f"{name} file not found: {v}")
    return v


class GeminiSettings(BaseSettings):
    """Gemini via the Gemini Developer API (API key). Prefix CHAT_GEMINI_: the Google
    SDK reads GEMINI_API_KEY / GOOGLE_API_KEY / GOOGLE_GENAI_USE_VERTEXAI /
    GOOGLE_GEMINI_BASE_URL on its own; everything here is passed explicitly instead.
    (Vertex AI / Google Cloud credentials: not supported yet.)"""

    model_config = SettingsConfigDict(
        env_prefix="CHAT_GEMINI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        env_ignore_empty=True,  # an empty CHAT_… line must not hide the standard key name
        hide_input_in_errors=True,  # errors must never echo .env values (keys)
    )

    # Standard names work too; CHAT_GEMINI_API_KEY wins. (Safe: base URL and API mode below
    # are always explicit, so GOOGLE_GEMINI_BASE_URL / GOOGLE_GENAI_USE_VERTEXAI can't redirect.)
    api_key: SecretStr = Field(
        min_length=8,
        validation_alias=AliasChoices("CHAT_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )
    # An alias from the SDK's own documentation; pin a specific model ID your company allows.
    model: str = "gemini-flash-latest"
    base_url: str = "https://generativelanguage.googleapis.com/"
    proxy: str | None = None
    ca_bundle: Path | None = None
    timeout_s: float = Field(default=120.0, gt=0, le=600)

    @field_validator("ca_bundle")
    @classmethod
    def _ca(cls, v: Path | None) -> Path | None:
        return _existing_file(v, "CHAT_GEMINI_CA_BUNDLE")

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if urlsplit(v).scheme != "https":
            raise ValueError("CHAT_GEMINI_BASE_URL must be https")
        return v


class HarnessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HARNESS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # stdio (default): the harness starts the MCP server itself, no token needed; the
    # server's permissions come from MCP_STDIO_SCOPES. http: a running server at mcp_url,
    # with a JWT in bearer_token (e.g. `make token`).
    transport: Literal["stdio", "http"] = "stdio"
    mcp_url: str = "http://127.0.0.1:8090/mcp"
    bearer_token: SecretStr | None = None

    @field_validator("bearer_token", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:  # blank .env lines mean "not set"
        return None if isinstance(v, str) and not v.strip() else v

    protocol: Literal["auto", "legacy"] = "auto"
    max_steps: int = Field(default=8, ge=1, le=30)
    trace_dir: Path = Path(".data/harness")
    # Layer C: the host's own system prompt (versioned file, eval-gated).
    system_prompt_file: Path = Path("lab/prompts/agent.system.md")
    # Layer B: add the MCP server's `instructions` below the host prompt.
    use_server_instructions: bool = True
    # Layer E (host side): input/output guardrails.
    guardrails_file: Path = Path("lab/config/guardrails.json")
