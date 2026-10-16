from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import rx_scan
from app.auth import AuthedUser, get_authed_user
from app.main import app
from app.schedule import Meals

CARETAKER = AuthedUser(uid="c1", email=None, provider="google.com", role="caretaker")
MEALS = Meals(breakfast="08:30", lunch=None, dinner="19:30")
PHOTO = base64.b64encode(b"\xff\xd8\xff" + b"x" * 200).decode()


def fake_gemini(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Models:
        def generate_content(self, **kw: Any) -> Any:
            calls.append(kw)
            return type("R", (), {"text": answer, "usage_metadata": None})()
    monkeypatch.setattr(rx_scan, "genai_client", lambda: type("C", (), {"models": Models()})())
    return calls


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_authed_user] = lambda: CARETAKER
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_one_zero_one_lands_at_the_elders_breakfast_and_dinner(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini(monkeypatch, '{"medicines":[{"name":"Telma 40","strength":"40 mg","slots":["morning","night"],"food":"with"}]}')
    r = rx_scan.scan(b"img", "image/jpeg", MEALS)
    assert not r.unreadable
    m = r.medicines[0]
    assert (m.name, m.dose, m.times, m.withFood) == ("Telma 40", "40 mg", ["08:30", "19:30"], True)


def test_a_skipped_meal_falls_back_to_a_sensible_time(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini(monkeypatch, '{"medicines":[{"name":"Pan 40","slots":["afternoon"],"food":"before"}]}')
    m = rx_scan.scan(b"img", "image/jpeg", MEALS).medicines[0]
    assert m.times == ["13:00"] and not m.withFood and "Before food" in m.notes


def test_no_timing_written_is_flagged_for_the_family_to_check(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini(monkeypatch, '{"medicines":[{"name":"Dolo 650","slots":[]},{"name":"Crocin","slots":[],"asNeeded":true}]}')
    unclear, sos = rx_scan.scan(b"img", "image/jpeg", MEALS).medicines
    assert unclear.unsure and unclear.times == ["08:30"]
    assert not sos.unsure and sos.asNeeded and "Only when needed" in sos.notes


def test_not_a_prescription_comes_back_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_gemini(monkeypatch, '{"medicines":[],"unreadable":true}')
    assert rx_scan.scan(b"img", "image/jpeg", MEALS).unreadable
    assert "never guess" in calls[0]["config"].system_instruction


def test_scan_endpoint_rejects_non_images_and_bad_base64(client: TestClient) -> None:
    assert client.post("/medicines/scan", json={"image": PHOTO, "mimeType": "application/pdf"}).status_code == 415
    assert client.post("/medicines/scan", json={"image": "!" * 200, "mimeType": "image/jpeg"}).status_code == 422


def test_scan_endpoint_returns_drafts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini(monkeypatch, '{"medicines":[{"name":"Metformin","strength":"500 mg","slots":["morning","night"],"food":"with"}]}')
    r = client.post("/medicines/scan", json={"image": PHOTO, "mimeType": "image/jpeg", "meals": {"breakfast": "09:00", "dinner": "20:00"}})
    assert r.status_code == 200
    assert r.json()["medicines"][0]["times"] == ["09:00", "20:00"]


def test_scan_failure_is_a_friendly_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any) -> None:
        raise RuntimeError("vertex down")
    monkeypatch.setattr(rx_scan, "scan", boom)
    r = client.post("/medicines/scan", json={"image": PHOTO, "mimeType": "image/jpeg"})
    assert r.status_code == 502 and "type it in" in r.json()["detail"]
