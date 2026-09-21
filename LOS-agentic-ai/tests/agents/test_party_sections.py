"""
The response, grouped by whose documents these are.

WHAT PHASE 5 ADDS, AND WHAT IT MUST NOT. `primary_applicant` and
`co_applicant` give a client one place to read each party's documents,
profile match and verification counts, instead of splitting `documents[]`
by `party_id` itself.

IT IS A REGROUPING. The entries in a section are the same dicts published
at the top level, and the profile match is the one Phase 4 already
computed. Nothing is re-verified, re-extracted or re-matched. Most of this
file exists to pin that: if the sections could ever disagree with the
top-level fields, a client reading one and a reviewer reading the other
would see different applications.

THE OWNERSHIP RULE. A document belongs to the party stamped on it in the
flow — never to whoever's filename or document type looks right. Both
parties routinely upload `pan.jpg` and both routinely send a PAN, so
neither says whose it is.
"""

from __future__ import annotations

import pytest

from app.agents.los import parties, response
from app.agents.los.flow import _party_sections, _public_envelope


def compact(source_id="pan.jpg", party_id="APP-1", verification="PASS",
            document_type="PAN"):
    """A document in the compact public shape, as a section carries it."""
    return {"source_id": source_id, "type": document_type,
            "status": verification, "verification": verification,
            "party_id": party_id}


def internal(source_id="pan.jpg", party_id="APP-1", party_role=None,
             verification="PASS", document_type="PAN", fields=None):
    """A document as the flow carries it, before the response boundary."""
    entry = {
        "source_id": source_id, "status": verification,
        "document": {"type": document_type},
        "verification": {"status": verification, "reason_codes": []},
        "extraction": {"fields": fields or {"pan_number": "AECPV7900A"}},
    }
    if party_id:
        entry["party_id"] = party_id
        entry["party_role"] = party_role or (
            "CO_APPLICANT" if party_id.startswith("COAPP") else
            "PRIMARY_APPLICANT")
    return entry


def envelope(documents, *, applicant_id="APP-1", co_applicant_id=None,
             profile_match=None):
    body = {
        "request_id": "req-1", "applicant_id": applicant_id,
        "case_id": "CASE-1", "status": "SUCCESS", "documents": documents,
        "summary": "", "summary_source": "deterministic",
        "processing": {"total_ms": 1.0}, "errors": [],
    }
    if co_applicant_id:
        body["co_applicant_id"] = co_applicant_id
    if profile_match:
        body["profile_match"] = profile_match
    return body


def match_for(party_id, role, status="PASS"):
    """A Phase 4 party match, in its published shape."""
    return {
        "party_id": party_id, "role": role, "party_role": role,
        "score": 100 if status == "PASS" else 0, "confidence": 94,
        "fields_expected": 1, "fields_extracted": 1, "fields_compared": 1,
        "fields": [{
            "field": "PAN_NUMBER", "status": status,
            "match_score": 100 if status == "PASS" else 0, "confidence": 94,
            "reason_code": ("PROFILE_MATCH" if status == "PASS"
                            else "PROFILE_MISMATCH"),
            "reason": "...",
            "source": {"source_id": "pan.jpg", "document_type": "PAN"},
        }],
    }


# ==========================================================================
# A. PRIMARY ONLY
# ==========================================================================


def test_a_single_applicant_gets_a_primary_section():
    public = _public_envelope(envelope([internal()]))

    assert public["primary_applicant"]["party_id"] == "APP-1"
    assert public["primary_applicant"]["role"] == "PRIMARY_APPLICANT"


def test_a_single_applicant_section_holds_their_documents():
    public = _public_envelope(envelope([internal(), internal("dl.jpg")]))

    assert public["primary_applicant"]["document_ids"] == ["pan.jpg", "dl.jpg"]


def test_the_primary_section_is_present_even_with_no_documents():
    """One shape to render, not two code paths."""
    public = _public_envelope(envelope([]))

    assert public["primary_applicant"]["document_ids"] == []
    assert public["primary_applicant"]["verification_summary"][
        "total_documents"] == 0


# ==========================================================================
# B. CO-APPLICANT ABSENT / PRESENT
# ==========================================================================


def test_no_co_applicant_section_without_a_co_applicant():
    """
    Absent, not null and not empty. A null would read as "there is a
    second party and we do not know who"; an empty section would read as
    "there is a second party who sent nothing".
    """
    public = _public_envelope(envelope([internal()]))

    assert "co_applicant" not in public


