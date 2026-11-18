"""The one dedupe status (app/agents/dedupe/dedupe_status.py): DUPLICATE_FOUND / POSSIBLE_DUPLICATE /
NO_DUPLICATE_FOUND / CHECK_INCOMPLETE, derived only from the assessment. Includes the invariants: an incomplete search
is never NO_DUPLICATE_FOUND, an existing customer alone is never a duplicate, an unsigned policy never decides
DUPLICATE_FOUND, and a missing identifier is never a matched one. Synthetic data only."""
from __future__ import annotations

import itertools

import pytest

from app.agents.dedupe import dedupe_status as D

POLICY = {"policy_id": "DEDUPE", "policy_version": "0.2.0-UNSIGNED", "signed_off": False}
SIGNED = {**POLICY, "signed_off": True}


def _result(match="NO_MATCH", loans=(), search="NO_MATCH", matches=(), case="ASSESSED", scenarios=None,
            identifier=None, parameter="PAN"):
    return {"searched_parameter": parameter,
            "history": {"customer": {"match": match, "customer_ref": "AP******01", "searched_identifier_matches": identifier,
                                     "role_in_source": "APPLICANT"},
                        "loans": [{"prospect_ref": f"PR******{i}", "effective_status": "ACTIVE"} for i in range(len(loans))]},
            "los_dedupe": {"search": {"status": search, "matches": list(matches)},
                           "case": {"status": case, "scenarios": [{"scenario": k, "status": v}
                                                                   for k, v in (scenarios or {}).items()]}}}


def test_a_new_customer_with_every_search_run_is_no_duplicate():
    d = D.derive(_result(), POLICY)
    assert (d["status"], d["search_complete"], d["recommended_action"]) == ("NO_DUPLICATE_FOUND", True, "CONTINUE")
    assert not d["customer_match"] and not d["existing_loan_match"] and d["matched_records"] == []


def test_an_existing_customer_with_loans_is_not_a_duplicate_application():
    d = D.derive(_result(match="CONFIRMED_MATCH", loans=(1, 2), identifier=True), POLICY)
    assert d["status"] == "NO_DUPLICATE_FOUND" and d["customer_match"] and d["existing_loan_match"]
    assert [r["record_type"] for r in d["matched_records"]] == ["EXISTING_CUSTOMER", "EXISTING_LOAN", "EXISTING_LOAN"]
    assert "PAN_MATCHES_EXISTING_CUSTOMER" in d["match_reasons"] and "EXISTING_LOANS_ON_RECORD" in d["match_reasons"]


@pytest.mark.parametrize("source", ["UNAVAILABLE", "SOURCE_ERROR", "MALFORMED"])
def test_a_source_that_did_not_answer_is_check_incomplete_never_negative(source):
    d = D.derive(_result(match=source), POLICY)
    assert d["status"] == "CHECK_INCOMPLETE" and d["search_complete"] is False
    assert d["searches_missing"] == [f"MIFIN_{source}"] and d["recommended_action"] == "MANUAL_REVIEW"


def test_a_failed_los_search_or_case_assessment_is_check_incomplete():
    assert D.derive(_result(search="FAILED"), POLICY)["status"] == "CHECK_INCOMPLETE"
    assert D.derive(_result(case="FAILED"), POLICY)["status"] == "CHECK_INCOMPLETE"


@pytest.mark.parametrize("kwargs, reason", [
    ({"match": "POTENTIAL_MATCH"}, "POSSIBLE_MATCH_NOT_CONFIRMED"),
    ({"match": "AMBIGUOUS"}, "MORE_THAN_ONE_CUSTOMER_MATCHED"),
    ({"match": "CONFIRMED_MATCH", "identifier": False}, "IDENTIFIER_DIFFERS_ON_MIFIN_RECORD"),
    ({"matches": [{"case_id": "REDACTED", "role": "CO_APPLICANT", "matched_fields": ["MOBILE"]}]},
     "IDENTIFIER_ON_OTHER_LOS_CASES"),
    ({"scenarios": {"S09_ROLE_CHANGE": "DETECTED"}}, "ROLE_CHANGE_ON_ANOTHER_CASE"),
    ({"scenarios": {"S11_IDENTITY_CONFLICT": "DETECTED"}}, "PAN_ON_ANOTHER_CUSTOMER_RECORD"),
    ({"scenarios": {"S12_DOCUMENT_REUSE": "DETECTED"}}, "DOCUMENT_REUSED_ACROSS_CUSTOMERS"),
    ({"scenarios": {"S10_SIMULTANEOUS_APPLICATION": "DETECTED"}}, "SAME_PRODUCT_APPLICATION_EXISTS"),
])
def test_review_evidence_is_a_possible_duplicate_with_its_reason(kwargs, reason):
    d = D.derive(_result(**kwargs), POLICY)
    assert d["status"] == "POSSIBLE_DUPLICATE" and reason in d["match_reasons"]
    assert d["recommended_action"] == "MANUAL_REVIEW"


