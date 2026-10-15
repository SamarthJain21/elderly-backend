"""Gemini access behind a small interface so tests can swap in a fake."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, TypeVar

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


Execute = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ChatModel(Protocol):
    async def complete(self, system: str, history: list[Turn], schema: type[R]) -> R: ...

    async def run(self, system: str, history: list[Turn], tools: list[dict[str, Any]], execute: Execute) -> AgentReply:
        """Agent turn with tools. The model must call a function: `reply` to talk, or an action tool,
        after which it's forced to `reply` given the tool's result. At most one action per turn."""
        ...


def _reply_from_args(args: dict[str, Any]) -> AgentReply:
    quick = [str(q).strip() for q in args.get("quickReplies") or [] if str(q).strip()][:3]
    return AgentReply(reply=str(args.get("text", "")).strip(), quickReplies=quick)


log = logging.getLogger(__name__)


def _is_capacity_error(exc: Exception) -> bool:
    """Overloaded (503), rate-limited (429), or hung past our per-call limit: worth trying the next model."""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, errors.APIError) and exc.code in (429, 503)


# An overloaded model sometimes doesn't error — it just hangs. Cap each call so the next model gets a turn
# well inside the caller's overall budget (chat turns allow ~45s in total).
PER_CALL_TIMEOUT_S = 12.0  # worst tool turn: 3 calls ≈ 36s, inside the 45s turn budget


class GeminiChatModel:
    def __init__(self, client: genai.Client, models: list[str], per_call_timeout_s: float = PER_CALL_TIMEOUT_S) -> None:
        if not models:
            raise ValueError("at least one model required")
        self._client = client
        self._models = models
        self._timeout = per_call_timeout_s

    async def _call(self, model: str, contents: list[types.Content], config: types.GenerateContentConfig) -> types.GenerateContentResponse:
        return await asyncio.wait_for(
            self._client.aio.models.generate_content(model=model, contents=contents, config=config), self._timeout)

    def _fallback_or_raise(self, i: int, model: str, exc: Exception) -> None:
        if not _is_capacity_error(exc) or i == len(self._models) - 1:
            raise exc
        why = "timed out" if isinstance(exc, TimeoutError) else str(getattr(exc, "code", exc))
        log.warning("Gemini %s unavailable (%s); falling back to %s", model, why, self._models[i + 1])

    async def complete(self, system: str, history: list[Turn], schema: type[R]) -> R:
        contents = [
            types.Content(role="user" if t.role == "elder" else "model", parts=[types.Part(text=t.text)])
            for t in history
        ]
        for i, model in enumerate(self._models):
            try:
                return await self._generate(model, system, contents, schema)
            except (errors.APIError, TimeoutError) as exc:
                self._fallback_or_raise(i, model, exc)
        raise AssertionError("unreachable")

    def _tool_config(self, system: str, tools: list[dict[str, Any]], allowed: list[str] | None) -> types.GenerateContentConfig:
        decls = [types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
                 for t in tools]
        return types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)],
            # ANY = must call a function. Free text is impossible, so it can't "pretend" to act.
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=allowed)),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.5,
        )

    async def run(self, system: str, history: list[Turn], tools: list[dict[str, Any]], execute: Execute) -> AgentReply:
        contents = [
            types.Content(role="user" if t.role == "elder" else "model", parts=[types.Part(text=t.text)])
            for t in history
        ]
        for i, model in enumerate(self._models):
            try:
                first = await self._call(model, contents, self._tool_config(system, tools, None))
            except (errors.APIError, TimeoutError) as exc:
                self._fallback_or_raise(i, model, exc)
                continue
            return await self._after_first(model, system, contents, tools, first, execute)
        raise AssertionError("unreachable")

    async def _after_first(self, model: str, system: str, contents: list[types.Content], tools: list[dict[str, Any]],
                           first: types.GenerateContentResponse, execute: Execute) -> AgentReply:
        calls = first.function_calls or []
        if not calls:
            return AgentReply(reply=(first.text or "").strip() or "Sorry, could you say that again?", quickReplies=[])
        call = next((c for c in calls if c.name != "reply"), calls[0])
        if call.name == "reply":
            return _reply_from_args(dict(call.args or {}))

        result = await execute(call.name or "", dict(call.args or {}))
        # Send back the model's own content: Gemini 3 requires its thought_signature on the function call.
        followup = [*contents, first.candidates[0].content,  # type: ignore[index]
                    types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(name=call.name, response=result))])]
        try:
            # Same model as the first call: thought signatures don't transfer across models.
            second = await self._call(model, followup, self._tool_config(system, tools, ["reply"]))
            reply_call = next((c for c in second.function_calls or [] if c.name == "reply"), None)
            if reply_call is not None:
                return _reply_from_args(dict(reply_call.args or {}))
        except (errors.APIError, TimeoutError):
            log.exception("Gemini follow-up failed after tool %s ran", call.name)
        # The action already happened: never fail the turn now; fall back to a plain statement.
        return AgentReply(reply=str(result.get("say") or "Done."), quickReplies=[])

    async def _generate(self, model: str, system: str, contents: list[types.Content], schema: type[R]) -> R:
        resp = await self._call(
            model,
            contents,
            types.GenerateContentConfig(
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
