"""
The joint-application cross-document contract, and what feeds it.

THE DEFECT THIS CLOSES. `cross_document` was forced to
`{"status": "SKIPPED", "checks": []}` on every case with a co-applicant.
The checks HAD run -- per party, correctly -- and were published under
each person, so the empty list looked like a conservative choice. It was
not. SKIPPED means "nothing was comparable", so the response asserted
that nothing had been cross-checked on precisely the cases where the
most checking happened.

WHAT MAKES PUBLISHING THEM SAFE is that each row now carries the
`party_id` it was computed under. Every check still ran WITHIN one
person's documents; nothing in this response compares one applicant
against another, and the tests below hold that line rather than
describing it.

THE SECOND HALF is the bank statement's account holder, which did not
exist as a field at all -- so a co-applicant uploading PAN + bank
statement reported NAME_SINGLE_SOURCE and the account went unchecked
against the person claiming it.
"""

from __future__ import annotations

import pytest

from app.agents.bank_statement import parse as bank_parse
from app.agents.bank_statement.schemas import BankStatementResult
from app.agents.kyc.schemas import CheckStatus
from app.agents.los.flow import _case_kyc, _kyc_for_party, _public_cross_document
from app.agents.los.mapping import to_kyc_source
from app.agents.los.response import cross_document_from

PRIMARY = {"name": "RISHABH AJIT SINGH", "date_of_birth": "2002-06-12",
           "pan_number": "NUHPS4875K", "father_name": "AJIT SINGH"}
CO = {"name": "SUSMITA DAS", "date_of_birth": "1998-03-04",
      "pan_number": "EVPPG6189E", "father_name": "SANTOSH DAS"}

IDENTITY_MISMATCHES = {"NAME_MISMATCH", "DOB_MISMATCH", "PAN_MISMATCH",
                       "FATHER_NAME_MISMATCH"}

DL_ADDRESS = ("BLOCK NO-F/5, R.NO.3, DEONAR NEW MUNICIPAL COLONY, "
              "Greater Mumbai, MH")


def document(person, *, source_id, party_id="APP-1", document_type="PAN",
             verification="PASS", **overrides):
    """One document as the LOS flow carries it internally."""
    return {
        "source_id": source_id, "status": verification, "party_id": party_id,
        "party_role": ("CO_APPLICANT" if party_id.startswith("COAPP")
                       else "PRIMARY_APPLICANT"),
        "document": {"type": document_type},
        "verification": {"status": verification, "reason_codes": []},
        "extraction": {"fields": {**person, **overrides}},
    }


def kyc_for(documents, party_id="APP-1"):
    return _kyc_for_party(documents, party_id=party_id, request_id="test")


def joint_case():
    """The exact shape under investigation: PAN+DL, and PAN+bank."""
    primary = kyc_for([
        document(PRIMARY, source_id="pan.jpg"),
        document(PRIMARY, source_id="dl.jpg", document_type="DRIVING_LICENCE",
                 address=DL_ADDRESS, pin_code="400043"),
    ], "APP-1")
    co = kyc_for([
        document(CO, source_id="copan.jpg", party_id="COAPP-9"),
        document({"name": "SUSMITA DAS"}, source_id="bank.pdf",
                 party_id="COAPP-9", document_type="BANK_STATEMENT"),
    ], "COAPP-9")
    payload, _rank = _case_kyc([primary, co])
    return primary, co, payload


def published(envelope, kyc):
    return _public_cross_document(envelope, kyc)


def codes(payload) -> set[str]:
    return set(payload.get("reason_codes") or [])


# ==========================================================================
# 1. SINGLE PARTY, UNCHANGED
# ==========================================================================


def test_a_single_applicant_with_pan_and_a_licence_is_consistent():
    payload = kyc_for([
        document(PRIMARY, source_id="pan.jpg"),
        document(PRIMARY, source_id="dl.jpg", document_type="DRIVING_LICENCE",
                 address=DL_ADDRESS, pin_code="400043"),
    ])
    by_check = {c["check"]: c for c in payload["checks"]}

    assert by_check["NAME"]["status"] == "PASS"
    assert by_check["DOB"]["status"] == "PASS"
    assert by_check["FATHER_NAME"]["status"] == "PASS"


