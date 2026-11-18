"""Loan history = MIFIN + LMS, verified facts only (app/agents/dedupe/loan_history.py). Demo / synthetic data only."""

from __future__ import annotations

import json

import pytest

from app.agents.dedupe import demo_sources
from app.agents.dedupe import lms as L
from app.agents.dedupe import loan_history as H
from app.agents.dedupe import mifin as M


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setenv("MIFIN_PROVIDER", "demo")
    monkeypatch.setenv("LMS_PROVIDER", "demo")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    demo_sources.reload()


class DownLms:
    def loan(self, code):
        raise L.LmsUnavailable("LMS_TIMEOUT")


class OneShape:
    """A MIFIN that answers one fixed body (shape tests)."""

    def __init__(self, body):
        self.body = body

    def lookup(self, parameter, value):
        return self.body


def test_existing_customer_with_live_closed_and_cancelled_loans():
    h = H.history("MOBILE", "98765 02222")
    # the cancelled application is final from MIFIN (never disbursed); the live and closed ones come from the LMS
    assert (h["customer"]["match"], h["history_status"], h["loan_count"], h["is_demo"]) == (
        "CONFIRMED_MATCH", "COMPLETE", 3, True)
    cancelled = next(x for x in h["loans"] if x["effective_status"] == "CANCELED")
    assert (cancelled["lms_status"], cancelled["resolved"]) == ("NOT_FOUND", True)
    a = h["aggregates"]
    assert a["by_status"] == {"ACTIVE": 1, "CLOSED": 1, "CANCELED": 1}
    assert (a["active_exposure"]["status"], a["active_exposure"]["value"]) == ("COMPLETE", "312400")
    assert (a["monthly_emi_burden"]["status"], a["monthly_emi_burden"]["value"]) == ("COMPLETE", "11200")
    assert a["worst_dpd_12m"]["value"] == 0
    live = next(x for x in h["loans"] if x["effective_status"] == "ACTIVE")
    assert (live["status_source"], live["source_status"]) == ("LMS", "DISBURSED")   # MIFIN's word kept beside it


def test_cancelled_applications_are_never_exposure():
    h = H.history("PAN", "ABCPK4321L")
    assert h["aggregates"]["by_status"] == {"CANCELED": 3}
    assert h["aggregates"]["active_exposure"]["status"] == "NOT_APPLICABLE"
    assert h["aggregates"]["active_exposure"]["value"] is None


def test_strong_match_with_an_empty_loan_list_is_no_previous_loans():
    h = H.history("AP_CODE", "AP00752673")
    assert (h["history_status"], h["loan_count"]) == ("NO_PREVIOUS_LOANS", 0)


def test_a_missing_loan_section_is_unknown_not_no_loans():
    body = {"RESPONSE": {"STATUS": "S", "APPLICANTDETAILS": {"CUSTID": "AP00799001", "STRONG_MATCH": "Y"}}}
    assert H.history("PAN", "AAAPZ0000A", mifin_source=OneShape(body))["history_status"] == "UNKNOWN"


@pytest.mark.parametrize("parameter, value, match, status", [
    ("PAN", "DEFPR1357S", "POTENTIAL_MATCH", "NOT_ATTRIBUTED"),
    ("MOBILE", "9876505555", "AMBIGUOUS", "NOT_ATTRIBUTED"),
    ("PAN", "EFGPS9753T", "SOURCE_ERROR", "UNAVAILABLE"),
    ("PAN", "ZZZPZ1111Z", "NO_MATCH", "NOT_APPLICABLE"),
])
def test_unconfirmed_and_failed_matches_never_attribute_loans(parameter, value, match, status):
    h = H.history(parameter, value)
    assert (h["customer"]["match"], h["history_status"], h["loans"]) == (match, status, [])


def test_a_potential_match_counts_its_loans_without_attributing_them():
    assert H.history("PAN", "DEFPR1357S")["loans_withheld"] == 1


def test_mifin_not_configured_is_unavailable_never_no_loans(monkeypatch):
    monkeypatch.delenv("MIFIN_PROVIDER")
    monkeypatch.delenv("MIFIN_DEDUPE_URL", raising=False)
    h = H.history("PAN", "ABCPK4321L")
    assert (h["customer"]["match"], h["history_status"]) == ("UNAVAILABLE", "UNAVAILABLE")