def test_a_duplicate_application_is_decided_only_under_a_signed_off_policy():
    evidence = _result(scenarios={"S10_SIMULTANEOUS_APPLICATION": "DETECTED"})
    assert D.derive(evidence, POLICY)["status"] == "POSSIBLE_DUPLICATE"
    signed = D.derive(evidence, SIGNED)
    assert signed["status"] == "DUPLICATE_FOUND" and signed["application_match"] is True


def test_other_cases_are_referenced_safely_with_their_role():
    d = D.derive(_result(matches=[{"case_id": "REDACTED", "role": "PRIMARY_APPLICANT", "matched_fields": ["PAN"]}]),
                 POLICY)
    rec = next(r for r in d["matched_records"] if r["record_type"] == "LOS_APPLICATION")
    assert rec == {"record_ref": "REDACTED", "record_type": "LOS_APPLICATION", "source": "LOS",
                   "matched_fields": ["PAN"], "role": "PRIMARY_APPLICANT"}


def test_actions_come_from_config():
    policy = {**POLICY, "decision": {"actions": {"POSSIBLE_DUPLICATE": "SEND_TO_RCU"}}}
    assert D.derive(_result(match="POTENTIAL_MATCH"), policy)["recommended_action"] == "SEND_TO_RCU"


# ---- invariants over every combination of the inputs ----------------------------------------------------------
MATCHES = ["CONFIRMED_MATCH", "POTENTIAL_MATCH", "NO_MATCH", "AMBIGUOUS", "UNAVAILABLE", "SOURCE_ERROR", "MALFORMED"]
SEARCHES = ["MATCH", "POSSIBLE_MATCH", "NO_MATCH", "INSUFFICIENT_DATA", "FAILED"]
CASES = ["ASSESSED", "INSUFFICIENT_DATA", "FAILED"]
SCEN = [None, {"S10_SIMULTANEOUS_APPLICATION": "DETECTED"}, {"S12_DOCUMENT_REUSE": "DETECTED"}]


@pytest.mark.parametrize("match, search, case, scen, identifier, policy",
                         list(itertools.product(MATCHES, SEARCHES, CASES, SCEN, [None, True, False], [POLICY, SIGNED])))
def test_invariants(match, search, case, scen, identifier, policy):
    d = D.derive(_result(match=match, search=search, case=case, scenarios=scen, identifier=identifier,
                         loans=(1,) if match == "CONFIRMED_MATCH" else ()), policy)
    assert d["status"] in D.STATUSES
    incomplete = match in ("UNAVAILABLE", "SOURCE_ERROR", "MALFORMED") or search == "FAILED" or case == "FAILED"
    assert (d["status"] == "CHECK_INCOMPLETE") == incomplete              # incomplete is never a negative
    assert d["search_complete"] is (not incomplete)
    if d["status"] == "DUPLICATE_FOUND":
        assert policy["signed_off"] and d["application_match"]             # only an approved rule decides
    if d["status"] == "NO_DUPLICATE_FOUND":
        assert not d["application_match"] and identifier is not False      # no unreviewed evidence was dropped
    if match != "CONFIRMED_MATCH":
        assert not d["customer_match"] and not d["existing_loan_match"]    # a non-match is never a matched record


# ---- the workflow recommendation and the assessment agree with the status (2026-10-22) -------------------------
CFG = {**POLICY, "decision": {"workflow": {"NO_DUPLICATE_FOUND": "PROCEED_TO_NEXT_STAGE", "POSSIBLE_DUPLICATE":
                                           "HOLD_FOR_REVIEW", "CHECK_INCOMPLETE": "HOLD_FOR_REVIEW",
                                           "DUPLICATE_FOUND": None},
                              "questions": {"DUPLICATE_FOUND": "block or hold?"}}}


def _party_row(status, role="CO_APPLICANT", reasons=()):
    return {"role": role, "status": status, "match_reasons": list(reasons), "existing_customer": False}


@pytest.mark.parametrize("match, search, case, scen, party", list(itertools.product(
    MATCHES, SEARCHES, CASES, SCEN, [None, "NO_DUPLICATE_FOUND", "POSSIBLE_DUPLICATE", "CHECK_INCOMPLETE"])))
