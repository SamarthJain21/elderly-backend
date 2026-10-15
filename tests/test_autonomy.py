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


from app.autonomy import decide  # noqa: E402


def _rules(**modes: tuple[str, int | None]) -> AutonomyRules:
    d = default_rules("INR").model_dump()
    for tool, (mode, limit) in modes.items():
        d["tools"][tool] = {"mode": mode, "limitMinor": limit}
    return AutonomyRules.model_validate(d)


def test_emergency_and_weather_always_execute_even_if_everything_is_blocked() -> None:
    r = _rules(order_food=("blocked", 0))
    assert decide("escalate_emergency", r) == "execute"
    assert decide("check_weather", r) == "execute"


def test_modes_map_to_decisions_within_limit() -> None:
    assert decide("order_food", _rules(order_food=("autonomous", 50_000)), 18_000) == "execute"
    assert decide("order_food", _rules(order_food=("elder_confirm", 50_000)), 18_000) == "ask_elder"
    assert decide("order_food", _rules(order_food=("caretaker_approve", 50_000)), 18_000) == "caretaker_approve"
    assert decide("order_food", _rules(order_food=("blocked", 50_000)), 18_000) == "block"


def test_over_limit_goes_to_caretaker_even_in_autonomous_mode() -> None:
    r = _rules(order_food=("autonomous", 50_000))
    assert decide("order_food", r, 50_000) == "execute"       # exactly at the limit is fine
    assert decide("order_food", r, 50_001) == "caretaker_approve"


def test_spending_tool_without_a_quote_fails_safe() -> None:
    assert decide("order_food", _rules(order_food=("autonomous", 50_000)), None) == "caretaker_approve"


def test_unknown_tool_fails_safe() -> None:
    assert decide("transfer_money", default_rules("INR")) == "caretaker_approve"


from app.autonomy import format_money  # noqa: E402


@pytest.mark.parametrize(
    ("minor", "cur", "shown"),
    [(18_000, "INR", "₹180"), (1_234_567, "INR", "₹12,345.67"), (4_850, "THB", "฿48.50"), (5, "SGD", "S$0.05"),
     (1_500, "JPY", "¥1,500"), (10_000, "KRW", "₩10,000")],
)
def test_format_money_respects_each_currencys_minor_unit(minor: int, cur: str, shown: str) -> None:
    assert format_money(minor, cur) == shown


def test_format_money_rejects_unknown_currency() -> None:
    with pytest.raises(KeyError):
        format_money(100, "XYZ")
