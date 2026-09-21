"""
Cross-document consistency: what the layer already guarantees, pinned.

WHY THIS FILE EXISTS. The cross-document layer is not a new engine -- it
is the KYC agent, enabled by default on `/api/v1/los/process` and
disabled only by the FOS route, which has no authority over KYC. Most of
what a reviewer needs from it was already true and simply untested as a
whole: missing evidence reports SKIPPED rather than FAIL, the
verification gate holds, and a two-party case never compares one person
against the other.

THE ONE REAL DEFECT was in address parsing, and it is pinned here too.
`_HOUSE_RE` matched any digit run, so "Mumbai, Maharashtra 400043"
parsed as house=400043 AND pincode=400043 -- one datum carrying 50% of
the comparison weight under two names. On an address printing several
numbers, each side picked a different one, so two spellings of a single
address reported a house MISMATCH.

NOTHING HERE ASSERTS A THRESHOLD VALUE. The policy lives in
kyc_policies.yaml and is a business decision; these tests assert the
DIRECTION a case must fall in, which is not.
"""

from __future__ import annotations

import pytest

from app.agents.kyc import address as address_lib
from app.agents.kyc import config as kyc_config
from app.agents.kyc.checks import check_address, check_name
from app.agents.kyc.schemas import (
    AddressInput,
    CheckStatus,
    KycDocumentType,
    ReasonCode,
    SourceDocument,
)
from app.agents.los.flow import _kyc_for_party, _released_for_matching
from app.agents.los.parties import owned_by

# Two different people, as a joint application actually looks.
PERSON_A = {"name": "RISHABH AJIT SINGH", "date_of_birth": "2002-06-12",
            "pan_number": "NUHPS4875K", "father_name": "AJIT SINGH"}
PERSON_B = {"name": "LAXMI SANTOSH GUPTA", "date_of_birth": "2004-12-20",
            "pan_number": "EVPPG6189E", "father_name": "SANTOSH RAMASHARE GUPTA"}

IDENTITY_MISMATCHES = {"NAME_MISMATCH", "DOB_MISMATCH", "PAN_MISMATCH",
                       "FATHER_NAME_MISMATCH"}


def document(person, *, source_id="pan.jpg", party_id="APP-1",
             document_type="PAN", verification="PASS", **overrides):
    """One document as the LOS flow carries it internally."""
    fields = {**person, **overrides}
    return {
        "source_id": source_id, "status": verification, "party_id": party_id,
        "party_role": ("CO_APPLICANT" if party_id.startswith("COAPP")
                       else "PRIMARY_APPLICANT"),
        "document": {"type": document_type},
        "verification": {"status": verification, "reason_codes": []},
        "extraction": {"fields": fields},
    }


def source(source_id, document_type, **kw) -> SourceDocument:
    return SourceDocument(source_id=source_id, document_type=document_type, **kw)


def addressed(source_id, raw, document_type=KycDocumentType.DRIVING_LICENCE):
    return source(source_id, document_type, address=AddressInput(raw=raw))


def score_of(left: str, right: str) -> float:
    """The weighted address score, under the live policy."""
    section = kyc_config.section("address")
    score, _verdicts, _comparable = address_lib.compare(
        AddressInput(raw=left), AddressInput(raw=right),
        section.get("weights") or {},
        kyc_config.threshold("address", "component_threshold", 0.85),
    )
    return score


def kyc_for(documents, party_id="APP-1"):
    return _kyc_for_party(documents, party_id=party_id, request_id="test")


def codes(payload) -> set[str]:
    return set(payload.get("reason_codes") or [])


# ==========================================================================
# 1-4. IDENTITY ACROSS DOCUMENT PAIRS
# ==========================================================================


@pytest.mark.parametrize("other_type", [
    KycDocumentType.DRIVING_LICENCE,
    KycDocumentType.PASSPORT,
    KycDocumentType.VOTER_ID,
])
def test_a_pan_and_another_id_naming_one_person_agree(other_type):
    """PAN <-> DL / Passport / Voter ID, the pairs the brief names."""
    result = check_name([
        source("pan.jpg", KycDocumentType.PAN, name="RISHABH AJIT SINGH"),
        source("other.jpg", other_type, name="Rishabh Ajit Singh"),
    ])

    assert result.status is CheckStatus.PASS


