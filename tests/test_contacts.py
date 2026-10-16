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
