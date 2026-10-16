"""Family chat across languages: Emma writes English, Kamla reads (and hears) Hindi — and her replies reach Emma in
English. One fast model call per message, run alongside the chip suggestions so it adds no extra wait."""
from __future__ import annotations

from functools import lru_cache

from google.genai import types

from app import usage
from app.agent.llm import genai_client
from app.config import get_settings

# Plain names for translation (the chat prompt's Hinglish entry carries voice instructions this call mustn't follow).
_NAMES = {"en-US": "English", "hi-IN": "Hindi (Devanagari script)", "th-TH": "Thai", "ja-JP": "Japanese", "ko-KR": "Korean",
          "hi-Latn": "Hinglish — Hindi written in Roman letters, mixed with common English words, as people in India text"}

_PROMPT = (
    "Translate the family chat message into {lang}. Warm, natural, the way family talk to each other; keep names, "
    "numbers, times and emoji as they are. If it is already in {lang}, return it unchanged. Output only the translation."
)


def needs_translation(text: str, target: str, source: str | None = None) -> bool:
    """Nothing to translate (emoji only), or both sides use the same language. Without a known source, plain
    ASCII is taken as already English."""
    if not any(ch.isalpha() for ch in text):
        return False
    if source is not None:
        return source != target
    return not (target == "en-US" and text.isascii())


@lru_cache(maxsize=1024)
def translate(text: str, target: str, source: str | None = None) -> str | None:
    """None when no translation is needed or it failed (the original is shown)."""
    if not needs_translation(text, target, source):
        return None
    lang = _NAMES.get(target, "English")
    r = genai_client().models.generate_content(
        model=get_settings().gemini_model, contents=text,
        config=types.GenerateContentConfig(system_instruction=_PROMPT.format(lang=lang), temperature=0.2),
    )
    usage.gemini(get_settings().gemini_model, r, "family-translate")
    out = (r.text or "").strip()
    return out if out and out != text.strip() else None
