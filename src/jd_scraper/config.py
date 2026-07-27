"""Runtime settings, read from the environment or a local .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    theirstack_api_key: str = ""
    theirstack_base_url: str = "https://api.theirstack.com"

    jd_db_path: Path = Path("jobs.db")
    jd_timeout_seconds: float = 60.0

    # Runs asking for more than this many results prompt for confirmation first,
    # since TheirStack bills per job returned.
    jd_confirm_threshold: int = 100

    def require_api_key(self) -> str:
        if not self.theirstack_api_key:
            raise RuntimeError(
                "THEIRSTACK_API_KEY is not set. Copy .env.example to .env and fill it in, "
                "or export THEIRSTACK_API_KEY in your shell."
            )
        return self.theirstack_api_key


def load_settings() -> Settings:
    return Settings()
