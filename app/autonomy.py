"""Autonomy rules (rules/{elderId}): what the agent may do on the elder's behalf, and spending limits."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Mode = Literal["autonomous", "elder_confirm", "caretaker_approve", "blocked"]
Currency = Literal["INR", "THB", "SGD"]
ToolName = Literal[
    "order_food", "order_medicine", "book_ride", "notify_volunteer", "send_message_to_caretaker", "set_reminder",
]
SPENDING_TOOLS: frozenset[str] = frozenset({"order_food", "order_medicine", "book_ride"})


class ToolRule(BaseModel):
    mode: Mode
    # Minor units (paise/satang/cents) of the rules' currency — never floats. Only for spending tools.
    limitMinor: int | None = Field(default=None, ge=0, le=10_000_000)


# Defaults per currency (≈ same real-world value). Caretakers adjust these.
_DEFAULT_LIMITS: dict[Currency, dict[str, int]] = {
    "INR": {"order_food": 50_000, "order_medicine": 200_000, "book_ride": 100_000},   # ₹500 / ₹2,000 / ₹1,000
    "THB": {"order_food": 15_000, "order_medicine": 60_000, "book_ride": 30_000},     # ฿150 / ฿600 / ฿300
    "SGD": {"order_food": 2_000, "order_medicine": 8_000, "book_ride": 4_000},        # S$20 / S$80 / S$40
}


class AutonomyRules(BaseModel):
    currency: Currency
    tools: dict[ToolName, ToolRule]
    trustedVolunteersOnly: bool = True

    @model_validator(mode="after")
    def _limits_only_on_spending_tools(self) -> AutonomyRules:
        for name, rule in self.tools.items():
            if name in SPENDING_TOOLS and rule.limitMinor is None:
                raise ValueError(f"{name} needs a spending limit")
            if name not in SPENDING_TOOLS and rule.limitMinor is not None:
                raise ValueError(f"{name} doesn't spend money, so it can't have a limit")
        missing = set(ToolName.__args__) - set(self.tools)  # type: ignore[attr-defined]
        if missing:
            raise ValueError(f"missing rules for: {', '.join(sorted(missing))}")
        return self


def currency_for_timezone(tz: str | None) -> Currency:
    return {"Asia/Bangkok": "THB", "Asia/Singapore": "SGD"}.get(tz or "", "INR")


def default_rules(currency: Currency) -> AutonomyRules:
    limits = _DEFAULT_LIMITS[currency]
    return AutonomyRules(currency=currency, tools={
        "order_food": ToolRule(mode="elder_confirm", limitMinor=limits["order_food"]),
        "order_medicine": ToolRule(mode="elder_confirm", limitMinor=limits["order_medicine"]),
        "book_ride": ToolRule(mode="elder_confirm", limitMinor=limits["book_ride"]),
        "notify_volunteer": ToolRule(mode="caretaker_approve"),
        "send_message_to_caretaker": ToolRule(mode="autonomous"),
        "set_reminder": ToolRule(mode="autonomous"),
    })