def test_only_the_licence_carries_an_address_so_it_is_single_source():
    """A PAN card prints no address. One source is not a mismatch."""
    payload = kyc_for([
        document(PRIMARY, source_id="pan.jpg"),
        document(PRIMARY, source_id="dl.jpg", document_type="DRIVING_LICENCE",
                 address=DL_ADDRESS, pin_code="400043"),
    ])
    address = {c["check"]: c for c in payload["checks"]}["ADDRESS"]

    assert address["status"] == "SKIPPED"
    assert "ADDRESS_SINGLE_SOURCE" in address["reason_codes"]


def test_a_single_applicant_response_carries_no_party_id():
    """The existing contract, byte for byte."""
    out = cross_document_from({
        "checks": [{"check": "NAME", "status": "PASS", "reason_codes": []}]})

    assert out["checks"] == [
        {"check": "NAME", "status": "PASS", "reason_codes": []}]


# ==========================================================================
# 2-4. THE JOINT CASE
# ==========================================================================


def test_a_joint_case_no_longer_reports_nothing_was_cross_checked():
    """
    THE DEFECT. Checks ran for both people; the response said SKIPPED
    with an empty list.
    """
    _primary, _co, kyc = joint_case()

    out = published({"co_applicant_id": "COAPP-9"}, kyc)

    assert out["checks"], "a joint case published no checks at all"
    assert out["status"] != "SKIPPED"


def test_every_published_check_says_whose_it_is():
    _primary, _co, kyc = joint_case()

    out = published({"co_applicant_id": "COAPP-9"}, kyc)

    assert {c["party_id"] for c in out["checks"]} == {"APP-1", "COAPP-9"}


def test_two_different_people_never_become_a_mismatch():
    """
    NON-NEGOTIABLE. RISHABH and SUSMITA are different people on one
    application. Nothing may report that as a disagreement.
    """
    primary, co, kyc = joint_case()

    assert not (codes(primary) & IDENTITY_MISMATCHES)
    assert not (codes(co) & IDENTITY_MISMATCHES)

    out = published({"co_applicant_id": "COAPP-9"}, kyc)
    for check in out["checks"]:
        assert not (set(check["reason_codes"]) & IDENTITY_MISMATCHES), check


def test_each_partys_name_check_passes_on_its_own_documents():
    primary, co, _kyc = joint_case()
    name_of = lambda p: {c["check"]: c for c in p["checks"]}["NAME"]["status"]

    assert name_of(primary) == "PASS"
    assert name_of(co) == "PASS"


def test_the_status_is_not_promoted_to_pass_merely_because_checks_exist():
    out = cross_document_from({"checks": [
        {"check": "NAME", "status": "SKIPPED",
         "reason_codes": ["NAME_SINGLE_SOURCE"], "party_id": "COAPP-9"},
    ]})

    assert out["status"] == "SKIPPED"


def test_one_partys_failure_still_carries_the_case_status():
    out = cross_document_from({"checks": [
        {"check": "NAME", "status": "PASS", "reason_codes": [],
         "party_id": "APP-1"},
        {"check": "DOB", "status": "FAIL", "reason_codes": ["DOB_MISMATCH"],
         "party_id": "COAPP-9", "blocking": True},
    ]})

    assert out["status"] == "FAIL"


def test_conflict_detection_switched_off_still_publishes_nothing():
    """The one remaining reason to return an empty list."""
    from unittest.mock import patch

    _p, _c, kyc = joint_case()
    with patch("app.agents.los.config.conflict_detection_enabled",
               return_value=False):
        out = published({"co_applicant_id": "COAPP-9"}, kyc)

    assert out == {"status": "SKIPPED", "checks": []}


# ==========================================================================
# 5-7. THE BANK STATEMENT'S ACCOUNT HOLDER
# ==========================================================================


