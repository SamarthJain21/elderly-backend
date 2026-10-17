"""People around the elder (elders/{id}/contacts) and help requests sent to volunteers (elders/{id}/requests)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from google.cloud.firestore import Query
from pydantic import BaseModel, Field, field_validator

from app import firebase
from app.validation import STORED, Name, Phone, ShortText

ContactKind = Literal["family", "friend", "neighbour", "volunteer", "doctor"]
RequestStatus = Literal["open", "notified", "confirmed", "done", "cancelled"]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ContactIn(BaseModel):
    name: Name
    phone: Phone
    kind: ContactKind = "family"
    relationship: ShortText = ""  # "son", "neighbour in 4B"…
    skills: list[str] = Field(default_factory=list, max_length=10)  # for volunteers: plumbing, tech help…
    trusted: bool = False
    email: str = Field(default="", max_length=200)
    # Illustration key for people without a photo (e.g. "emma"); the app shows initials when unset.
    face: str | None = Field(default=None, max_length=20)

    @field_validator("skills")
    @classmethod
    def _clean_skills(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in (x.strip().lower() for x in v):
            if s and s not in out:
                out.append(s[:40])
        return out


class Contact(ContactIn):
    id: str = Field(default_factory=lambda: f"con_{uuid.uuid4().hex[:12]}")
    addedBy: str = ""
    rating: float | None = None
    createdAt: dt.datetime = Field(default_factory=_now)


class HelpRequest(BaseModel):
    id: str = Field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    skill: str
    title: str
    description: str = ""
    preferredTime: str = ""
    volunteerId: str | None = None
    volunteerName: str | None = None
    status: RequestStatus = "open"
    notifiedVia: list[Literal["sms", "log"]] = Field(default_factory=list)
    createdAt: dt.datetime = Field(default_factory=_now)
    updatedAt: dt.datetime = Field(default_factory=_now)


def _contacts(elder_id: str):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("contacts")


def _requests(elder_id: str):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("requests")


def list_contacts(elder_id: str) -> list[Contact]:
    return sorted((Contact.model_validate(d.to_dict() or {}, context=STORED) for d in _contacts(elder_id).stream()),
                  key=lambda c: (c.kind != "family", c.name.lower()))


def get_contact(elder_id: str, contact_id: str) -> Contact | None:
    snap = _contacts(elder_id).document(contact_id).get()
    return Contact.model_validate(snap.to_dict(), context=STORED) if snap.exists else None


def save_contact(elder_id: str, c: Contact) -> None:
    _contacts(elder_id).document(c.id).set(c.model_dump())


def delete_contact(elder_id: str, contact_id: str) -> None:
    _contacts(elder_id).document(contact_id).delete()


def save_request(elder_id: str, r: HelpRequest) -> None:
    r.updatedAt = _now()
    _requests(elder_id).document(r.id).set(r.model_dump())


def get_request(elder_id: str, request_id: str) -> HelpRequest | None:
    snap = _requests(elder_id).document(request_id).get()
    return HelpRequest(**snap.to_dict()) if snap.exists else None


def recent_requests(elder_id: str, limit: int) -> list[HelpRequest]:
    docs = _requests(elder_id).order_by("createdAt", direction=Query.DESCENDING).limit(limit).stream()
    return [HelpRequest(**(d.to_dict() or {})) for d in docs]


# ---------- pure helpers (tested) ----------

def pick_volunteer(contacts: list[Contact], skill: str, trusted_only: bool) -> Contact | None:
    """Volunteers whose skills match; trusted first, then best rated. Respects the trusted-only rule."""
    want = skill.strip().lower()

    def matches(c: Contact) -> bool:
        return any(want in s or s in want for s in c.skills) if want else bool(c.skills)

    pool = [c for c in contacts if c.kind == "volunteer" and matches(c) and (c.trusted or not trusted_only)]
    pool.sort(key=lambda c: (not c.trusted, -(c.rating or 0), c.name.lower()))
    return pool[0] if pool else None


_REL_SYNONYMS = {
    "son": {"son", "beta", "boy"}, "daughter": {"daughter", "beti", "girl"},
    "doctor": {"doctor", "doc", "dr"}, "neighbour": {"neighbour", "neighbor"},
}


def find_contact(contacts: list[Contact], who: str) -> Contact | None:
    """Match "Ravi", "my son", "the doctor" against names, relationships and kinds."""
    q = who.strip().lower().removeprefix("my ").removeprefix("the ")
    if not q:
        return None
    for c in contacts:  # exact-ish name first
        if q == c.name.lower() or q in c.name.lower().split():
            return c
    for c in contacts:
        rel = c.relationship.lower()
        # "beti" → the whole daughter group, so it matches a contact whose relationship says "daughter".
        words = next((group for group in _REL_SYNONYMS.values() if q in group), {q})
        if any(w in rel.split() or w == rel for w in words) or (q in ("doctor", "doc", "dr") and c.kind == "doctor"):
            return c
    for c in contacts:  # partial name
        if q in c.name.lower():
            return c
    return None
