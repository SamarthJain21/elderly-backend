"""Gemini access behind a small interface so tests can swap in a fake."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

from google import genai
from google.genai import types
from google.oauth2 import service_account
from pydantic import BaseModel, Field

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


class ChatModel(Protocol):
    async def reply(self, system: str, history: list[Turn]) -> AgentReply: ...


class GeminiChatModel:
    def __init__(self, client: genai.Client, model: str) -> None:
        self._client = client
        self._model = model

    async def reply(self, system: str, history: list[Turn]) -> AgentReply:
        contents = [
            types.Content(role="user" if t.role == "elder" else "model", parts=[types.Part(text=t.text)])
            for t in history
        ]
        resp = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=AgentReply,
                temperature=0.6,
            ),
        )
        parsed = resp.parsed
        if isinstance(parsed, AgentReply):
            out = parsed
        elif resp.text:
            out = AgentReply.model_validate_json(resp.text)
        else:
            raise RuntimeError("Empty response from Gemini")
        # Schema can't enforce list length; the UI expects exactly 3 chips (or none).
        out.quickReplies = [q.strip() for q in out.quickReplies if q.strip()][:3]
        return out


@lru_cache
def get_chat_model() -> ChatModel:
    settings = get_settings()
    cred_path = settings.firebase_credentials
    credentials = (
        service_account.Credentials.from_service_account_file(
            str(cred_path), scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if cred_path is not None and cred_path.exists()
        else None  # Cloud Run: use the service's identity (ADC)
    )
    client = genai.Client(
        vertexai=True,
        project=settings.firebase_project_id,
        location=settings.vertex_location,
        credentials=credentials,
    )
    return GeminiChatModel(client, settings.gemini_model)
