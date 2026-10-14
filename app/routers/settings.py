"""Caretaker-managed settings for one elder: autonomy rules and profile."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, Field

from app import firebase
from app.access import load_elder
from app.auth import CurrentUser
from app.autonomy import AutonomyRules, currency_for_timezone, default_rules
from app.routers.elders import Language

router = APIRouter(prefix="/elders/{elder_id}", tags=["settings"])

SharingTier = Literal["alerts_only", "daily_summary", "full_access"]


def _rules_from(doc: dict[str, Any], elder: dict[str, Any]) -> AutonomyRules:
    if "tools" not in doc or "currency" not in doc:
        return default_rules(currency_for_timezone(elder.get("timezone")))
    return AutonomyRules(
        currency=doc["currency"], tools=doc["tools"], trustedVolunteersOnly=doc.get("trustedVolunteersOnly", True),
    )


@router.get("/rules")
def get_rules(elder_id: str, user: CurrentUser) -> AutonomyRules:
    elder = load_elder(user, elder_id)
    doc = firebase.get_db().collection("rules").document(elder_id).get().to_dict() or {}
    return _rules_from(doc, elder)


@router.put("/rules")
def put_rules(elder_id: str, body: AutonomyRules, user: CurrentUser) -> AutonomyRules:
    load_elder(user, elder_id, caretaker_only=True)
    # merge=True keeps the check-in schedule that lives in the same document.
    firebase.get_db().collection("rules").document(elder_id).set(body.model_dump(), merge=True)
    return body


class EmergencyContact(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=5, max_length=20)
    relationship: str = Field(default="", max_length=50)


class ProfileUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=5, max_length=20)
    language: Language
    sharingTier: SharingTier
    emergencyContacts: list[EmergencyContact] = Field(default_factory=list, max_length=10)


@router.get("/profile")
def get_profile(elder_id: str, user: CurrentUser) -> ProfileUpdate:
    e = load_elder(user, elder_id)
    return ProfileUpdate(
        name=e["name"], phone=e["phone"], language=e["language"],
        sharingTier=e.get("sharingTier", "daily_summary"), emergencyContacts=e.get("emergencyContacts", []),
    )


@router.put("/profile")
def put_profile(elder_id: str, body: ProfileUpdate, user: CurrentUser) -> ProfileUpdate:
    load_elder(user, elder_id, caretaker_only=True)
    db = firebase.get_db()
    batch = db.batch()
    batch.update(db.collection("elders").document(elder_id), body.model_dump())
    # users/{uid} mirrors name + language (used for login display and agent language).
    batch.update(db.collection("users").document(elder_id), {"displayName": body.name, "language": body.language})
    batch.commit()
    return body


class Approval(BaseModel):
    id: str
    actionId: str
    elderId: str
    status: Literal["pending", "approved", "declined"]
    elderRequestText: str


@router.get("/approvals")
def pending_approvals(elder_id: str, user: CurrentUser) -> list[Approval]:
    """Filled by the agent's tools later; caretaker sees what's waiting on them."""
    load_elder(user, elder_id, caretaker_only=True)
    docs = (
        firebase.get_db().collection("approvals")
        .where(filter=FieldFilter("elderId", "==", elder_id))
        .where(filter=FieldFilter("status", "==", "pending"))
        .stream()
    )
    return [Approval(id=d.id, **(d.to_dict() or {})) for d in docs]
