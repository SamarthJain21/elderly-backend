from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth as fb_auth

from app import firebase

Role = Literal["elder", "caretaker"]

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthedUser:
    """A verified Firebase user. `role` is None until onboarding creates users/{uid}."""

    uid: str
    email: str | None
    provider: str
    role: Role | None
    sandbox: bool = False  # a 'Try with real AI' account (app/sandbox.py)
    picture: str | None = None  # Google profile photo (family members)


def authenticate(token: str) -> AuthedUser:
    """Verify a Firebase ID token and load the user's role. Raises HTTPException (401/403/503)."""
    try:
        claims = firebase.verify_id_token(token)
    except (ValueError, fb_auth.InvalidIdTokenError, fb_auth.UserDisabledError) as exc:
        # InvalidIdTokenError covers expired and revoked tokens too.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    except fb_auth.CertificateFetchError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth unavailable") from exc

    uid: str = claims["uid"]
    provider: str = claims.get("firebase", {}).get("sign_in_provider", "")
    doc = firebase.get_user_doc(uid)
    role = doc.get("role") if doc else None
    if role not in ("elder", "caretaker", None):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Unknown role")

    # Caretakers must use Google; a password login claiming caretaker is rejected — except sandbox caretakers,
    # whose user doc only the server writes (app/sandbox.py).
    sandbox = bool(doc and doc.get("sandbox"))
    if role == "caretaker" and provider != "google.com" and not sandbox:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Caretakers must sign in with Google")

    return AuthedUser(uid=uid, email=claims.get("email"), provider=provider, role=role, sandbox=sandbox,
                      picture=claims.get("picture") if provider == "google.com" else None)


def get_authed_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthedUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return authenticate(creds.credentials)


def require_role(role: Role) -> Callable[[AuthedUser], AuthedUser]:
    def dep(user: Annotated[AuthedUser, Depends(get_authed_user)]) -> AuthedUser:
        if user.role != role:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role {role}")
        return user

    return dep


CurrentUser = Annotated[AuthedUser, Depends(get_authed_user)]
Caretaker = Annotated[AuthedUser, Depends(require_role("caretaker"))]
Elder = Annotated[AuthedUser, Depends(require_role("elder"))]
