from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from the environment and (if present) .env.

    Every field defaults to something that runs out of the box against the
    docker-compose Postgres instance. `edgar_contact_email` defaults to an obvious
    placeholder rather than a real address — see CLAUDE.md 4.1: never commit a real
    email, override it via .env. That field isn't used until the EDGAR adapter
    (M2) makes its first request, so it fails loudly there, not at CLI startup.
    """

    model_config = SettingsConfigDict(env_file=".env", env_prefix="PDW_", extra="ignore")

    database_url: str = "postgresql://pdw:pdw@localhost:5433/pdw"
    edgar_contact_email: str = "set-me@example.com"
    edgar_requests_per_second: float = 10.0
    tiingo_api_token: str = "set-me"
    log_level: str = "INFO"
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
