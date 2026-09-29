"""An address answer carries the address -- never an identifier typed into it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.applicant.copilot.answering import profile
from app.agents.applicant.copilot.routing import subjects
from app.security import sensitivity


@pytest.mark.parametrize("stored,expected", [
    ("12 MG Road, Pune (A/c no. 123456789012)", "12 MG Road, Pune"),
    ("12 MG Road, Pune (Aadhaar 1234 5678 9012, A/c no. 123456789012)", "12 MG Road, Pune"),
    ("Plot 12, Sector 5, Noida - 201301, mob 9876543210", "Plot 12, Sector 5, Noida - 201301"),
    ("House 7, email a@b.com, Delhi", "House 7, Delhi"),
    ("House 9, PAN ABCDE1234F, Jaipur", "House 9, Jaipur"),
    ("Flat 4B, 221 Baker Street, Mumbai 400001", "Flat 4B, 221 Baker Street, Mumbai 400001"),
])
def test_embedded_identifiers_are_removed_and_the_address_kept(stored, expected):
    assert sensitivity.without_identifiers(stored) == expected


def test_a_value_that_is_only_an_identifier_is_masked_not_published():
    said = sensitivity.without_identifiers("A/c 123456789012")
    assert "123456789012" not in said


@pytest.mark.parametrize("stored", [
    "12 MG Road, Pune (Aadhaar 1234 5678 9012, A/c no. 123456789012)",
    "7 Hill Road, Pune, mobile 9811122233, email x@y.com",
])
def test_the_caller_and_the_co_applicant_get_the_address_only(stored):
    mine = profile._shown("address", stored)
    party = subjects.Party("COAPP-1", subjects.Kind.CO)
    theirs = subjects._party_field("address", party, SimpleNamespace(address=stored), True,
                                   "case-1", "en", None)
    for said in (mine, theirs):
        for leaked in ("1234 5678 9012", "123456789012", "9012", "9811122233", "x@y.com", "XXXX"):
            assert leaked not in said, (said, leaked)
    assert "Pune" in mine and "Pune" in theirs
