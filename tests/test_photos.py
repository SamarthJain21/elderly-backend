from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import rx_scan
from app.agent import photos
from app.auth import AuthedUser, get_authed_user
from app.main import app
from app.routers import photos_api

ELDER = AuthedUser(uid="e1", email=None, provider="password", role="elder")
CARETAKER = AuthedUser(uid="c1", email=None, provider="google.com", role="caretaker")
JPEG = base64.b64encode(b"\xff\xd8\xff" + b"x" * 300).decode()
ELDER_DOC = {"name": "Kamla", "language": "hi-IN", "meals": {"breakfast": "08:00", "dinner": "19:30"},
             "medicationSchedule": [{"name": "Metformin", "dose": "500mg"}]}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    st: dict[str, Any] = {"user": ELDER, "photos": {}, "saved": [], "frames": [], "push": [], "rx": [], "described_in": []}
    monkeypatch.setattr(photos_api, "load_elder", lambda user, eid, **_: ELDER_DOC)
    monkeypatch.setattr(photos, "save", lambda eid, data, mime, by: st["photos"].setdefault("p1", (data, mime)) and "p1")
    monkeypatch.setattr(photos, "load", lambda eid, pid: st["photos"].get(pid))

    def describe(data: bytes, mime: str, lang: str) -> photos.PhotoRead:
        st["described_in"].append(lang)
        return photos.PhotoRead(caption=st.get("caption", "A prescription from Dr Mehta"), kind=st.get("kind", "prescription"))
    monkeypatch.setattr(photos, "describe", describe)
    monkeypatch.setattr(photos_api.family, "save", lambda eid, m: st["saved"].append(m))
    monkeypatch.setattr(photos_api.family, "set_rx", lambda eid, mid, meds: st["rx"].append(meds))
    monkeypatch.setattr(photos_api.family, "caretaker_name", lambda uid: "Priya")

    async def broadcast(eid: str, frame: dict[str, Any], channel: str) -> None:
        st["frames"].append(frame)
    monkeypatch.setattr(photos_api.live, "broadcast", broadcast)
    monkeypatch.setattr(photos_api.push, "notify_family", lambda elder, title, body, path: st["push"].append(body))
    monkeypatch.setattr(rx_scan, "scan", lambda data, mime, meals, existing: rx_scan.ScanResult(unreadable=False, medicines=[
        rx_scan.DraftMedicine(name="Glycomet", dose="500 mg", times=["08:00", "19:30"], withFood=True, notes="", unsure=False,
                              asNeeded=False, sameAs="Metformin 500mg")]))
    app.dependency_overrides[get_authed_user] = lambda: st["user"]
    yield st
    app.dependency_overrides.clear()


def test_mums_prescription_photo_reaches_family_with_the_medicines_to_review(env: dict[str, Any]) -> None:
    r = TestClient(app).post("/elders/e1/family/photo", json={"image": JPEG, "mimeType": "image/jpeg"})
    assert r.status_code == 200
    m = r.json()
    assert m["sender"] == "elder" and m["photoId"] == "p1" and m["photoAlt"] == "A prescription from Dr Mehta"
    assert env["described_in"] == ["English"]                       # described for the family, who read English
    assert env["push"] == ["A prescription from Dr Mehta"]
    first, update = env["frames"]
    assert first["type"] == "family_message" and update["type"] == "family_update"
    assert update["message"]["rx"][0]["sameAs"] == "Metformin 500mg"  # double-dose warning travels with it
    assert env["rx"] and env["rx"][0][0]["name"] == "Glycomet"        # stored for the review, never auto-added


def test_familys_photo_is_described_in_mums_language_and_not_read_for_medicines(env: dict[str, Any]) -> None:
    env["user"], env["kind"], env["caption"] = CARETAKER, "photo", "राहुल स्कूल की वर्दी में मुस्कुरा रहा है"
    r = TestClient(app).post("/elders/e1/family/photo", json={"image": JPEG, "mimeType": "image/jpeg", "text": "Rahul's first day!"})
    m = r.json()
    assert m["sender"] == "caretaker" and m["senderName"] == "Priya" and m["text"] == "Rahul's first day!"
    assert env["described_in"] == ["Hindi (Devanagari script)"] and m["photoAlt"].startswith("राहुल")
    assert len(env["frames"]) == 1 and not env["rx"] and not env["push"]
    assert [c["label"] for c in m["chips"]] == ["❤️ बहुत सुंदर!", "🙏 धन्यवाद"]   # big reply buttons, in her language


def test_bad_uploads_are_refused(env: dict[str, Any]) -> None:
    c = TestClient(app)
    assert c.post("/elders/e1/family/photo", json={"image": JPEG, "mimeType": "image/gif"}).status_code == 415
    assert c.post("/elders/e1/family/photo", json={"image": "!" * 200, "mimeType": "image/jpeg"}).status_code == 422
    big = base64.b64encode(b"x" * (photos.MAX_BYTES + 1)).decode()
    assert c.post("/elders/e1/family/photo", json={"image": big, "mimeType": "image/jpeg"}).status_code in (413, 422)
    assert not env["saved"]


def test_the_image_is_served_to_the_family_only_through_the_api(env: dict[str, Any]) -> None:
    c = TestClient(app)
    c.post("/elders/e1/family/photo", json={"image": JPEG, "mimeType": "image/jpeg"})
    r = c.get("/elders/e1/photos/p1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content.startswith(b"\xff\xd8\xff")
    assert "private" in r.headers["cache-control"]
    assert c.get("/elders/e1/photos/nope").status_code == 404