def test_the_real_canara_statement_yields_its_account_holder():
    """
    The actual sample in this repository, not a fixture. Its header
    prints `Name GUDDI DEVI`.
    """
    from app.agents.bank_statement.extract import extract_bank_statement

    raw = extract_bank_statement("samples/documents/Canara Bank Statement.pdf")

    assert raw.account_holder == "GUDDI DEVI"


def test_reading_the_holder_does_not_disturb_the_rest_of_the_extraction():
    from app.agents.bank_statement.extract import extract_bank_statement

    raw = extract_bank_statement("samples/documents/Canara Bank Statement.pdf")

    assert raw.status.value == "SUCCESS"
    assert raw.balance_reconciles is True
    assert len(raw.transactions) > 500


@pytest.mark.parametrize("header,expected", [
    ("Name GUDDI DEVI", "GUDDI DEVI"),
    ("Welcome:\nBRIJ RAJ GURJAR", "BRIJ RAJ GURJAR"),
    ("Account Holder Name: SUSMITA DAS", "SUSMITA DAS"),
    ("Customer Name : Rishabh Ajit Singh", "RISHABH AJIT SINGH"),
])
def test_a_labelled_holder_is_read(header, expected):
    assert bank_parse.detect_account_holder(header) == expected


@pytest.mark.parametrize("header", [
    "Branch Name SITAMARHI",
    "Bank Name CANARA BANK",
    "IFSC Code CNRB0002312",
    "Address W O PURUSHOTTAM KUMAR",
    "Nominee Registered Yes",
    "Name of Branch SITAMARHI",
    "Customer Id XXXXXXX30",
    "Account No. 6347393980",
    "Name 12345",
    "Name UPI/CR/60226/OM",
    "Name Central Market, SITAMARHI",
    "Account Type  Savings",
    "Guardian Name RAM KUMAR",
    "Father Name AJIT SINGH",
    "Name ",
    "",
])
def test_nothing_that_is_not_a_holder_is_read_as_one(header):
    """
    EVERY ONE OF THESE APPEARS ON A REAL SAMPLE, most of them on the
    Canara statement within three lines of the genuine name. A wrong
    name here is compared against the applicant's PAN and decides their
    case, so the bar is "labelled, or nothing".
    """
    assert bank_parse.detect_account_holder(header) is None


def test_an_unlabelled_holder_is_given_up_rather_than_guessed(monkeypatch):
    """
    The Kotak statement prints the holder as a bare second line, with
    no caption. It is indistinguishable in shape from a branch or a
    city, so it is not extracted.

    GIVEN ROOM, so the outcome does not depend on how loaded the machine
    is: under a busy suite run the upload budget can legitimately queue
    a 39-page statement, and that says nothing about the holder.
    """
    from app.agents.bank_statement.extract import extract_bank_statement

    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "120000")

    raw = extract_bank_statement("samples/documents/KOTAK BANK STATEMENT.pdf")

    assert raw.account_holder is None
    assert raw.status.value in {"SUCCESS", "PARTIAL"}


def test_a_statement_with_no_readable_holder_is_still_a_good_statement():
    """Extraction failure of ONE optional field rejects nothing."""
    result = BankStatementResult.model_validate({
        "status": "SUCCESS", "source_kind": "DIGITAL"})

    assert result.account_holder is None
    assert result.status.value == "SUCCESS"


def test_the_holder_reaches_kyc_as_an_ordinary_name():
    """
    No bank-specific identity path. `name` is the field
    `mapping._NAME_FIELDS` already read and `check_name` already
    compares.
    """
    source = to_kyc_source({
        "document": {"type": "BANK_STATEMENT"},
        "extraction": {"fields": {"name": "SUSMITA DAS",
                                  "account_number_masked": "XXXX0820"}},
    }, "bank.pdf")

    assert source is not None
    assert source.name == "SUSMITA DAS"


def test_a_co_applicant_with_pan_and_a_named_bank_statement_passes():
    """
    The case from the investigation: NAME_SINGLE_SOURCE becomes PASS
    once the statement names its holder.
    """
    payload = kyc_for([
        document(CO, source_id="copan.jpg", party_id="COAPP-9"),
        document({"name": "SUSMITA DAS"}, source_id="bank.pdf",
                 party_id="COAPP-9", document_type="BANK_STATEMENT"),
    ], "COAPP-9")
    name = {c["check"]: c for c in payload["checks"]}["NAME"]

    assert name["status"] == "PASS"


