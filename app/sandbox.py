"""'Try with real AI': a throwaway elder + caretaker pair per visitor, seeded with a realistic fortnight.

Everything is real (Firebase sign-in, Firestore, Gemini) except outside effects: orders use the demo providers and
sandbox phone numbers (+000…) are never texted (app/sms.py). Sandboxes expire after SANDBOX_TTL and are erased by
the next visitor's request (cleanup_expired)."""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo

from firebase_admin import auth as fb_auth
from google.cloud.firestore import SERVER_TIMESTAMP
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from app import firebase
from app.checkin import CheckIn, checkin_id
from app.config import get_settings
from app.contacts import Contact
from app.erase import erase_elder
from app.schedule import ALL_DAYS, Medication, medication_reminders

log = logging.getLogger(__name__)

SANDBOX_TTL = dt.timedelta(hours=24)
SandboxLanguage = Literal["en-US", "hi-IN", "hi-Latn", "th-TH"]
FAKE_PHONE_PREFIX = "+000"  # app/sms.py never texts these


@dataclass(frozen=True)
class Persona:
    elder: str
    caretaker: str
    relationship: str           # the caretaker, as the elder would say it
    timezone: str
    city: str
    address: dict[str, Any]
    meds: tuple[tuple[str, str, tuple[str, ...], bool], ...]   # name, dose, times, with food
    meals: tuple[str, ...]      # what they ate, newest last
    neighbour: str
    son: str
    doctor: str
    health_notes: str


PERSONAS: dict[SandboxLanguage, Persona] = {
    "en-US": Persona(
        "Rose", "Emma", "granddaughter", "Asia/Kolkata", "Pune",
        {"line1": "12 MG Road", "line2": "Flat 3B", "landmark": "Near Camp market", "city": "Pune", "postcode": "411001",
         "lat": 18.5167, "lng": 73.8777, "precision": "street"},
        (("Amlodipine", "5mg", ("08:00", "20:00"), True), ("Metformin", "500mg", ("08:00",), True)),
        ("porridge and tea", "dal, rice and a banana", "idli and coffee", "toast and an egg"),
        "David", "Tom", "Dr Lewis", "Mild high blood pressure and type 2 diabetes. Knees ache on cold mornings.",
    ),
    "hi-IN": Persona(
        "Kamla", "Priya", "daughter", "Asia/Kolkata", "New Delhi",
        {"line1": "B-14 Lajpat Nagar II", "line2": "", "landmark": "Near Central Market", "city": "New Delhi",
         "postcode": "110024", "lat": 28.5677, "lng": 77.2433, "precision": "street"},
        (("Amlodipine", "5mg", ("08:00", "20:00"), True), ("Metformin", "500mg", ("08:00",), True)),
        ("poha and chai", "dal, roti and sabzi", "khichdi", "paratha and curd"),
        "Mr Gupta", "Rahul", "Dr Mehta", "High blood pressure and type 2 diabetes. Walks slowly on stairs.",
    ),
    "hi-Latn": Persona(  # Hinglish: same household as Hindi; the helper writes Roman-script Hindi
        "Kamla", "Priya", "daughter", "Asia/Kolkata", "New Delhi",
        {"line1": "B-14 Lajpat Nagar II", "line2": "", "landmark": "Near Central Market", "city": "New Delhi",
         "postcode": "110024", "lat": 28.5677, "lng": 77.2433, "precision": "street"},
        (("Amlodipine", "5mg", ("08:00", "20:00"), True), ("Metformin", "500mg", ("08:00",), True)),
        ("poha and chai", "dal, roti and sabzi", "khichdi", "paratha and curd"),
        "Mr Gupta", "Rahul", "Dr Mehta", "High blood pressure and type 2 diabetes. Walks slowly on stairs.",
    ),
    "th-TH": Persona(
        "Somsri", "Nok", "daughter", "Asia/Bangkok", "Bangkok",
        {"line1": "45 Sukhumvit Soi 24", "line2": "", "landmark": "Near Emporium", "city": "Bangkok",
         "postcode": "10110", "lat": 13.7246, "lng": 100.5660, "precision": "street"},
        (("Amlodipine", "5mg", ("08:00", "20:00"), True), ("Metformin", "500mg", ("08:00",), True)),
        ("rice porridge (jok)", "khao man gai", "noodle soup", "rice with stir-fried vegetables"),
        "Khun Somchai", "Lek", "Dr Suda", "High blood pressure and type 2 diabetes. Sometimes forgets the evening pill.",
    ),
}

