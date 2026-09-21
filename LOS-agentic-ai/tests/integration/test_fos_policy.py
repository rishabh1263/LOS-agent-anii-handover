"""
The policy engine, through the public API.

WHAT THIS SUITE IS FOR that the unit tests are not. `test_policy_engine.py`
builds its own policy fixture and checks the resolver in isolation. These
tests go through the real FastAPI routes against the shipped configuration,
and they answer a different question: does a field officer holding the API
response actually get told what to collect and WHY?

THE THREE THINGS BEING PROTECTED.

First, that a requirement can always be traced. Every checklist row names
the rule that produced it and the response names the policy version those
rules came from. A requirement an officer cannot explain to a customer is a
requirement they will apologise for instead of collecting.

Second, that an unknown is shown as an unknown. A case with no loan amount
gets the base checklist and an explicit statement that amount-based rules
could not be applied -- not a short list that looks complete.

Third, that the placeholder thresholds in the shipped policy file stay
visibly placeholder all the way to the response. They are marked
UNCONFIRMED in configuration and that marking has to survive the trip.
"""

from __future__ import annotations

import pytest

from tests.integration.test_fos_api import client, fos_token  # noqa: F401


def open_case(client, **application):
    """A case, opened through the real route."""
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Policy Test", "mobile": "9876500123",
                      "date_of_birth": "1990-04-12", "address": "Mumbai"},
        "application": {"product": "PERSONAL_LOAN", **application},
    })
    assert response.status_code == 201, response.text
    return response.json()


def rows(body):
    return {e["slot"]: e for e in body["checklist"]}


# ==========================================================================
# A. EVERY REQUIREMENT IS TRACEABLE
# ==========================================================================


def test_every_checklist_row_names_the_rule_behind_it(client):
    body = open_case(client, loan_amount=500000)

    assert body["checklist"]
    for entry in body["checklist"]:
        assert entry["rule_ids"], f"{entry['slot']} names no rule"
        for rule_id in entry["rule_ids"]:
            assert rule_id in body["policy"]["applied_rules"]


def test_the_response_names_the_policy_version(client):
    body = open_case(client, loan_amount=500000)
    policy = body["policy"]

    assert policy["policy_id"]
    assert policy["policy_version"]
    assert policy["source"].endswith(".yaml")


def test_a_requirement_carries_a_reason_a_person_can_read(client):
    body = open_case(client, loan_amount=500000)
    pan = rows(body)["PAN"]

    assert len(pan["reason"]) > 20
    for internal in ("Traceback", ".py", "None", "slot="):
        assert internal not in pan["reason"]


def test_every_row_says_how_strongly_it_is_required(client):
    body = open_case(client, loan_amount=500000)

    for entry in body["checklist"]:
        assert entry["requirement"] in {
            "REQUIRED", "CONDITIONAL", "OPTIONAL", "NOT_APPLICABLE"}
        assert entry["fulfilment"] in {
            "SATISFIED", "MISSING", "UNDER_REVIEW", "FAILED", "IN_PROGRESS"}


def test_requirement_and_fulfilment_are_separate_answers(client):
    """
    A row is both "required" and "still missing" at once. One field cannot
    say both, and a UI that has to infer one from the other renders a
    required-and-missing slot the same as an optional one.
    """
    body = open_case(client, loan_amount=500000)
    pan = rows(body)["PAN"]

    assert pan["requirement"] == "REQUIRED"
    assert pan["fulfilment"] == "MISSING"


# ==========================================================================
# B. THE AMOUNT CHANGES THE CHECKLIST
# ==========================================================================


def test_a_larger_loan_can_require_more_than_a_smaller_one(client):
    """
    Not a claim about any particular threshold -- the shipped bands are
    placeholders. The claim is that the engine is WIRED: the amount reaches
    it and the checklist responds.
    """
    small = open_case(client, loan_amount=100000)
    large = open_case(client, loan_amount=5000000)

    assert set(rows(small)) <= set(rows(large))
    assert len(rows(large)) > len(rows(small)), (
        "the shipped policy has no band that adds a document, so this test "
        "is not exercising anything"
    )


def test_the_base_documents_survive_every_amount(client):
    for amount in (0, 100000, 5000000, 100000000):
        present = rows(open_case(client, loan_amount=amount))
        assert present["PAN"]["mandatory"] is True
        assert present["ADDRESS_PROOF"]["mandatory"] is True


def test_the_band_that_applied_is_named(client):
    small = open_case(client, loan_amount=100000)["policy"]["applied_rules"]
    large = open_case(client, loan_amount=5000000)["policy"]["applied_rules"]

    assert small != large


# ==========================================================================
# C. WHAT THE CASE HAS NOT CAPTURED
# ==========================================================================


