"""
Configuration asks for fields the extractors can actually produce.

THE DEFECT THIS EXISTS TO PREVENT, stated as it actually happened.

`documents.yaml` required `voter_id` for a VOTER_ID. The voter extractor
emitted `epic_number`. Both were individually correct and nothing compared
them, so every voter ID in the service came back:

    verification: REVIEW
    reason_codes: [REQUIRED_FIELD_MISSING]
    "Not read from the document: voter_id."

while the EPIC number sat in the extraction, read at 0.95 confidence, under
a different key. Three of the four identity types happened to agree; the
fourth did not, and nothing anywhere would have said so.

WHY AN ALIAS IS NOT THE FIX. Adding `voter_id: [epic_number]` repairs the
one case. It does nothing about the next required field somebody adds, or
the next extractor key somebody renames. This file is the fix: it fails the
build the moment the two vocabularies diverge again, for any type.

WHAT IS DELIBERATELY NOT ASSERTED HERE. Nothing about whether a particular
document passes. This is a wiring contract -- "the thing configuration asks
for is a thing that can arrive" -- and it holds regardless of what any
sample looks like.
"""

from __future__ import annotations

import pytest

from app.agents.document_agent import pipeline
from app.agents.document_agent.schemas import DocumentType
from app.agents.verification import rules

#: Verification's class name -> the extractor spec that fills a document of
#: that class. Taken from `pipeline._spec_for`, which is the function the
#: pipeline itself uses, so this cannot drift from the real routing.
CLASSES = {
    "PAN": DocumentType.PAN,
    "VOTER_ID": DocumentType.VOTER_ID,
    "PASSPORT": DocumentType.PASSPORT,
    "DRIVING_LICENCE": DocumentType.DRIVING_LICENCE,
}


def producible(document_class: str) -> set[str]:
    """
    Every field name that can reach verification for this class.

    The extractor's own keys, plus any alias configuration declares for
    them. An alias is how a lender's vocabulary is allowed to differ from
    an extractor's internal one.
    """
    spec = pipeline._spec_for(CLASSES[document_class])
    keys = set(spec)

    for canonical, alternatives in rules._field_aliases(document_class).items():
        if keys & set(alternatives):
            keys.add(canonical)

    return keys


# ==========================================================================
# THE CONTRACT
# ==========================================================================


@pytest.mark.parametrize("document_class", sorted(CLASSES))
def test_every_required_field_can_actually_be_extracted(document_class):
    """
    THE ONE THAT WOULD HAVE CAUGHT IT.

    A required field no extractor produces is not a strict check -- it is a
    check that can never pass, and every document of that type reviews
    forever.
    """
    required = set(rules._required_fields(document_class))
    available = producible(document_class)

    unreachable = required - available
    assert not unreachable, (
        f"{document_class} requires {sorted(unreachable)}, which its "
        f"extractor cannot produce. Extractor keys: "
        f"{sorted(pipeline._spec_for(CLASSES[document_class]))}. Either "
        f"rename the requirement, add a field_aliases entry in "
        f"documents.yaml, or extract the field."
    )


@pytest.mark.parametrize("document_class", sorted(CLASSES))
def test_every_class_requires_at_least_one_field(document_class):
    """
    A type with no required fields passes verification on a readable blank.
    Not a hard rule of the universe -- but if one is ever emptied, that
    should be a deliberate act rather than an edit nobody noticed.
    """
    assert rules._required_fields(document_class), (
        f"{document_class} requires nothing, so a document of that type "
        f"needs no field to pass"
    )


@pytest.mark.parametrize("document_class", sorted(CLASSES))
def test_no_alias_points_at_a_field_that_does_not_exist(document_class):
    """
    An alias naming a key no extractor emits is dead configuration that
    reads as though it does something.
    """
    spec = set(pipeline._spec_for(CLASSES[document_class]))

    for canonical, alternatives in rules._field_aliases(document_class).items():
        if canonical not in rules._required_fields(document_class):
            continue
        assert spec & set(alternatives), (
            f"{document_class}: alias {canonical} -> {alternatives} names "
            f"nothing the extractor produces ({sorted(spec)})"
        )


