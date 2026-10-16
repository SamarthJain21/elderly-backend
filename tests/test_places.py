from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import firebase
from app.main import app
from app.tools import places


class R:
    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data

    def raise_for_status(self) -> None: ...

    def json(self) -> dict[str, Any]:
        return self._d


def test_details_maps_google_components_to_our_address_form(monkeypatch: pytest.MonkeyPatch) -> None:
    class S:
        def get(self, url: str, **_: Any) -> R:
            return R({"location": {"latitude": 18.52, "longitude": 73.87}, "formattedAddress": "12, MG Road, Pune", "addressComponents": [
                {"longText": "12", "types": ["street_number"]}, {"longText": "MG Road", "types": ["route"]},
                {"longText": "Camp", "types": ["sublocality_level_1", "sublocality"]}, {"longText": "Pune", "types": ["locality"]},
                {"longText": "411001", "types": ["postal_code"]}]})
    monkeypatch.setattr(places, "_session", lambda: S())
    a = places.details("pid")
    assert (a["line1"], a["line2"], a["city"], a["postcode"], a["lat"], a["precision"]) == ("12 MG Road", "Camp", "Pune", "411001", 18.52, "address")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(firebase, "verify_id_token", lambda t: {"uid": "c1", "firebase": {"sign_in_provider": "google.com"}})
    monkeypatch.setattr(firebase, "get_user_doc", lambda uid: {"role": "caretaker"})
    return TestClient(app)


def test_search_needs_sign_in_and_a_few_letters(client: TestClient) -> None:
    assert client.get("/places/autocomplete?q=MG Road").status_code == 401
    assert client.get("/places/autocomplete?q=MG", headers={"Authorization": "Bearer t"}).status_code == 422


def test_search_failure_tells_them_to_type_instead(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*_: Any) -> list[dict[str, str]]:
        raise RuntimeError("quota")
    monkeypatch.setattr(places, "autocomplete", down)
    r = client.get("/places/autocomplete?q=MG Road", headers={"Authorization": "Bearer t"})
    assert r.status_code == 502 and "type the address" in r.json()["detail"]
