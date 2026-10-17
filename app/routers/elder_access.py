"""How an elder gets (back) into their app.

  POST /elders/{id}/setup-link  family: a fresh one-time password for a new setup link (the old link stops working)
  GET  /elders/{id}/pin         elder or family: is a sign-in PIN set?
  PUT  /elders/{id}/pin         elder or family: set / change the PIN
  POST /auth/pin                anyone: phone number + PIN → a Firebase custom token for the elder's account
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, Request, status
from firebase_admin import auth as fb_auth
from pydantic import BaseModel

from app import elder_pin, firebase, ratelimit
from app.access import load_elder
from app.auth import CurrentUser
from app.routers.elders import ElderCredentials, generate_temp_password
from app.routers.sandbox_api import client_ip
from app.validation import Phone

log = logging.getLogger(__name__)
router = APIRouter(tags=["elder access"])


class SetupLinkRequest(BaseModel):
    signOutOthers: bool = False  # lost phone: also end the sessions already signed in


@router.post("/elders/{elder_id}/setup-link")
def new_setup_link(elder_id: str, body: SetupLinkRequest, user: CurrentUser) -> ElderCredentials:
    load_elder(user, elder_id, caretaker_only=True)
    if user.sandbox:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not available in the sandbox")
    ratelimit.account_writes.check(user.uid)
    password = generate_temp_password()
    record = fb_auth.update_user(elder_id, password=password, app=firebase.get_app())
    if body.signOutOthers:
        fb_auth.revoke_refresh_tokens(elder_id, app=firebase.get_app())
    return ElderCredentials(elderId=elder_id, email=record.email or "", tempPassword=password)


class PinStatus(BaseModel):
    set: bool


class PinSet(BaseModel):
    pin: elder_pin.Pin


@router.get("/elders/{elder_id}/pin")
def pin_status(elder_id: str, user: CurrentUser) -> PinStatus:
    load_elder(user, elder_id)
    return PinStatus(set=elder_pin.has_pin(elder_id))


@router.put("/elders/{elder_id}/pin")
def set_pin(elder_id: str, body: PinSet, user: CurrentUser) -> PinStatus:
    elder = load_elder(user, elder_id)  # the elder themself, or their family (sandbox too: judges can try it)
    ratelimit.account_writes.check(user.uid)
    elder_pin.set_pin(elder_id, elder["phone"], body.pin)
    return PinStatus(set=True)


class PinLogin(BaseModel):
    phone: Phone
    pin: str  # not validated as a Pin: an old weak PIN must still get a plain "doesn't match"


class PinToken(BaseModel):
    token: str


@router.post("/auth/pin")
async def pin_login(body: PinLogin, request: Request) -> PinToken:
    ratelimit.pin_logins.check(client_ip(request))
    try:
        elder_id = await asyncio.to_thread(elder_pin.check, body.phone, body.pin)
    except elder_pin.Locked as e:
        mins = max(1, round((e.until - dt.datetime.now(dt.UTC)).total_seconds() / 60))
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Too many tries — wait {mins} minutes, or ask your family for a new link") from None
    if not elder_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That phone number and PIN don't match")
    try:
        return PinToken(token=await asyncio.to_thread(elder_pin.custom_token, elder_id))
    except Exception:
        log.exception("custom token failed (does the service account have Service Account Token Creator?)")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Can't sign in with a PIN right now — use your family's link") from None
