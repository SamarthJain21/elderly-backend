from __future__ import annotations

from functools import lru_cache
from typing import Any

import firebase_admin
from firebase_admin import auth, credentials, firestore
from google.cloud.firestore import Client

from app.config import get_settings


@lru_cache
def get_app() -> firebase_admin.App:
    settings = get_settings()
    cred_path = settings.firebase_credentials
    cred = (
        credentials.Certificate(str(cred_path))
        if cred_path is not None and cred_path.exists()
        else credentials.ApplicationDefault()
    )
    return firebase_admin.initialize_app(cred, {"projectId": settings.firebase_project_id})


def get_db() -> Client:
    return firestore.client(get_app())


def verify_id_token(token: str) -> dict[str, Any]:
    """Raises firebase_admin.auth errors on invalid, expired or revoked tokens."""
    return auth.verify_id_token(token, app=get_app(), check_revoked=True)


def get_user_doc(uid: str) -> dict[str, Any] | None:
    snap = get_db().collection("users").document(uid).get()
    return snap.to_dict() if snap.exists else None
