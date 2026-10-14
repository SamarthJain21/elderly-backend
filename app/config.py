from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    firebase_project_id: str = "elderly-3e18c"
    # Local dev only. On Cloud Run, leave unset and use the service's identity (ADC).
    firebase_credentials: Path | None = Path("service-account.json")
    cors_origins: list[str] = ["http://localhost:5173"]
    elder_email_domain: str = "elderly.app"


@lru_cache
def get_settings() -> Settings:
    return Settings()
