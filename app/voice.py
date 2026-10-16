"""Natural read-aloud voice: Google Cloud Text-to-Speech (Chirp 3 HD). The browser's built-in voices sound robotic
and many devices have no Thai or Hindi voice at all; the frontend falls back to them only if this fails."""
from __future__ import annotations

import base64
import re
from collections import OrderedDict
from functools import lru_cache

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app import usage
from app.config import get_settings

# One warm, unhurried female voice per language ("Sulafat" is described by Google as warm).
VOICES = {
    "en-US": ("en-US", "en-US-Chirp3-HD-Sulafat"),
    "hi-IN": ("hi-IN", "hi-IN-Chirp3-HD-Sulafat"),
    "hi-Latn": ("hi-IN", "hi-IN-Chirp3-HD-Sulafat"),  # Hinglish: converted to Devanagari first (to_devanagari)
    "th-TH": ("th-TH", "th-TH-Chirp3-HD-Sulafat"),
    "ja-JP": ("ja-JP", "ja-JP-Chirp3-HD-Sulafat"),
    "ko-KR": ("ko-KR", "ko-KR-Chirp3-HD-Sulafat"),
}
SPEAKING_RATE = 0.9  # a little slower than normal for older listeners
_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")

# Same sentences repeat a lot (greetings, "Tap Yes or No", replays of a message): keep the last few hundred clips.
_CACHE_SIZE = 300
_cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()


def clean(text: str) -> str:
    return " ".join(_EMOJI.sub("", text).split())


@lru_cache
def _session() -> AuthorizedSession:
    s = get_settings()
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    path = s.firebase_credentials
    if path is not None and path.exists():
        creds = service_account.Credentials.from_service_account_file(str(path), scopes=scopes)
    else:  # Cloud Run: the service's own identity
        creds, _ = google.auth.default(scopes=scopes)
    # Both identities belong to the project, so usage is billed there — no quota-project header (that would need
    # an extra IAM role on Cloud Run).
    return AuthorizedSession(creds)


_TO_DEVANAGARI = (
    "Rewrite this Hinglish (Hindi in Roman letters, with some English words) in Devanagari script so a Hindi "
    "text-to-speech voice pronounces it naturally. Keep the exact words and meaning; write English words, names and "
    "numbers the way a Hindi speaker says them (e.g. 'medicine' → 'मेडिसिन', '8 baje' → '8 बजे'). Keep symbols like ₹. "
    "Reply with the rewritten text only."
)


def to_devanagari(text: str) -> str:
    """Hinglish → Devanagari for the Hindi voice (it reads Roman letters with English sounds). Fast model, no thinking."""
    from google.genai import types

    from app.agent.llm import genai_client
    r = genai_client().models.generate_content(
        model=get_settings().gemini_model, contents=text,
        config=types.GenerateContentConfig(system_instruction=_TO_DEVANAGARI, temperature=0),
    )
    usage.gemini(get_settings().gemini_model, r, "hinglish-voice")
    return (r.text or "").strip() or text


def synthesize(text: str, language: str) -> bytes:
    """MP3 bytes. Raises on API errors (the caller turns that into a 502 → browser voice)."""
    lang, voice = VOICES.get(language, VOICES["en-US"])
    key = (f"{voice}|{language}", text)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    spoken = to_devanagari(text) if language == "hi-Latn" else text
    r = _session().post(_URL, json={
        "input": {"text": spoken},
        "voice": {"languageCode": lang, "name": voice},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": SPEAKING_RATE},
    }, timeout=10)
    r.raise_for_status()
    usage.add("tts-chirp3-hd", chars=len(spoken), calls=1)
    audio = base64.b64decode(r.json()["audioContent"])
    _cache[key] = audio
    if len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return audio
