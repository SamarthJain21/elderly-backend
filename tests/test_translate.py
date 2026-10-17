from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.agent import translate
from app.main import app
from tests.test_chat import FakeModel, Store, _connect, model, store  # noqa: F401  (fixtures)


def test_what_needs_translating() -> None:
    assert translate.needs_translation("Did you take your medicine?", "hi-IN")
    assert not translate.needs_translation("👍❤️", "hi-IN")                 # nothing to translate
    assert not translate.needs_translation("Yes, I took it", "en-US")      # already English
    assert translate.needs_translation("हाँ, ले ली", "en-US")
    assert translate.needs_translation("Haan, le li dawai", "en-US", source="hi-Latn")   # Hinglish is ASCII but not English
    assert not translate.needs_translation("I took it", "en-US", source="en-US")


def test_family_messages_arrive_in_the_receivers_language(store: Store, model: FakeModel) -> None:  # noqa: F811
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        _connect(ws)
        ws.send_json({"type": "family_reply", "text": "हाँ, दवाई ले ली"})
        f: dict[str, Any] = ws.receive_json()
        while f["type"] != "family_message":
            f = ws.receive_json()
    m = f["message"]
    assert m["text"] == "हाँ, दवाई ले ली"                       # the original is kept
    assert m["translated"] == "[en-US] हाँ, दवाई ले ली" and m["translatedTo"] == "en-US"   # family reads English


def test_writer_relationship_comes_from_the_elders_contacts():
    from app.routers.chat import _writer
    elder = {"emergencyContacts": [{"name": "Priya Sharma", "relationship": "daughter"}, {"name": "Ravi", "relationship": ""}]}
    assert _writer(elder, "priya") == "priya (daughter)"
    assert _writer(elder, "Ravi") == "Ravi"            # no relationship → name only, gender not guessed
    assert _writer(elder, "Family") == "Family"
    assert _writer({}, "") == ""


def test_a_reply_is_translated_knowing_what_it_answers(store: Store, model: FakeModel, monkeypatch: Any) -> None:  # noqa: F811
    """'खाना खा लिया' alone reads as 'Have you eaten?'; after 'Did you eat lunch?' it's 'I've eaten'."""
    from app.agent import family
    asked = family.FamilyMessage(sender="caretaker", senderUid="c1", senderName="Priya", text="Did you eat lunch, Mum?")
    monkeypatch.setattr(family, "recent", lambda eid, n: [asked])
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(translate, "translate", lambda *a: calls.append(a) or "I've eaten")
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        _connect(ws)
        ws.send_json({"type": "family_reply", "text": "खाना खा लिया"})
        f: dict[str, Any] = ws.receive_json()
        while f["type"] != "family_message":
            f = ws.receive_json()
    text, target, source, writer, context = calls[0]
    assert (text, target, context) == ("खाना खा लिया", "en-US", "Did you eat lunch, Mum?")
    assert "older family member" in writer and f["message"]["translated"] == "I've eaten"
