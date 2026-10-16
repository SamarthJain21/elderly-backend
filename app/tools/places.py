"""Address search with Google Places (New), called from the server with its own Google identity — no API key in the
browser. Autocomplete while typing; the chosen place fills the form; saving re-reads the place for exact coordinates."""
from __future__ import annotations

from typing import Any

from app.voice import _session  # same authorised Google session (service account locally, Cloud Run identity in prod)

_BASE = "https://places.googleapis.com/v1"


def autocomplete(text: str, country: str | None, session: str | None) -> list[dict[str, str]]:
    body: dict[str, Any] = {"input": text}
    if country:
        body["includedRegionCodes"] = [country.lower()]
    if session:
        body["sessionToken"] = session  # groups keystrokes + the final lookup into one billed session
    r = _session().post(f"{_BASE}/places:autocomplete", json=body, timeout=8)
    r.raise_for_status()
    out = []
    for s in r.json().get("suggestions", []):
        p = s.get("placePrediction")
        if p:
            fmt = p.get("structuredFormat", {})
            out.append({"placeId": p["placeId"], "main": fmt.get("mainText", {}).get("text", p["text"]["text"]),
                        "secondary": fmt.get("secondaryText", {}).get("text", ""), "text": p["text"]["text"]})
    return out[:5]


def details(place_id: str, session: str | None = None) -> dict[str, Any]:
    """Address fields for our form + exact location."""
    params = {"sessionToken": session} if session else None
    r = _session().get(f"{_BASE}/places/{place_id}", params=params, timeout=8,
                       headers={"X-Goog-FieldMask": "addressComponents,location,formattedAddress"})
    r.raise_for_status()
    d = r.json()
    comp: dict[str, str] = {}
    for c in d.get("addressComponents", []):
        for t in c.get("types", []):
            comp.setdefault(t, c.get("longText", ""))
    street = " ".join(x for x in (comp.get("street_number"), comp.get("route")) if x)
    line1 = ", ".join(x for x in (comp.get("premise"), street) if x) or d.get("formattedAddress", "").split(",")[0]
    area = comp.get("sublocality_level_1") or comp.get("sublocality") or comp.get("neighborhood") or ""
    if area == line1:
        area = ""  # an area was picked, not a building: don't repeat it
    city = comp.get("locality") or comp.get("administrative_area_level_3") or comp.get("administrative_area_level_2") or ""
    loc = d.get("location") or {}
    return {"line1": line1[:200], "line2": area[:200], "landmark": "", "city": city[:100],
            "postcode": comp.get("postal_code", "")[:12], "lat": loc.get("latitude"), "lng": loc.get("longitude"),
            "precision": "address", "placeId": place_id}
