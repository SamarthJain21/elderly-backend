"""Photos in the family chat: stored next to the chat (elders/{id}/photos/{photoId}), described by Gemini.

The app shrinks a photo to ~1280 px before sending (~150–300 KB), so it fits in a Firestore document — no extra
storage service. Each photo gets a one-line description in the reader's language (read aloud for weak eyesight),
and is classified: a prescription or medicine pack is then read for medicines the family can add.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field

from app import firebase, usage
from app.agent.llm import genai_client
from app.config import get_settings

MAX_BYTES = 900_000  # under Firestore's 1 MiB document limit
MIMES = {"image/jpeg", "image/png", "image/webp"}

_PROMPT = (
    "A photo was shared in a family chat with an older person. Write ONE short, warm sentence in {lang} saying what "
    "it shows, for someone with weak eyesight who will hear it read aloud (who/what is in it, anything written that "
    "matters). No guessing names. Then classify it: 'prescription' (a doctor's prescription or medicine list), "
    "'medicine' (a pill strip, box or bottle), or 'photo' (anything else)."
)


class PhotoRead(BaseModel):
    caption: str = Field(description="One short sentence describing the photo.")
    kind: Literal["prescription", "medicine", "photo"] = "photo"


def _col(elder_id: str):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("photos")


def save(elder_id: str, data: bytes, mime: str, uploaded_by: str) -> str:
    photo_id = uuid.uuid4().hex
    _col(elder_id).document(photo_id).set({"data": data, "mime": mime, "uploadedBy": uploaded_by,
                                           "createdAt": dt.datetime.now(dt.UTC)})
    return photo_id


def load(elder_id: str, photo_id: str) -> tuple[bytes, str] | None:
    snap = _col(elder_id).document(photo_id).get()
    if not snap.exists:
        return None
    d = snap.to_dict() or {}
    return bytes(d["data"]), str(d.get("mime") or "image/jpeg")


def describe(data: bytes, mime: str, lang: str) -> PhotoRead:
    model = get_settings().gemini_model
    r = genai_client().models.generate_content(
        model=model, contents=[types.Part.from_bytes(data=data, mime_type=mime), "Describe this photo."],
        config=types.GenerateContentConfig(system_instruction=_PROMPT.format(lang=lang), temperature=0.2,
                                           response_mime_type="application/json", response_schema=PhotoRead),
    )
    usage.gemini(model, r, "photo-caption")
    return PhotoRead.model_validate_json(r.text) if r.text else PhotoRead(caption="")
