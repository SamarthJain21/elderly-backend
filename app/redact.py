"""Keep personal data out of server logs."""
from __future__ import annotations


def mask_phone(phone: str | None) -> str:
    """'+91 98765 43210' → '+91•••••210'. Enough to tell numbers apart when debugging, not enough to call."""
    if not phone:
        return "(none)"
    digits = [c for c in phone if c.isdigit()]
    if len(digits) <= 4:
        return "•" * len(digits)
    prefix = phone.strip()[:3] if phone.strip().startswith("+") else ""
    return f"{prefix}•••••{''.join(digits[-3:])}"
