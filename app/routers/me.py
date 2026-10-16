from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from google.cloud.firestore import SERVER_TIMESTAMP, ArrayUnion
from pydantic import BaseModel, Field

from app import firebase, push
from app.auth import CurrentUser, Role
from app.routers.admin_api import is_admin
from app.validation import Name

router = APIRouter(tags=["auth"])


class Me(BaseModel):
    uid: str
    email: str | None
    role: Role
    admin: bool = False  # can open the cost dashboard


@router.get("/me")
def get_me(user: CurrentUser) -> Me:
    if user.role is None:
        # Signed in but not onboarded: frontend routes to /onboarding.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not onboarded")
    return Me(uid=user.uid, email=user.email, role=user.role, admin=is_admin(user.email))


class CaretakerSignup(BaseModel):
    name: Name


@router.post("/onboarding/caretaker", status_code=status.HTTP_201_CREATED)
def onboard_caretaker(body: CaretakerSignup, user: CurrentUser) -> Me:
    if user.role is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already onboarded as {user.role}")
    if user.provider != "google.com":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Caretakers must sign in with Google")

    db = firebase.get_db()
    batch = db.batch()
    batch.set(db.collection("users").document(user.uid), {
        "role": "caretaker",
        "displayName": body.name,
        "email": user.email,
        "createdAt": SERVER_TIMESTAMP,
    })
    # Pending invites from other caretakers (keyed by lowercase email) attach on first sign-in.
    invite_ref = db.collection("invites").document((user.email or "").lower())
    invited: list[str] = (invite_ref.get().to_dict() or {}).get("elderIds", []) if user.email else []
    batch.set(db.collection("caretakers").document(user.uid), {
        "uid": user.uid,
        "name": body.name,
        "email": user.email,
        "elderIds": invited,
    })
    for elder_id in invited:
        batch.update(db.collection("elders").document(elder_id), {"caretakerIds": ArrayUnion([user.uid])})
    if invited:
        batch.delete(invite_ref)
    batch.commit()
    return Me(uid=user.uid, email=user.email, role="caretaker")


class PushToken(BaseModel):
    token: str = Field(min_length=20, max_length=4096)


@router.post("/me/push-token", status_code=status.HTTP_204_NO_CONTENT)
def add_push_token(body: PushToken, user: CurrentUser) -> None:
    """This device wants notifications (SOS, worrying check-ins, approvals, messages). Family members only."""
    if user.role != "caretaker":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Notifications are for family members")
    push.add_token(user.uid, body.token)


@router.delete("/me/push-token", status_code=status.HTTP_204_NO_CONTENT)
def remove_push_token(body: PushToken, user: CurrentUser) -> None:
    push.remove_token(user.uid, body.token)