def test_an_lms_outage_leaves_mifin_facts_and_no_totals():
    h = H.history("MOBILE", "9876502222", lms_source=DownLms())
    assert h["history_status"] == "MIFIN_ONLY"
    assert {x["lms_status"] for x in h["loans"]} == {"UNAVAILABLE"}
    lms = next(s for s in h["sources"] if s["source"] == "LMS")
    assert lms["status"] == "UNAVAILABLE"
    exposure = h["aggregates"]["active_exposure"]
    assert (exposure["status"], exposure["value"]) == ("NOT_EVALUATED", None)      # DISBURSED/CLOSED unmapped
    assert exposure["reason"] == "LOAN_STATUS_UNKNOWN"


def test_a_loan_the_lms_does_not_hold_makes_the_history_partial():
    h = H.history("PAN", "FGHPT8642U")
    assert h["history_status"] == "PARTIAL"
    missing = next(x for x in h["loans"] if x["lms_status"] == "NOT_FOUND")
    # MIFIN says DISBURSED (owner 2026-10-23: shown as Disbursed); the LMS has no record -> live state unconfirmed
    assert (missing["effective_status"], missing["status_source"]) == ("DISBURSED", "MIFIN")
    assert "SANCTIONED_AMOUNT_NOT_A_NUMBER" in missing["amount_problems"]
    assert h["aggregates"]["active_exposure"]["loans_not_confirmed"] == ["PR******31"]


def test_a_conflicting_duplicate_is_never_summed():
    exposure = H.history("PAN", "FGHPT8642U")["aggregates"]["active_exposure"]
    assert (exposure["status"], exposure["reason"]) == ("NOT_EVALUATED", "CONFLICTING_DUPLICATE_RECORDS")
    assert exposure["loans_conflicting"] == ["PR******17"]


def test_lms_values_are_typed_and_bad_ones_reported_not_zeroed():
    loan = L.parse("PR02000001", {"status": "active", "emi": "abc", "dpd_current": -3, "settled": "yes",
                                  "principal_outstanding": "1,20,000"})
    assert loan.values == {"status": "ACTIVE", "principal_outstanding": M.money("120000")[0]}
    assert set(loan.problems) == {"EMI_NOT_A_NUMBER", "DPD_CURRENT_NEGATIVE", "SETTLED_INVALID"}
    assert "emi" in loan.unavailable and "overdue_amount" in loan.unavailable


def test_demo_sources_are_refused_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert H.history("PAN", "ABCPK4321L")["customer"]["match"] == "UNAVAILABLE"
    assert isinstance(L.client(), L._NotConfigured)


def test_no_identifier_or_name_leaves_the_history():
    hist = H.history("MOBILE", "9876502222")
    # MIFIN's name / DOB only in the matching table's own block (mifin.yaml privacy.show_identity_values, sir
    # 2026-10-23); a full PAN / mobile nowhere, not even there (masked)
    shown = hist["customer"].pop("identity_values")
    assert (shown["mifin"]["name"], shown["mifin"]["dob"]) == ("PRIYA MORE", "1988-11-02")
    assert "BCDPM5678N" not in json.dumps(shown) and "9876502222" not in json.dumps(shown)
    assert shown["mifin"]["pan"].endswith("678N") and set(shown["mifin"]["pan"][:-4]) == {"*"}
    text = json.dumps(hist)
    for secret in ("BCDPM5678N", "9876502222", "PRIYA", "MORE\"", "1988-11-02"):
        assert secret not in text, secret
    assert _codes_only_where_allowed(hist, "PR02407753")   # a PR code in full only in its own field
    assert hist["customer"]["customer_code"] == "AP00745218"   # the AP number in full (owner 2026-10-23)
    possible = H.history("PAN", "DEFPR1357S")
    assert possible["customer"]["customer_code"] == "AP00768031"      # a possible match is named in full too...
    assert possible["loans"] == [] and "PR02455102" not in json.dumps(possible)   # ...its loans stay withheld
    assert H.history("MOBILE", "9876502222")["customer"]["searched_identifier_matches"] is True


def _codes_only_where_allowed(obj, value, allowed=("customer_code", "prospect_code", "record_ref")):
    """True when `value` appears in `obj` ONLY under the allowed keys (the full AP / PR code fields)."""
    if isinstance(obj, dict):
        return all((k in allowed and v == value) or _codes_only_where_allowed(v, value, allowed) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_codes_only_where_allowed(x, value, allowed) for x in obj)
    return not (isinstance(obj, str) and value in obj)