def test_no_loan_amount_produces_a_provisional_checklist_that_says_so(client):
    body = open_case(client)

    gaps = {u["rule_id"]: u for u in body["policy"]["unevaluated_rules"]}
    assert "AMOUNT_BANDS" in gaps
    assert gaps["AMOUNT_BANDS"]["missing_attributes"] == ["loan_amount"]
    assert gaps["AMOUNT_BANDS"]["reason"]


def test_no_loan_amount_still_returns_the_base_requirements(client):
    """A case with no amount is not a case with no requirements."""
    body = open_case(client)

    assert "PAN" in rows(body)
    assert "ADDRESS_PROOF" in rows(body)
    assert body["required_documents"]


def test_an_uncaptured_amount_does_not_silently_pick_a_band(client):
    none_given = open_case(client)
    lowest = open_case(client, loan_amount=0)

    assert (none_given["policy"]["applied_rules"]
            != lowest["policy"]["applied_rules"])


def test_an_uncaptured_attribute_is_reported_not_guessed(client):
    body = open_case(client, loan_amount=500000)

    gaps = {u["rule_id"] for u in body["policy"]["unevaluated_rules"]}
    assert gaps, "no rule keys on an attribute, so this test proves nothing"
    for gap in body["policy"]["unevaluated_rules"]:
        assert gap["missing_attributes"]
        assert gap["would_require"] or gap["reason"]


def test_capturing_the_attribute_resolves_the_gap(client):
    """The gap is a prompt, and acting on it has to close it."""
    before = open_case(client, loan_amount=500000)
    after = open_case(client, loan_amount=500000,
                      employment_type="SELF_EMPLOYED")

    unresolved = {u["rule_id"] for u in before["policy"]["unevaluated_rules"]}
    resolved = {u["rule_id"] for u in after["policy"]["unevaluated_rules"]}
    assert unresolved - resolved, "capturing the attribute changed nothing"


def test_a_fired_condition_says_what_made_it_apply(client):
    body = open_case(client, loan_amount=500000,
                     employment_type="SELF_EMPLOYED")

    conditional = [e for e in body["checklist"]
                   if e["requirement"] == "CONDITIONAL"]
    assert conditional, "no conditional rule fired, so nothing is tested"
    for entry in conditional:
        assert entry["applicable_conditions"]
        assert any("employment_type" in c
                   for c in entry["applicable_conditions"])


def test_a_conditional_requirement_still_blocks_the_handoff(client):
    """
    CONDITIONAL describes why it applies, not whether it counts. Once the
    condition has matched the document is as required as any other, and a
    readiness check that skipped it would hand over an incomplete case.
    """
    body = open_case(client, loan_amount=500000,
                     employment_type="SELF_EMPLOYED")

    conditional = [e for e in body["checklist"]
                   if e["requirement"] == "CONDITIONAL"]
    assert conditional
    for entry in conditional:
        assert entry["mandatory"] is True
        assert entry["slot"] in body["required_documents"]


# ==========================================================================
# D. PLACEHOLDER THRESHOLDS STAY VISIBLY PLACEHOLDER
# ==========================================================================


def test_the_response_says_the_shipped_thresholds_are_unconfirmed(client):
    """
    The demo bands must not reach a screen looking like signed-off policy.
    If somebody replaces them with a lender's real matrix they flip the
    status in the file, and this assertion is what makes that deliberate.
    """
    body = open_case(client, loan_amount=500000)

    assert body["policy"]["status"] == "UNCONFIRMED"
    assert all(e["policy_status"] == "UNCONFIRMED"
               for e in body["checklist"])


def test_a_row_publishes_what_must_be_readable_on_the_document(client):
    """
    PUBLISHED, NOT ENFORCED HERE. Verification decides whether a document
    is any good. What this adds is that an officer can be told what a
    bank statement has to show BEFORE they collect one, rather than
    finding out from a rejection afterwards.
    """
    body = open_case(client, loan_amount=500000)
    statement = rows(body)["BANK_STATEMENT"]

    requirements = statement["content_requirements"]["BANK_STATEMENT"]
    assert requirements["required_fields"]
    assert requirements["verification_required"] is True


def test_a_placeholder_content_threshold_stays_marked(client):
    body = open_case(client, loan_amount=500000)
    requirements = (rows(body)["BANK_STATEMENT"]["content_requirements"]
                    ["BANK_STATEMENT"])

    assert requirements["min_months_status"] == "UNCONFIRMED"


