"""Caretaker management of the elder's contacts, help requests, and co-caretakers."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from firebase_admin import auth as fb_auth
from google.cloud.firestore import ArrayUnion
from pydantic import BaseModel, Field

from app import contacts, firebase, ratelimit
from app.access import load_elder
from app.auth import CurrentUser
from app.contacts import Contact, ContactIn, HelpRequest

router = APIRouter(prefix="/elders/{elder_id}", tags=["people"])


@router.get("/contacts")
def list_contacts(elder_id: str, user: CurrentUser) -> list[Contact]:
    load_elder(user, elder_id, caretaker_only=True)
    return contacts.list_contacts(elder_id)


@router.post("/contacts", status_code=status.HTTP_201_CREATED)
def add_contact(elder_id: str, body: ContactIn, user: CurrentUser) -> Contact:
    load_elder(user, elder_id, caretaker_only=True)
    c = Contact(**body.model_dump(), addedBy=user.uid)
    contacts.save_contact(elder_id, c)
    return c


@router.put("/contacts/{contact_id}")
def update_contact(elder_id: str, contact_id: str, body: ContactIn, user: CurrentUser) -> Contact:
    load_elder(user, elder_id, caretaker_only=True)
    old = contacts.get_contact(elder_id, contact_id)
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    c = old.model_copy(update=body.model_dump())
    contacts.save_contact(elder_id, c)
    return c


@router.delete("/contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_contact(elder_id: str, contact_id: str, user: CurrentUser) -> None:
    load_elder(user, elder_id, caretaker_only=True)
    contacts.delete_contact(elder_id, contact_id)


@router.get("/requests")
def help_requests(elder_id: str, user: CurrentUser, limit: int = Query(20, ge=1, le=100)) -> list[HelpRequest]:
    load_elder(user, elder_id, caretaker_only=True)
    return contacts.recent_requests(elder_id, limit)


class RequestUpdate(BaseModel):
    status: Literal["confirmed", "done", "cancelled"]


@router.patch("/requests/{request_id}")
def update_request(elder_id: str, request_id: str, body: RequestUpdate, user: CurrentUser) -> HelpRequest:
    """Volunteer replies aren't parsed (spec): the caretaker confirms by phone and marks it here."""
    load_elder(user, elder_id, caretaker_only=True)
    r = contacts.get_request(elder_id, request_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    r.status = body.status
    contacts.save_request(elder_id, r)
    return r


class CaretakerOut(BaseModel):
    uid: str
    name: str
    email: str


@router.get("/caretakers")
def list_caretakers(elder_id: str, user: CurrentUser) -> list[CaretakerOut]:
    elder = load_elder(user, elder_id, caretaker_only=True)
    db = firebase.get_db()
    refs = [db.collection("caretakers").document(uid) for uid in elder.get("caretakerIds", [])]
    return [CaretakerOut(uid=s.id, name=(d := s.to_dict() or {}).get("name", ""), email=d.get("email", ""))
            for s in db.get_all(refs) if s.exists]


class Invite(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InviteResult(BaseModel):
    status: Literal["added", "invited"]


@router.post("/caretakers")
def invite_caretaker(elder_id: str, body: Invite, user: CurrentUser) -> InviteResult:
    """Existing caretaker → added now. Otherwise → pending invite, attached when they first sign in with Google."""
    load_elder(user, elder_id, caretaker_only=True)
    ratelimit.account_writes.check(user.uid)
    if user.sandbox:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available in the sandbox")
    email = body.email.strip().lower()
    db = firebase.get_db()
    try:
        record = fb_auth.get_user_by_email(email, app=firebase.get_app())
    except fb_auth.UserNotFoundError:
        record = None
    if record is not None:
        role = (db.collection("users").document(record.uid).get().to_dict() or {}).get("role")
        if role == "elder":
            raise HTTPException(status.HTTP_409_CONFLICT, "That email belongs to an elder account")
        if role == "caretaker":
            batch = db.batch()
            batch.update(db.collection("elders").document(elder_id), {"caretakerIds": ArrayUnion([record.uid])})
            batch.update(db.collection("caretakers").document(record.uid), {"elderIds": ArrayUnion([elder_id])})
            batch.commit()
            return InviteResult(status="added")
    db.collection("invites").document(email).set({"elderIds": ArrayUnion([elder_id]), "invitedBy": user.uid}, merge=True)
    return InviteResult(status="invited")