def test_a_pan_and_a_licence_naming_two_people_do_not_agree():
    result = check_name([
        source("pan.jpg", KycDocumentType.PAN, name="RISHABH AJIT SINGH"),
        source("dl.jpg", KycDocumentType.DRIVING_LICENCE,
               name="LAXMI SANTOSH GUPTA"),
    ])

    assert result.status is CheckStatus.FAIL
    assert ReasonCode.NAME_MISMATCH in result.reason_codes


def test_a_date_of_birth_that_differs_is_reported():
    """
    Through the flow, so the reason code is the one a reviewer sees --
    not a check called in isolation.
    """
    payload = kyc_for([
        document(PERSON_A, source_id="pan.jpg"),
        document({**PERSON_A, "date_of_birth": "1999-01-01"},
                 source_id="dl.jpg", document_type="DRIVING_LICENCE"),
    ])

    assert "DOB_MISMATCH" in codes(payload)


def test_a_father_name_that_differs_is_reported():
    payload = kyc_for([
        document(PERSON_A, source_id="pan.jpg"),
        document({**PERSON_A, "father_name": "SOMEBODY ELSE"},
                 source_id="dl.jpg", document_type="DRIVING_LICENCE"),
    ])

    assert "FATHER_NAME_MISMATCH" in codes(payload)


def test_a_father_name_is_never_matched_against_the_applicants_own_name():
    """
    The two lists must not merge. If they did, an applicant would match
    their own father and the check would pass the wrong person.
    """
    from app.agents.los.mapping import _FATHER_NAME_FIELDS, _NAME_FIELDS

    assert not set(_NAME_FIELDS) & set(_FATHER_NAME_FIELDS)


# ==========================================================================
# 5-9. ADDRESS
# ==========================================================================


def test_an_address_printed_identically_matches():
    text = "5/14, DEONAR, Mumbai, Maharashtra - 400043"

    assert score_of(text, text) == pytest.approx(1.0)


def test_the_same_address_written_two_ways_is_consistent():
    """
    The brief's first example. Case, punctuation, the `/` vs `-` in the
    house number and the dash before the pincode all differ; the address
    does not.
    """
    score = score_of("5/14, Mumbai, Maharashtra - 400043",
                     "5-14 Mumbai Maharashtra 400043")

    assert score >= kyc_config.threshold("address", "pass_score", 0.80)


@pytest.mark.parametrize("left,right", [
    ("12, m.g. road,  bengaluru, ka-560001",
     "12 MG ROAD BENGALURU KARNATAKA 560001"),
    ("H.No 45, Sec 7, Gurgaon, HR 122001",
     "HOUSE NO 45 SECTOR 7 GURUGRAM HARYANA 122001"),
    ("400043, Maharashtra, Mumbai, Deonar",
     "Deonar, Mumbai, Maharashtra, 400043"),
])
def test_normalised_variants_of_one_address_are_consistent(left, right):
    """
    Abbreviations, the old city names, token order and spacing. Each of
    these is one address printed by two different issuers.
    """
    assert score_of(left, right) >= kyc_config.threshold(
        "address", "pass_score", 0.80)


def test_two_genuinely_different_addresses_do_not_pass():
    """The brief's second example: different city, different pincode."""
    score = score_of("Mumbai, Maharashtra 400043", "Pune, Maharashtra 411001")

    assert score < kyc_config.threshold("address", "review_score", 0.55)


def test_one_address_source_is_skipped_rather_than_failed():
    """
    MISSING EVIDENCE IS NOT A MISMATCH. One address cannot disagree with
    anything, and reporting FAIL here would reject an applicant for
    uploading a single document.
    """
    result = check_address([
        addressed("dl.jpg", "5/14, Mumbai, Maharashtra - 400043"),
        source("pan.jpg", KycDocumentType.PAN, name="RISHABH AJIT SINGH"),
    ])

    assert result.status is CheckStatus.SKIPPED
    assert ReasonCode.ADDRESS_SINGLE_SOURCE in result.reason_codes


def test_no_address_at_all_is_skipped_rather_than_failed():
    result = check_address([
        source("pan.jpg", KycDocumentType.PAN, name="RISHABH AJIT SINGH"),
        source("pan2.jpg", KycDocumentType.PAN, name="RISHABH AJIT SINGH"),
    ])

    assert result.status is CheckStatus.SKIPPED
    assert ReasonCode.ADDRESS_MISSING in result.reason_codes


# --------------------------------------------------------------------------
# The defect itself.
# --------------------------------------------------------------------------


