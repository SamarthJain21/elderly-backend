import datetime as dt

from app.agent.prompt import build_system_prompt
from app.schedule import Reminder

NOW = dt.datetime(2026, 10, 16, 5, 30, tzinfo=dt.UTC)


def test_quick_replies_stay_in_the_elders_language_even_if_they_write_in_another() -> None:
    p = build_system_prompt({"name": "Rose", "language": "th-TH"}, [], NOW)
    assert "in Thai even if they wrote in another language" in p


def test_call_button_is_not_described_as_a_call_in_progress() -> None:
    assert "ready_to_call → the call has NOT started" in build_system_prompt({"name": "Rose"}, [], NOW)


def test_passed_medicine_reminder_is_not_treated_as_taken() -> None:
    r = Reminder(id="m", elderId="e", title="Amlodipine", kind="medication", time="08:00")
    p = build_system_prompt({"name": "Rose", "timezone": "Asia/Kolkata"}, [r], NOW)  # 11:00 local
    assert "08:00 Amlodipine (already passed)" in p
    assert "does NOT mean they did it" in p


def test_hinglish_elder_gets_roman_script_hindi() -> None:
    p = build_system_prompt({"name": "Kamla", "language": "hi-Latn"}, [], NOW)
    assert "Always reply in Hinglish" in p and "Never use Devanagari" in p


def test_emergency_tool_is_never_described_as_calling_an_ambulance() -> None:
    assert "never say you called an ambulance" in build_system_prompt({"name": "Rose"}, [], NOW)


def test_skipped_sleep_times_are_simply_not_mentioned() -> None:
    p = build_system_prompt({"name": "Rose", "sleep": {"wakeTime": "06:30", "bedtime": None}}, [], NOW)
    assert "Usually wakes at 06:30." in p and "goes to bed" not in p
