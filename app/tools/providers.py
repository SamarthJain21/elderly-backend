"""Mock delivery/ride providers. Real partner APIs aren't public; these return deterministic,
realistic quotes so the autonomy + approval flow can be demoed end to end.

Prices are integer minor units. The quote — never the model's guess — is what limits are checked against.
"""
from __future__ import annotations

import hashlib
import re
import uuid

from pydantic import BaseModel

from app.autonomy import Currency

FOOD_PROVIDER: dict[Currency, str] = {"INR": "Swiggy", "THB": "LINE MAN", "SGD": "GrabFood"}
PHARMACY: dict[Currency, str] = {"INR": "Blinkit", "THB": "Grab Mart", "SGD": "Guardian"}
RIDES: dict[Currency, str] = {"INR": "Uber", "THB": "Grab", "SGD": "Grab"}

# Base prices in INR paise; other currencies scale by a fixed demo rate.
_FOOD_INR = {
    "dal": 12_000, "roti": 2_500, "chapati": 2_500, "rice": 8_000, "idli": 6_000, "dosa": 9_000, "khichdi": 14_000,
    "soup": 11_000, "sandwich": 13_000, "tea": 3_000, "chai": 3_000, "coffee": 6_000, "fruit": 9_000, "milk": 3_500,
    "bread": 4_500, "eggs": 7_000, "curd": 4_000, "dahi": 4_000, "noodles": 15_000, "porridge": 10_000, "jok": 10_000,
}
_DEFAULT_FOOD_INR = 15_000
_DELIVERY_INR = 3_000
_MEDICINE_INR = 9_000  # per strip/pack
_SCALE: dict[Currency, float] = {"INR": 1.0, "THB": 0.4, "SGD": 0.016}
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a": 1, "an": 1}


class Quote(BaseModel):
    provider: str
    lines: list[str]          # human-readable, e.g. "2 × roti"
    amountMinor: int
    currency: Currency
    eta: str
    estimate: bool = False    # fare estimate (rides) — the app sets the real price
    demo: bool = True         # simulated provider: nothing is really ordered


def _convert(inr_minor: int, currency: Currency) -> int:
    # Round to whole units so mock prices look real (฿48, not ฿48.37).
    return max(100, round(inr_minor * _SCALE[currency] / 100) * 100)


def _qty_and_name(item: str) -> tuple[int, str]:
    m = re.match(r"^\s*(\d+|one|two|three|four|five|six|a|an)\s*(?:x|×)?\s+(.*)$", item.strip(), re.IGNORECASE)
    if m is None:
        return 1, item.strip()
    raw = m.group(1).lower()
    return (int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]), m.group(2).strip()


def _price_for(name: str) -> int:
    lowered = name.lower()
    for key, price in _FOOD_INR.items():
        if key in lowered:
            return price
    return _DEFAULT_FOOD_INR


def quote_food(items: list[str], currency: Currency) -> Quote:
    total, lines = _DELIVERY_INR, []
    for item in items:
        qty, name = _qty_and_name(item)
        qty = min(qty, 10)
        total += qty * _price_for(name)
        lines.append(f"{qty} × {name}")
    return Quote(provider=FOOD_PROVIDER[currency], lines=lines, amountMinor=_convert(total, currency),
                 currency=currency, eta="30–40 min")


def quote_medicine(medicine: str, quantity: int, currency: Currency) -> Quote:
    quantity = max(1, min(quantity, 6))
    return Quote(provider=PHARMACY[currency], lines=[f"{quantity} × {medicine}"],
                 amountMinor=_convert(quantity * _MEDICINE_INR, currency), currency=currency, eta="about 20 min")


# Rough taxi fare: base + per km (minor units), road distance ≈ 1.3 × straight line.
_FARE: dict[Currency, tuple[int, int]] = {"INR": (5_000, 1_800), "THB": (3_500, 800), "SGD": (400, 80)}


def quote_ride(destination: str, km: float | None, currency: Currency, from_here: bool | None = None) -> Quote:
    """A simulated in-app booking (like food and medicine) until a ride partner API is connected; fare is an estimate."""
    if km is None:
        h = int(hashlib.sha256(destination.lower().encode()).hexdigest(), 16)
        km = 3 + (h % 120) / 10  # unknown distance: deterministic 3–15 km
    road_km = km * 1.3
    base, per_km = _FARE[currency]
    fare = base + round(per_km * road_km)
    lines = [f"Ride to {destination} (~{road_km:.0f} km)"]
    if from_here is not None:  # where the car comes to (the elder app says it in their language)
        lines.append("From where you are now" if from_here else "From home")
    return Quote(provider=RIDES[currency], lines=lines,
                 amountMinor=round(fare / 100) * 100, currency=currency, eta=f"~{max(5, round(road_km * 3))} min drive",
                 estimate=True, demo=True)


def place(quote: Quote) -> str:
    """Pretend to place the order; returns the provider's order id."""
    return f"{quote.provider.upper().replace(' ', '')}-{uuid.uuid4().hex[:6].upper()}"
