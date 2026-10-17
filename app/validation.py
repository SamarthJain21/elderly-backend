"""Shared field rules, so every form the API accepts is checked the same way (the app checks too, but the API is
the line that matters)."""
from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, StringConstraints, ValidationInfo

_PHONE_CHARS = re.compile(r"^\+[\d\s\-()]+$")


# Reading back what's already saved: numbers from before these rules (e.g. '9592812463', no country code) must
# not break the whole list — they're shown as they are, and the rules apply when the family next edits them.
STORED = {"stored": True}


def _phone(v: str, info: ValidationInfo) -> str:
    v = " ".join(v.split())
    if info.context == STORED:
        return v
    if not v.startswith("+"):
        raise ValueError("include the country code, e.g. +91 98765 43210")
    if not _PHONE_CHARS.match(v):
        raise ValueError("use digits only after the country code")
    digits = sum(ch.isdigit() for ch in v)
    if not 7 <= digits <= 15:  # E.164: at most 15 digits including the country code
        raise ValueError("a phone number has 7 to 15 digits")
    return v


Phone = Annotated[str, AfterValidator(_phone)]

# The country an elder's timezone implies, for numbers saved before the country code was required.
_DIAL_BY_TZ = {"Asia/Kolkata": "+91", "Asia/Calcutta": "+91", "Asia/Bangkok": "+66", "Asia/Singapore": "+65",
               "Asia/Kuala_Lumpur": "+60", "Asia/Dubai": "+971", "Asia/Tokyo": "+81", "Asia/Seoul": "+82"}


def with_country(phone: str, tz: str | None) -> str:
    """'9592812463' (saved before country codes were required) → '+91 9592812463' for an elder in India.
    Anything that already has a + (or isn't just digits) is left as it is."""
    v = " ".join(str(phone).split())
    digits = v.replace(" ", "").replace("-", "")
    if not v or v.startswith("+") or not digits.isdigit():
        return v
    return f"{_DIAL_BY_TZ.get(tz or '', '+91')} {digits.lstrip('0')}"
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]
