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

from pydantic import AliasChoices, Field, SecretStr, field_validator
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


def _existing_file(v: Path | None, name: str) -> Path | None:
    if v is not None and not v.is_file():
        raise ValueError(f"{name} file not found: {v}")
    return v


class ClaudeSettings(BaseSettings):
    """Claude via the Anthropic API. Prefix CHAT_CLAUDE_, deliberately NOT CLAUDE_ or
    ANTHROPIC_: Claude Code exports variables such as CLAUDE_EFFORT and
    ANTHROPIC_BASE_URL into shells it runs, and the Anthropic SDK reads
    ANTHROPIC_BASE_URL on its own. Launched from such a shell, the chat app would
    otherwise send your key to a different endpoint. Everything here is explicit."""

    model_config = SettingsConfigDict(
        env_prefix="CHAT_CLAUDE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        env_ignore_empty=True,  # an empty CHAT_… line must not hide the standard key name
    )

    # The standard name works too; CHAT_CLAUDE_API_KEY wins if both are set. (Safe: the
    # risk was ANTHROPIC_BASE_URL redirecting the key, and base_url below is always explicit.)
    api_key: SecretStr = Field(
        min_length=8, validation_alias=AliasChoices("CHAT_CLAUDE_API_KEY", "ANTHROPIC_API_KEY")
    )
    model: str = "claude-opus-5"
    max_tokens: int = Field(default=16000, ge=256, le=64000)  # non-streaming: keep < ~16k
    # low | medium | high | xhigh | max; None = the model's default (Opus 5: high).
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # Server-side refusal fallback (`fallbacks: "default"`): a declined request is re-run
    # on Anthropic's recommended substitute. Recommended for claude-opus-5.
    fallbacks: bool = True
    # Always sent explicitly (never taken from ANTHROPIC_BASE_URL). Change only for an
    # approved corporate gateway in front of the Anthropic API.
    base_url: str = "https://api.anthropic.com"
    proxy: str | None = None  # else HTTPS_PROXY from the environment is used
    ca_bundle: Path | None = None
    timeout_s: float = Field(default=120.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("ca_bundle")
    @classmethod
    def _ca(cls, v: Path | None) -> Path | None:
        return _existing_file(v, "CHAT_CLAUDE_CA_BUNDLE")

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if urlsplit(v).scheme != "https":
            raise ValueError("CHAT_CLAUDE_BASE_URL must be https")
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
        env_prefix="HARNESS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # stdio (default): the harness starts the MCP server itself, no token needed; the
    # server's permissions come from MCP_STDIO_SCOPES. http: a running server at mcp_url,
    # with a JWT in bearer_token (e.g. `make token`).
    transport: Literal["stdio", "http"] = "stdio"
    mcp_url: str = "http://127.0.0.1:8090/mcp"
    bearer_token: SecretStr | None = None
    # claude | gemini | azure. Unset: the first configured one, in that order.
    llm: Literal["claude", "gemini", "azure"] | None = None

    @field_validator("bearer_token", "llm", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:  # blank .env lines mean "not set"
        return None if isinstance(v, str) and not v.strip() else v

    protocol: Literal["auto", "legacy"] = "auto"
    max_steps: int = Field(default=8, ge=1, le=30)
    confirm_destructive: bool = True
    trace_dir: Path = Path(".data/harness")
    # Layer C: the host's own system prompt (versioned file, eval-gated).
    system_prompt_file: Path = Path("lab/prompts/agent.system.md")
    # Layer B: add the MCP server's `instructions` below the host prompt.
    use_server_instructions: bool = True
    # Layer E (host side): input/output guardrails.
    guardrails_file: Path = Path("lab/config/guardrails.json")