# The fortnight before today, oldest first: steady, one off day (not a trend), then the last 2 days low → insights
# shows "Keep an eye". Today is left open so the visitor does the check-in live.
_HISTORY: list[tuple[str, str, bool]] = [  # mood, sleep, medicine taken
    ("good", "well", True), ("great", "well", True), ("good", "okay", True), ("good", "well", True),
    ("great", "well", True), ("good", "well", True), ("okay", "poorly", True), ("good", "well", True),
    ("good", "well", True), ("great", "okay", True), ("good", "well", True), ("low", "okay", True),
    ("low", "poorly", False),
]


class Credentials(BaseModel):
    name: str
    email: str
    password: str


class Sandbox(BaseModel):
    elderId: str
    language: SandboxLanguage
    elder: Credentials
    caretaker: Credentials
    expiresAt: dt.datetime


def checkin_time(now: dt.datetime, tz: str) -> str:
    """Today's check-in is due when the visitor arrives (rounded down to the quarter hour), so they can try it."""
    local = now.astimezone(ZoneInfo(tz))
    return f"{local.hour:02d}:{local.minute // 15 * 15:02d}"


def _fake_phone() -> str:
    return f"{FAKE_PHONE_PREFIX} {secrets.randbelow(10**7):07d}"


def _password() -> str:
    return secrets.token_urlsafe(12)


def _history(elder_id: str, p: Persona, today: dt.date) -> list[CheckIn]:
    out: list[CheckIn] = []
    tz = ZoneInfo(p.timezone)
    for i, (mood, sleep, took) in enumerate(_HISTORY):
        day = today - dt.timedelta(days=len(_HISTORY) - i)
        at = dt.datetime.combine(day, dt.time(9, 10), tz).astimezone(dt.UTC)
        low = mood in ("low", "bad")
        out.append(CheckIn(
            id=checkin_id(elder_id, day), elderId=elder_id, date=day, status="done", mood=mood,  # type: ignore[arg-type]
            sleepQuality=sleep, medicationTaken=took,  # type: ignore[arg-type]
            meals="just some tea" if low else p.meals[i % len(p.meals)],
            anomalies=(["feeling lonely"] if low else []) + ([] if took else ["missed morning medicine"]),
            turns=4, startedAt=at, completedAt=at + dt.timedelta(minutes=3),
        ))
    return out


