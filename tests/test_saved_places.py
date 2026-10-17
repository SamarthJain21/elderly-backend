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
        captured.update(args=args, url=kw.get("bookingUrl"), lines=quote.lines)
        return {"status": "awaiting_elder_confirmation"}
    monkeypatch.setattr(executor, "_gated", gated)
    elder = {"name": "Rose", "timezone": "Asia/Kolkata", "places": [CLINIC],
             "address": {"line1": "12 MG Road", "city": "Pune", "lat": 18.52, "lng": 73.87}}
    ctx = executor.ToolContext(elder_id="e1", elder=elder, rules=default_rules("INR"), request_text="take me to the clinic", actor_uid="e1")
    await executor.run_tool(ctx, "book_ride", {"destination": "the clinic"})
    assert captured["args"]["destination"] == "Clinic" and captured["url"] is None   # booked in the app, no link
    assert captured["lines"][0] == "Ride to Clinic (~2 km)"   # distance to the saved place's exact spot


def test_the_helper_knows_the_saved_places() -> None:
    p = build_system_prompt({"name": "Rose", "places": [CLINIC, TEMPLE]}, [], dt.datetime(2026, 10, 17, tzinfo=dt.UTC))
    assert "Saved places for rides" in p and "Clinic (Koregaon Park)" in p and "Priya's home (Pune)" in p


@pytest.mark.anyio
async def test_a_reported_scam_warns_the_family_and_tells_the_helper_what_to_say(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import push
    from app.agent import live
    logs: list[Any] = []
    frames: list[dict[str, Any]] = []

    async def fake_log(ctx: Any, message: str, kind: str, actor: str = "agent") -> None:
        logs.append((kind, message))

    async def fake_broadcast(eid: str, frame: dict[str, Any], channel: str = "agent") -> None:
        frames.append(frame)
    monkeypatch.setattr(executor, "_log", fake_log)
    monkeypatch.setattr(live, "broadcast", fake_broadcast)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(push, "notify_family", lambda elder, title, body, path="/caretaker": sent.append((title, body)))
    ctx = executor.ToolContext(elder_id="e1", elder={"name": "Rose"}, rules=default_rules("INR"), request_text="", actor_uid="e1")
    r = await executor.run_tool(ctx, "report_scam", {"what": "Caller claiming to be the bank asked for her OTP"})
    assert r["status"] == "family_warned" and "do NOT share" in r["instruction"]
    assert logs[0][0] == "alert" and "Possible scam" in logs[0][1]
    assert frames[0]["type"] == "notice" and sent[0][0].startswith("⚠️ Possible scam")


@pytest.mark.anyio
async def test_a_taxi_without_a_destination_asks_where_offering_saved_places(monkeypatch: pytest.MonkeyPatch) -> None:
    """'Taxi book kijiye' said nothing about where: the model guessed the clinic. Now it has to ask."""
    async def no_booking(*_: Any, **__: Any) -> None:
        pytest.fail("must not quote a ride nobody asked for")
    monkeypatch.setattr(executor, "_gated", no_booking)
    elder = {"name": "Kamla", "timezone": "Asia/Kolkata", "places": [CLINIC, TEMPLE],
             "address": {"line1": "B-14", "city": "New Delhi", "lat": 28.56, "lng": 77.24}}
    ctx = executor.ToolContext(elder_id="e1", elder=elder, rules=default_rules("INR"), request_text="taxi book kijiye",
                               actor_uid="e1", heard="taxi book kijiye")
    out = await executor.run_tool(ctx, "book_ride", {"destination": "Clinic", "as_said": ""})
    assert out["status"] == "need_destination" and out["savedPlaces"] == ["Clinic", "Priya's home"]
    out = await executor.run_tool(ctx, "book_ride", {"destination": "Clinic", "as_said": "clinic"})  # made up
    assert out["status"] == "need_destination"


def test_where_they_said_counts_in_any_language_and_from_just_before() -> None:
    assert executor._was_said("मंदिर", "मुझे मंदिर जाने के लिए टैक्सी चाहिए")
    assert executor._was_said("Ruby Hall Clinic", "take me to ruby hall please")
    assert executor._was_said("temple", "I want to go to the temple now")            # said a turn earlier, then "now"
    assert not executor._was_said("clinic", "taxi book kijiye")
    assert not executor._was_said("", "take me to the clinic")


@pytest.mark.anyio
async def test_a_ride_picks_her_up_where_she_is_now(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def gated(ctx: Any, name: str, args: dict[str, Any], quote: Any, **kw: Any) -> dict[str, Any]:
        captured.update(args=args, url=kw.get("bookingUrl"), lines=quote.lines)
        return {"status": "awaiting_elder_confirmation"}
    monkeypatch.setattr(executor, "_gated", gated)
    elder = {"name": "Rose", "timezone": "Asia/Kolkata", "places": [CLINIC],
             "address": {"line1": "12 MG Road", "city": "Pune", "lat": 18.52, "lng": 73.87, "precision": "street"}}
    out_shopping = (18.5100, 73.8500)
    ctx = executor.ToolContext(elder_id="e1", elder=elder, rules=default_rules("INR"), request_text="take me to the clinic",
                               actor_uid="e1", here=out_shopping)
    await executor.run_tool(ctx, "book_ride", {"destination": "Clinic", "as_said": "clinic"})
    assert captured["url"] is None and captured["args"]["from"] == "current_location"
    assert captured["lines"] == ["Ride to Clinic (~5 km)", "From where you are now"]   # measured from where she is (home is ~2 km)

    ctx.here = None  # no location (or not shared): from home, as before
    await executor.run_tool(ctx, "book_ride", {"destination": "Clinic", "as_said": "clinic"})
    assert captured["args"]["from"] == "home" and captured["lines"] == ["Ride to Clinic (~2 km)", "From home"]


def test_only_a_fresh_accurate_location_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import chat
    s = chat.Session.__new__(chat.Session)
    s._here = None
    assert s.here() is None
    clock = [1000.0]
    monkeypatch.setattr(chat.time, "monotonic", lambda: clock[0])
    s.set_here(chat.Here(lat=18.51, lng=73.85, accuracy=30))
    assert s.here() == (18.51, 73.85)
    clock[0] += chat.HERE_FRESH_S + 1
    assert s.here() is None                                      # 10 minutes old: use home
    s.set_here(chat.Here(lat=18.51, lng=73.85, accuracy=1500))   # a rough fix (wifi/cell): use home
    assert s.here() is None
