"""Photos in the family chat (app/agent/photos.py).

  POST /elders/{id}/family/photo   elder or family: send a photo (+ optional words); Gemini describes it for the
                                   reader, and an elder's prescription photo comes with the medicines read from it
  GET  /elders/{id}/photos/{pid}   the image itself, for the elder and their family only
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from app import push, ratelimit, rx_scan
from app.access import load_elder
from app.agent import family, live, photos
from app.agent.family import FamilyMessage
from app.agent.translate import language_name
from app.auth import CurrentUser
from app.schedule import Meals

router = APIRouter(tags=["family"])
log = logging.getLogger(__name__)


class PhotoPost(BaseModel):
    image: str = Field(min_length=100, max_length=photos.MAX_BYTES * 4 // 3 + 4, description="base64")
    mimeType: str
    text: str = Field(default="", max_length=300)  # optional words with the photo


@router.post("/elders/{elder_id}/family/photo")
async def send_photo(elder_id: str, body: PhotoPost, user: CurrentUser) -> FamilyMessage:
    elder = await asyncio.to_thread(load_elder, user, elder_id)  # the elder themself, or their family
    if body.mimeType not in photos.MIMES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Send a JPEG, PNG or WebP photo")
    try:
        data = base64.b64decode(body.image, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That photo didn't come through — try again") from None
    if len(data) > photos.MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "That photo is too large")
    ratelimit.photo_posts.check(user.uid)

    from_elder = user.role == "elder"
    reader_lang = "en-US" if from_elder else elder.get("language", "en-US")  # described for whoever receives it
    photo_id = await asyncio.to_thread(photos.save, elder_id, data, body.mimeType, user.uid)
    try:
        read = await asyncio.to_thread(photos.describe, data, body.mimeType, language_name(reader_lang))
    except Exception:  # noqa: BLE001 — the photo still goes; it just has no description
        log.warning("photo caption failed for elder %s", elder_id, exc_info=True)
        read = photos.PhotoRead(caption="")

    name = elder.get("name", "") if from_elder else await asyncio.to_thread(family.caretaker_name, user.uid)
    msg = FamilyMessage(sender="elder" if from_elder else "caretaker", senderUid=user.uid, senderName=name,
                        text=body.text.strip() or "📷", photoId=photo_id, photoAlt=read.caption or None)
    await asyncio.to_thread(family.save, elder_id, msg)
    await live.broadcast(elder_id, {"type": "family_message", "message": msg.model_dump(mode="json")}, "family")
    if from_elder:
        push.notify_family(elder, f"📷 {elder.get('name', '')}", read.caption or "sent a photo", "/caretaker/chat")

    if from_elder and read.kind in ("prescription", "medicine"):
        # Read the medicines for the family to review (never saved by itself). Arrives as an update to the message.
        meals = Meals(**(elder.get("meals") or {}))
        existing = [f"{m.get('name', '')} {m.get('dose', '')}".strip() for m in elder.get("medicationSchedule") or []]
        try:
            found = await asyncio.to_thread(rx_scan.scan, data, body.mimeType, meals, existing)
        except Exception:  # noqa: BLE001
            log.warning("prescription read failed for elder %s", elder_id, exc_info=True)
            found = None
        if found and not found.unreadable:
            msg.rx = found.medicines
            await asyncio.to_thread(family.set_rx, elder_id, msg.id, [m.model_dump() for m in found.medicines])
            await live.broadcast(elder_id, {"type": "family_update", "message": msg.model_dump(mode="json")}, "family")
    return msg


@router.get("/elders/{elder_id}/photos/{photo_id}", response_class=Response, responses={200: {"content": {"image/jpeg": {}}}})
async def get_photo(elder_id: str, photo_id: str, user: CurrentUser) -> Response:
    await asyncio.to_thread(load_elder, user, elder_id)
    got = await asyncio.to_thread(photos.load, elder_id, photo_id)
    if got is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found")
    data, mime = got
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})
