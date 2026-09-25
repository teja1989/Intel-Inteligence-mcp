"""MCP server settings (env / .env, prefix MCP_). Tokens are read separately by the verifier."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    access_config: Path = Path("config/access.json")
    # stdio has no headers, so the process runs as ONE configured caller.
    stdio_caller: str = "alice"

    host: str = "127.0.0.1"  # never 0.0.0.0 in the lab (spec: bind to localhost)
    port: int = Field(default=8090, ge=1, le=65535)
    # The URL clients use; also the OAuth "resource" this server protects (RFC 8707/9728).
    public_url: str = "http://127.0.0.1:8090/mcp"
    # Browser origins allowed to call us (DNS-rebinding protection). Inspector's UI by default.
    allowed_origins: list[str] = ["http://127.0.0.1:6274", "http://localhost:6274"]

    # LAB DEMO ONLY: pass free text (notes) to the model verbatim, to see the injection risk.
    unsafe_raw_free_text: bool = False
