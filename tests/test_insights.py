from __future__ import annotations

import datetime as dt

from app.checkin import CheckIn
from app.insights import assess_insights, detect_drift, week_stats

TODAY = dt.date(2026, 10, 20)


def ci(days_ago: int, mood: str = "good", sleep: str = "well", meds: bool | None = True, status: str = "done",
       anomalies: list[str] | None = None) -> CheckIn:
    d = TODAY - dt.timedelta(days=days_ago)
    return CheckIn(id=f"e_{d}", elderId="e", date=d, status=status, mood=mood, sleepQuality=sleep,  # type: ignore[arg-type]
                   meals="rice", medicationTaken=meds, anomalies=anomalies or [],
                   startedAt=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))


def steady(days: range) -> list[CheckIn]:
    return [ci(i) for i in days]


def test_stable_week_has_no_drift() -> None:
    assert detect_drift(steady(range(0, 14)), TODAY) == []
    assert assess_insights(steady(range(0, 14)), TODAY).status == "stable"


def test_one_bad_day_is_not_drift() -> None:
    data = steady(range(1, 14)) + [ci(0, mood="low")]
    assert detect_drift(data, TODAY) == []


def test_two_low_mood_days_after_a_good_baseline_is_drift() -> None:
    data = steady(range(2, 14)) + [ci(1, mood="low"), ci(0, mood="low")]
    drifts = detect_drift(data, TODAY)
    assert [(d.signal, d.days) for d in drifts] == [("mood", 2)]
    assert drifts[0].since == TODAY - dt.timedelta(days=1)


def test_baseline_is_personal_okay_is_normal_for_some_people() -> None:
    usually_okay = [ci(i, mood="okay") for i in range(0, 14)]
    assert detect_drift(usually_okay, TODAY) == []


def test_missed_medicines_and_checkins() -> None:
    data = steady(range(3, 14)) + [ci(2, meds=False), ci(1, meds=False)]
    drifts = {d.signal: d for d in detect_drift(data, TODAY)}
    assert drifts["medication"].days == 2
    # Today not done yet → judged up to yesterday; 0 days missing is fine.
    gaps = steady(range(3, 14))  # days 2 and 1 have no check-in
    assert {d.signal for d in detect_drift(gaps, TODAY)} == {"checkins"}


def test_today_not_done_yet_is_not_counted_as_missed() -> None:
    data = steady(range(1, 14)) + [ci(0, status="in_progress")]
    assert detect_drift(data, TODAY) == []


def test_not_enough_history_means_no_judgement() -> None:
    assert detect_drift([ci(1, mood="bad"), ci(0, mood="bad")], TODAY) == []


def test_week_stats_and_trend() -> None:
    data = steady(range(7, 14)) + [ci(i, mood="low", sleep="poorly", meds=(i % 2 == 0), anomalies=["knee pain"]) for i in range(0, 7)]
    w = week_stats(data, TODAY)
    assert w.checkinsDone == 7 and w.moodAvg == 2.0 and w.moodTrend == "down"
    assert w.medsTakenPct == 57 and w.sleptWellPct == 0 and w.concerns == ["knee pain"]
    assert assess_insights(data, TODAY).status == "concern"


def test_a_good_check_in_today_does_not_clear_a_drift_until_tomorrow() -> None:
    data = steady(range(3, 14)) + [ci(2, mood="low"), ci(1, mood="low"), ci(0, mood="good")]
    drifts = detect_drift(data, TODAY)
    assert [(d.signal, d.days, d.since) for d in drifts] == [("mood", 2, TODAY - dt.timedelta(days=2))]
    tomorrow = data + [ci(-1, mood="good")]  # a second good day: the run before it no longer reaches today
    assert detect_drift(tomorrow, TODAY + dt.timedelta(days=1)) == []


def test_another_low_day_today_extends_the_drift() -> None:
    data = steady(range(3, 14)) + [ci(2, mood="low"), ci(1, mood="low"), ci(0, mood="low")]
    assert [(d.days, d.since) for d in detect_drift(data, TODAY)] == [(3, TODAY - dt.timedelta(days=2))]
