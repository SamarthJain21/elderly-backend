"""Weather via Open-Meteo (free, no API key)."""
from __future__ import annotations

import time

import httpx
from pydantic import BaseModel

HEAT_RISK_C = 38.0  # "feels like" max; above this, older adults are at real risk

# WMO weather codes → plain words. https://open-meteo.com/en/docs
_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
        80: "rain showers", 81: "rain showers", 82: "violent rain showers", 95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm"}


class Weather(BaseModel):
    place: str
    nowC: float
    feelsLikeC: float
    conditions: str
    feelsLikeMaxC: float
    rainChancePct: int
    heatRisk: bool
    advice: str


def advice_for(feels_max: float, rain_pct: int) -> str:
    tips = []
    if feels_max >= HEAT_RISK_C:
        tips.append("very hot today: drink water often, stay indoors 11am–4pm, wear light clothes")
    if rain_pct >= 60:
        tips.append("rain likely: take an umbrella, careful on wet floors and steps")
    return "; ".join(tips) or "a comfortable day"


# Open-Meteo's servers are far from Asia (~0.5–1.5 s per call, two calls per lookup). Forecasts change slowly:
# cache per city for 15 minutes. In-process is fine (one Cloud Run instance; a cold start just refetches).
_CACHE_TTL_S = 15 * 60
_cache: dict[str, tuple[float, Weather]] = {}


async def get_weather(city: str) -> Weather:
    key = city.strip().lower()
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    w = await _fetch_weather(city)
    _cache[key] = (time.monotonic(), w)
    return w


async def _fetch_weather(city: str) -> Weather:
    async with httpx.AsyncClient(timeout=10) as client:
        geo = (await client.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1})).json()
        results = geo.get("results") or []
        if not results:
            raise ValueError(f"Couldn't find a place called {city!r}")
        g = results[0]
        f = (await client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": "apparent_temperature_max,precipitation_probability_max",
        })).json()
    cur, daily = f["current"], f["daily"]
    feels_max = float(daily["apparent_temperature_max"][0])
    rain = int(daily["precipitation_probability_max"][0] or 0)
    return Weather(
        place=f"{g['name']}, {g.get('country', '')}".strip(", "),
        nowC=float(cur["temperature_2m"]), feelsLikeC=float(cur["apparent_temperature"]),
        conditions=_WMO.get(int(cur["weather_code"]), "mixed"),
        feelsLikeMaxC=feels_max, rainChancePct=rain, heatRisk=feels_max >= HEAT_RISK_C,
        advice=advice_for(feels_max, rain),
    )