# ==========================================================================
# THE ALIAS ITSELF
# ==========================================================================


def test_the_canonical_name_is_preferred_over_an_alias():
    """
    An alias is a fallback for a vocabulary difference, never a way to
    shadow the real field. A document carrying both must be read as
    carrying the one configuration asked for.
    """
    resolved = rules._resolve_field(
        "voter_id",
        {"voter_id": "AAA1111111", "epic_number": "BBB2222222"},
        {"voter_id": ["epic_number"]},
    )
    assert resolved == "AAA1111111"


def test_an_alias_is_used_when_the_canonical_name_is_absent():
    resolved = rules._resolve_field(
        "voter_id", {"epic_number": "BBB2222222"},
        {"voter_id": ["epic_number"]},
    )
    assert resolved == "BBB2222222"


def test_a_blank_canonical_value_falls_through_to_the_alias():
    """An empty string is not a value; it is the field not being read."""
    resolved = rules._resolve_field(
        "voter_id", {"voter_id": "   ", "epic_number": "BBB2222222"},
        {"voter_id": ["epic_number"]},
    )
    assert resolved == "BBB2222222"


def test_a_field_present_under_neither_name_is_missing():
    assert rules._resolve_field(
        "voter_id", {"name": "SUNITA"}, {"voter_id": ["epic_number"]},
    ) is None


def test_a_type_with_no_aliases_resolves_normally():
    assert rules._field_aliases("PAN") == {} or "pan_number" not in (
        rules._field_aliases("PAN"))


# ==========================================================================
# THE VERDICT, END TO END THROUGH THE RULES ENGINE
# ==========================================================================


def test_a_voter_id_under_the_extractor_key_now_passes():
    """
    THE REGRESSION, at the layer that produced it. Not a claim about any
    particular file -- these are the field names the voter extractor
    emits, whatever image they came from.
    """
    status, codes, _ = rules.apply(
        document_class="VOTER_ID",
        status="PASS",
        fields={"epic_number": "ZAX0399947", "name": "SUNITA"},
    )

    assert status == "PASS"
    assert "REQUIRED_FIELD_MISSING" not in codes


def test_a_voter_id_with_no_number_at_all_still_reviews():
    """
    The alias must not have weakened the check. A card whose number was
    genuinely not read is still incomplete.
    """
    status, codes, _ = rules.apply(
        document_class="VOTER_ID", status="PASS", fields={"name": "SUNITA"},
    )

    assert status == "REVIEW"
    assert "REQUIRED_FIELD_MISSING" in codes


def test_a_voter_id_with_no_name_still_reviews():
    status, codes, _ = rules.apply(
        document_class="VOTER_ID", status="PASS",
        fields={"epic_number": "ZAX0399947"},
    )

    assert status == "REVIEW"
    assert "REQUIRED_FIELD_MISSING" in codes


@pytest.mark.parametrize("document_class,fields", [
    ("PAN", {"pan_number": "ABCPD1234E", "name": "R SHARMA",
             "date_of_birth": "1990-04-12"}),
    ("PASSPORT", {"passport_number": "Z1234567", "name": "R SHARMA",
                  "date_of_birth": "1990-04-12"}),
    ("DRIVING_LICENCE", {"dl_number": "MH0120110012345", "name": "R SHARMA",
                         "date_of_birth": "1990-04-12"}),
])
def test_the_other_identity_types_are_unaffected(document_class, fields):
    """The alias change must be inert everywhere it was not needed."""
    status, codes, _ = rules.apply(
        document_class=document_class, status="PASS", fields=fields,
    )

    assert "REQUIRED_FIELD_MISSING" not in codes
    assert status == "PASS"
