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

from app.config import get_settings

# One warm, unhurried female voice per language ("Sulafat" is described by Google as warm).
VOICES = {
    "en-US": ("en-US", "en-US-Chirp3-HD-Sulafat"),
    "hi-IN": ("hi-IN", "hi-IN-Chirp3-HD-Sulafat"),
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


def synthesize(text: str, language: str) -> bytes:
    """MP3 bytes. Raises on API errors (the caller turns that into a 502 → browser voice)."""
    lang, voice = VOICES.get(language, VOICES["en-US"])
    key = (voice, text)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    r = _session().post(_URL, json={
        "input": {"text": text},
        "voice": {"languageCode": lang, "name": voice},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": SPEAKING_RATE},
    }, timeout=10)
    r.raise_for_status()
    audio = base64.b64decode(r.json()["audioContent"])
    _cache[key] = audio
    if len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return audio
