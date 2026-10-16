from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.agent.prompt import build_system_prompt
from app.autonomy import default_rules
from app.routers.places_saved_api import SavedPlace, match
from app.tools import executor

CLINIC = {"id": "pl_1", "name": "Clinic", "address": {"line1": "Ruby Hall Clinic", "line2": "Koregaon Park", "landmark": "", "city": "Pune",
                                                      "postcode": "411001", "lat": 18.5312, "lng": 73.8770, "precision": "address"}}
TEMPLE = {"id": "pl_2", "name": "Priya's home", "address": {"line1": "B-14", "line2": "", "landmark": "", "city": "Pune", "postcode": "",
                                                             "lat": 18.5, "lng": 73.9, "precision": "address"}}


def test_names_match_the_way_people_say_them() -> None:
    ps = [SavedPlace(**CLINIC), SavedPlace(**TEMPLE)]
    assert match(ps, "the clinic").name == "Clinic"            # type: ignore[union-attr]
    assert match(ps, "Priyas home").name == "Priya's home"     # type: ignore[union-attr]
    assert match(ps, "the airport") is None


@pytest.mark.anyio
async def test_a_ride_to_a_saved_place_goes_to_its_exact_spot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_lookup(*_: Any) -> None:
        pytest.fail("saved places don't need a map lookup")
    monkeypatch.setattr(executor, "geocode", no_lookup)
    captured: dict[str, Any] = {}

    async def gated(ctx: Any, name: str, args: dict[str, Any], quote: Any, **kw: Any) -> dict[str, Any]:
        captured.update(args=args, url=kw.get("bookingUrl"))
        return {"status": "awaiting_elder_confirmation"}
    monkeypatch.setattr(executor, "_gated", gated)
    elder = {"name": "Rose", "timezone": "Asia/Kolkata", "places": [CLINIC],
             "address": {"line1": "12 MG Road", "city": "Pune", "lat": 18.52, "lng": 73.87}}
    ctx = executor.ToolContext(elder_id="e1", elder=elder, rules=default_rules("INR"), request_text="", actor_uid="e1")
    await executor.run_tool(ctx, "book_ride", {"destination": "the clinic"})
    assert captured["args"]["destination"] == "Clinic"
    assert "18.531200" in captured["url"] and "73.877000" in captured["url"]   # dropoff = the saved place


def test_the_helper_knows_the_saved_places() -> None:
    p = build_system_prompt({"name": "Rose", "places": [CLINIC, TEMPLE]}, [], dt.datetime(2026, 10, 17, tzinfo=dt.UTC))
    assert "Saved places for rides" in p and "Clinic (Koregaon Park)" in p and "Priya's home (Pune)" in p