def test_the_pincode_is_never_also_read_as_a_house_number():
    """
    THE DEFECT. `_HOUSE_RE` matched any digit run, so an address with no
    house number at all reported the pincode as one -- and pincode and
    house together carry 50% of the comparison weight, so a single datum
    was scored twice under two names.
    """
    parsed = address_lib.parse(AddressInput(raw="Mumbai, Maharashtra 400043"))

    assert parsed["pincode"] == "400043"
    assert parsed["house"] != "400043"


def test_an_address_with_no_house_number_leaves_it_uncompared():
    """
    Absence is not disagreement. A component missing on either side is
    excluded from the score, not counted against the applicant.
    """
    _score, verdicts, comparable = address_lib.compare(
        AddressInput(raw="Mumbai, Maharashtra 400043"),
        AddressInput(raw="Mumbai, Maharashtra 400043"),
        kyc_config.section("address").get("weights") or {},
        kyc_config.threshold("address", "component_threshold", 0.85),
    )

    assert verdicts["house"] == "NOT_COMPARABLE"
    assert "house" not in comparable


def test_a_real_house_number_is_still_compared():
    """The fix must not throw the genuine case away with the phantom one."""
    parsed = address_lib.parse(
        AddressInput(raw="5/14, Mumbai, Maharashtra - 400043"))

    assert parsed["house"] == "514"


def test_one_house_number_rule_serves_both_the_public_view_and_the_comparison():
    """
    A house published as one number and compared as another is how the
    two drift. Same function, one caller reducing it for comparison.
    """
    from app.agents.los.address_public import house_number

    assert address_lib.house_number is house_number


# ==========================================================================
# 10-11. BANK ACCOUNT HOLDER
# ==========================================================================


def test_a_bank_statement_account_holder_is_compared_with_the_party():
    """
    The Financial Agent publishes the account holder as `fields["name"]`,
    which is already what the identity comparison reads.
    """
    result = check_name([
        source("pan.jpg", KycDocumentType.PAN, name="RISHABH AJIT SINGH"),
        source("bank.pdf", KycDocumentType.BANK_STATEMENT,
               name="Rishabh Ajit Singh"),
    ])

    assert result.status is CheckStatus.PASS
    assert {e.source_id for e in result.evidence} == {"pan.jpg", "bank.pdf"}


def test_a_bank_statement_naming_someone_else_does_not_agree():
    result = check_name([
        source("pan.jpg", KycDocumentType.PAN, name="RISHABH AJIT SINGH"),
        source("bank.pdf", KycDocumentType.BANK_STATEMENT,
               name="LAXMI SANTOSH GUPTA"),
    ])

    assert result.status is CheckStatus.FAIL


def test_a_bank_statement_with_no_account_holder_is_skipped_not_failed():
    """
    NO NAME MEANS NO COMPARISON. It does not mean the account belongs to
    someone else.
    """
    result = check_name([
        source("bank.pdf", KycDocumentType.BANK_STATEMENT),
        source("pan.jpg", KycDocumentType.PAN, name="RISHABH AJIT SINGH"),
    ])

    assert result.status is CheckStatus.SKIPPED
    assert ReasonCode.NAME_SINGLE_SOURCE in result.reason_codes


def test_account_ownership_is_never_inferred_from_the_account_number():
    """
    A masked account number and an income figure are not identity. Only
    the four identity attributes may carry a person.
    """
    fields = set(SourceDocument.model_fields)

    assert "account_number_masked" not in fields
    assert "income" in fields and "name" in fields


# ==========================================================================
# 12, 14. PARTY ISOLATION
# ==========================================================================


def test_a_primary_and_a_co_applicant_are_not_compared_with_each_other():
    """
    Two different people, every identity field differing. Neither party's
    own documents disagree, so neither party may report a mismatch.
    """
    primary = kyc_for([document(PERSON_A, source_id="pan.jpg",
                                party_id="APP-1")], "APP-1")
    co = kyc_for([document(PERSON_B, source_id="copan.jpg",
                           party_id="COAPP-9")], "COAPP-9")

    assert not (codes(primary) & IDENTITY_MISMATCHES)
    assert not (codes(co) & IDENTITY_MISMATCHES)


def test_the_ownership_filter_gives_each_party_only_their_own_documents():
    documents = [
        document(PERSON_A, source_id="pan.jpg", party_id="APP-1"),
        document(PERSON_B, source_id="copan.jpg", party_id="COAPP-9"),
    ]

    mine = owned_by(documents, "APP-1", is_primary=True)
    theirs = owned_by(documents, "COAPP-9", is_primary=False)

    assert [d["source_id"] for d in mine] == ["pan.jpg"]
    assert [d["source_id"] for d in theirs] == ["copan.jpg"]


