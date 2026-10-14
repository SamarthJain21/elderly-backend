from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from app.schedule import (
    Assessment,
    Medication,
    Reminder,
    ReminderIn,
    local_today,
    medication_reminders,
    reminders_for_day,
)

BASE = {"timezone": "Asia/Kolkata", "sleep": {"bedtime": "22:00", "wakeTime": "06:30"}}


@pytest.mark.parametrize("bad", ["8:00", "24:00", "12:60", "noon", "08:00:00"])
def test_rejects_malformed_times(bad: str) -> None:
    with pytest.raises(ValidationError):
        Assessment(**{**BASE, "sleep": {"bedtime": bad, "wakeTime": "06:30"}})


def test_rejects_unknown_timezone() -> None:
    with pytest.raises(ValidationError):
        Assessment(**{**BASE, "timezone": "Mars/Olympus"})


def test_checkin_days_must_be_unique_and_come_back_in_week_order() -> None:
    with pytest.raises(ValidationError):
        Assessment(**BASE, checkIn={"time": "09:00", "days": ["mon", "mon"]})
    a = Assessment(**BASE, checkIn={"time": "09:00", "days": ["sun", "mon", "wed"]})
    assert a.checkIn.days == ["mon", "wed", "sun"]


def test_medication_times_are_deduped_and_sorted() -> None:
    assert Medication(name="A", times=["20:00", "08:00", "08:00"]).times == ["08:00", "20:00"]


def test_one_reminder_per_medication_time_with_stable_ids() -> None:
    meds = [Medication(id="m1", name="Amlodipine", dose="5mg", times=["08:00", "20:00"], withFood=True)]
    first = medication_reminders("e1", meds)
    assert [r.time for r in first] == ["08:00", "20:00"]
    assert first[0].title == "Take Amlodipine (5mg) with food"
    assert [r.id for r in first] == [r.id for r in medication_reminders("e1", meds)]


def test_reminder_needs_exactly_one_of_days_or_date() -> None:
    with pytest.raises(ValidationError):
        ReminderIn(title="Doctor", time="10:00")
    with pytest.raises(ValidationError):
        ReminderIn(title="Doctor", time="10:00", days=["mon"], date=dt.date(2026, 10, 15))


def test_reminders_for_day_filters_by_weekday_and_date_and_sorts() -> None:
    thu = dt.date(2026, 10, 15)  # a Thursday
    rs = [
        Reminder(id="a", elderId="e", title="Late", kind="custom", time="21:00", days=["thu"]),
        Reminder(id="b", elderId="e", title="Mon only", kind="custom", time="07:00", days=["mon"]),
        Reminder(id="c", elderId="e", title="Doctor", kind="appointment", time="10:00", date=thu),
        Reminder(id="d", elderId="e", title="Off", kind="custom", time="06:00", days=["thu"], active=False),
        Reminder(id="e", elderId="e", title="Tomorrow", kind="appointment", time="09:00", date=thu + dt.timedelta(1)),
    ]
    assert [r.id for r in reminders_for_day(rs, thu)] == ["c", "a"]


def test_today_is_the_elders_date_not_the_servers() -> None:
    # 20:00 UTC on Oct 14 is already Oct 15 in Bangkok (UTC+7) but still Oct 14 in UTC.
    now = dt.datetime(2026, 10, 14, 20, 0, tzinfo=dt.UTC)
    assert local_today("Asia/Bangkok", now) == dt.date(2026, 10, 15)
    assert local_today("Asia/Kolkata", now) == dt.date(2026, 10, 15)  # UTC+5:30 → 01:30
    assert local_today("UTC", now) == dt.date(2026, 10, 14)
