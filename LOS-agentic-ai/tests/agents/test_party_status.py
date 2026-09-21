"""
Each party's own state, and the one decision that belongs to the case.

WHAT IS EXPOSED. `primary_applicant.status` and `co_applicant.status`
describe THAT PARTY'S documents and their own KYC — the same worst-wins
roll-up the case uses, over a smaller set.

WHAT IS DELIBERATELY NOT. There is no party-level `next_action` and no
party-level `decision`. A party-level CONTINUE sitting beside a
case-level MANUAL_REVIEW reads as permission to proceed, and there is
ONE decision on a loan. Several tests below exist only to keep those
fields from appearing.

THE GUARD THAT MATTERS MOST. A declared co-applicant who has uploaded
nothing has no documents and no KYC, and both of those rank as harmless
— so the arithmetic alone reports SUCCESS for a party about whom NOTHING
IS KNOWN. That is the most misleading thing a per-party status could
say, and `_status_for` refuses it explicitly.
"""

from __future__ import annotations

import pytest

from app.agents.los import parties
from app.agents.los.flow import _kyc_rank_of, _public_envelope, _status_for


def internal(source_id="pan.jpg", party_id="APP-1", verification="PASS",
             reason_codes=None, document_type="PAN"):
    entry = {
        "source_id": source_id, "status": verification,
        "document": {"type": document_type},
        "verification": {"status": verification,
                         "reason_codes": list(reason_codes or [])},
        "extraction": {"fields": {"pan_number": "AECPV7900A"}},
    }
    if party_id:
        entry["party_id"] = party_id
        entry["party_role"] = ("CO_APPLICANT" if party_id.startswith("COAPP")
                               else "PRIMARY_APPLICANT")
    return entry


def envelope(documents, *, co_applicant_id=None, party_status=None,
             status="SUCCESS"):
    body = {
        "request_id": "r", "applicant_id": "APP-1", "case_id": "C",
        "status": status, "documents": documents, "summary": "",
        "summary_source": "deterministic", "processing": {"total_ms": 1.0},
        "errors": [],
    }
    if co_applicant_id:
        body["co_applicant_id"] = co_applicant_id
    if party_status:
        body["party_status"] = party_status
    return body


# ==========================================================================
# THE ROLL-UP HELPER
# ==========================================================================


def test_a_clean_set_of_documents_is_a_success():
    status, nothing_verified = _status_for([internal()], 0)

    assert status == "SUCCESS"
    assert nothing_verified is False


def test_a_failing_document_drags_the_roll_up_down():
    assert _status_for([internal(verification="FAILED")], 0)[0] == "FAILED"


def test_a_kyc_review_reaches_the_roll_up():
    assert _status_for([internal()], 1)[0] == "PARTIAL"


def test_a_type_mismatch_is_capped_at_review():
    """
    A WRONG UPLOAD IS NOT A CREDIT REJECTION. The applicant has the
    document and sent the wrong file; they can send the right one.
    """
    mismatch = internal(verification="FAILED",
                        reason_codes=["DOCUMENT_TYPE_MISMATCH"])

    assert _status_for([mismatch], 0)[0] != "FAILED"


def test_nothing_verified_is_not_a_success():
    status, nothing_verified = _status_for(
        [internal(verification="SKIPPED")], 0)

    assert status == "REVIEW"
    assert nothing_verified is True


def test_the_worst_document_wins():
    assert _status_for([internal("a.jpg"),
                        internal("b.jpg", verification="FAILED")], 0
                       )[0] == "FAILED"


# ==========================================================================
# E. A DECLARED PARTY WITH NOTHING UPLOADED
# ==========================================================================


def test_a_party_with_no_documents_is_never_a_success():
    """
    THE GUARD. Empty documents and an absent KYC both rank as harmless,
    so the arithmetic alone returned SUCCESS for a party about whom
    nothing is known.
    """
    status, nothing_verified = _status_for([], 0)

    assert status != "SUCCESS"
    assert status == "REVIEW"
    # Not "nothing verified" -- there was nothing to verify. The case
    # owes the caller an error for the former and not for the latter.
    assert nothing_verified is False


def test_a_declared_co_applicant_who_sent_nothing_reports_review():
    public = _public_envelope(envelope(
        [internal()], co_applicant_id="COAPP-9",
        party_status={"APP-1": "SUCCESS", "COAPP-9": "REVIEW"}))

    assert public["co_applicant"]["document_ids"] == []
    assert public["co_applicant"]["status"] == "REVIEW"


