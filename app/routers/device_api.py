"""The elder app's settings (voice, loudness, speed, text size), managed from the family app too — 'adjust Mum's
phone for her'. A website can't change the phone's own system volume; these are the app's own settings, applied live."""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import firebase
from app.access import load_elder
from app.agent import live
from app.auth import CurrentUser

router = APIRouter(prefix="/elders/{elder_id}/device-settings", tags=["device"])


class DeviceSettings(BaseModel):
    voice: bool = True                                    # read replies and messages aloud
    volume: float = Field(default=1.0, ge=0.1, le=1.0)    # loudness of the helper's voice
    rate: float = Field(default=1.0, ge=0.7, le=1.3)      # speaking speed (1 = normal; the voice is already a bit slow)
    textSize: Literal["normal", "large", "xlarge"] = "normal"


def read(elder: dict) -> DeviceSettings:  # type: ignore[type-arg]
    return DeviceSettings(**(elder.get("deviceSettings") or {}))


@router.get("")
def get_settings(elder_id: str, user: CurrentUser) -> DeviceSettings:
    return read(load_elder(user, elder_id))


@router.put("")
async def put_settings(elder_id: str, body: DeviceSettings, user: CurrentUser) -> DeviceSettings:
    """The elder (their own toggle) or any of their family. Open elder apps apply it straight away."""
    await asyncio.to_thread(load_elder, user, elder_id)
    await asyncio.to_thread(lambda: firebase.get_db().collection("elders").document(elder_id).update(
        {"deviceSettings": body.model_dump()}))
    await live.broadcast(elder_id, {"type": "device_settings", "settings": body.model_dump()}, "agent")
    return body