def create_sandbox(language: SandboxLanguage, now: dt.datetime | None = None) -> Sandbox:
    now = now or dt.datetime.now(dt.UTC)
    p = PERSONAS[language]
    domain = get_settings().elder_email_domain
    app, db = firebase.get_app(), firebase.get_db()
    expires = now + SANDBOX_TTL
    tag = secrets.token_hex(4)

    ct = Credentials(name=p.caretaker, email=f"sandbox-family-{tag}@{domain}", password=_password())
    el = Credentials(name=p.elder, email=f"sandbox-elder-{tag}@{domain}", password=_password())
    ct_uid = fb_auth.create_user(email=ct.email, password=ct.password, display_name=ct.name, app=app).uid
    try:
        elder_id = fb_auth.create_user(email=el.email, password=el.password, display_name=el.name, app=app).uid
    except Exception:
        fb_auth.delete_user(ct_uid, app=app)
        raise

    try:
        meds = [Medication(id=f"med{i}", name=n, dose=d, times=list(t), withFood=f) for i, (n, d, t, f) in enumerate(p.meds)]
        batch = db.batch()
        sandbox = {"sandbox": True, "sandboxExpiresAt": expires}
        batch.set(db.collection("users").document(ct_uid), {
            "role": "caretaker", "displayName": ct.name, "email": ct.email, "createdAt": SERVER_TIMESTAMP, **sandbox})
        batch.set(db.collection("caretakers").document(ct_uid), {
            "uid": ct_uid, "name": ct.name, "email": ct.email, "elderIds": [elder_id], **sandbox})
        batch.set(db.collection("users").document(elder_id), {
            "role": "elder", "displayName": el.name, "email": el.email, "language": language,
            "createdAt": SERVER_TIMESTAMP, **sandbox})
        emma_phone = _fake_phone()
        batch.set(db.collection("elders").document(elder_id), {
            "uid": elder_id, "name": el.name, "phone": _fake_phone(), "language": language,
            "sharingTier": "daily_summary", "caretakerIds": [ct_uid], "baselineWindowDays": 7,
            "timezone": p.timezone, "city": p.city, "address": p.address,
            "sleep": {"bedtime": "22:00", "wakeTime": "06:30"},
            "meals": {"breakfast": "08:00", "lunch": "13:00", "dinner": "19:30"},
            "medicationSchedule": [m.model_dump() for m in meds], "healthNotes": p.health_notes,
            "emergencyContacts": [{"name": ct.name, "phone": emma_phone, "relationship": p.relationship}],
            "assessedAt": SERVER_TIMESTAMP, "sandboxCaretakerId": ct_uid, **sandbox,
        })
        batch.set(db.collection("rules").document(elder_id), {"checkInTime": checkin_time(now, p.timezone),
                                                              "checkInDays": list(ALL_DAYS)})
        for r in medication_reminders(elder_id, meds):
            batch.set(db.collection("reminders").document(r.id), r.model_dump(mode="json"))
        people = [
            Contact(name=ct.name, phone=emma_phone, kind="family", relationship=p.relationship, trusted=True),
            Contact(name=p.son, phone=_fake_phone(), kind="family", relationship="son", trusted=True),
            Contact(name=p.neighbour, phone=_fake_phone(), kind="volunteer", relationship="neighbour",
                    skills=["plumbing", "shopping", "tech help"], trusted=True),
            Contact(name=p.doctor, phone=_fake_phone(), kind="doctor", relationship="doctor"),
        ]
        contacts = db.collection("elders").document(elder_id).collection("contacts")
        for c in people:
            batch.set(contacts.document(c.id), c.model_dump(mode="json") | {"addedBy": ct_uid})
        today = now.astimezone(ZoneInfo(p.timezone)).date()
        for c in _history(elder_id, p, today):
            batch.set(db.collection("check_ins").document(c.id), c.model_dump(mode="json"))
        batch.commit()
    except Exception:
        erase_elder(elder_id, also_caretakers=[ct_uid])
        raise
    log.info("sandbox %s created (%s), expires %s", elder_id, language, expires.isoformat())
    return Sandbox(elderId=elder_id, language=language, elder=el, caretaker=ct, expiresAt=expires)


def cleanup_expired(now: dt.datetime | None = None, limit: int = 10) -> int:
    """Erase up to `limit` expired sandboxes. Called on each new sandbox so they never pile up."""
    now = now or dt.datetime.now(dt.UTC)
    db = firebase.get_db()
    snaps = (db.collection("elders").where(filter=FieldFilter("sandboxExpiresAt", "<", now)).limit(limit).stream())
    n = 0
    for s in snaps:
        e = s.to_dict() or {}
        if not e.get("sandbox"):  # belt and braces: never erase a real elder
            continue
        erase_elder(s.id, also_caretakers=[e["sandboxCaretakerId"]] if e.get("sandboxCaretakerId") else [])
        n += 1
    return n