def test_a_co_applicant_gets_their_own_section():
    public = _public_envelope(envelope(
        [internal(), internal(party_id="COAPP-9")],
        co_applicant_id="COAPP-9"))

    assert public["co_applicant"]["party_id"] == "COAPP-9"
    assert public["co_applicant"]["role"] == "CO_APPLICANT"


def test_a_declared_co_applicant_with_no_documents_still_gets_a_section():
    """
    Declaring the second party before collecting their documents is an
    ordinary sequence. An empty list is the honest answer; omitting the
    section would lose the fact that they exist.
    """
    public = _public_envelope(envelope([internal()],
                                       co_applicant_id="COAPP-9"))

    assert public["co_applicant"]["document_ids"] == []
    assert public["co_applicant"]["verification_summary"][
        "total_documents"] == 0


# ==========================================================================
# C/D. THE SAME FILENAME, AND THE SAME TYPE, FROM BOTH PARTIES
# ==========================================================================


def test_the_same_filename_from_both_parties_lands_in_both_sections():
    """
    THE COLLISION PHASE 3 EXISTS FOR, now at the response boundary. Both
    uploaded `pan.jpg`. Grouping on the filename would put one document
    in both sections, or one party's card on the other's file.
    """
    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9"))

    assert public["primary_applicant"]["document_ids"] == ["pan.jpg"]
    assert public["co_applicant"]["document_ids"] == ["pan.jpg"]
    # Same name, two people. The section a document is listed under is
    # what says whose it is; the full objects carry `party_id`.
    assert len(public["documents"]) == 2
    assert {d["party_id"] for d in public["documents"]} == {"APP-1", "COAPP-9"}


def test_the_same_document_type_from_both_parties_stays_separate():
    """Both sent a PAN. The type says what it is, never whose it is."""
    public = _public_envelope(envelope(
        [internal("a.jpg", "APP-1", document_type="PAN"),
         internal("b.jpg", "COAPP-9", document_type="PAN")],
        co_applicant_id="COAPP-9"))

    assert public["primary_applicant"]["document_ids"] == ["a.jpg"]
    assert public["co_applicant"]["document_ids"] == ["b.jpg"]


def test_ownership_is_read_from_the_stamp_and_nothing_else():
    documents = [compact("pan.jpg", "APP-1"), compact("pan.jpg", "COAPP-9")]

    assert parties.owned_by(documents, "APP-1",
                            is_primary=True) == [documents[0]]
    assert parties.owned_by(documents, "COAPP-9",
                            is_primary=False) == [documents[1]]


# ==========================================================================
# I. NO CROSS-PARTY CONTAMINATION
# ==========================================================================


def test_no_document_appears_in_both_sections():
    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("dl.jpg", "APP-1"),
         internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9"))

    owned = {}
    for document in public["documents"]:
        owned.setdefault(document["party_id"], set()).add(document["source_id"])

    # Two people sent `pan.jpg`, so the ids alone overlap. What must not
    # overlap is the DOCUMENTS, which the top-level list keys by party.
    assert owned["APP-1"] == set(public["primary_applicant"]["document_ids"])
    assert owned["COAPP-9"] == set(public["co_applicant"]["document_ids"])
    assert len(public["documents"]) == 3


def test_every_document_is_accounted_for_exactly_once():
    """
    Neither lost nor duplicated: the two sections partition the case.
    """
    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("dl.jpg", "APP-1"),
         internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9"))

    grouped = (len(public["primary_applicant"]["document_ids"])
               + len(public["co_applicant"]["document_ids"]))

    assert grouped == len(public["documents"]) == 3


def test_a_section_never_carries_the_other_partys_documents():
    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9"))

    owned = {}
    for document in public["documents"]:
        owned.setdefault(document["party_id"], set()).add(document["source_id"])

    for section, owner in (("primary_applicant", "APP-1"),
                           ("co_applicant", "COAPP-9")):
        assert set(public[section]["document_ids"]) == owned[owner]


# ==========================================================================
# H. LEGACY ROWS WRITTEN BEFORE PARTIES EXISTED
# ==========================================================================


def test_an_unstamped_document_belongs_to_the_primary_applicant():
    """
    Before the party model, every document on a case belonged to its
    applicant. A stored row written then carries no party_id, and
    dropping it would hide a document from the person who sent it.
    """
    public = _public_envelope(envelope([internal(party_id=None)]))

    assert public["primary_applicant"]["document_ids"] == ["pan.jpg"]


