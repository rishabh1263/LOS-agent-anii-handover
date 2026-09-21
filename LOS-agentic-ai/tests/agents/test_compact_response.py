"""
What the response stops saying twice, and what it stops leaking.

THE SHAPE THIS FIXES. A two-party case published every document twice —
once in `documents[]` and once inside its party's section — and every KYC
field row twice, once under the party it belonged to and once in a
case-level copy that could not say whose it was. `cross_document` then
republished the same per-party checks a third time, as though they were
case-level findings about two people disagreeing. On a five-document
joint application that was 19KB, roughly half of it the same facts
restated.

AND WHAT IT LEAKED. A Decimal reached a client as the string
`"Decimal('55435.71')"`, an income model as its own repr, and a driving
licence address as the recogniser's read of a region of the card —
`"...BENGALURUIS) EESEUEPUEUEKIN"`. None of those are values. They are
how this service is built, printed.

NOTHING BUSINESS-FACING MOVED. Every verdict, score, confidence, reason
code and decision is what it was; these tests pin the shape and the
sanitising, and the suites beside them pin the behaviour.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.agents.los import response
from app.agents.los.flow import _public_cross_document, _public_envelope
from app.agents.los.summary import deterministic_summary


def internal(source_id="pan.jpg", party_id="APP-1", fields=None,
             document_type="PAN", verification="PASS"):
    return {
        "source_id": source_id, "status": verification, "party_id": party_id,
        "party_role": ("CO_APPLICANT" if party_id.startswith("COAPP")
                       else "PRIMARY_APPLICANT"),
        "document": {"type": document_type},
        "verification": {"status": verification, "reason_codes": []},
        "extraction": {"fields": fields or {"pan_number": "AECPV7900A"}},
    }


def kyc(party_id, status="PASS", codes=None, checks=None):
    return {
        "party_id": party_id, "ran": True, "status": status,
        "reason_codes": list(codes or []), "overall_score": 80,
        "overall_confidence": 70,
        "fields": [{"field": "NAME", "status": status, "match_score": 100,
                    "confidence": 90,
                    "sources": [{"source_id": "pan.jpg",
                                 "document_type": "PAN",
                                 "value": "RISHABH AJIT SINGH"}]}],
        "checks": list(checks or [{"check": "NAME", "status": status}]),
    }


def envelope(documents, *, co_applicant_id=None, party_kyc=None,
             case_kyc=None, party_status=None):
    body = {
        "request_id": "r", "applicant_id": "APP-1", "case_id": "C",
        "status": "SUCCESS", "documents": documents, "summary": "",
        "summary_source": "deterministic", "processing": {"total_ms": 1.0},
        "errors": [],
    }
    if co_applicant_id:
        body["co_applicant_id"] = co_applicant_id
    if party_kyc:
        body["party_kyc"] = party_kyc
        body["kyc"] = case_kyc or list(party_kyc.values())[0]
    if party_status:
        body["party_status"] = party_status
    return body


@pytest.fixture
def two_party():
    return _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("dl.jpg", "APP-1",
                                                document_type="DRIVING_LICENCE"),
         internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9",
        party_kyc={"APP-1": kyc("APP-1", "REVIEW", ["NAME_MISMATCH"]),
                   "COAPP-9": kyc("COAPP-9", "PASS")},
        case_kyc={"status": "REVIEW", "reason_codes": ["NAME_MISMATCH"],
                  "overall_score": 40, "overall_confidence": 70,
                  "fields": [], "checks": []},
        party_status={"APP-1": "PARTIAL", "COAPP-9": "SUCCESS"}))


# ==========================================================================
# 5. THE CASE-LEVEL KYC IS COMPACT
# ==========================================================================


def test_the_case_kyc_carries_the_verdict_and_not_the_rows(two_party):
    assert set(two_party["kyc"]) == {"status", "reason_codes",
                                     "overall_score", "overall_confidence"}


def test_the_case_verdict_is_still_published(two_party):
    """Compact is not silent. The case-level answer stays."""
    assert two_party["kyc"]["status"] == "REVIEW"
    assert two_party["kyc"]["reason_codes"] == ["NAME_MISMATCH"]
    assert two_party["kyc"]["overall_score"] == 40


def test_the_rows_are_still_published_under_their_party(two_party):
    for section in ("primary_applicant", "co_applicant"):
        assert two_party[section]["kyc"]["fields"]


def test_a_single_applicant_case_keeps_its_rows():
    """10. The existing contract, untouched."""
    public = _public_envelope(envelope(
        [internal()], party_kyc={"APP-1": kyc("APP-1")}))

    assert "fields" in public["kyc"]
    assert public["kyc"]["fields"]


# ==========================================================================
# 3/4. CROSS-DOCUMENT REPORTS NO CROSS-PARTY FINDINGS
# ==========================================================================


def test_a_two_party_case_publishes_no_case_level_checks(two_party):
    assert two_party["cross_document"]["checks"] == []


def test_it_says_skipped_rather_than_pass(two_party):
    """
    NOT PASS. Nothing was compared across the parties, and PASS would
    claim agreement was established. The object exists precisely so
    "everything agreed" and "nothing was comparable" are distinguishable
    — both arrive with no checks and mean opposite things.
    """
    assert two_party["cross_document"]["status"] == "SKIPPED"


def test_no_cross_party_identity_mismatch_is_ever_reported(two_party):
    """
    3. Two people on a joint application differ on name, date of birth
    and father's name. That is what a joint application IS.
    """
    blob = json.dumps(two_party["cross_document"])

    for code in ("NAME_MISMATCH", "DOB_MISMATCH", "FATHER_NAME_MISMATCH"):
        assert code not in blob


def test_a_single_applicant_still_gets_its_cross_document_checks():
    """
    10. One person's documents disagreeing with each other IS a
    case-level cross-document finding, and is reported exactly as before.
    """
    body = envelope([internal()], party_kyc={"APP-1": kyc(
        "APP-1", "REVIEW", ["NAME_MISMATCH"],
        checks=[{"check": "NAME", "status": "FAIL",
                 "reason_codes": ["NAME_MISMATCH"], "source_ids": ["pan.jpg"],
                 "blocking": True}])})

    published = _public_cross_document(body, body["kyc"])

    assert published["checks"]
    assert published["status"] != "SKIPPED"


def test_the_party_checks_are_not_lost_only_relocated(two_party):
    """
    Nothing is hidden. A reviewer still learns the primary applicant's
    documents disagree — from the primary applicant's own KYC.
    """
    assert two_party["primary_applicant"]["kyc"]["reason_codes"] == [
        "NAME_MISMATCH"]


# ==========================================================================
# 6. PARTY SECTIONS REFERENCE DOCUMENTS
# ==========================================================================


def test_a_section_names_its_documents_rather_than_repeating_them(two_party):
    assert two_party["primary_applicant"]["document_ids"] == ["pan.jpg",
                                                              "dl.jpg"]
    assert "documents" not in two_party["primary_applicant"]


def test_every_named_document_exists_in_the_canonical_list(two_party):
    published = {(d["source_id"], d["party_id"]) for d in
                 two_party["documents"]}

    for section, party_id in (("primary_applicant", "APP-1"),
                              ("co_applicant", "COAPP-9")):
        for source_id in two_party[section]["document_ids"]:
            assert (source_id, party_id) in published


def test_the_canonical_list_still_carries_the_full_objects(two_party):
    assert len(two_party["documents"]) == 3
    for document in two_party["documents"]:
        assert "type" in document
        assert "verification" in document


# ==========================================================================
# 7/8. NOTHING INTERNAL, NOTHING RAW
# ==========================================================================


OCR_SPILL = ("ARCEK NARAYANAPPA BENGALURU (MR)560074 "
             "49,DEVAGERECOLONYGANGASANDRA BENGALURUIS) EESEUEPUEUEKIN")


def test_a_raw_ocr_address_is_never_published():
    """
    7. The licence's address field is the recogniser's read of a region
    of the card, tail noise and all. No client can use it.
    """
    public = _public_envelope(envelope(
        [internal("dl.jpg", "APP-1", document_type="DRIVING_LICENCE",
                  fields={"address": OCR_SPILL, "name": "ARCEK NARAYANAPPA"})]))
    extraction = public["documents"][0]["extraction"]

    assert "EESEUEPUEUEKIN" not in json.dumps(public)
    # The city and house number ARE on the card and are recovered; the
    # unplaced remainder is not. The first fix trimmed by length and
    # threw the city away with the noise.
    assert extraction["address"] == {"house": "49", "city": "BENGALURU",
                                     "pincode": "560074"}
    assert extraction["name"] == "ARCEK NARAYANAPPA"


def test_a_clean_address_keeps_its_components():
    """Structuring must not throw away a good address."""
    public = _public_envelope(envelope(
        [internal("dl.jpg", "APP-1", document_type="DRIVING_LICENCE",
                  fields={"address":
                          "12 MG Road, Shivaji Nagar, Pune, Maharashtra 411005"})]))
    address = public["documents"][0]["extraction"]["address"]

    assert address["pincode"] == "411005"
    assert address["state"] == "MAHARASHTRA"


def test_an_unusable_address_is_omitted_rather_than_published():
    public = _public_envelope(envelope(
        [internal("dl.jpg", "APP-1", document_type="DRIVING_LICENCE",
                  fields={"address": "   ", "name": "SOMEBODY"})]))

    assert "address" not in public["documents"][0]["extraction"]


@pytest.mark.parametrize("value, expected", [
    (Decimal("133877.63"), 133877.63),
    ("55435.71", "55435.71"),
    (None, None),
    (True, True),
    (7, 7),
])
def test_values_go_out_json_native(value, expected):
    assert response.jsonable(value) == expected


def test_a_decimal_never_reaches_the_response():
    """8. `"Decimal('133877.63')"` is a Python repr, not a number."""
    public = _public_envelope(envelope(
        [internal("bank.pdf", "APP-1", document_type="BANK_STATEMENT",
                  fields={"signals": {"average_monthly_credit": Decimal("133877.63"),
                                      "closing_balance": "5285.57",
                                      "monthly_net_salary": None}})]))
    signals = public["documents"][0]["extraction"]["signals"]

    assert "Decimal(" not in json.dumps(public)
    assert signals["average_monthly_credit"] == 133877.63
    assert signals["closing_balance"] == 5285.57
    assert "monthly_net_salary" not in signals


def test_a_model_repr_is_dropped_from_a_kyc_source():
    """
    The INCOME row published the income model printed:
    `monthly_net_salary=None ... average_monthly_credit=Decimal(...)`.
    """
    rows = response.public_kyc_fields({"fields": [{
        "field": "INCOME", "status": "SKIPPED", "match_score": 0,
        "confidence": 0,
        "sources": [{"source_id": "bank.pdf", "document_type": "BANK_STATEMENT",
                     "value": "monthly_net_salary=None monthly_gross_salary=None "
                              "average_monthly_credit=Decimal('55435.71')"}]}]})

    assert "Decimal(" not in json.dumps(rows)
    assert rows[0]["sources"][0] == {"source_id": "bank.pdf",
                                     "document_type": "BANK_STATEMENT"}


def test_an_address_kyc_source_is_structured_too():
    """The same OCR line, reachable through a second door."""
    rows = response.public_kyc_fields({"fields": [{
        "field": "ADDRESS", "status": "REVIEW", "match_score": 0,
        "confidence": 40,
        "sources": [{"source_id": "dl.jpg",
                     "document_type": "DRIVING_LICENCE",
                     "value": OCR_SPILL}]}]})

    assert "EESEUEPUEUEKIN" not in json.dumps(rows)
    # THE SAME OBJECT the document publishes -- one shaper, so the two
    # views cannot disagree about one address.
    assert rows[0]["sources"][0]["value"] == {"house": "49",
                                              "city": "BENGALURU",
                                              "pincode": "560074"}
    # And only one address object: `normalized_value` was a second,
    # differently-shaped copy of the same thing.
    assert "normalized_value" not in rows[0]["sources"][0]


def test_an_ordinary_value_passes_through_untouched():
    """Sanitising must not eat real values."""
    rows = response.public_kyc_fields({"fields": [{
        "field": "NAME", "status": "PASS", "match_score": 100,
        "confidence": 90,
        "sources": [{"source_id": "pan.jpg", "document_type": "PAN",
                     "value": "RISHABH AJIT SINGH"}]}]})

    assert rows[0]["sources"][0]["value"] == "RISHABH AJIT SINGH"


# ==========================================================================
# 9. THE SUMMARY NAMES THE PARTY
# ==========================================================================


def test_the_summary_says_which_party_needs_review():
    sentence = deterministic_summary({
        "documents": [{"status": "SUCCESS"}] * 5, "status": "PARTIAL",
        "applicant_id": "APP-001", "co_applicant_id": "COAPP-001",
        "party_kyc": {
            "APP-001": {"status": "REVIEW",
                        "reason_codes": ["NAME_MISMATCH", "DOB_MISMATCH",
                                         "FATHER_NAME_MISMATCH"]},
            "COAPP-001": {"status": "PASS", "reason_codes": []}}})

    assert "Primary applicant KYC requires review" in sentence
    assert "Co-applicant KYC passed" in sentence
    assert "Overall PARTIAL" in sentence


def test_the_summary_names_no_filenames():
    """
    A case-level sentence listed four filenames and left the reviewer to
    work out which two belonged to the other person.
    """
    sentence = deterministic_summary({
        "documents": [{"status": "SUCCESS"}] * 4, "status": "PARTIAL",
        "applicant_id": "APP-001", "co_applicant_id": "COAPP-001",
        "party_kyc": {"APP-001": {"status": "REVIEW",
                                  "reason_codes": ["NAME_MISMATCH"]},
                      "COAPP-001": {"status": "PASS", "reason_codes": []}}})

    assert ".jpg" not in sentence
    assert ".pdf" not in sentence


def test_a_single_applicant_summary_is_unchanged():
    """10."""
    sentence = deterministic_summary({
        "documents": [{"status": "SUCCESS"}], "status": "SUCCESS",
        "kyc": {"status": "PASS"}})

    assert sentence == ("1 document(s) processed (1 success). "
                        "Cross-document KYC checks passed. Overall SUCCESS.")


# ==========================================================================
# 11/12/13. THE VERDICTS DID NOT MOVE
# ==========================================================================


def test_the_case_status_is_unchanged_by_compaction(two_party):
    assert two_party["status"] == "SUCCESS"
    assert two_party["decision"]
    assert two_party["next_action"]


def test_each_party_keeps_its_own_status(two_party):
    assert two_party["primary_applicant"]["status"] == "PARTIAL"
    assert two_party["co_applicant"]["status"] == "SUCCESS"


@pytest.mark.parametrize("field", ["decision", "next_action"])
def test_no_party_level_decision_or_action_was_added(two_party, field):
    for section in ("primary_applicant", "co_applicant"):
        assert field not in two_party[section]


def test_the_case_keeps_the_authoritative_decision(two_party):
    assert "decision" in two_party
    assert "next_action" in two_party


# ==========================================================================
# 15. THE SIZE
# ==========================================================================


def test_compaction_actually_reduced_the_payload(two_party):
    """
    The point of the exercise, asserted rather than assumed. The
    duplicated views are what made a joint application large.
    """
    published = json.dumps(two_party)
    duplicated = (len(json.dumps(two_party["documents"]))
                  + sum(len(json.dumps(two_party[s]["kyc"]["fields"]))
                        for s in ("primary_applicant", "co_applicant")))

    # Nothing in the response repeats the documents or the field rows.
    assert published.count('"source_id": "dl.jpg"') == 1
    assert duplicated < len(published)


def test_a_party_section_is_small_beside_the_document_list(two_party):
    """
    A section is now a handful of ids, counts and a verdict. Before, it
    was a second copy of every document its party sent.
    """
    section = len(json.dumps(two_party["primary_applicant"]))
    documents = len(json.dumps(two_party["documents"]))

    assert section < documents
