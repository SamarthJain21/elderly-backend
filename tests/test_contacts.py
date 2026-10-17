from __future__ import annotations
import pytest

from app.contacts import Contact, ContactIn, find_contact, pick_volunteer


def c(name: str, **kw: object) -> Contact:
    return Contact(name=name, phone="+911234567", **kw)


PEOPLE = [
    c("Ravi Kumar", kind="family", relationship="son"),
    c("Priya", kind="family", relationship="daughter"),
    c("Dr. Mehta", kind="doctor", relationship="GP"),
    c("Anil", kind="volunteer", skills=["plumbing"], trusted=False, rating=4.9),
    c("Suresh", kind="volunteer", skills=["plumbing", "electrical"], trusted=True, rating=4.2),
    c("Meena", kind="volunteer", skills=["tech help"], trusted=True),
]


def test_skills_are_normalised() -> None:
    assert ContactIn(name="x", phone="+91 98765 43210", skills=[" Plumbing", "plumbing", "", "Tech Help"]).skills == ["plumbing", "tech help"]


def test_trusted_volunteers_come_first_even_if_rated_lower() -> None:
    assert pick_volunteer(PEOPLE, "plumbing", trusted_only=False).name == "Suresh"  # type: ignore[union-attr]


def test_trusted_only_rule_excludes_untrusted() -> None:
    only_untrusted = [p for p in PEOPLE if p.name != "Suresh"]
    assert pick_volunteer(only_untrusted, "plumbing", trusted_only=True) is None
    assert pick_volunteer(only_untrusted, "plumbing", trusted_only=False).name == "Anil"  # type: ignore[union-attr]


def test_skill_match_is_fuzzy_both_ways() -> None:
    assert pick_volunteer(PEOPLE, "tech", trusted_only=True).name == "Meena"  # type: ignore[union-attr]
    assert pick_volunteer(PEOPLE, "electrical repair", trusted_only=True).name == "Suresh"  # type: ignore[union-attr]
    assert pick_volunteer(PEOPLE, "gardening", trusted_only=False) is None


def test_find_contact_by_name_relationship_and_kind() -> None:
    assert find_contact(PEOPLE, "Ravi").name == "Ravi Kumar"  # type: ignore[union-attr]
    assert find_contact(PEOPLE, "my son").name == "Ravi Kumar"  # type: ignore[union-attr]
    assert find_contact(PEOPLE, "the doctor").name == "Dr. Mehta"  # type: ignore[union-attr]
    assert find_contact(PEOPLE, "beti").name == "Priya"  # type: ignore[union-attr]
    assert find_contact(PEOPLE, "the president") is None


@pytest.mark.parametrize("phone", ["98765 43210", "+91 12", "+91 98765 4321O", ""])
def test_phone_numbers_need_a_country_code_and_7_to_15_digits(phone: str) -> None:
    with pytest.raises(ValueError):
        ContactIn(name="Ravi", phone=phone)


def test_names_are_trimmed_and_cannot_be_blank() -> None:
    assert ContactIn(name="  Ravi  ", phone="+91 98765 43210").name == "Ravi"
    with pytest.raises(ValueError):
        ContactIn(name="   ", phone="+91 98765 43210")


def test_a_number_saved_before_country_codes_were_required_still_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """'9592812463' was accepted once; reading it back must not break the whole contacts list."""
    from pydantic import ValidationError

    from app import contacts as contacts_mod
    old = {"id": "c1", "name": "Ravi", "phone": "9592812463", "kind": "family"}
    snap = type("S", (), {"to_dict": lambda _s: old})()
    monkeypatch.setattr(contacts_mod, "_contacts", lambda eid: type("C", (), {"stream": lambda _s: [snap]})())
    assert [c.phone for c in contacts_mod.list_contacts("e1")] == ["9592812463"]
    with pytest.raises(ValidationError):  # …but new input still needs the country code
        contacts_mod.Contact(**old)


def test_old_numbers_get_the_country_of_the_elders_timezone() -> None:
    from app.validation import with_country
    assert with_country("9592812463", "Asia/Kolkata") == "+91 9592812463"
    assert with_country("081 234 5678", "Asia/Bangkok") == "+66 812345678"
    assert with_country("+91 98765 43210", "Asia/Bangkok") == "+91 98765 43210"   # already has one: untouched
    assert with_country("", "Asia/Kolkata") == ""