# ---- every previous case: common applicant, PAN, cancellation reason (owner request 2026-10-23) -----------------
@pytest.mark.parametrize("pan,expected,cases", [("BCDPM5678N", "MATCH", 3), ("ZZZPZ9999Z", "MISMATCH", 3),
                                                (None, "NOT_COMPARED", 3)])
def test_every_case_carries_common_applicant_and_pan(pan, expected, cases):
    hist = H.history("MOBILE", "9876502222", applicant={"pan": pan} if pan else None)
    assert hist["common_applicant"]["cases"] == cases == len(hist["loans"])
    assert hist["common_applicant"]["same_customer_on_all"] is True
    assert hist["common_applicant"]["pan"] == expected
    for loan in hist["loans"]:
        assert loan["applicant_check"]["common_applicant"] is True
        assert loan["applicant_check"]["pan"] == expected
    if pan:
        assert pan not in json.dumps(hist), "a PAN is compared, never returned"


def test_searched_by_pan_checks_that_pan_on_every_case():
    hist = H.history("PAN", "ABCPK4321L")
    assert [x["applicant_check"]["pan"] for x in hist["loans"]] == ["MATCH"] * 3


def test_cancelled_loans_show_the_source_reason_and_none_when_not_given():
    hist = H.history("PAN", "ABCPK4321L")
    reasons = {x["prospect_code"]: x["cancel_reason"] for x in hist["loans"]}
    assert reasons == {"PR02184467": "Customer withdrew", "PR02236105": "Property title defect",
                       "PR02311890": "Income not verified"}
    assert hist["common_applicant"]["cancelled_cases"] == 3
    assert hist["common_applicant"]["cancelled_with_reason"] == 3
    other = H.history("PAN", "BCDPM5678N")              # a cancelled loan the source gave no reason for
    assert [x["cancel_reason"] for x in other["loans"] if x["effective_status"] == "CANCELED"] == [None]


def test_a_cancellation_reason_is_masked_and_capped():
    assert M.cancel_reason({"CANCEL_REASON": "dup of PR02184467 pan ABCPK4321L ph 9876501111"}) == \
        "dup of [masked] pan [masked] ph [masked]"
    assert len(M.cancel_reason({"CANCEL_REASON": "x" * 500})) == M.cfg()["loans"]["cancel_reason"]["max_length"]
    assert M.cancel_reason({"CANCEL_REASON": "   "}) is None
    assert M.cancel_reason({}) is None


def test_a_possible_match_has_no_per_case_check():
    hist = H.history("PAN", "DEFPR1357S")
    assert hist["history_status"] == "NOT_ATTRIBUTED" and "common_applicant" not in hist and hist["loans"] == []


def test_a_disbursed_loan_the_lms_cannot_confirm_is_never_counted_as_zero():
    h = H.history("MOBILE", "9876502222", lms_source=DownLms())
    by = {x["prospect_code"]: x["effective_status"] for x in h["loans"]}
    assert by == {"PR02407753": "DISBURSED", "PR02251346": "UNKNOWN", "PR02190028": "CANCELED"}   # CLOSED: not mapped
    for name in ("active_exposure", "monthly_emi_burden"):
        t = h["aggregates"][name]
        assert t["status"] == "NOT_EVALUATED" and t["value"] is None, name
        assert t["loans_not_confirmed"] == ["PR******53"] and t["loans_status_unknown"] == ["PR******46"], name


def test_a_status_nobody_confirms_stays_unknown(monkeypatch):
    raw = {"RESPONSE": {"STATUS": "S", "APPLICANTDETAILS": {"CUSTID": "AP00000001", "STRONG_MATCH": "Y", "CUSTTYPE": "Applicant",
           "APPLICANTLOANINFO": {"PROSPECTCODES": [{"PROSPECTCODE": "PR00000001", "SCHEME": "HL", "LOAN_STATUS": "ON HOLD",
                                                     "SANCTIONED_AMOUNT": "100000"}]}}}}

    class One:
        def lookup(self, parameter, value):
            return raw
    h = H.history("PAN", "ABCDE1234F", mifin_source=One(), lms_source=DownLms())
    assert h["loans"][0]["effective_status"] == "UNKNOWN"
    assert h["aggregates"]["unknown_status_loans"] == 1

