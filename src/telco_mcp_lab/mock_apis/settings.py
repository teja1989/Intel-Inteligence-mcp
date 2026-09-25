"""Configuration for the mock backend, read from environment variables / `.env`.

Nothing secret is hard-coded: `MOCK_GATEWAY_TOKEN` has no default, so the app refuses
to start without it. Spring equivalent: a `@ConfigurationProperties` class bound
from `application.yml` + environment variables.
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MOCK_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # The bearer token the mock gateway accepts. It stands in for the service
    # token your real gateway would validate (Option A: the MCP server's own
    # identity, never the end user's token passed through).
    gateway_token: SecretStr = Field(min_length=16)

    host: str = "127.0.0.1"
    port: int = 8081
    db_path: Path = Path(".data/mock_backend.sqlite3")

    # How long a draft order stays valid before it can no longer be submitted.
    draft_ttl_seconds: int = Field(default=900, ge=1)

    # Chaos switches (can also be changed at runtime via POST /_admin/chaos).
    chaos_delay_ms: int = Field(default=0, ge=0)
    chaos_fail_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    chaos_fail_status: int = Field(default=503, ge=500, le=599)
    admin_enabled: bool = True
