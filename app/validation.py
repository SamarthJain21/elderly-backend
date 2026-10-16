"""Shared field rules, so every form the API accepts is checked the same way (the app checks too, but the API is
the line that matters)."""
from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

_PHONE_CHARS = re.compile(r"^\+[\d\s\-()]+$")


def _phone(v: str) -> str:
    v = " ".join(v.split())
    if not v.startswith("+"):
        raise ValueError("include the country code, e.g. +91 98765 43210")
    if not _PHONE_CHARS.match(v):
        raise ValueError("use digits only after the country code")
    digits = sum(ch.isdigit() for ch in v)
    if not 7 <= digits <= 15:  # E.164: at most 15 digits including the country code
        raise ValueError("a phone number has 7 to 15 digits")
    return v


Phone = Annotated[str, AfterValidator(_phone)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]
