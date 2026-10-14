from __future__ import annotations

import secrets
import uuid
from typing import Literal

from fastapi import APIRouter, status
from firebase_admin import auth as fb_auth
from google.cloud.firestore import SERVER_TIMESTAMP, ArrayUnion
from pydantic import BaseModel, Field

from app import firebase
from app.auth import Caretaker
from app.config import get_settings

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
