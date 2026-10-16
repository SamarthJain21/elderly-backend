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

router = APIRouter(prefix="/elders", tags=["elders"])

Language = Literal["en-US", "hi-IN", "th-TH", "ja-JP", "ko-KR"]

# No 0/O, 1/l/I — the elder may have to type this off a phone screen.
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_temp_password(length: int = 10) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


class ElderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=5, max_length=20)
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


def _assessment_from(elder: dict[str, Any], rules: dict[str, Any]) -> Assessment | None:
    if "timezone" not in elder or "sleep" not in elder:
        return None
    return Assessment(
        timezone=elder["timezone"],
        sleep=Sleep(**elder["sleep"]),
        meals=Meals(**elder.get("meals", {})),
        medications=[Medication(**m) for m in elder.get("medicationSchedule", [])],
        checkIn=CheckInSchedule(
            time=rules.get("checkInTime", "09:00"),
            days=rules.get("checkInDays", CheckInSchedule().days),
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
    rules_snap = firebase.get_db().collection("rules").document(elder_id).get()
    return ElderProfile(
        elderId=elder_id,
        name=elder["name"],
        phone=elder["phone"],
        language=elder["language"],
        sharingTier=elder.get("sharingTier", "daily_summary"),
        assessment=_assessment_from(elder, rules_snap.to_dict() or {}),
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
