"""'Did you mean…?' for medicine names typed during setup. Gemini only checks the NAME: spelling, and the generic name
of a brand. It never gives dose, use or treatment advice — that's for the doctor and the box."""
from __future__ import annotations

from functools import lru_cache

from google.genai import types
from pydantic import BaseModel, Field

from app.agent.llm import genai_client
from app.config import get_settings

_PROMPT = (
    "You check medicine NAMES typed by a family member in {country}. For the name given, answer only: is it a real "
    "medicine name (brand or generic) sold there; if it looks misspelled, the correct spelling; and if it is a brand, "
    "its generic (active ingredient) name. Never give doses, uses, side effects or any advice. If unsure, say not recognised."
)


class MedicineCheck(BaseModel):
    input: str
    recognised: bool = Field(description="A real medicine name (brand or generic), allowing for small typos.")
    suggestion: str | None = Field(default=None, description="Correct spelling if the input looks misspelled, else null.")
    generic: str | None = Field(default=None, description="Generic / active-ingredient name if the input is a brand, else null.")


@lru_cache(maxsize=512)
def check(name: str, country: str) -> MedicineCheck:
    r = genai_client().models.generate_content(
        model=get_settings().gemini_model, contents=name,
        config=types.GenerateContentConfig(
            system_instruction=_PROMPT.format(country=country), temperature=0,
            response_mime_type="application/json", response_schema=MedicineCheck),
    )
    out = MedicineCheck.model_validate_json(r.text or "{}") if r.text else MedicineCheck(input=name, recognised=False)
    # Don't echo the input back as a "suggestion" or a brand as its own generic.
    # …nor a shortened copy of it ("Telma 40" → "Telma" just drops the strength).
    sug = out.suggestion if out.suggestion and out.suggestion.strip().lower() not in name.strip().lower() else None
    gen = out.generic if out.generic and out.generic.strip().lower() not in (name.strip().lower(), (sug or "").lower()) else None
    return MedicineCheck(input=name, recognised=out.recognised, suggestion=sug, generic=gen)
