"""Geocoding via OpenStreetMap Nominatim (free; usage policy: ≤1 req/s, identify the app) and ride links."""
from __future__ import annotations

import math
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "Elderly-elder-care-companion/0.1"}  # app identity only — no personal data
_COUNTRY = {"Asia/Kolkata": "in", "Asia/Bangkok": "th", "Asia/Singapore": "sg"}


class Place(BaseModel):
    lat: float
    lng: float
    name: str  # display name from OSM


def _params(query: str, tz: str | None, near: tuple[float, float] | None) -> dict[str, str | int]:
    p: dict[str, str | int] = {"q": query, "format": "jsonv2", "limit": 1}
    if tz in _COUNTRY:
        p["countrycodes"] = _COUNTRY[tz]
    if near is not None:  # prefer results near home ("City Hospital" → the one in their town)
        lat, lng = near
        p["viewbox"] = f"{lng - 0.3},{lat + 0.3},{lng + 0.3},{lat - 0.3}"
    return p


def _parse(rows: list[dict[str, str]]) -> Place | None:
    if not rows:
        return None
    r = rows[0]
    return Place(lat=float(r["lat"]), lng=float(r["lon"]), name=r["display_name"])


def geocode_sync(query: str, tz: str | None = None, near: tuple[float, float] | None = None) -> Place | None:
    r = httpx.get(_NOMINATIM, params=_params(query, tz, near), headers=_HEADERS, timeout=10)
    r.raise_for_status()
    return _parse(r.json())


# Places don't move: cache lookups (also keeps us well inside Nominatim's ~1 request/second policy).
_places: dict[tuple[str, str | None, tuple[float, float] | None], Place | None] = {}


async def geocode(query: str, tz: str | None = None, near: tuple[float, float] | None = None) -> Place | None:
    key = (query.strip().lower(), tz, (round(near[0], 2), round(near[1], 2)) if near else None)
    if key in _places:
        return _places[key]
    async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as c:
        r = await c.get(_NOMINATIM, params=_params(query, tz, near))
        r.raise_for_status()
        place = _parse(r.json())
    if len(_places) > 2000:
        _places.clear()
    _places[key] = place
    return place


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance (haversine)."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def uber_link(pickup: tuple[float, float] | None, pickup_label: str, dropoff: Place, dropoff_label: str) -> str:
    """Uber universal link (developer.uber.com/docs/deep-linking): opens the app, or m.uber.com, prefilled.
    Without a precise pickup, Uber uses the phone's current location."""
    params = {"action": "setPickup"}
    params |= ({"pickup[latitude]": f"{pickup[0]:.6f}", "pickup[longitude]": f"{pickup[1]:.6f}",
                "pickup[nickname]": pickup_label} if pickup else {"pickup": "my_location"})
    params |= {"dropoff[latitude]": f"{dropoff.lat:.6f}", "dropoff[longitude]": f"{dropoff.lng:.6f}",
               "dropoff[nickname]": dropoff_label, "dropoff[formatted_address]": dropoff.name}
    return "https://m.uber.com/ul/?" + urlencode(params)


def maps_link(pickup: tuple[float, float] | None, dropoff: Place) -> str:
    """Google Maps directions URL (documented 'Maps URLs' API). Without origin, Maps uses current location."""
    params: dict[str, str | int] = {"api": 1, "destination": f"{dropoff.lat},{dropoff.lng}", "travelmode": "driving"}
    if pickup:
        params["origin"] = f"{pickup[0]},{pickup[1]}"
    return "https://www.google.com/maps/dir/?" + urlencode(params)