# ==========================================================================
# A-D. EACH PARTY REFLECTS THEIR OWN STATE
# ==========================================================================


@pytest.fixture
def two_party():
    def build(primary_status, co_status, primary_docs=None, co_docs=None):
        documents = (primary_docs or [internal("pan.jpg", "APP-1")]) + \
                    (co_docs or [internal("pan.jpg", "COAPP-9")])
        return _public_envelope(envelope(
            documents, co_applicant_id="COAPP-9",
            party_status={"APP-1": primary_status, "COAPP-9": co_status}))
    return build


def test_a_clean_primary_reports_clean(two_party):
    assert two_party("SUCCESS", "SUCCESS")["primary_applicant"][
        "status"] == "SUCCESS"


def test_a_clean_co_applicant_reports_clean(two_party):
    assert two_party("SUCCESS", "SUCCESS")["co_applicant"][
        "status"] == "SUCCESS"


def test_a_primary_review_does_not_colour_the_co_applicant(two_party):
    public = two_party("REVIEW", "SUCCESS")

    assert public["primary_applicant"]["status"] == "REVIEW"
    assert public["co_applicant"]["status"] == "SUCCESS"


def test_a_co_applicant_review_does_not_colour_the_primary(two_party):
    public = two_party("SUCCESS", "REVIEW")

    assert public["primary_applicant"]["status"] == "SUCCESS"
    assert public["co_applicant"]["status"] == "REVIEW"


def test_a_type_mismatch_stays_with_the_party_who_sent_it():
    """
    F. The wrong file is one person's mistake, and the other party's
    status must not carry it.
    """
    mismatch = internal("wrong.jpg", "APP-1", verification="FAILED",
                        reason_codes=["DOCUMENT_TYPE_MISMATCH"])
    clean = internal("pan.jpg", "COAPP-9")

    primary = _status_for(
        parties.owned_by([mismatch, clean], "APP-1", is_primary=True), 0)[0]
    co = _status_for(
        parties.owned_by([mismatch, clean], "COAPP-9", is_primary=False), 0)[0]

    assert primary != "SUCCESS"
    assert co == "SUCCESS"


def test_each_partys_kyc_rank_is_their_own():
    review = {"party_id": "APP-1", "ran": True, "status": "REVIEW"}
    clean = {"party_id": "COAPP-9", "ran": True, "status": "PASS"}
    absent = {"party_id": "X", "ran": False, "status": "SKIPPED"}

    assert _kyc_rank_of(review) == 1
    assert _kyc_rank_of(clean) == 0
    assert _kyc_rank_of(absent) == 0
    assert _kyc_rank_of(None) == 0


# ==========================================================================
# K/L. NO PARTY-LEVEL DECISION, NO PARTY-LEVEL ACTION
# ==========================================================================


def test_a_section_carries_no_decision(two_party):
    for section in ("primary_applicant", "co_applicant"):
        assert "decision" not in two_party("SUCCESS", "SUCCESS")[section]


def test_a_section_carries_no_next_action(two_party):
    """
    L. A party-level CONTINUE beside a case-level REJECT would read as
    permission to proceed. There is ONE decision on a loan.
    """
    for section in ("primary_applicant", "co_applicant"):
        assert "next_action" not in two_party("SUCCESS", "SUCCESS")[section]


def test_a_clean_party_on_a_rejected_case_exposes_no_action():
    """
    THE EXACT SHAPE THAT WOULD MISLEAD: the case is rejected on the
    co-applicant's documents, and the primary applicant's own are
    spotless.
    """
    public = _public_envelope(envelope(
        [internal("pan.jpg", "APP-1"),
         internal("bad.jpg", "COAPP-9", verification="FAILED")],
        co_applicant_id="COAPP-9", status="REJECTED",
        party_status={"APP-1": "SUCCESS", "COAPP-9": "FAILED"}))

    assert public["decision"] == "REJECT"
    assert public["primary_applicant"]["status"] == "SUCCESS"
    for key in ("decision", "next_action", "CONTINUE"):
        assert key not in public["primary_applicant"]


def test_the_section_keys_are_a_closed_set(two_party):
    for section in ("primary_applicant", "co_applicant"):
        assert set(two_party("SUCCESS", "SUCCESS")[section]) <= {
            "party_id", "role", "status", "document_ids",
            "verification_summary", "profile_match", "kyc"}


