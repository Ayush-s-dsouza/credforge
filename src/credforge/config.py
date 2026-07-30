"""Central settings, loaded from environment variables (and .env if present).

CREDFORGE_-prefixed vars are credforge's own knobs. BRAVE_API_KEY and
ANTHROPIC_API_KEY are deliberately unprefixed and pulled in via an explicit
validation_alias -- they're the same key a human would set for any tool
using those vendors' APIs, not credforge-specific config. See
DECISIONS.md D-001.
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CREDFORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Path(".credforge")

    vault_key: SecretStr | None = None

    brave_api_key: SecretStr | None = Field(default=None, validation_alias="BRAVE_API_KEY")
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: SecretStr | None = None
    email_alias_domain: str = "example.com"

    resolve_confidence_threshold: float = 0.7
    default_rate_per_sec: float = 0.5
    default_burst: int = 2
    robots_ttl_seconds: float = 3600.0
    user_agent: str = "credforge-bot/0.1 (+https://composio.dev)"
    max_concurrent_provisions: int = 2
    max_response_bytes: int = 5 * 1024 * 1024


def load_settings() -> Settings:
    return Settings()
