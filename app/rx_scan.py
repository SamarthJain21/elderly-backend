"""Prescription / medicine-strip photo → draft medicine list (Gemini vision).

Only a DRAFT: the family reviews every line before anything is saved. Gemini reads what is written — names, strengths,
and how often — and never adds doses, uses or advice. Indian notation ("1-0-1", "BD", "after food") is read as
morning / afternoon / night slots, which we place at the elder's own meal times.
"""
from __future__ import annotations

from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field

from app import usage
from app.agent.llm import genai_client
from app.config import get_settings
from app.schedule import Meals

_PROMPT = (
    "You read a photo of a doctor's prescription, a medicine strip or a pill box, for a family setting up medicine "
    "reminders. List each medicine that is clearly written: its name as printed, keeping a number that belongs to the "
    "brand (\"Telma 40\", \"Dolo 650\"); a strength only when written separately with units (\"500 mg\"); "
    "and when it is taken as slots: morning, afternoon, night. Read common notations: \"1-0-1\" = morning + night, "
    "\"1-1-1\" or TDS = all three, BD = morning + night, OD = morning, HS = night, SOS = only when needed (no slots). "
    "AC / before food = before food; PC / after food = with food. Only copy what is written: never guess a medicine, "
    "dose or timing, and never add advice. If a field is unclear, leave it empty and set unsure. If the photo isn't a "
    "prescription or medicine pack, or nothing can be read, return no medicines and unreadable = true."
)

Slot = Literal["morning", "afternoon", "night"]


class ScannedMedicine(BaseModel):
    name: str = Field(description="Medicine name as printed.")
    strength: str = Field(default="", description="Strength / dose as printed, e.g. '40 mg', '1 tablet'. Empty if not written.")
    slots: list[Slot] = Field(default_factory=list, description="When it is taken. Empty if only when needed or not written.")
    food: Literal["before", "with", "unknown"] = "unknown"
    asNeeded: bool = Field(default=False, description="SOS / only when needed.")
    durationDays: int | None = Field(default=None, description="For how many days, if written.")
    unsure: bool = Field(default=False, description="Some of this line was hard to read.")


class _Read(BaseModel):
    medicines: list[ScannedMedicine] = Field(default_factory=list)
    unreadable: bool = False


class DraftMedicine(BaseModel):
    """Shaped like schedule.Medication, so the form can take it as is."""
    name: str
    dose: str
    times: list[str]
    withFood: bool
    notes: str
    unsure: bool
    asNeeded: bool  # SOS: offered unticked — a daily reminder would be wrong


class ScanResult(BaseModel):
    medicines: list[DraftMedicine]
    unreadable: bool


_DEFAULT = {"morning": "08:00", "afternoon": "13:00", "night": "20:00"}


def _times(slots: list[Slot], meals: Meals) -> list[str]:
    at = {"morning": meals.breakfast, "afternoon": meals.lunch, "night": meals.dinner}
    return sorted({at[s] or _DEFAULT[s] for s in slots})


def to_draft(m: ScannedMedicine, meals: Meals) -> DraftMedicine | None:
    name = m.name.strip()[:100]
    if not name:
        return None
    notes = []
    if m.food == "before":
        notes.append("Before food")
    if m.asNeeded:
        notes.append("Only when needed")
    if m.durationDays:
        notes.append(f"For {m.durationDays} days")
    times = _times(m.slots, meals)
    if not times:  # Medication needs a time; the family adjusts it in the review
        times, m.unsure = [meals.breakfast or _DEFAULT["morning"]], m.unsure or not m.asNeeded
    return DraftMedicine(name=name, dose=m.strength.strip()[:60], times=times, withFood=m.food == "with",
                         notes=" · ".join(notes)[:300], unsure=m.unsure, asNeeded=m.asNeeded)


def scan(image: bytes, mime: str, meals: Meals) -> ScanResult:
    s = get_settings()
    model = (s.gemini_fallback_models or [s.gemini_model])[0]  # handwriting needs the stronger model
    r = genai_client().models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=image, mime_type=mime), "Read the medicines in this photo."],
        config=types.GenerateContentConfig(
            system_instruction=_PROMPT, temperature=0,
            response_mime_type="application/json", response_schema=_Read),
    )
    usage.gemini(model, r, "rx-scan")
    read = _Read.model_validate_json(r.text) if r.text else _Read(unreadable=True)
    drafts = [d for m in read.medicines[:20] if (d := to_draft(m, meals))]
    return ScanResult(medicines=drafts, unreadable=read.unreadable or not drafts)
