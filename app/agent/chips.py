"""One-tap chips in the family chat: a message to send, or a small action (reminder, call).

Chips are made for whoever *receives* a message: the elder on a caretaker's message, the caretakers
on the elder's. Only these action types exist, and every one is validated here before it can run —
the model can suggest, never invent, an action.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class RemindAction(BaseModel):
    type: Literal["remind"] = "remind"
    title: str = Field(min_length=1, max_length=80)
    minutes: int | None = Field(default=None, ge=5, le=720)  # "in 30 min"
    at: str | None = None                                    # "15:00" …
    date: dt.date | None = None                              # … on this day; omitted = next time it comes round

    @field_validator("at")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v is not None and not _HHMM.match(v):
            raise ValueError("at must be HH:MM")
        return v

    @model_validator(mode="after")
    def _date_needs_time(self) -> RemindAction:
        if self.date is not None and self.at is None:
            raise ValueError("date needs a time (at)")
        return self

    def when(self, tz: str, now_utc: dt.datetime) -> tuple[dt.date, str]:
        """(local date, HH:MM) in the elder's timezone. Raises ValueError for a moment already past."""
        local = now_utc.astimezone(ZoneInfo(tz)).replace(second=0, microsecond=0)
        if self.at is not None and self.date is not None:
            if (self.date, self.at) <= (local.date(), f"{local:%H:%M}") or self.date > local.date() + dt.timedelta(days=60):
                raise ValueError("reminder time must be in the next 60 days")
            return self.date, self.at
        if self.at is not None:
            day = local.date() if self.at > f"{local:%H:%M}" else local.date() + dt.timedelta(days=1)
            return day, self.at
        target = local + dt.timedelta(minutes=self.minutes or 30)
        return target.date(), f"{target:%H:%M}"


class CallAction(BaseModel):
    type: Literal["call"] = "call"
    name: str
    phone: str


class Chip(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    kind: Literal["message", "action"] = "message"
    action: RemindAction | CallAction | None = None


def msg(label: str) -> Chip:
    return Chip(label=label)


def remind(label: str, title: str, minutes: int | None = None, at: str | None = None) -> Chip:
    return Chip(label=label, kind="action", action=RemindAction(title=title, minutes=minutes, at=at))


def call(label: str, name: str, phone: str) -> Chip:
    return Chip(label=label, kind="action", action=CallAction(name=name, phone=phone))


# ---------- presets (English; other languages ask the model) ----------

def elder_presets(text: str, sender: str, sender_phone: str | None) -> list[Chip] | None:
    """One-tap answers for the elder to a caretaker's preset question."""
    t = text.strip().lower().rstrip("?").strip()
    call_back = [call(f"📞 Call {sender}", sender, sender_phone)] if sender_phone else []
    if t == "did you take your medicine":
        return [msg("✅ Yes, I took it"), msg("❌ Not yet"), remind("⏰ Remind me in 30 min", "Take your medicine", minutes=30)]
    if t == "how are you feeling today":
        return [msg("😊 I'm good"), msg("😐 Okay"), msg("😟 Not great"), *call_back][:3 + len(call_back)]
    if t == "shall i order dinner":
        return [msg("🍲 Yes please"), msg("🙅 No thanks"), msg("🍞 Something light")]
    return None


def caretaker_presets(text: str, elder_first: str, elder_phone: str | None) -> list[Chip] | None:
    """Next steps for caretakers after the elder answers a preset."""
    t = text.lower()
    call_her = [call(f"📞 Call {elder_first}", elder_first, elder_phone)] if elder_phone else []
    if "not yet" in t or "forgot" in t:
        return [remind(f"⏰ Remind {elder_first} in 30 min", "Take your medicine", minutes=30),
                msg("💬 Please take it now ❤️"), *call_her]
    if "not great" in t or "not good" in t or "low" in t or "sad" in t or "lonely" in t:
        return [msg("💬 I'll call you soon ❤️"), msg("💬 Shall I come over?"), *call_her]
    if "took it" in t or "i'm good" in t or "yes" in t:
        return [msg("💬 Great, thank you! ❤️"), msg("👍"), *call_her]
    return None


FALLBACK_FOR_ELDER = [msg("👍 Okay"), msg("❤️ Love you"), remind("⏰ Remind me later", "Reply to family", minutes=60)]
FALLBACK_FOR_CARETAKER = [msg("💬 Thanks for letting me know ❤️"), msg("👍")]


# ---------- model suggestions → validated chips ----------

class ChipIdea(BaseModel):
    """What the model may suggest. Anything that doesn't validate is dropped."""

    label: str = Field(description="Short, max 5 words, starts with an emoji.")
    kind: Literal["message", "remind", "call"] = Field(description="message = send the label as text; remind = set a reminder; call = phone them.")
    remindTitle: str | None = Field(default=None, description="For remind: what the reminder says.")
    remindInMinutes: int | None = Field(default=None, description="For remind: minutes from now (5-720).")
    remindAt: str | None = Field(default=None, description="For remind: HH:MM instead of minutes.")
    remindDate: str | None = Field(default=None, description="For remind with remindAt: YYYY-MM-DD when it's not today.")


def _strip_emoji(text: str) -> str:
    """Drop leading emoji/symbols ("🔧 Call plumber" → "Call plumber")."""
    return re.sub(r"^[^\w(]+", "", text, flags=re.UNICODE).strip()


def chips_from_ideas(ideas: list[ChipIdea], call_name: str | None, call_phone: str | None) -> list[Chip]:
    out: list[Chip] = []
    for idea in ideas:
        try:
            if idea.kind == "message":
                out.append(msg(idea.label))
            elif idea.kind == "remind":
                # Reminder chips always read as reminders, whatever emoji the model picked.
                words = _strip_emoji(idea.label)
                label = idea.label if idea.label.startswith("⏰") else f"⏰ {words}"
                title = _strip_emoji(idea.remindTitle or "") or words
                act = RemindAction(title=title[:80], minutes=idea.remindInMinutes if idea.remindAt is None else None,
                                   at=idea.remindAt, date=dt.date.fromisoformat(idea.remindDate) if idea.remindAt and idea.remindDate else None)
                out.append(Chip(label=label[:60], kind="action", action=act))
            elif idea.kind == "call" and call_phone and call_name:
                out.append(call(idea.label, call_name, call_phone))
        except ValueError:
            continue  # invalid suggestion (bad time, too long…) — skip it, don't guess
        if len(out) == 3:
            break
    return out
