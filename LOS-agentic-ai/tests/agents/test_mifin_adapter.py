"""
MIFIN dedupe response normaliser + client (app/agents/dedupe/mifin.py).

SYNTHETIC FIXTURES ONLY, built below in the SHAPE of the owner's reference response (2026-10-22). No value from that
reference -- names, PAN, mobile, bank, address, customer or prospect codes -- appears here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.agents.dedupe import mifin as M

SYN_PAN = "AAAPZ9999Z"
SYN_MOBILE = "9000000001"
SYN_CUST = "AP00731942"


def prospect(code="PR02184467", scheme="SYN SCHEME", status="CANCELED", sanctioned="100000", principal="100000",
             interest="", other="0"):
    return {"SCHEME": scheme, "OS_OTHERCHARGES": other, "PROSPECTCODE": code, "OS_INTEREST": interest,
            "LOAN_STATUS": status, "SANCTIONED_AMOUNT": sanctioned, "OS_PRINCIPAL": principal}


def response(*, status="S", strong="Y", potential="", cust=SYN_CUST, prospects=None, details=True, score=5,
             many=None):
    applicant = {
        "SCORE": score, "LNAME": "SYNTHETIC", "FNAME": "TEST", "MNAME": "", "CUSTTYPE": "Applicant", "CUSTID": cust,
        "STRONG_MATCH": strong, "POTENTIAL_MATCH": potential, "DOB": "1990-01-01", "PAN": SYN_PAN,
        "MOBILE": SYN_MOBILE, "FATHERNAME": "SYN FATHER", "MOTHERNAME": "SYN MOTHER",
        "BANKDETAIL": {"BANKDETAILS": {"BANK_AC_NUMBER": "00000000000099", "IFSC_CODE": "ZZZZ0000001"}},
        "ADDRESSDETAIL": {"ADDRESSLIST": [{"ADDRESS_LINE1": "1 Synthetic Road", "ADDRESS_LINE3": 12345,
                                            "PINCODE": "400001"}]},
        "APPLICANTLOANINFO": {"PROSPECTCODES": prospects if prospects is not None else []},
    }
    body = {"RESPONSE": {"MESSAGE": "SYNTHETIC MESSAGE", "STATUS": status}}
    if details:
        body["RESPONSE"]["APPLICANTDETAILS"] = many if many is not None else applicant
    return body


# ---- 1-4: matches and loan records ----------------------------------------------------------------------------
def test_1_strong_match_with_zero_prospects_is_a_confirmed_customer_with_no_loans():
    r = M.normalise(response(prospects=[]))
    assert (r.match, r.customer_id, r.loans) == (M.CONFIRMED, SYN_CUST, [])
    assert r.public()["loan_count"] == 0


def test_2_one_canceled_prospect_is_kept_and_never_counted_as_exposure():
    r = M.normalise(response(prospects=[prospect()]))
    [loan] = r.loans
    assert (loan.status_class, loan.source_status, str(loan.amounts["os_principal"])) == ("CANCELED", "CANCELED",
                                                                                           "100000")
    assert M.cfg()["loans"]["exposure_statuses"] == []          # nothing counts as exposure until confirmed


def test_3_and_4_mixed_statuses_unknown_ones_are_never_classed_active_or_closed():
    rows = [prospect("PR02184467", status="CANCELED"), prospect("PR02236105", status="ACTIVE"),
            prospect("PR02311890", status="CLOSED"), prospect("PR02407753", status="")]
    r = M.normalise(response(prospects=rows))
    classes = [loan.status_class for loan in r.loans]
    assert classes == ["CANCELED", "UNKNOWN", "UNKNOWN", "UNKNOWN"]   # ACTIVE / CLOSED not confirmed MIFIN values
    assert r.public()["loans_by_status_class"] == {"CANCELED": 1, "UNKNOWN": 3}


def test_5_duplicates_are_collapsed_and_conflicting_duplicates_flagged():
    same, other = prospect("PR02251346"), prospect("PR02251346", sanctioned="999999")
    r = M.normalise(response(prospects=[same, dict(same), other]))
    [loan] = r.loans
    assert loan.duplicate_count == 1 and loan.conflict is True
    assert "CONFLICTING_DUPLICATE_PROSPECT" in r.warnings


# ---- 6: match classes ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs, expected", [
    (dict(strong="", potential="Y"), M.POTENTIAL),
    (dict(strong="Y", potential="Y"), M.CONFIRMED),          # the reference had both; strong wins (UNVERIFIED rule)
    (dict(details=False), M.NO_MATCH),
    (dict(status="F"), M.SOURCE_ERROR),
    (dict(status=""), M.SOURCE_ERROR),
    (dict(strong="Y", cust=""), M.AMBIGUOUS),                 # a strong match nobody can be linked to
])
def test_6_match_classes(kwargs, expected):
    assert M.normalise(response(**kwargs)).match == expected


def test_6_two_customer_ids_are_ambiguous_never_a_pick():
    a, b = response()["RESPONSE"]["APPLICANTDETAILS"], dict(response()["RESPONSE"]["APPLICANTDETAILS"])
    b["CUSTID"] = "AP00731958"
    r = M.normalise(response(many=[a, b]))
    assert (r.match, r.customer_id, r.candidates) == (M.AMBIGUOUS, None, 2)


def test_the_score_is_reported_never_used_without_an_approved_threshold():
    r = M.normalise(response(strong="", potential="Y", score=99))
    assert r.match == M.POTENTIAL and r.public()["match_basis"]["score_threshold"] is None


# ---- 7: amounts and optional fields ----------------------------------------------------------------------------
@pytest.mark.parametrize("raw, value, problem", [
    ("150000", "150000", None), ("1,50,000", "150000", None), ("", None, None), (None, None, None),
    ("abc", None, "NOT_A_NUMBER"), ("-5", "-5", "NEGATIVE"), ("NaN", None, "NOT_A_NUMBER"), (0, "0", None),
])
def test_7_money_is_parsed_safely_and_empty_is_unknown_not_zero(raw, value, problem):
    parsed, found = M.money(raw)
    assert (str(parsed) if parsed is not None else None, found) == (value, problem)


@pytest.mark.parametrize("info", [None, "", {}, {"PROSPECTCODES": None}, {"PROSPECTCODES": ""}])
def test_7_missing_loan_sections_are_no_loans(info):
    body = response()
    body["RESPONSE"]["APPLICANTDETAILS"]["APPLICANTLOANINFO"] = info
    assert M.normalise(body).loans == []


def test_7_malformed_records_are_reported_not_fatal():
    r = M.normalise(response(prospects=["junk", {"SCHEME": "X"}, prospect(sanctioned="n/a")]))
    assert len(r.loans) == 1 and "SANCTIONED_AMOUNT_NOT_A_NUMBER" in r.loans[0].problems
    assert {"PROSPECT_RECORD_MALFORMED", "PROSPECT_WITHOUT_CODE"} <= set(r.warnings)


@pytest.mark.parametrize("raw", ["not json", b"{", 42, [], {"NO": "RESPONSE"}, {"RESPONSE": "x"}])
def test_7_malformed_responses_are_malformed_never_no_match(raw):
    assert M.normalise(raw).match == M.MALFORMED


# ---- 8: the client: not configured, timeout, outage, retries, malformed ----------------------------------------
def test_8_not_configured_is_unavailable(monkeypatch):
    monkeypatch.delenv("MIFIN_DEDUPE_URL", raising=False)
    result, raw = M.lookup("PAN", SYN_PAN)
    assert (result.match, raw, result.warnings) == (M.UNAVAILABLE, None, ["MIFIN_NOT_CONFIGURED"])


def _http(monkeypatch, outcomes):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    return M.HttpMifinClient("https://mifin.invalid/dedupe", "tok", '{"KEY": "{parameter}", "VAL": "{value}"}'), calls


def test_8_timeouts_are_retried_then_unavailable(monkeypatch):
    client, calls = _http(monkeypatch, [httpx.TimeoutException("t")])
    result, _ = M.lookup("PAN", SYN_PAN, source=client)
    assert result.match == M.UNAVAILABLE and result.warnings == ["MIFIN_TIMEOUT"]
    assert len(calls) == M.cfg()["client"]["retries"] + 1


def test_8_a_5xx_then_success_recovers(monkeypatch):
    ok = httpx.Response(200, json=response(prospects=[prospect()]))
    client, calls = _http(monkeypatch, [httpx.Response(503), ok])
    result, _ = M.lookup("PAN", SYN_PAN, source=client)
    assert result.match == M.CONFIRMED and len(calls) == 2
    assert calls[0] == {"KEY": "PAN", "VAL": SYN_PAN}              # the request built from the supplied template


def test_8_a_4xx_is_never_retried(monkeypatch):
    client, calls = _http(monkeypatch, [httpx.Response(401)])
    result, _ = M.lookup("PAN", SYN_PAN, source=client)
    assert result.warnings == ["MIFIN_HTTP_401"] and len(calls) == 1


def test_8_a_body_that_is_not_json_is_malformed(monkeypatch):
    client, _ = _http(monkeypatch, [httpx.Response(200, content=b"<html>oops</html>")])
    assert M.lookup("PAN", SYN_PAN, source=client)[0].match == M.MALFORMED


# ---- privacy -------------------------------------------------------------------------------------------------
def test_nothing_personal_leaves_the_public_result():
    r = M.normalise(response(prospects=[prospect("PR02190028")]))
    text = json.dumps(r.public())
    for secret in (SYN_PAN, SYN_MOBILE, "SYNTHETIC", "1990-01-01", "00000000000099", "Synthetic Road", "SYN FATHER"):
        assert secret not in text, secret
    for code in (SYN_CUST, "PR02190028"):             # in full only in customer_code / prospect_code
        assert _codes_only_where_allowed(r.public(), code), code
    assert r.public()["customer_ref"] == "AP******42"
    assert set(r.identity_fields_present) >= {"PAN", "MOBILE", "DOB"}


def test_the_searched_identifier_is_compared_by_hmac_without_being_returned():
    raw = response()
    assert M.same_identifier(raw, "PAN", SYN_PAN.lower()) is True
    assert M.same_identifier(raw, "PAN", "BBBPZ8888Y") is False
    assert M.same_identifier(raw, "MOBILE", "not a mobile") is None


# ---- AP / PR code formats (observed: AP + 8 digits, PR + 8 digits) ----------------------------------------------
@pytest.mark.parametrize("kind, value, fits", [
    ("AP_CODE", "AP00731942", True), ("AP_CODE", "ap00731942", True), ("AP_CODE", "AP731942", False),
    ("AP_CODE", "XP00731942", False), ("PR_CODE", "PR02184467", True), ("PR_CODE", "PR0218446", False),
])
def test_ap_and_pr_code_formats(kind, value, fits):
    assert M.fits(kind, value) is fits


def test_codes_outside_the_observed_format_are_kept_and_flagged():
    r = M.normalise(response(cust="CUST-1", prospects=[prospect("LOAN-77")]))
    assert r.customer_id == "CUST-1" and "AP_CODE_FORMAT_UNEXPECTED" in r.warnings
    assert r.loans[0].problems == ["PR_CODE_FORMAT_UNEXPECTED"]


def test_mifin_custtype_is_the_customers_role_mapped_by_config_never_guessed():
    from app.agents.dedupe import mifin

    assert mifin._customer_type("Applicant") == "APPLICANT"
    assert mifin._customer_type("Co-Applicant") == "CO_APPLICANT"
    assert mifin._customer_type("co applicant") == "CO_APPLICANT"
    assert mifin._customer_type("Borrower") == "UNMAPPED"          # a value the config does not know is not guessed
    assert mifin._customer_type(None) is None and mifin._customer_type("") is None


def _codes_only_where_allowed(obj, value, allowed=("customer_code", "prospect_code", "record_ref")):
    """True when `value` appears in `obj` ONLY under the allowed keys (the full AP / PR code fields)."""
    if isinstance(obj, dict):
        return all((k in allowed and v == value) or _codes_only_where_allowed(v, value, allowed) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_codes_only_where_allowed(x, value, allowed) for x in obj)
    return not (isinstance(obj, str) and value in obj)


def test_with_full_codes_off_no_code_is_returned(monkeypatch):
    monkeypatch.setattr(M, "full_codes", lambda: False)
    r = M.normalise(response(prospects=[prospect("PR02190028")]))
    text = json.dumps(r.public())
    assert SYN_CUST not in text and "PR02190028" not in text
    assert r.public()["customer_code"] is None and r.public()["loans"][0]["prospect_code"] is None

