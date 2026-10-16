"""Caretaker-managed settings for one elder: autonomy rules and profile."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import firebase
from app.access import load_elder
from app.auth import CurrentUser
from app.autonomy import AutonomyRules, currency_for_timezone, default_rules
from app.routers.elders import Language
from app.tools import places
from app.tools.geo import geocode_sync
from app.validation import Name, Phone, ShortText

router = APIRouter(prefix="/elders/{elder_id}", tags=["settings"])

SharingTier = Literal["alerts_only", "daily_summary", "full_access"]


def _rules_from(doc: dict[str, Any], elder: dict[str, Any]) -> AutonomyRules:
    if "tools" not in doc or "currency" not in doc:
        return default_rules(currency_for_timezone(elder.get("timezone")))
    return AutonomyRules(
        currency=doc["currency"], tools=doc["tools"], trustedVolunteersOnly=doc.get("trustedVolunteersOnly", True),
    )


def load_rules(elder_id: str, elder: dict[str, Any]) -> AutonomyRules:
    doc = firebase.get_db().collection("rules").document(elder_id).get().to_dict() or {}
    return _rules_from(doc, elder)


@router.get("/rules")
def get_rules(elder_id: str, user: CurrentUser) -> AutonomyRules:
    return load_rules(elder_id, load_elder(user, elder_id))


@router.put("/rules")
def put_rules(elder_id: str, body: AutonomyRules, user: CurrentUser) -> AutonomyRules:
    load_elder(user, elder_id, caretaker_only=True)
    # merge=True keeps the check-in schedule that lives in the same document.
    firebase.get_db().collection("rules").document(elder_id).set(body.model_dump(), merge=True)
    return body


class EmergencyContact(BaseModel):
    name: Name
    phone: Phone
    relationship: ShortText = ""
    # Illustration key for people without a photo (e.g. "emma"); the app shows initials when unset.
    face: str | None = Field(default=None, max_length=20)


class Address(BaseModel):
    line1: str = Field(min_length=1, max_length=200)
    line2: str = Field(default="", max_length=200)
    landmark: str = Field(default="", max_length=120)
    city: str = Field(min_length=1, max_length=100)
    postcode: str = Field(default="", max_length=12)
    # Filled by geocoding on save; None if the address couldn't be located.
    lat: float | None = None
    lng: float | None = None
    # How precisely we located it. "city" means only the town matched — don't use it as a pickup point.
    precision: Literal["address", "street", "city"] | None = None
    # Set when the family picked the address from Google search: the server re-reads the place for its location.
    placeId: str | None = Field(default=None, max_length=300)

    def one_line(self) -> str:
        return ", ".join(x for x in (self.line1, self.line2, self.landmark, self.city, self.postcode) if x)


def _locate(a: Address, tz: str | None) -> Address:
    """Best effort. Picked from Google search → that place's exact location. Typed → try the full address, then
    without line2/landmark (OSM rarely knows flat numbers)."""
    if a.placeId:
        try:
            d = places.details(a.placeId)
            if d.get("lat") is not None:
                return a.model_copy(update={"lat": d["lat"], "lng": d["lng"], "precision": "address"})
        except Exception:  # noqa: BLE001 — fall back to the typed address
            pass
    attempts: list[tuple[str, Literal["address", "street", "city"]]] = [
        (a.one_line(), "address"),
        (", ".join(x for x in (a.line1, a.city, a.postcode) if x), "street"),
        (f"{a.city} {a.postcode}".strip(), "city"),
    ]
    for q, precision in attempts:
        try:
            place = geocode_sync(q, tz)
        except Exception:  # noqa: BLE001 — geocoding is optional; never block saving the profile
            return a
        if place is not None:
            return a.model_copy(update={"lat": place.lat, "lng": place.lng, "precision": precision})
    return a.model_copy(update={"lat": None, "lng": None, "precision": None})


class ProfileUpdate(BaseModel):
    name: Name
    phone: Phone
    language: Language
    sharingTier: SharingTier
    address: Address | None = None  # delivery + ride pickup; city also drives weather
    emergencyContacts: list[EmergencyContact] = Field(default_factory=list, max_length=10)


@router.get("/profile")
def get_profile(elder_id: str, user: CurrentUser) -> ProfileUpdate:
    e = load_elder(user, elder_id)
    return ProfileUpdate(
        name=e["name"], phone=e["phone"], language=e["language"],
        sharingTier=e.get("sharingTier", "daily_summary"), address=e.get("address"),
        emergencyContacts=e.get("emergencyContacts", []),
    )


@router.put("/profile")
def put_profile(elder_id: str, body: ProfileUpdate, user: CurrentUser) -> ProfileUpdate:
    elder = load_elder(user, elder_id, caretaker_only=True)
    if body.address is not None:
        old = elder.get("address") or {}
        unchanged = {k: old.get(k) for k in ("line1", "line2", "landmark", "city", "postcode")} == \
            body.address.model_dump(include={"line1", "line2", "landmark", "city", "postcode"})
        body.address = (body.address.model_copy(update={k: old.get(k) for k in ("lat", "lng", "precision")})
                        if unchanged else _locate(body.address, elder.get("timezone")))
    db = firebase.get_db()
    batch = db.batch()
    batch.update(db.collection("elders").document(elder_id), {
        **body.model_dump(), "city": body.address.city if body.address else elder.get("city", ""),
    })
    # users/{uid} mirrors name + language (used for login display and agent language).
    batch.update(db.collection("users").document(elder_id), {"displayName": body.name, "language": body.language})
    batch.commit()
    return body
