from __future__ import annotations

import datetime as dt

import pytest

from app.checkin import CheckIn, Extracted, assess, merge, next_missing, status_for

UTC = dt.UTC
EVERY_DAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def ci(**kw: object) -> CheckIn:
    base = {"id": "e_2026-10-15", "elderId": "e", "date": dt.date(2026, 10, 15), "startedAt": dt.datetime(2026, 10, 15, tzinfo=UTC)}
    return CheckIn(**{**base, **kw})


@pytest.mark.parametrize(
    ("utc_hhmm", "expected"),
    [
        ("03:00", "upcoming"),  # 08:30 IST, check-in at 09:00
        ("03:30", "due"),       # 09:00 IST exactly
        ("06:29", "due"),       # 11:59 IST
        ("06:30", "missed"),    # 12:00 IST = 3h after
    ],
)
def test_status_window_in_elders_timezone(utc_hhmm: str, expected: str) -> None:
    h, m = map(int, utc_hhmm.split(":"))
    now = dt.datetime(2026, 10, 15, h, m, tzinfo=UTC)
    status, day = status_for(time_hhmm="09:00", days=EVERY_DAY, tz="Asia/Kolkata", now_utc=now, existing=None)
    assert status == expected
    assert day == dt.date(2026, 10, 15)


def test_not_today_on_unscheduled_weekday() -> None:
    thu = dt.datetime(2026, 10, 15, 5, 0, tzinfo=UTC)
    status, _ = status_for(time_hhmm="09:00", days=["mon"], tz="Asia/Kolkata", now_utc=thu, existing=None)
    assert status == "not_today"


def test_local_date_not_utc_date() -> None:
    # 20:00 UTC Oct 14 = 03:00 Oct 15 in Bangkok → Thursday's schedule applies, and it's before 09:00.
    now = dt.datetime(2026, 10, 14, 20, 0, tzinfo=UTC)
    status, day = status_for(time_hhmm="09:00", days=["thu"], tz="Asia/Bangkok", now_utc=now, existing=None)
    assert (status, day) == ("upcoming", dt.date(2026, 10, 15))


def test_existing_checkin_status_wins_even_after_window() -> None:
    late = dt.datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
    status, _ = status_for(time_hhmm="09:00", days=EVERY_DAY, tz="Asia/Kolkata", now_utc=late, existing=ci(status="done"))
    assert status == "done"


def test_questions_asked_in_order_and_medication_skipped_when_none() -> None:
    assert next_missing(ci()) == "mood"
    assert next_missing(ci(mood="good")) == "sleep"
    assert next_missing(ci(mood="good", sleepQuality="well")) == "meals"
    assert next_missing(ci(mood="good", sleepQuality="well", meals="idli")) == "medication"
    assert next_missing(ci(mood="good", sleepQuality="well", meals="idli", needsMedication=False)) is None


def test_merge_never_overwrites_with_null_and_dedupes_concerns() -> None:
    c = merge(ci(mood="good", anomalies=["knee pain"]), Extracted(sleepQuality="poorly", concerns=["knee pain", " dizzy "]))
    assert c.mood == "good" and c.sleepQuality == "poorly"
    assert c.anomalies == ["knee pain", "dizzy"]


@pytest.mark.parametrize(
    ("fields", "kind", "escalate"),
    [
        ({"mood": "good", "sleepQuality": "well", "meals": "rice", "medicationTaken": True}, "ok", False),
        ({"mood": "low", "sleepQuality": "well", "meals": "rice", "medicationTaken": True}, "warn", False),
        ({"mood": "good", "sleepQuality": "well", "meals": "rice", "medicationTaken": False}, "warn", False),
        ({"mood": "bad", "sleepQuality": "well", "meals": "rice", "medicationTaken": True}, "alert", True),
        ({"mood": "good", "anomalies": ["knee pain"]}, "warn", False),
        ({"mood": "good", "anomalies": ["felt dizzy"], "urgent": ["felt dizzy"]}, "alert", True),
    ],
)
def test_triage_is_deterministic(fields: dict[str, object], kind: str, escalate: bool) -> None:
    got_kind, got_escalate, summary = assess(ci(**fields))
    assert (got_kind, got_escalate) == (kind, escalate)
    assert summary.startswith("Check-in:")


def test_summary_marks_incomplete_checkins() -> None:
    assert assess(ci(mood="okay"))[2].endswith("(incomplete)")


def test_urgent_concerns_are_also_recorded_as_anomalies() -> None:
    c = merge(ci(), Extracted(concerns=["knee pain"], urgent=["fell in bathroom"]))
    assert c.anomalies == ["knee pain", "fell in bathroom"]
    assert c.urgent == ["fell in bathroom"]


def test_check_in_opener_is_in_the_elders_language() -> None:
    from app.phrases import checkin_opener
    assert checkin_opener("th-TH", "good") == "วันนี้รู้สึกดี"
    assert checkin_opener("hi-Latn", "bad") == "Aaj tabiyat theek nahi hai"
    assert checkin_opener("ja-JP", "good") == "I'm feeling good"   # no translation yet → English
    assert checkin_opener("hi-IN", None) == "आज का हाल बताना है"


def test_a_skipped_daily_check_in_has_no_days(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app import checkin_store

    class Doc:
        def __init__(self, d: dict) -> None:  # type: ignore[type-arg]
            self._d = d

        def get(self) -> "Doc":
            return self

        def to_dict(self) -> dict:  # type: ignore[type-arg]
            return self._d

    class DB:
        def __init__(self, d: dict) -> None:  # type: ignore[type-arg]
            self.d = d

        def collection(self, _: str) -> "DB":
            return self

        def document(self, _: str) -> Doc:
            return Doc(self.d)
    monkeypatch.setattr(checkin_store, "_db", lambda: DB({"checkInTime": "09:00", "checkInDays": ["mon"], "checkInEnabled": False}))
    assert checkin_store.schedule_of("e1") == ("09:00", [])
    monkeypatch.setattr(checkin_store, "_db", lambda: DB({"checkInTime": "09:00", "checkInDays": ["mon"]}))
    assert checkin_store.schedule_of("e1") == ("09:00", ["mon"])