def test_an_unstamped_document_is_never_given_to_the_co_applicant():
    """
    The adoption is offered to the primary applicant ONLY. Offering it to
    both would hand one person's document to another on a two-party case
    — the exact failure the party model prevents.
    """
    public = _public_envelope(envelope(
        [internal(party_id=None), internal(party_id="COAPP-9")],
        co_applicant_id="COAPP-9"))

    assert public["primary_applicant"]["document_ids"] == ["pan.jpg"]
    assert public["co_applicant"]["document_ids"] == ["pan.jpg"]
    assert {d.get("party_id") for d in public["documents"]} == {
        None, "COAPP-9"}


def test_the_legacy_adoption_is_for_the_primary_only():
    documents = [compact("pan.jpg", party_id=None)]

    assert len(parties.owned_by(documents, "APP-1", is_primary=True)) == 1
    assert parties.owned_by(documents, "COAPP-9", is_primary=False) == []


# ==========================================================================
# E/F. THE PROFILE MATCH APPEARS UNDER THE RIGHT PARTY
# ==========================================================================


def test_each_partys_match_lands_in_their_own_section():
    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9",
        profile_match=[match_for("APP-1", "PRIMARY_APPLICANT"),
                       match_for("COAPP-9", "CO_APPLICANT", status="FAIL")]))

    assert public["primary_applicant"]["profile_match"]["party_id"] == "APP-1"
    assert public["co_applicant"]["profile_match"]["party_id"] == "COAPP-9"


def test_a_mismatch_is_reported_under_the_party_it_belongs_to():
    """
    THE ONE THAT MATTERS. A mismatch shown under the wrong party sends a
    reviewer after the wrong person's documents.
    """
    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9",
        profile_match=[match_for("APP-1", "PRIMARY_APPLICANT"),
                       match_for("COAPP-9", "CO_APPLICANT", status="FAIL")]))

    assert public["primary_applicant"]["profile_match"][
        "fields"][0]["status"] == "PASS"
    assert public["co_applicant"]["profile_match"][
        "fields"][0]["status"] == "FAIL"


def test_a_party_with_no_profile_has_no_match_key():
    """
    Absent, not empty. An empty object reads as "we matched and found
    nothing", which is a much stronger claim than "nobody told us who
    this person is".
    """
    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9",
        profile_match=[match_for("APP-1", "PRIMARY_APPLICANT")]))

    assert "profile_match" in public["primary_applicant"]
    assert "profile_match" not in public["co_applicant"]


def test_the_section_match_is_the_same_object_as_the_top_level_one():
    """
    Not a copy that could drift. Phase 4 computed it once.
    """
    match = match_for("APP-1", "PRIMARY_APPLICANT")
    public = _public_envelope(envelope([internal()], profile_match=[match]))

    assert public["primary_applicant"]["profile_match"] is public[
        "profile_match"][0]


def test_the_published_match_fields_carry_nothing_internal():
    allowed = {"field", "status", "match_score", "confidence",
               "reason_code", "reason", "source"}
    public = _public_envelope(envelope(
        [internal()],
        profile_match=[match_for("APP-1", "PRIMARY_APPLICANT")]))

    for row in public["primary_applicant"]["profile_match"]["fields"]:
        assert set(row) <= allowed


# ==========================================================================
# 4. THE VERIFICATION SUMMARY
# ==========================================================================


def test_the_summary_counts_each_verdict():
    summary = response.verification_summary([
        compact(verification="PASS"), compact(verification="PASS"),
        compact(verification="REVIEW"), compact(verification="FAIL"),
        compact(verification="SKIPPED"),
    ])

    assert summary == {"total_documents": 5, "passed": 2, "review": 1,
                       "failed": 1, "skipped": 1}


@pytest.mark.parametrize("verdict", ["FAIL", "FAILED", "REJECTED"])
def test_every_kind_of_refusal_counts_as_failed(verdict):
    """
    One means unprocessable and the other processed-and-refused. Triage
    treats both the same; the distinction survives on the document.
    """
    summary = response.verification_summary([compact(verification=verdict)])

    assert summary["failed"] == 1


def test_the_buckets_always_sum_to_the_total():
    """
    A summary that quietly loses a document is worse than one that files
    it under the wrong heading, so an unrecognised verdict is counted.
    """
    summary = response.verification_summary([
        compact(verification="PASS"), compact(verification="WHAT_IS_THIS"),
    ])
    counted = sum(summary[k] for k in ("passed", "review", "failed", "skipped"))

    assert counted == summary["total_documents"] == 2


def test_a_missing_verdict_is_skipped_not_dropped():
    summary = response.verification_summary([{"source_id": "x.jpg"}])

    assert summary["skipped"] == 1
    assert summary["total_documents"] == 1