def test_a_multi_type_slot_publishes_requirements_per_type(client):
    """
    ADDRESS_PROOF accepts three documents and they do not carry the same
    fields. One merged list would tell an officer a passport needs a
    licence number.
    """
    body = open_case(client, loan_amount=500000)
    published = rows(body)["ADDRESS_PROOF"]["content_requirements"]

    assert set(published) == {"DRIVING_LICENCE", "PASSPORT", "VOTER_ID"}
    assert (published["PASSPORT"]["required_fields"]
            != published["VOTER_ID"]["required_fields"])


# ==========================================================================
# E. THE VERSION IS PINNED TO THE CASE
# ==========================================================================


def test_the_case_records_the_policy_version_it_was_opened_under(client):
    body = open_case(client, loan_amount=500000)

    assert body["policy"]["pinned_version"] == body["policy"]["policy_version"]
    assert body["policy"]["pinned_at"]
    assert body["policy"]["version_changed"] is False


def test_the_pin_survives_a_later_read(client):
    opened = open_case(client, loan_amount=500000)
    later = client.post("/api/v1/fos/copilot", json={
        "applicant_id": opened["applicant_id"],
        "case_id": opened["case_id"],
        "action": "GET_DOCUMENT_CHECKLIST",
    }).json()

    assert later["policy"]["pinned_version"] == opened["policy"]["pinned_version"]
    assert later["policy"]["pinned_at"] == opened["policy"]["pinned_at"]


def test_a_policy_edit_is_reported_rather_than_applied_silently(
        client, monkeypatch):
    """
    THE HONEST PART OF VERSIONING, and the limitation it admits.

    This service resolves against the CURRENT policy file; it does not keep
    superseded versions and cannot re-run one. So when the file moves under
    a case that was opened earlier, the response must not print the pinned
    version next to a checklist that was built from a different one. It
    reports both and says they differ.
    """
    from app.agents.policy import loader

    opened = open_case(client, loan_amount=500000)
    pinned = opened["policy"]["pinned_version"]

    policy = dict(loader.policy_for("PERSONAL_LOAN") or {})
    policy["policy_version"] = "99.99.99-TEST"
    monkeypatch.setattr(loader, "policy_for",
                        lambda product: policy
                        if str(product or "").upper() == "PERSONAL_LOAN"
                        else None)

    later = client.post("/api/v1/fos/copilot", json={
        "applicant_id": opened["applicant_id"],
        "case_id": opened["case_id"],
        "action": "GET_DOCUMENT_CHECKLIST",
    }).json()

    assert later["policy"]["pinned_version"] == pinned
    assert later["policy"]["policy_version"] == "99.99.99-TEST"
    assert later["policy"]["version_changed"] is True
    assert pinned in later["policy"]["note"]
    assert "99.99.99-TEST" in later["policy"]["note"]


# ==========================================================================
# F. THE POLICY BLOCK TRAVELS WITH THE CHECKLIST AND NOWHERE ELSE
# ==========================================================================


def test_an_answer_about_documents_carries_the_policy_behind_them(client):
    opened = open_case(client, loan_amount=500000)

    body = client.post("/api/v1/fos/copilot", json={
        "applicant_id": opened["applicant_id"],
        "case_id": opened["case_id"],
        "action": "CUSTOM_QUERY",
        "message": "Which documents are required?",
    }).json()

    assert body.get("checklist")
    assert body.get("policy"), (
        "a checklist with no policy block is a requirement nobody can trace"
    )


def test_an_answer_about_something_else_does_not_carry_it(client):
    """
    A policy block on an answer with no checklist is noise a chat client
    throws away.
    """
    opened = open_case(client, loan_amount=500000)

    body = client.post("/api/v1/fos/copilot", json={
        "applicant_id": opened["applicant_id"],
        "case_id": opened["case_id"],
        "action": "CUSTOM_QUERY",
        "message": "What are the applicant's details?",
    }).json()

    assert "checklist" not in body
    assert "policy" not in body


# ==========================================================================
# G. A PRODUCT WITH NO POLICY FILE STILL WORKS
# ==========================================================================


def test_a_product_with_no_policy_file_falls_back_and_says_so(client):
    """
    Nothing may break because a lender has not written a matrix yet. The
    checklist still comes back; it simply does not claim a version.
    """
    from app.agents.policy import engine as policy

    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "No Policy"},
        "application": {"product": "HOME_LOAN", "loan_amount": 5000000},
    })
    body = response.json()

    assert response.status_code == 201
    assert body["checklist"]
    assert body["policy"]["policy_id"] == policy.LEGACY_POLICY_ID
    assert body["policy"]["policy_version"] == policy.LEGACY_VERSION
    assert body["policy"]["status"] == "UNVERSIONED"


def test_a_case_with_no_product_still_has_a_traceable_checklist(client):
    response = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "No Product"},
    })
    body = response.json()

    assert body["checklist"]
    assert body["policy"]["policy_id"]
