from __future__ import annotations

import pytest

from app.autonomy import default_rules
from app.tools import executor
from app.tools.weather import Weather


def _w(place: str) -> Weather:
    return Weather(place=place, nowC=28.0, feelsLikeC=31.0, conditions="cloudy", feelsLikeMaxC=32.0,
                   rainChancePct=20, heatRisk=False, advice="a comfortable day")


@pytest.mark.anyio
async def test_weather_falls_back_to_the_saved_city_when_the_models_spelling_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(city: str) -> Weather:
        if city == "Dilli":
            raise ValueError("Couldn't find a place called 'Dilli'")
        return _w(f"{city}, India")
    monkeypatch.setattr(executor, "get_weather", fake)
    ctx = executor.ToolContext(elder_id="e1", elder={"city": "New Delhi"}, rules=default_rules("INR"), request_text="", actor_uid="e1")
    r = await executor.run_tool(ctx, "check_weather", {"city": "Dilli"})
    assert r["status"] == "ok" and r["place"] == "New Delhi, India"


@pytest.mark.anyio
async def test_weather_says_unavailable_when_every_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(city: str) -> Weather:
        raise TimeoutError
    monkeypatch.setattr(executor, "get_weather", down)
    ctx = executor.ToolContext(elder_id="e1", elder={"city": "Pune"}, rules=default_rules("INR"), request_text="", actor_uid="e1")
    assert (await executor.run_tool(ctx, "check_weather", {}))["status"] == "unavailable"
