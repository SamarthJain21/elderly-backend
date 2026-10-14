from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    firebase_project_id: str = "elderly-3e18c"
    # Local dev only. On Cloud Run, leave unset and use the service's identity (ADC).
    firebase_credentials: Path | None = Path("service-account.json")
    cors_origins: list[str] = ["http://localhost:5173"]
    elder_email_domain: str = "elderly.app"
    # Gemini: AI Studio key if set (free tier); otherwise Vertex AI billed to firebase_project_id.
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.8-flash"
    # Tried in order when the primary is overloaded (503) or rate-limited (429).
    gemini_fallback_models: list[str] = ["gemini-3.5-flash-lite"]
    vertex_location: str = "global"
    # SMS to volunteers. Unset → texts are only recorded in the activity log.
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_from_number: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