def test_recommendation_and_assessment_invariants(match, search, case, scen, party):
    parties = [_party_row(party, reasons=["NO_PAN_OR_MOBILE_ON_RECORD"] if party == "CHECK_INCOMPLETE" else [])] \
        if party else []
    for policy in (CFG, {**CFG, "signed_off": True}):
        d = D.derive(_result(match=match, search=search, case=case, scenarios=scen), policy, parties)
        rec = d["workflow_recommendation"]["recommendation"]
        assert rec in D.RECOMMENDATIONS
        assert d["assessment"]["credit_decision"] == "NOT_DECIDED_BY_DEDUPE"      # never a credit approval
        assert d["assessment"]["search_complete"] is d["search_complete"]
        if not d["search_complete"]:
            assert d["status"] == "CHECK_INCOMPLETE" and rec == "HOLD_FOR_REVIEW"
        if rec == "PROCEED_TO_NEXT_STAGE":
            assert d["status"] == "NO_DUPLICATE_FOUND" and d["search_complete"]
        if d["status"] == "DUPLICATE_FOUND":
            assert rec == "UNABLE_TO_DECIDE" and d["workflow_recommendation"]["policy_question"]
        if party == "POSSIBLE_DUPLICATE" and d["search_complete"]:
            assert d["status"] in ("POSSIBLE_DUPLICATE", "DUPLICATE_FOUND")     # a party's evidence is never dropped


def test_an_unmapped_status_is_unable_to_decide_never_an_invented_action():
    assert D.recommend("NO_DUPLICATE_FOUND", {})["recommendation"] == "UNABLE_TO_DECIDE"
    assert D.recommend("POSSIBLE_DUPLICATE", {"decision": {"workflow": {"POSSIBLE_DUPLICATE": "APPROVE_LOAN"}}})[
        "recommendation"] == "UNABLE_TO_DECIDE"                                # not a known recommendation


# ---- the trigger: a confirmed NEW customer does not invoke the agent; nothing unproven is ever NEW (2026-10-22) ---
TRIG = {**CFG, "trigger": {"invoke_for_new": False, "new_customer_recommendation": "PROCEED_TO_NEXT_STAGE"}}


@pytest.mark.parametrize("match, search, identifier, expected", [
    ("NO_MATCH", "NO_MATCH", None, "NEW"),
    ("NO_MATCH", "INSUFFICIENT_DATA", None, "NEW"),                 # AP / PR code: the LOS does not store it
    ("NO_MATCH", "MATCH", None, "EXISTING"),                       # the LOS holds it on another case
    ("NO_MATCH", "POSSIBLE_MATCH", None, "EXISTING"),
    ("CONFIRMED_MATCH", "NO_MATCH", True, "EXISTING"),
    ("POTENTIAL_MATCH", "NO_MATCH", None, "POSSIBLE_EXISTING"),
    ("AMBIGUOUS", "NO_MATCH", None, "CONFLICTING"),
    ("CONFIRMED_MATCH", "NO_MATCH", False, "CONFLICTING"),         # MIFIN's record holds another identifier
    ("UNAVAILABLE", "NO_MATCH", None, "LOOKUP_FAILED"),
    ("SOURCE_ERROR", "NO_MATCH", None, "LOOKUP_FAILED"),
    ("MALFORMED", "NO_MATCH", None, "LOOKUP_FAILED"),
    ("NO_MATCH", "FAILED", None, "LOOKUP_FAILED"),                 # MIFIN said no, but the LOS search did not run
])
def test_the_customer_lookup_decides_the_trigger(match, search, identifier, expected):
    t = D.trigger(_result(match=match, search=search, identifier=identifier), TRIG)
    assert t["customer_lookup"] == expected
    assert t["dedupe_invoked"] is (expected != "NEW")


@pytest.mark.parametrize("match, search", list(itertools.product(MATCHES, SEARCHES)))
def test_nothing_unproven_is_ever_new(match, search):
    t = D.trigger(_result(match=match, search=search), TRIG)
    if t["customer_lookup"] == "NEW":
        assert match == "NO_MATCH" and search in ("NO_MATCH", "INSUFFICIENT_DATA")


def test_a_new_customer_answer_claims_no_dedupe_and_proceeds_through_normal_checks():
    trig = D.trigger(_result(), TRIG)
    d = D.not_invoked(_result(), {**TRIG, "decision": {**CFG["decision"], "texts": {"next_actions": {
        "PROCEED_TO_NEXT_STAGE": "normal LOS checks; not a loan approval"}}}}, trig)
    assert d["invoked"] is False and d["status"] is None and d["trigger"]["customer_lookup"] == "NEW"
    assert d["workflow_recommendation"]["recommendation"] == "PROCEED_TO_NEXT_STAGE"
    assert d["assessment"]["duplicate_application"] == "NOT_EVALUATED"
    assert d["assessment"]["credit_decision"] == "NOT_DECIDED_BY_DEDUPE"
    # no configured recommendation for a new customer: unable to decide, with the question
    bare = D.not_invoked(_result(), {"trigger": {}}, trig)
    assert bare["workflow_recommendation"]["recommendation"] == "UNABLE_TO_DECIDE"
    assert bare["workflow_recommendation"]["policy_question"]
