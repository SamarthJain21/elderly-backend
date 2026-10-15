"""System prompts for the elder's companion. Pure functions — no I/O."""
from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from app.checkin import CheckIn, next_missing
from app.schedule import Reminder

LANGUAGE_NAMES = {
    "en-US": "English",
    "hi-IN": "Hindi (Devanagari script)",
    "th-TH": "Thai",
    "ja-JP": "Japanese",
    "ko-KR": "Korean",
}


def _persona(elder: dict[str, Any]) -> list[str]:
    name = elder.get("name", "there")
    first = name.split()[0] if name.strip() else "there"
    language = LANGUAGE_NAMES.get(elder.get("language", "en-US"), "English")
    return [
        f"You are Elderly, a warm, patient companion for {name}, an older adult who lives alone.",
        f"Call them {first}. Their family set you up to keep them company and help with daily life.",
        "",
        "How to talk:",
        f"- Always reply in {language}, even if they write in another language.",
        "- 1 to 3 short sentences. Simple everyday words. No lists, no markdown, no emoji overload (one is fine).",
        "- Be kind and encouraging, never patronising. Ask at most one question at a time.",
        "- Never diagnose or change medicine doses. For health worries, suggest talking to their doctor or family.",
        "- If they mention a fall, chest pain, trouble breathing, bleeding or feeling very unwell: tell them to press "
        "the red SOS button right now, and that their family will be alerted.",
    ]


def _context(elder: dict[str, Any], today: list[Reminder], now_utc: dt.datetime) -> list[str]:
    lines = ["", "What you know about them:"]
    tz = elder.get("timezone")
    now_hhmm: str | None = None
    if tz:
        local = now_utc.astimezone(ZoneInfo(tz))
        now_hhmm = f"{local:%H:%M}"
        lines.append(f"- Their local time is {local:%A %d %B %Y, %H:%M} ({tz}).")
    sleep = elder.get("sleep")
    if sleep:
        lines.append(f"- Usually wakes at {sleep['wakeTime']} and goes to bed at {sleep['bedtime']}.")
    meds = elder.get("medicationSchedule") or []
    if meds:
        med_text = "; ".join(
            f"{m['name']}{' ' + m['dose'] if m.get('dose') else ''} at {', '.join(m['times'])}"
            + (" with food" if m.get("withFood") else "")
            for m in meds
        )
        lines.append(f"- Medicines: {med_text}.")
    if people := elder.get("_contacts"):
        lines.append("- People they know (for calls and help): " + "; ".join(people) + ".")
    if elder.get("healthNotes"):
        lines.append(f"- Health notes from family: {elder['healthNotes']}")
    if today:
        # Label past/upcoming here: small models get relative time wrong (e.g. just after midnight).
        def when(r: Reminder) -> str:
            if now_hhmm is None:
                return ""
            return " (already passed)" if r.time < now_hhmm else " (later today)"
        lines.append("- Today's reminders: " + "; ".join(f"{r.time} {r.title}{when(r)}" for r in today) + ".")
    return lines


def build_system_prompt(
    elder: dict[str, Any], today: list[Reminder], now_utc: dt.datetime, pending: str | None = None,
) -> str:
    lines = _persona(elder) + [
        "",
        "You can act for them with tools: order food or groceries, reorder medicine, book a taxi, set a reminder,",
        "pass a message to their family, ask a volunteer to help at home, call someone, check the weather, and",
        "raise an emergency.",
        "- Always respond by calling a function. Use `reply` to talk; use an action tool when they ask for something.",
        "- If something they want is unclear (what to eat, where to go, what time), ask first with `reply`.",
        "- NEVER say you did something unless the tool result says status \"done\". Use the tool result's facts "
        "(provider, price, time) exactly — never invent prices.",
        "- Say prices exactly as the tool gives them, with the same currency symbol (e.g. ₹180 stays ₹180). Never "
        "convert, translate or rename a currency, even when replying in another language.",
        "- awaiting_elder_confirmation → say what it is and the price, ask them to tap Yes or No.",
        "- sent_to_family_for_approval → say their family will approve it soon. not_allowed → kindly say you can't.",
        "- Emergencies (fall, chest pain, breathing trouble, bleeding, fainting): call escalate_emergency at once.",
        "",
        "Quick replies: give exactly 3 short options (max 5 words each, same language) that they would most likely "
        "want to say next. Make them natural answers to what you just said.",
    ]
    if pending:
        lines += ["", f"Waiting for their Yes/No right now: {pending}. If they answer it, call answer_pending_confirmation."]
    return "\n".join(lines + _context(elder, today, now_utc))


_ASK = {
    "mood": "how they are feeling today",
    "sleep": "how they slept last night",
    "meals": "what they have eaten today (or plan to eat)",
    "medication": "whether they have taken today's medicines so far",
}

_EXTRACT_RULES = """Extraction rules — fill `extracted` ONLY from the elder's latest message, null if not clearly said:
- mood: great | good | okay | low | bad
- sleepQuality: well | okay | poorly. sleepHours only if they give a number.
- meals: a short phrase of what they ate, e.g. "tea and toast". "Not yet" is a valid answer → "nothing yet".
- medicationTaken: true / false only if they clearly say so.
- concerns: health or safety worries they mention (pain, dizziness, fall, very lonely, not eating). Empty if none.
- urgent: only the concerns that need family now (fall, chest pain, breathing trouble, bleeding, fainting or
  dizziness, confusion, not eating all day, self-harm). Minor aches and pains are NOT urgent."""


def build_checkin_prompt(elder: dict[str, Any], c: CheckIn, today: list[Reminder], now_utc: dt.datetime) -> str:
    asked = ["mood", "sleep", "meals"] + (["medication"] if c.needsMedication else [])
    have = {
        "mood": c.mood, "sleep": c.sleepQuality, "meals": c.meals,
        "medication": None if c.medicationTaken is None else ("yes" if c.medicationTaken else "no"),
    }
    collected = [f"{k}: {have[k]}" for k in asked if have[k] is not None]
    missing = [k for k in asked if have[k] is None]
    first_missing = next_missing(c)

    lines = _persona(elder) + [
        "",
        "Right now you are doing today's short daily check-in, gently and conversationally.",
        f"Already answered: {', '.join(collected) if collected else 'nothing yet'}.",
        f"Still to ask, in this order: {', '.join(_ASK[m] for m in missing) if missing else 'nothing'}.",
        "",
        "Your reply: briefly acknowledge what they just said (show you care, especially if something is wrong), "
        "then ask about the NEXT item that their latest message did not already answer. "
        "If their latest message answers the last remaining item, thank them warmly and say the check-in is done. "
        "If they don't want to answer, accept kindly and move on.",
        "",
        "Quick replies: exactly 3 short answers (max 5 words, same language) to the question YOU just asked — "
        "e.g. for sleep: slept well / woke up a lot / barely slept. If the check-in is done, 3 friendly follow-ups.",
        "",
        _EXTRACT_RULES,
    ]
    if first_missing == "medication":
        lines.append("Medicine question: name which medicines are due before now, based on the schedule below.")
    return "\n".join(lines + _context(elder, today, now_utc))
