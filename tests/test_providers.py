from __future__ import annotations

from app.tools.providers import quote_food, quote_medicine, quote_ride


def test_food_quote_parses_quantities_and_adds_delivery() -> None:
    q = quote_food(["dal", "two rotis", "3 x idli"], "INR")
    assert q.provider == "Swiggy"
    assert q.lines == ["1 × dal", "2 × rotis", "3 × idli"]
    assert q.amountMinor == 12_000 + 2 * 2_500 + 3 * 6_000 + 3_000
    assert isinstance(q.amountMinor, int)


def test_thai_quotes_use_thai_providers_and_whole_baht() -> None:
    q = quote_food(["jok"], "THB")
    assert q.provider == "LINE MAN" and q.currency == "THB"
    assert q.amountMinor % 100 == 0


def test_quantities_are_capped() -> None:
    assert quote_food(["100 rotis"], "INR").lines == ["10 × rotis"]
    assert quote_medicine("Amlodipine", 50, "INR").lines == ["6 × Amlodipine"]


def test_same_ride_same_price() -> None:
    assert quote_ride("City Hospital", "INR").amountMinor == quote_ride("city hospital", "INR").amountMinor


from app.tools.weather import advice_for  # noqa: E402


def test_weather_advice_flags_heat_and_rain() -> None:
    assert "very hot" in advice_for(38.0, 0)
    assert "umbrella" in advice_for(30.0, 60)
    assert advice_for(30.0, 10) == "a comfortable day"
