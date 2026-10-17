from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from app.agent.chips import (
    CallAction,
    ChipIdea,
    RemindAction,
    caretaker_presets,
    chips_from_ideas,
    elder_presets,
)

NOW = dt.datetime(2026, 10, 15, 4, 0, tzinfo=dt.UTC)  # 09:30 in India, 11:00 in Bangkok


def test_medicine_question_offers_a_remind_me_action() -> None:
    chips = elder_presets("Did you take your medicine?", "Samarth", None)
    assert [c.label for c in chips] == ["✅ Yes, I took it", "❌ Not yet", "⏰ Remind me in 30 min"]  # type: ignore[union-attr]
    act = chips[2].action  # type: ignore[index]
    assert isinstance(act, RemindAction) and act.minutes == 30


def test_not_yet_gives_caretaker_remind_and_call_actions() -> None:
    chips = caretaker_presets("❌ Not yet", "Rose", "+91 98765")
    assert chips is not None
    assert isinstance(chips[0].action, RemindAction)
    assert isinstance(chips[-1].action, CallAction) and chips[-1].action.phone == "+91 98765"


def test_no_call_chip_without_a_phone_number() -> None:
    chips = caretaker_presets("❌ Not yet", "Rose", None)
    assert chips is not None and not any(isinstance(c.action, CallAction) for c in chips)


def test_reminder_time_is_in_the_elders_timezone() -> None:
    assert RemindAction(title="x", minutes=30).when("Asia/Kolkata", NOW) == (dt.date(2026, 10, 15), "10:00")
    assert RemindAction(title="x", minutes=30).when("Asia/Bangkok", NOW) == (dt.date(2026, 10, 15), "11:30")


def test_reminder_at_a_time_that_already_passed_means_tomorrow() -> None:
    assert RemindAction(title="x", at="09:00").when("Asia/Kolkata", NOW) == (dt.date(2026, 10, 16), "09:00")
    assert RemindAction(title="x", at="15:00").when("Asia/Kolkata", NOW) == (dt.date(2026, 10, 15), "15:00")


@pytest.mark.parametrize("bad", [{"minutes": 1}, {"minutes": 5000}, {"at": "25:00"}, {"at": "3pm"}])
def test_reminder_limits(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RemindAction(title="x", **bad)  # type: ignore[arg-type]


def test_model_ideas_are_validated_and_bad_ones_dropped() -> None:
    ideas = [
        ChipIdea(label="👍 Sure", kind="message"),
        ChipIdea(label="⏰ Remind me", kind="remind", remindInMinutes=99999),  # invalid → dropped
        ChipIdea(label="📞 Call Ravi", kind="call"),                           # no phone → dropped
        ChipIdea(label="⏰ At 3", kind="remind", remindAt="15:00", remindTitle="Doctor"),
    ]
    chips = chips_from_ideas(ideas, call_name=None, call_phone=None)
    assert [c.label for c in chips] == ["👍 Sure", "⏰ At 3"]
    assert isinstance(chips[1].action, RemindAction) and chips[1].action.title == "Doctor"


def test_reminder_on_a_specific_day() -> None:
    act = RemindAction(title="Doctor", at="15:00", date=dt.date(2026, 10, 16))
    assert act.when("Asia/Kolkata", NOW) == (dt.date(2026, 10, 16), "15:00")
    with pytest.raises(ValueError):
        RemindAction(title="x", at="08:00", date=dt.date(2026, 10, 15)).when("Asia/Kolkata", NOW)  # already past
    with pytest.raises(ValidationError):
        RemindAction(title="x", date=dt.date(2026, 10, 16))  # date without a time


def test_reminder_chips_always_look_like_reminders() -> None:
    chips = chips_from_ideas([ChipIdea(label="🔧 Call plumber about tap", kind="remind", remindAt="13:00",
                                       remindTitle="🔧 Call plumber about tap")], None, None)
    assert chips[0].label == "⏰ Call plumber about tap"
    assert isinstance(chips[0].action, RemindAction) and chips[0].action.title == "Call plumber about tap"


def test_model_dated_reminder_is_kept() -> None:
    chips = chips_from_ideas([ChipIdea(label="⏰ Remind me at 3pm", kind="remind", remindAt="15:00", remindDate="2026-10-16",
                                       remindTitle="Doctor appointment")], None, None)
    assert isinstance(chips[0].action, RemindAction) and chips[0].action.date == dt.date(2026, 10, 16)


def test_family_message_with_dated_remind_chip_saves_firestore_safe_types(monkeypatch):
    """A 'remind on Sunday' chip holds a datetime.date — Firestore rejects those, which silently lost the message."""
    import datetime as dt

    from app.agent import family
    from app.agent.chips import Chip, RemindAction

    saved = {}

    class _Doc:
        def set(self, d):
            saved.update(d)

    class _Col:
        def document(self, _id):
            return _Doc()

    monkeypatch.setattr(family, "_col", lambda _e: _Col())
    chip = Chip(label="⏰ Remind me", kind="action", action=RemindAction(title="Visit", at="10:00", date=dt.date(2026, 10, 18)))
    msg = family.FamilyMessage(sender="caretaker", senderUid="u", senderName="Priya", text="I will visit on Sunday", chips=[chip])
    family.save("e1", msg)

    def plain_date(v):
        if isinstance(v, dict):
            return any(plain_date(x) for x in v.values())
        if isinstance(v, list):
            return any(plain_date(x) for x in v)
        return isinstance(v, dt.date) and not isinstance(v, dt.datetime)

    assert not plain_date(saved)
    assert isinstance(saved["createdAt"], dt.datetime)  # still a timestamp, so ordering works
    assert family.FamilyMessage(**saved).chips[0].action.date == dt.date(2026, 10, 18)  # reads back
