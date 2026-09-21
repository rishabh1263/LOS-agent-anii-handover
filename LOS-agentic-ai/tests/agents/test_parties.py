"""
Two people on one case, and the wall between them.

THE FAILURE THIS PREVENTS, concretely. Before the party model, a document
was keyed `case_id:source_id` and implicitly belonged to the case's
applicant. Phones name photographs identically — `pan.jpg`, `IMG_0042.jpg`
— so the moment a case carried two people, the co-applicant's upload
collided with the primary applicant's on one key and silently overwrote it.
The primary applicant's PAN became the co-applicant's. Nothing errored,
nothing logged, and KYC then compared a person against a stranger's
document and reported a name mismatch on a perfectly good file.

WHAT THESE TESTS PIN.

  Two parties on one case never share an identity or a document key.
  A shared identifier is refused rather than silently merged.
  A single-applicant request behaves exactly as it did before.
  A document written before parties existed keeps its owner.
"""

from __future__ import annotations

import pytest

from app.agents.los import parties
from app.agents.los.parties import PartyError, PartyRole
from app.store.models import Document, DocumentStatus


# ==========================================================================
# A. RESOLVING THE PARTIES ON A REQUEST
# ==========================================================================


def test_a_request_with_no_co_applicant_has_exactly_one_party():
    primary, co = parties.resolve(case_id="C1", applicant_id="APP-1")

    assert primary.party_id == "APP-1"
    assert primary.party_role is PartyRole.PRIMARY_APPLICANT
    assert co is None


def test_a_request_with_a_co_applicant_has_two():
    primary, co = parties.resolve(
        case_id="C1", applicant_id="APP-1", co_applicant_id="COAPP-9")

    assert primary.party_id == "APP-1"
    assert co is not None
    assert co.party_id == "COAPP-9"
    assert co.party_role is PartyRole.CO_APPLICANT


def test_both_parties_share_the_case():
    primary, co = parties.resolve(
        case_id="CASE-001", applicant_id="APP-1", co_applicant_id="COAPP-9")

    assert primary.case_id == co.case_id == "CASE-001"


def test_the_party_id_is_the_public_id():
    """
    A second opaque identifier alongside `applicant_id` would be one more
    thing to correlate in a log and one more place for the two to drift.
    """
    primary, co = parties.resolve(
        case_id="C1", applicant_id="APP-1", co_applicant_id="COAPP-9")

    assert primary.party_id == "APP-1"
    assert co.party_id == "COAPP-9"
    assert primary.public_field == "applicant_id"
    assert co.public_field == "co_applicant_id"


def test_a_missing_applicant_id_is_generated():
    primary, _ = parties.resolve(case_id="C1")

    assert primary.party_id.startswith("APP-")
    assert len(primary.party_id) > 5


def test_a_generated_co_applicant_id_is_distinguishable():
    _, co = parties.resolve(case_id="C1", applicant_id="APP-1",
                            require_co_applicant=True)

    assert co.party_id.startswith("COAPP-")


def test_a_case_with_no_id_cannot_resolve_parties():
    with pytest.raises(PartyError):
        parties.resolve(case_id="")


# ==========================================================================
# B. THE TWO PARTIES CANNOT BE THE SAME PERSON
# ==========================================================================


def test_a_shared_identifier_is_refused():
    """
    THE ONE THAT MATTERS MOST HERE. Two parties with one id are one party
    wearing two hats: their documents land in the same namespace, each
    one's checklist is satisfied by the other's uploads, and a KYC
    comparison compares a person against themselves and always agrees.
    That is a silent wrong answer, so it is rejected loudly.
    """
    with pytest.raises(PartyError, match="cannot share one identity"):
        parties.resolve(case_id="C1", applicant_id="APP-1",
                        co_applicant_id="APP-1")


@pytest.mark.parametrize("bad", [
    "has space", "semi;colon", "slash/es", "quote'", "<script>", "a" * 200,
])
def test_an_unsafe_identifier_is_refused(bad):
    """
    The id is embedded in a composite document key. One containing a
    separator could be crafted to collide with another party's key.
    """
    with pytest.raises(PartyError):
        parties.resolve(case_id="C1", applicant_id="APP-1",
                        co_applicant_id=bad)