def test_the_summary_matches_the_documents_beneath_it():
    public = _public_envelope(envelope(
        [internal("a.jpg", "APP-1", verification="PASS"),
         internal("b.jpg", "APP-1", verification="REVIEW")]))
    section = public["primary_applicant"]

    assert section["verification_summary"]["total_documents"] == len(
        section["document_ids"])
    assert section["verification_summary"]["passed"] == 1
    assert section["verification_summary"]["review"] == 1


def test_each_party_is_counted_separately():
    public = _public_envelope(envelope(
        [internal("a.jpg", "APP-1", verification="PASS"),
         internal("b.jpg", "COAPP-9", verification="REVIEW")],
        co_applicant_id="COAPP-9"))

    assert public["primary_applicant"]["verification_summary"]["passed"] == 1
    assert public["primary_applicant"]["verification_summary"]["review"] == 0
    assert public["co_applicant"]["verification_summary"]["review"] == 1
    assert public["co_applicant"]["verification_summary"]["passed"] == 0


# ==========================================================================
# PARTY-SCOPED KYC, AS THE RESPONSE PUBLISHES IT
# ==========================================================================


def kyc_payload(party_id, status="PASS", codes=None):
    return {"party_id": party_id, "ran": True, "status": status,
            "reason_codes": list(codes or []), "overall_score": 90,
            "overall_confidence": 80,
            "fields": [{"field": "NAME", "status": status, "match_score": 100,
                        "confidence": 90, "sources": [
                            {"source_id": "pan.jpg", "document_type": "PAN"}]}],
            "checks": [{"check": "NAME", "status": status}]}


def with_kyc(documents, party_kyc, **kwargs):
    body = envelope(documents, **kwargs)
    body["party_kyc"] = party_kyc
    body["kyc"] = list(party_kyc.values())[0]
    return body


def test_each_party_publishes_its_own_kyc():
    public = _public_envelope(with_kyc(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        {"APP-1": kyc_payload("APP-1"),
         "COAPP-9": kyc_payload("COAPP-9", "REVIEW", ["NAME_MISMATCH"])},
        co_applicant_id="COAPP-9"))

    assert public["primary_applicant"]["kyc"]["status"] == "PASS"
    assert public["co_applicant"]["kyc"]["status"] == "REVIEW"
    assert public["co_applicant"]["kyc"]["reason_codes"] == ["NAME_MISMATCH"]


def test_a_partys_kyc_carries_no_internals():
    """
    The same allowlist the case-level object uses. `ran`, `party_id` and
    `checks` are internal: the cross-document view already publishes the
    checks, and repeating them would put the same codes in twice.
    """
    public = _public_envelope(with_kyc(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        {"APP-1": kyc_payload("APP-1"), "COAPP-9": kyc_payload("COAPP-9")},
        co_applicant_id="COAPP-9"))

    assert set(public["primary_applicant"]["kyc"]) == {
        "status", "reason_codes", "overall_score", "overall_confidence",
        "fields"}


def test_a_party_with_no_kyc_has_no_kyc_key():
    public = _public_envelope(envelope([internal()]))

    assert "kyc" not in public["primary_applicant"]


def test_a_single_applicant_kyc_is_not_published_twice():
    """
    On a single-applicant case the top-level `kyc` IS this party's, so
    repeating it in the section is a byte-identical copy carrying no
    information. It pushed a two-document response past the size guard
    in tests/integration/test_los_production_e2e.py, which is how it
    was caught.
    """
    public = _public_envelope(with_kyc(
        [internal()], {"APP-1": kyc_payload("APP-1")}))

    assert public["kyc"]["status"] == "PASS"
    assert "kyc" not in public["primary_applicant"]


def test_the_case_kyc_is_compact_on_a_two_party_case():
    """
    The verdict, score, confidence and reason codes -- but not the field
    rows. Those are already published under the party each one belongs
    to, and up here they could not say whose they were.
    """
    public = _public_envelope(with_kyc(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        {"APP-1": kyc_payload("APP-1"),
         "COAPP-9": kyc_payload("COAPP-9")},
        co_applicant_id="COAPP-9"))

    assert set(public["kyc"]) == {"status", "reason_codes", "overall_score",
                                  "overall_confidence"}
    assert public["primary_applicant"]["kyc"]["fields"]
    assert public["co_applicant"]["kyc"]["fields"]


