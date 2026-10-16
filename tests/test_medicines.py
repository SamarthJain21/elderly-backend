from __future__ import annotations

from typing import Any

import pytest

from app import medicines


def fake_gemini(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Models:
        def generate_content(self, **kw: Any) -> Any:
            calls.append(kw)
            return type("R", (), {"text": answer})()
    monkeypatch.setattr(medicines, "genai_client", lambda: type("C", (), {"models": Models()})())
    medicines.check.cache_clear()
    return calls


def test_a_brand_gets_its_generic_name(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_gemini(monkeypatch, '{"input":"Telma 40","recognised":true,"suggestion":null,"generic":"Telmisartan"}')
    r = medicines.check("Telma 40", "India")
    assert (r.recognised, r.suggestion, r.generic) == (True, None, "Telmisartan")
    assert "Never give doses" in calls[0]["config"].system_instruction


def test_the_input_is_not_echoed_back_as_a_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini(monkeypatch, '{"input":"Metformin","recognised":true,"suggestion":"metformin","generic":"Metformin"}')
    r = medicines.check("Metformin", "India")
    assert (r.suggestion, r.generic) == (None, None)


def test_repeated_names_are_answered_from_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_gemini(monkeypatch, '{"input":"x","recognised":false}')
    medicines.check("Zzz", "India"); medicines.check("Zzz", "India")
    assert len(calls) == 1


def test_dropping_the_strength_is_not_a_spelling_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini(monkeypatch, '{"input":"Telma 40","recognised":true,"suggestion":"Telma","generic":"Telmisartan"}')
    r = medicines.check("Telma 40", "India")
    assert (r.suggestion, r.generic) == (None, "Telmisartan")