def test_a_bank_statement_naming_someone_else_is_a_mismatch():
    payload = kyc_for([
        document(CO, source_id="copan.jpg", party_id="COAPP-9"),
        document({"name": "SOMEBODY ELSE ENTIRELY"}, source_id="bank.pdf",
                 party_id="COAPP-9", document_type="BANK_STATEMENT"),
    ], "COAPP-9")

    assert "NAME_MISMATCH" in codes(payload)


def test_an_unnamed_bank_statement_falls_back_to_single_source():
    """
    Asserted on the NAME check, not on the aggregate: the party-level
    roll-up collapses to INSUFFICIENT_SOURCES, which says the case had
    little to work with -- not what the NAME check itself found.
    """
    payload = kyc_for([
        document(CO, source_id="copan.jpg", party_id="COAPP-9"),
        document({"account_number_masked": "XXXX0820"}, source_id="bank.pdf",
                 party_id="COAPP-9", document_type="BANK_STATEMENT"),
    ], "COAPP-9")
    name = {c["check"]: c for c in payload["checks"]}["NAME"]

    assert name["status"] == "SKIPPED"
    assert name["reason_codes"] == ["NAME_SINGLE_SOURCE"]
    assert "NAME_MISMATCH" not in codes(payload)


# ==========================================================================
# 8. ADDRESS -- PRESERVED, AND NOW REACHABLE
# ==========================================================================


def test_two_address_bearing_documents_actually_invoke_the_matcher():
    """
    A licence and a voter card both print an address, so the comparison
    that PAN+DL could never reach does run.
    """
    payload = kyc_for([
        document(PRIMARY, source_id="dl.jpg", document_type="DRIVING_LICENCE",
                 address="5/14, Mumbai, Maharashtra - 400043"),
        document({**PRIMARY, "epic_number": "ABC1234567"},
                 source_id="voter.jpg", document_type="VOTER_ID",
                 address="5-14 Mumbai Maharashtra 400043"),
    ])
    address = {c["check"]: c for c in payload["checks"]}["ADDRESS"]

    assert address["status"] == "PASS"


def test_two_genuinely_different_addresses_are_still_caught():
    payload = kyc_for([
        document(PRIMARY, source_id="dl.jpg", document_type="DRIVING_LICENCE",
                 address="5/14, Deonar, Mumbai, Maharashtra - 400043"),
        document({**PRIMARY, "epic_number": "ABC1234567"},
                 source_id="voter.jpg", document_type="VOTER_ID",
                 address="88 MG Road Pune Maharashtra 411001"),
    ])

    assert "ADDRESS_MISMATCH" in codes(payload)


def test_an_address_disagreement_does_not_reject_the_documents():
    """Address is non-blocking: it routes to a human, it does not reject."""
    from app.agents.kyc import config as kyc_config

    assert kyc_config.check_blocking("address") is False


def test_no_address_is_invented_for_a_pan_or_a_bank_statement():
    for document_type in ("PAN", "BANK_STATEMENT"):
        source = to_kyc_source({
            "document": {"type": document_type},
            "extraction": {"fields": {"name": "SUSMITA DAS"}},
        }, "x.pdf")

        assert source is not None
        assert source.address is None, document_type


# ==========================================================================
# 9. THE VOTER CARD'S RELATION
# ==========================================================================


def voter(relation_type, relation_name="RAM KUMAR"):
    return to_kyc_source({
        "document": {"type": "VOTER_ID"},
        "extraction": {"fields": {
            "name": "SUSMITA DAS", "epic_number": "ABC1234567",
            "relation_name": relation_name, "relation_type": relation_type}},
    }, "voter.jpg")


@pytest.mark.parametrize("relation_type", ["FATHER", "Father", "father's",
                                           "FATHERS", "Father Name"])
def test_a_voter_card_naming_a_father_joins_the_father_name_check(relation_type):
    assert voter(relation_type).father_name == "RAM KUMAR"