def test_a_co_applicant_address_does_not_reach_the_primarys_comparison():
    """
    NO CROSS-PARTY LEAKAGE. Two people legitimately live at two
    addresses; comparing them would report a mismatch that is not one.
    """
    documents = [
        document(PERSON_A, source_id="dl.jpg", party_id="APP-1",
                 document_type="DRIVING_LICENCE",
                 address="5/14, Mumbai, Maharashtra - 400043"),
        document(PERSON_B, source_id="codl.jpg", party_id="COAPP-9",
                 document_type="DRIVING_LICENCE",
                 address="88, MG Road, Pune, Maharashtra 411001"),
    ]

    payload = kyc_for(owned_by(documents, "APP-1", is_primary=True), "APP-1")

    assert "ADDRESS_MISMATCH" not in codes(payload)


# ==========================================================================
# 13. THE VERIFICATION GATE
# ==========================================================================


def test_a_failed_document_contributes_no_fields_to_the_comparison():
    """
    ONE GATE, AND CROSS-DOCUMENT IS BEHIND IT. A document whose fields
    were never released to the caller must not be compared behind their
    back.
    """
    released = _released_for_matching([
        document(PERSON_A, source_id="good.jpg"),
        document(PERSON_B, source_id="bad.jpg", verification="FAIL"),
    ])

    assert [d["source_id"] for d in released] == ["good.jpg"]


def test_a_case_of_only_rejected_documents_reports_nothing_rather_than_agreement():
    payload = kyc_for([
        document(PERSON_A, source_id="a.jpg", verification="FAIL"),
        document(PERSON_A, source_id="b.jpg", verification="FAIL"),
    ])

    assert payload["status"] == CheckStatus.SKIPPED.value
    assert payload.get("ran") is False


def test_a_rejected_document_cannot_create_a_mismatch():
    """
    The sharp end: a FAILED document naming a different person must not
    drag a clean case into a NAME_MISMATCH.
    """
    payload = kyc_for([
        document(PERSON_A, source_id="pan.jpg"),
        document(PERSON_A, source_id="dl.jpg", document_type="DRIVING_LICENCE"),
        document(PERSON_B, source_id="rejected.jpg", verification="FAIL"),
    ])

    assert not (codes(payload) & IDENTITY_MISMATCHES)


# ==========================================================================
# 15-17. THE BOUNDARIES THIS PHASE MUST NOT MOVE
# ==========================================================================


def test_the_cross_document_layer_is_on_by_default():
    """
    It was never disabled for LOS. The only caller switching it off is
    the FOS route, whose stage has no authority over KYC.
    """
    import inspect

    from app.agents.los.flow import process_application

    assert (inspect.signature(process_application)
            .parameters["cross_document_checks"].default is True)


def test_the_fos_route_still_switches_it_off():
    """
    THE STAGE BOUNDARY. Enabling cross-document checks for LOS must not
    enable them for a field officer.
    """
    import pathlib

    text = pathlib.Path("app/api/routes/fos_api.py").read_text(encoding="utf-8")

    assert "cross_document_checks=False" in text


def test_the_published_check_shape_only_ever_grows():
    """
    The existing response contract. `party_id` was ADDED so a joint
    case can say whose check each row is; it is optional and absent on
    a single-applicant case, so every existing caller is unaffected.
    No field may disappear.
    """
    from app.agents.los.schemas import CrossDocumentCheck

    assert {"check", "status", "reason_codes", "sources", "details"} <= set(
        CrossDocumentCheck.model_fields)
    assert CrossDocumentCheck.model_fields["party_id"].default is None


def test_no_internal_detail_reaches_the_published_check():
    """
    A comparison carries parsed components and per-pair detail. The
    published view carries neither -- no payloads, no paths, no tokens.
    """
    from app.agents.los.response import cross_document_from

    published = cross_document_from({
        "checks": [{
            "check": "ADDRESS", "status": "FAIL",
            "reason_codes": ["ADDRESS_MISMATCH"],
            "source_ids": ["dl.jpg", "pan.jpg"],
            "values": {"dl.jpg": "MUMBAI", "pan.jpg": "PUNE"},
            "comparisons": [{"detail": "internal"}],
            "evidence": [{"value": "internal"}],
        }],
    })

    entry = published["checks"][0]
    assert set(entry) <= {"check", "status", "reason_codes", "sources", "details"}