def test_the_party_id_on_a_single_party_row_is_not_published():
    """
    A single-applicant response carries exactly the keys it carried
    before: the owner is only added where two parties make it necessary.
    """
    public = _public_envelope(with_kyc(
        [internal()], {"APP-1": kyc_payload("APP-1")}))

    for row in public["kyc"]["fields"]:
        assert "party_id" not in row
    assert "kyc" not in public["primary_applicant"]


def test_the_party_id_is_published_on_a_two_party_case():
    from app.agents.los.flow import _case_kyc

    aggregate, _rank = _case_kyc([kyc_payload("APP-1"),
                                  kyc_payload("COAPP-9")])
    body = with_kyc([internal("pan.jpg", "APP-1"),
                     internal("pan.jpg", "COAPP-9")],
                    {"APP-1": kyc_payload("APP-1"),
                     "COAPP-9": kyc_payload("COAPP-9")},
                    co_applicant_id="COAPP-9")
    body["kyc"] = aggregate
    public = _public_envelope(body)

    # The case object is compact, so the rows live where they belong:
    # inside each party's own KYC.
    assert "fields" not in public["kyc"]
    assert public["primary_applicant"]["kyc"]["fields"]
    assert public["co_applicant"]["kyc"]["fields"]


def test_profile_match_and_kyc_stay_separate_layers():
    """
    One asks whether the documents agree with each other, the other
    whether they match what the application declared. Both appear;
    neither is derived from the other.
    """
    public = _public_envelope(with_kyc(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        {"APP-1": kyc_payload("APP-1"), "COAPP-9": kyc_payload("COAPP-9")},
        co_applicant_id="COAPP-9",
        profile_match=[match_for("APP-1", "PRIMARY_APPLICANT")]))
    section = public["primary_applicant"]

    assert section["kyc"]["status"] == "PASS"
    assert section["profile_match"]["party_id"] == "APP-1"
    assert section["kyc"] is not section["profile_match"]


# ==========================================================================
# J. THE EXISTING CONTRACT IS UNTOUCHED
# ==========================================================================


def test_every_existing_top_level_key_survives():
    public = _public_envelope(envelope([internal()]))

    for key in ("request_id", "applicant_id", "case_id", "status",
                "documents", "cross_document", "decision", "next_action",
                "summary", "summary_source", "processing_ms", "errors"):
        assert key in public, key


def test_the_flat_document_list_is_unchanged():
    """
    Existing callers read `documents[]`. The sections are additive; they
    do not replace it and do not reorder it.
    """
    public = _public_envelope(envelope(
        [internal("a.jpg", "APP-1"), internal("b.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9"))

    assert [d["source_id"] for d in public["documents"]] == ["a.jpg", "b.jpg"]


def test_the_sections_reference_documents_rather_than_copying_them():
    """
    CHANGED DELIBERATELY. The sections used to carry the full objects,
    which put every document in the response twice -- half the payload
    was a second copy that could only ever agree with the first. They
    now name them by `source_id`.
    """
    public = _public_envelope(envelope([internal()]))

    assert public["primary_applicant"]["document_ids"] == ["pan.jpg"]
    assert public["documents"][0]["source_id"] == "pan.jpg"
    assert "documents" not in public["primary_applicant"]


def test_the_response_still_validates_against_its_contract():
    from app.agents.los.schemas import LosProcessResponse

    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"), internal("pan.jpg", "COAPP-9")],
        co_applicant_id="COAPP-9",
        profile_match=[match_for("APP-1", "PRIMARY_APPLICANT")]))
    public["kyc"] = {"status": "PASS", "reason_codes": [],
                     "overall_score": 100, "overall_confidence": 90,
                     "fields": []}

    parsed = LosProcessResponse.model_validate(public)

    assert parsed.primary_applicant.party_id == "APP-1"
    assert parsed.co_applicant.party_id == "COAPP-9"


def test_the_sections_decide_nothing():
    """
    A regrouping. No verdict, no score and no reason code is reachable
    from here — every one of them is what verification already found.
    """
    documents = [internal("pan.jpg", "APP-1", verification="REVIEW")]
    before = _public_envelope(envelope(documents))

    sections = _party_sections(
        envelope(documents), before["documents"])

    assert "decision" not in sections["primary_applicant"]
    assert "status" not in sections["primary_applicant"]
    assert before["decision"] == _public_envelope(
        envelope(documents))["decision"]


def test_a_case_with_no_applicant_gets_no_sections():
    """
    Nothing is invented. Without an applicant id there is no party to
    attribute anything to, and a made-up one would be a wrong answer.
    """
    body = envelope([internal()])
    body["applicant_id"] = None

    assert _party_sections(body, []) == {}