# ==========================================================================
# J. THE CASE LEVEL IS UNCHANGED
# ==========================================================================


def test_the_case_keeps_its_own_status_decision_and_action(two_party):
    public = two_party("SUCCESS", "SUCCESS")

    for key in ("status", "decision", "next_action"):
        assert key in public


def test_a_single_applicant_case_gains_only_a_status(two_party):
    public = _public_envelope(envelope(
        [internal()], party_status={"APP-1": "SUCCESS"}))

    assert public["primary_applicant"]["status"] == "SUCCESS"
    assert "co_applicant" not in public
    for key in ("request_id", "applicant_id", "case_id", "status",
                "documents", "cross_document", "decision", "next_action",
                "summary", "summary_source", "processing_ms", "errors"):
        assert key in public


def test_a_party_status_is_absent_when_the_flow_did_not_compute_one():
    """Nothing is invented at the response boundary."""
    public = _public_envelope(envelope([internal()]))

    assert "status" not in public["primary_applicant"]


# ==========================================================================
# 3. THE ONE CANONICAL OWNERSHIP FILTER
# ==========================================================================


def test_a_stamped_document_goes_to_its_own_party():
    documents = [internal("a.jpg", "APP-1"), internal("b.jpg", "COAPP-9")]

    assert [d["source_id"] for d in
            parties.owned_by(documents, "APP-1", is_primary=True)] == ["a.jpg"]
    assert [d["source_id"] for d in
            parties.owned_by(documents, "COAPP-9", is_primary=False)] == ["b.jpg"]


def test_an_unstamped_legacy_document_goes_to_the_primary():
    legacy = internal("old.jpg", party_id=None)

    assert parties.owned_by([legacy], "APP-1", is_primary=True) == [legacy]


def test_an_unstamped_legacy_document_never_goes_to_the_co_applicant():
    """
    G. Offering the adoption to both parties would hand one person's
    document to another — the whole failure the party model prevents.
    """
    legacy = internal("old.jpg", party_id=None)

    assert parties.owned_by([legacy], "COAPP-9", is_primary=False) == []


def test_asking_for_no_party_returns_nothing():
    assert parties.owned_by([internal()], "", is_primary=True) == []
    assert parties.owned_by([internal()], "   ", is_primary=True) == []


def test_the_legacy_adoption_cannot_be_acquired_by_forgetting():
    """
    `is_primary` is keyword-only with NO default, so a call site cannot
    inherit the adoption silently. That is the property that stopped the
    two filters drifting apart.
    """
    import inspect

    signature = inspect.signature(parties.owned_by)
    parameter = signature.parameters["is_primary"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty

    with pytest.raises(TypeError):
        parties.owned_by([], "APP-1")


# ==========================================================================
# H. ONE FILTER, EVERY CONSUMER
# ==========================================================================


def test_there_is_only_one_ownership_filter():
    """
    The duplicates are gone, not merely aligned. Two functions that
    agree today are two functions that can disagree tomorrow.
    """
    from app.agents.los import profile_match, response

    assert not hasattr(response, "documents_of")
    assert not hasattr(profile_match, "documents_for_party")


def test_profile_matching_kyc_and_the_sections_all_call_it():
    import ast
    import inspect

    from app.agents.los import flow

    for function in (flow._match_profiles, flow._party_sections):
        tree = ast.parse(inspect.getsource(function))
        called = {node.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute)}
        assert "owned_by" in called, function.__name__

    # KYC scopes its parties in `process_application`, where the split
    # is built once and reused for both KYC and the party statuses.
    assert "parties.owned_by" in inspect.getsource(flow.process_application)


def test_every_consumer_answers_ownership_identically():
    """
    I. The property the single filter buys: whatever KYC considers a
    party's document, profile matching considers theirs too.
    """
    documents = [internal("a.jpg", "APP-1"), internal("b.jpg", "COAPP-9"),
                 internal("legacy.jpg", party_id=None)]

    primary = parties.owned_by(documents, "APP-1", is_primary=True)
    co = parties.owned_by(documents, "COAPP-9", is_primary=False)

    assert {d["source_id"] for d in primary} == {"a.jpg", "legacy.jpg"}
    assert {d["source_id"] for d in co} == {"b.jpg"}
    assert not {id(d) for d in primary} & {id(d) for d in co}
