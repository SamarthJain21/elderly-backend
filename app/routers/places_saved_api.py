"""Saved places for rides ('Clinic', 'Temple', "Priya's home"): the family sets them up; the helper books a cab from
the elder's home to them by name (app/tools/executor.py book_ride)."""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import firebase
from app.access import load_elder
from app.auth import CurrentUser
from app.routers.settings import Address, _locate
from app.validation import Name

router = APIRouter(prefix="/elders/{elder_id}/places", tags=["places"])


class SavedPlace(BaseModel):
    id: str = Field(default_factory=lambda: f"pl_{uuid.uuid4().hex[:10]}")
    name: Name                      # what they'd say: "Clinic", "Temple", "Priya's home"
    address: Address


def read(elder: dict) -> list[SavedPlace]:  # type: ignore[type-arg]
    return [SavedPlace(**p) for p in elder.get("places") or []]


@router.get("")
def get_places(elder_id: str, user: CurrentUser) -> list[SavedPlace]:
    return read(load_elder(user, elder_id))


@router.put("")
async def put_places(elder_id: str, body: list[SavedPlace], user: CurrentUser) -> list[SavedPlace]:
    """Replace the list (max 20). New or changed addresses are located on the map (exact if picked from search)."""
    elder = await asyncio.to_thread(load_elder, user, elder_id, caretaker_only=True)
    old = {p.id: p for p in read(elder)}
    out: list[SavedPlace] = []
    for p in body[:20]:
        prev = old.get(p.id)
        same = prev is not None and prev.address.one_line() == p.address.one_line() and prev.address.lat is not None
        addr = prev.address if same else await asyncio.to_thread(_locate, p.address, elder.get("timezone"))
        out.append(p.model_copy(update={"address": addr}))
    await asyncio.to_thread(lambda: firebase.get_db().collection("elders").document(elder_id).update(
        {"places": [p.model_dump() for p in out]}))
    return out


def match(places: list[SavedPlace], said: str) -> SavedPlace | None:
    """'the clinic' / 'clinic' / 'take me to Priya's home' → the saved place, if one clearly fits."""
    s = " ".join(said.lower().replace("'", "").split())
    for p in places:
        n = " ".join(p.name.lower().replace("'", "").split())
        if n and (n in s or s in n):
            return p
    return None
