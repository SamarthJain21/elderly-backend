"""Gemini access behind a small interface so tests can swap in a fake."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from typing import Literal, Protocol, TypeVar

from google import genai
from google.genai import errors, types
from google.oauth2 import service_account
from pydantic import BaseModel, Field

from app.checkin import Extracted
from app.config import get_settings


@dataclass(frozen=True)
class Turn:
    role: Literal["elder", "agent"]
    text: str


class AgentReply(BaseModel):
    reply: str = Field(description="What Elderly says back. 1-3 short, warm sentences.")
    quickReplies: list[str] = Field(
        description="Exactly 3 short things the elder might tap next, in their language, max 5 words each.",
    )


class CheckInTurn(AgentReply):
    extracted: Extracted = Field(
        description="Facts stated in the elder's LATEST message only. Leave a field null if not clearly said.",
    )


R = TypeVar("R", bound=AgentReply)


class ChatModel(Protocol):
    async def complete(self, system: str, history: list[Turn], schema: type[R]) -> R: ...


log = logging.getLogger(__name__)


def _is_capacity_error(exc: errors.APIError) -> bool:
    """Overloaded (503) or rate-limited (429): worth trying the next model."""
    return exc.code in (429, 503)


class GeminiChatModel:
    def __init__(self, client: genai.Client, models: list[str]) -> None:
        if not models:
            raise ValueError("at least one model required")
        self._client = client
        self._models = models

    async def complete(self, system: str, history: list[Turn], schema: type[R]) -> R:
        contents = [
            types.Content(role="user" if t.role == "elder" else "model", parts=[types.Part(text=t.text)])
            for t in history
        ]
        for i, model in enumerate(self._models):
            try:
                return await self._generate(model, system, contents, schema)
            except errors.APIError as exc:
                if not _is_capacity_error(exc) or i == len(self._models) - 1:
                    raise
                log.warning("Gemini %s unavailable (%s); falling back to %s", model, exc.code, self._models[i + 1])
        raise AssertionError("unreachable")

    async def _generate(self, model: str, system: str, contents: list[types.Content], schema: type[R]) -> R:
        resp = await self._client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.6,
            ),
        )
        parsed = resp.parsed
        if isinstance(parsed, schema):
            out = parsed
        elif resp.text:
            out = schema.model_validate_json(resp.text)
        else:
            raise RuntimeError("Empty response from Gemini")
        # Schema can't enforce list length; the UI expects exactly 3 chips (or none).
        out.quickReplies = [q.strip() for q in out.quickReplies if q.strip()][:3]
        return out


def _vertex_client() -> genai.Client:
    settings = get_settings()
    cred_path = settings.firebase_credentials
    credentials = (
        service_account.Credentials.from_service_account_file(
            str(cred_path), scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if cred_path is not None and cred_path.exists()
        else None  # Cloud Run: use the service's identity (ADC)
    )
    return genai.Client(
        vertexai=True,
        project=settings.firebase_project_id,
        location=settings.vertex_location,
        credentials=credentials,
    )


@lru_cache
def get_chat_model() -> ChatModel:
    """AI Studio key if GEMINI_API_KEY is set (free tier, no billing), else Vertex AI (needs Blaze)."""
    settings = get_settings()
    if settings.gemini_api_key is not None:
        client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    else:
        client = _vertex_client()
    return GeminiChatModel(client, [settings.gemini_model, *settings.gemini_fallback_models])