@pytest.mark.parametrize("relation_type", ["HUSBAND", "MOTHER", "WIFE", "", None])
def test_a_relation_that_is_not_a_father_is_not_treated_as_one(relation_type):
    """
    AN EPIC PRINTS THE HUSBAND'S NAME FOR A MARRIED WOMAN. Reading that
    as the father's name reports FATHER_NAME_MISMATCH against a PAN
    that is perfectly consistent -- a false mismatch aimed squarely at
    married women. An unlabelled relation is given up, not assumed.
    """
    assert voter(relation_type).father_name is None


def test_an_explicit_father_name_still_wins_over_the_relation():
    source = to_kyc_source({
        "document": {"type": "VOTER_ID"},
        "extraction": {"fields": {
            "name": "SUSMITA DAS", "epic_number": "ABC1234567",
            "father_name": "AJIT SINGH",
            "relation_name": "SOMEONE ELSE", "relation_type": "FATHER"}},
    }, "voter.jpg")

    assert source.father_name == "AJIT SINGH"


def test_the_relation_is_never_matched_against_the_applicants_own_name():
    """It is a father's name. It must not reach the NAME check."""
    from app.agents.los.mapping import _NAME_FIELDS

    assert "relation_name" not in _NAME_FIELDS
    assert voter("FATHER").name == "SUSMITA DAS"


def test_a_voter_card_and_a_pan_agree_on_the_father():
    payload = kyc_for([
        document(PRIMARY, source_id="pan.jpg"),
        document({"name": "RISHABH AJIT SINGH", "epic_number": "ABC1234567",
                  "relation_name": "AJIT SINGH", "relation_type": "FATHER"},
                 source_id="voter.jpg", document_type="VOTER_ID"),
    ])
    father = {c["check"]: c for c in payload["checks"]}["FATHER_NAME"]

    assert father["status"] == "PASS"


# ==========================================================================
# 10-12. WHAT MUST NOT HAVE MOVED
# ==========================================================================


def test_the_fos_route_still_switches_cross_document_off():
    import pathlib

    text = pathlib.Path("app/api/routes/fos_api.py").read_text(encoding="utf-8")

    assert "cross_document_checks=False" in text


def test_the_verification_gate_still_holds():
    from app.agents.los.flow import _released_for_matching

    released = _released_for_matching([
        document(PRIMARY, source_id="good.jpg"),
        document(CO, source_id="bad.jpg", verification="FAIL"),
    ])

    assert [d["source_id"] for d in released] == ["good.jpg"]


def test_a_rejected_document_still_cannot_reach_the_published_checks():
    payload = kyc_for([
        document(PRIMARY, source_id="pan.jpg"),
        document(PRIMARY, source_id="dl.jpg", document_type="DRIVING_LICENCE"),
        document(CO, source_id="rejected.jpg", verification="FAIL"),
    ])

    out = cross_document_from(payload)
    for check in out["checks"]:
        assert "rejected.jpg" not in (check.get("sources") or [])


def test_the_published_check_shape_gained_only_party_id():
    from app.agents.los.schemas import CrossDocumentCheck

    assert set(CrossDocumentCheck.model_fields) == {
        "check", "party_id", "status", "reason_codes", "sources", "details"}


def test_no_internal_detail_reaches_the_published_check():
    out = cross_document_from({"checks": [{
        "check": "ADDRESS", "status": "FAIL",
        "reason_codes": ["ADDRESS_MISMATCH"], "party_id": "APP-1",
        "source_ids": ["dl.jpg", "voter.jpg"],
        "values": {"dl.jpg": "MUMBAI", "voter.jpg": "PUNE"},
        "comparisons": [{"detail": "internal"}],
        "evidence": [{"value": "internal"}],
    }]})

    assert set(out["checks"][0]) <= {
        "check", "party_id", "status", "reason_codes", "sources", "details"}


def test_the_account_number_is_never_published_as_identity():
    from app.agents.kyc.schemas import SourceDocument

    assert "account_number_masked" not in SourceDocument.model_fields
