"""Outgoing SMS. Twilio when configured (TWILIO_* env vars), else recorded in the activity log only."""
from __future__ import annotations

import logging
from typing import Literal

import httpx

from app.config import get_settings
from app.redact import mask_phone

log = logging.getLogger(__name__)


def send_sms(to: str, body: str) -> Literal["sms", "log"]:
    """Returns how it was delivered. Never raises: a failed text shouldn't break the agent turn."""
    s = get_settings()
    if not (s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number):
        # Never log the body: it carries names, addresses and health details.
        log.info("SMS not sent (Twilio not configured) to %s, %d chars", mask_phone(to), len(body))
        return "log"
    try:
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json",
            auth=(s.twilio_account_sid, s.twilio_auth_token.get_secret_value()),
            data={"To": to, "From": s.twilio_from_number, "Body": body[:1500]},
            timeout=10,
        )
        r.raise_for_status()
        return "sms"
    except Exception:  # noqa: BLE001
        log.exception("Twilio send failed to %s", mask_phone(to))
        return "log"