@pytest.mark.parametrize("good", [
    "APP-1", "COAPP_9", "case.123", "a:b", "ABC123",
])
def test_ordinary_identifiers_are_accepted(good):
    _, co = parties.resolve(case_id="C1", applicant_id="OTHER",
                            co_applicant_id=good)
    assert co.party_id == good


# ==========================================================================
# C. DOCUMENT KEYS ARE PARTY-QUALIFIED
# ==========================================================================


def test_the_same_filename_from_two_parties_produces_two_keys():
    """
    THE COLLISION, PINNED. Phones name photographs identically; the two
    uploads must not be one row.
    """
    primary = parties.document_key("C1", "APP-1", "pan.jpg")
    co = parties.document_key("C1", "COAPP-9", "pan.jpg")

    assert primary != co


def test_a_key_contains_the_case_and_the_party():
    key = parties.document_key("CASE-1", "APP-1", "pan.jpg")

    assert "CASE-1" in key
    assert "APP-1" in key
    assert "pan.jpg" in key


def test_the_same_party_re_uploading_the_same_file_reuses_its_key():
    """Otherwise a re-upload accumulates duplicate rows the checklist counts twice."""
    first = parties.document_key("C1", "APP-1", "pan.jpg")
    second = parties.document_key("C1", "APP-1", "pan.jpg")

    assert first == second


def test_the_legacy_key_is_still_derivable():
    """
    Needed to adopt a row written before documents carried a party,
    rather than duplicating it beside its replacement.
    """
    assert parties.legacy_document_key("C1", "pan.jpg") == "C1:pan.jpg"


def test_the_two_key_shapes_never_collide():
    """
    A legacy key and a party-qualified key must be distinguishable, or
    adopting one could overwrite the other.
    """
    assert (parties.legacy_document_key("C1", "pan.jpg")
            != parties.document_key("C1", "APP-1", "pan.jpg"))


# ==========================================================================
# D. OWNERSHIP ON THE STORED RECORD
# ==========================================================================


def document(party_id=None, role="PRIMARY_APPLICANT", applicant="APP-1"):
    return Document(
        document_id="D", case_id="C1", applicant_id=applicant,
        party_id=party_id, party_role=role, document_type="PAN",
        status=DocumentStatus.VERIFIED,
    )


def test_a_row_written_before_parties_belongs_to_the_applicant():
    """
    `applicant_id` was what "whose is this" meant when the row was
    written. Reading it as ownerless would hide every pre-upgrade
    document from its own owner.
    """
    legacy = document(party_id=None)

    assert legacy.owner_id == "APP-1"
    assert legacy.belongs_to("APP-1")


def test_a_co_applicant_document_does_not_belong_to_the_applicant():
    co = document(party_id="COAPP-9", role="CO_APPLICANT")

    assert co.owner_id == "COAPP-9"
    assert not co.belongs_to("APP-1")
    assert co.belongs_to("COAPP-9")


def test_an_applicant_document_does_not_belong_to_the_co_applicant():
    mine = document(party_id="APP-1")

    assert not mine.belongs_to("COAPP-9")


def test_ownership_of_nothing_belongs_to_nobody():
    assert not document(party_id="APP-1").belongs_to("")
    assert not document(party_id="APP-1").belongs_to("   ")


def test_a_blank_party_id_falls_back_rather_than_orphaning():
    assert document(party_id="   ").owner_id == "APP-1"


# ==========================================================================
# E. WHAT A RESPONSE SAYS
# ==========================================================================


def test_a_party_publishes_only_its_id_and_role():
    primary, _ = parties.resolve(case_id="C1", applicant_id="APP-1")

    assert set(primary.public()) == {"party_id", "party_role"}
    assert primary.public()["party_role"] == "PRIMARY_APPLICANT"


def test_the_role_is_published_as_a_plain_string():
    """A client should not have to know this is a Python enum."""
    _, co = parties.resolve(case_id="C1", applicant_id="A",
                            co_applicant_id="B")

    assert co.public()["party_role"] == "CO_APPLICANT"
    assert isinstance(co.public()["party_role"], str)
