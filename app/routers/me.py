from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from google.cloud.firestore import SERVER_TIMESTAMP
from pydantic import BaseModel

from app import firebase
from app.auth import CurrentUser, Role

router = APIRouter(tags=["auth"])


class Me(BaseModel):
    uid: str
    email: str | None
    role: Role


@router.get("/me")
def get_me(user: CurrentUser) -> Me:
    if user.role is None:
        # Signed in but not onboarded: frontend routes to /onboarding.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not onboarded")
    return Me(uid=user.uid, email=user.email, role=user.role)


class CaretakerSignup(BaseModel):
    name: str


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
    batch.set(db.collection("caretakers").document(user.uid), {
        "uid": user.uid,
        "name": body.name,
        "email": user.email,
        "elderIds": [],
    })
    batch.commit()
    return Me(uid=user.uid, email=user.email, role="caretaker")
