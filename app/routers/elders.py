from __future__ import annotations

import secrets
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from firebase_admin import auth as fb_auth
from google.cloud.firestore import SERVER_TIMESTAMP, ArrayUnion
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, Field

from app import firebase, ratelimit
from app.access import load_elder
from app.auth import Caretaker, CurrentUser
from app.config import get_settings
from app.schedule import Assessment, CheckInSchedule, Meals, Medication, Sleep, medication_reminders
from app.validation import Name, Phone, with_country

router = APIRouter(prefix="/elders", tags=["elders"])

# hi-Latn = Hinglish: Hindi written in Roman letters, mixed with English the way people text in India.
Language = Literal["en-US", "hi-IN", "hi-Latn", "th-TH", "ja-JP", "ko-KR"]

# No 0/O, 1/l/I — the elder may have to type this off a phone screen.
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_temp_password(length: int = 10) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


class ElderCreate(BaseModel):
    name: Name
    phone: Phone
    language: Language = "en-US"


class ElderCredentials(BaseModel):
    elderId: str
    email: str
    tempPassword: str


@router.post("", status_code=status.HTTP_201_CREATED)
def create_elder(body: ElderCreate, caretaker: Caretaker) -> ElderCredentials:
    ratelimit.account_writes.check(caretaker.uid)
    if caretaker.sandbox:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available in the sandbox")
    email = f"elder-{uuid.uuid4()}@{get_settings().elder_email_domain}"
    password = generate_temp_password()
    record = fb_auth.create_user(
        email=email, password=password, display_name=body.name, app=firebase.get_app()
    )
    elder_id = record.uid  # elderId == the elder's auth uid

    db = firebase.get_db()
    batch = db.batch()
    batch.set(db.collection("users").document(elder_id), {
        "role": "elder",
        "displayName": body.name,
        "email": email,
        "language": body.language,
        "createdAt": SERVER_TIMESTAMP,
    })
    batch.set(db.collection("elders").document(elder_id), {
        "uid": elder_id,
        "name": body.name,
        "phone": body.phone,
        "language": body.language,
        "medicationSchedule": [],
        "sharingTier": "daily_summary",
        "caretakerIds": [caretaker.uid],
        "emergencyContacts": [],
        "baselineWindowDays": 7,
    })
    batch.update(db.collection("caretakers").document(caretaker.uid), {
        "elderIds": ArrayUnion([elder_id]),
    })
    try:
        batch.commit()
    except Exception:
        # Don't leave an orphan auth account with no profile.
        fb_auth.delete_user(elder_id, app=firebase.get_app())
        raise

    return ElderCredentials(elderId=elder_id, email=email, tempPassword=password)


class ElderSummary(BaseModel):
    elderId: str
    name: str
    language: Language
    assessed: bool


class ElderProfile(BaseModel):
    elderId: str
    name: str
    phone: str
    language: Language
    sharingTier: str
    assessment: Assessment | None
    # Names only (no emails/phones) so the elder's app can say "Samarth set this up for you".
    caretakerNames: list[str] = []
    # Their family as the elder sees them: names + Google photos (no emails or phones).
    family: list[dict[str, str | None]] = []
    face: str | None = None  # illustration key (sandbox personas); initials when unset
    appReady: bool = False   # the elder finished the first-run walkthrough on some device — don't show it again


def _assessment_from(elder: dict[str, Any], rules: dict[str, Any]) -> Assessment | None:
    if "timezone" not in elder or "sleep" not in elder:
        return None
    return Assessment(
        timezone=elder["timezone"],
        sleep=Sleep(**elder["sleep"]),
        meals=Meals(**elder.get("meals", {})),
        medications=[Medication(**m) for m in elder.get("medicationSchedule", [])],
        checkIn=CheckInSchedule(
            enabled=rules.get("checkInEnabled", True),
            time=rules.get("checkInTime", "09:00"),
            days=rules.get("checkInDays") or CheckInSchedule().days,
        ),
        healthNotes=elder.get("healthNotes", ""),
    )


@router.get("")
def list_my_elders(caretaker: Caretaker) -> list[ElderSummary]:
    docs = (
        firebase.get_db().collection("elders")
        .where(filter=FieldFilter("caretakerIds", "array_contains", caretaker.uid))
        .stream()
    )
    out = [
        ElderSummary(elderId=d.id, name=e["name"], language=e["language"], assessed="sleep" in e)
        for d in docs
        if (e := d.to_dict() or {})
    ]
    return sorted(out, key=lambda x: x.name.lower())


@router.get("/{elder_id}")
def get_elder(elder_id: str, user: CurrentUser) -> ElderProfile:
    elder = load_elder(user, elder_id)
    db = firebase.get_db()
    rules_snap = db.collection("rules").document(elder_id).get()
    refs = [db.collection("caretakers").document(uid) for uid in elder.get("caretakerIds", [])]
    cts = [snap.to_dict() or {} for snap in db.get_all(refs) if snap.exists] if refs else []
    names = [str(c.get("name") or "") for c in cts]
    return ElderProfile(
        elderId=elder_id,
        name=elder["name"],
        phone=with_country(elder["phone"], elder.get("timezone")),
        language=elder["language"],
        sharingTier=elder.get("sharingTier", "daily_summary"),
        assessment=_assessment_from(elder, rules_snap.to_dict() or {}),
        caretakerNames=[n for n in names if n],
        family=[{"name": str(c.get("name")), "photo": c.get("photoUrl")} for c in cts if c.get("name")],
        face=elder.get("face"),
        appReady=bool(elder.get("appReadyAt")) or bool(elder.get("sandbox")),
    )


@router.put("/{elder_id}/assessment")
def save_assessment(elder_id: str, body: Assessment, user: CurrentUser) -> Assessment:
    load_elder(user, elder_id, caretaker_only=True)
    db = firebase.get_db()
    reminders = db.collection("reminders")

    batch = db.batch()
    batch.update(db.collection("elders").document(elder_id), {
        "timezone": body.timezone,
        "sleep": body.sleep.model_dump(),
        "meals": body.meals.model_dump(),
        "medicationSchedule": [m.model_dump() for m in body.medications],
        "healthNotes": body.healthNotes,
        "assessedAt": SERVER_TIMESTAMP,
    })
    batch.set(db.collection("rules").document(elder_id), {
        "checkInTime": body.checkIn.time,
        "checkInEnabled": body.checkIn.enabled,
        "checkInDays": body.checkIn.days,
    }, merge=True)

    # Medication reminders are derived data: replace them wholesale.
    stale = reminders.where(filter=FieldFilter("elderId", "==", elder_id)).where(
        filter=FieldFilter("kind", "==", "medication")).stream()
    for d in stale:
        batch.delete(d.reference)
    for r in medication_reminders(elder_id, body.medications):
        batch.set(reminders.document(r.id), r.model_dump(mode="json"))
    batch.commit()
    return body


@router.post("/{elder_id}/app-ready", status_code=status.HTTP_204_NO_CONTENT)
def mark_app_ready(elder_id: str, user: CurrentUser) -> None:
    """The elder finished the walkthrough: remembered on the server, so a new phone or cleared browser skips it."""
    load_elder(user, elder_id)
    firebase.get_db().collection("elders").document(elder_id).update({"appReadyAt": SERVER_TIMESTAMP})
