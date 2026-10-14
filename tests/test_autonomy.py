from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.autonomy import AutonomyRules, currency_for_timezone, default_rules


def test_defaults_cover_every_tool_and_follow_currency() -> None:
    inr, thb = default_rules("INR"), default_rules("THB")
    assert inr.tools["order_food"].limitMinor == 50_000   # ₹500
    assert thb.tools["order_food"].limitMinor == 15_000   # ฿150
    assert inr.tools["notify_volunteer"].limitMinor is None


@pytest.mark.parametrize(("tz", "cur"), [("Asia/Bangkok", "THB"), ("Asia/Kolkata", "INR"), ("Asia/Singapore", "SGD"), (None, "INR")])
def test_currency_follows_where_they_live(tz: str | None, cur: str) -> None:
    assert currency_for_timezone(tz) == cur


def _with(tool: str, rule: dict[str, object]) -> dict[str, object]:
    d = default_rules("INR").model_dump()
    d["tools"][tool] = rule
    return d


@pytest.mark.parametrize(
    "bad",
    [
        _with("order_food", {"mode": "autonomous", "limitMinor": None}),      # spending tool without limit
        _with("set_reminder", {"mode": "autonomous", "limitMinor": 100}),    # limit on non-spending tool
        _with("order_food", {"mode": "autonomous", "limitMinor": 500.5}),    # float money
        _with("order_food", {"mode": "autonomous", "limitMinor": -1}),
        _with("order_food", {"mode": "yolo", "limitMinor": 1}),
        _with("escalate_emergency", {"mode": "blocked"}),                    # emergencies are fixed in code
    ],
)
def test_rejects_invalid_rules(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AutonomyRules.model_validate(bad)


def test_rejects_missing_tools() -> None:
    d = default_rules("INR").model_dump()
    del d["tools"]["book_ride"]
    with pytest.raises(ValidationError):
        AutonomyRules.model_validate(d)


def test_blocked_mode_is_allowed() -> None:
    assert AutonomyRules.model_validate(_with("book_ride", {"mode": "blocked", "limitMinor": 0})).tools["book_ride"].mode == "blocked"
