"""
THE LMS (Leviosa) WHEN IT GOES WRONG -- negative tests through the real loan-history path (owner, 2026-10-23).
Leviosa is faked at the HTTP layer; MIFIN is the demo source; synthetic data only. Every failure must leave the loan
UNCONFIRMED and every total incomplete -- never a zero, never another loan's numbers, never a guessed status.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.agents.dedupe import demo_sources
from app.agents.dedupe import leviosa as LV
from app.agents.dedupe import lms as L
from app.agents.dedupe import loan_history as H

URL = "https://leviosa.example.invalid/lms/loans/{prospect_code}"
TOKEN = "synthetic-token-123"
CUSTOMER = ("MOBILE", "9876502222")          # demo: PR02407753 DISBURSED, PR02251346 CLOSED, PR02190028 CANCELED


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setenv("MIFIN_PROVIDER", "demo")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(LV.time, "sleep", lambda s: None)            # no real back-off waits in the suite
    demo_sources.reload()


class Fake:
    """httpx.get stand-in: `answer(url) -> (status, body | Exception)`; records every call."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        status, body = self.answer(url)
        if isinstance(body, Exception):
            raise body
        req = httpx.Request("GET", url)
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, request=req)
        return httpx.Response(status, text=str(body), request=req)


def good(code, **extra):
    return {"prospect_code": code, "status": "ACTIVE", "principal_outstanding": "312400", "emi": "11200",
            "dpd_current": 0, "dpd_max_12m": 0, "overdue_amount": "0", "disbursed_on": "15-03-2024", **extra}


def run(monkeypatch, answer):
    fake = Fake(answer)
    monkeypatch.setattr(httpx, "get", fake)
    return H.history(*CUSTOMER, lms_source=LV.HttpLeviosaLms(URL, TOKEN)), fake


def loan(h, code):
    return next(x for x in h["loans"] if x["prospect_code"] == code)


def never_zero(h):
    for name in ("active_exposure", "monthly_emi_burden"):
        t = h["aggregates"][name]
        assert t["status"] != "COMPLETE", (name, t)
        assert not (t["value"] in ("0", 0) and t["status"] != "COMPLETE"), (name, t)


# ---- the positive control: Leviosa answers properly ---------------------------------------------------------------
def test_control_a_good_reply_is_read_and_confirms_the_loan(monkeypatch):
    h, _ = run(monkeypatch, lambda url: (200, good(url.rsplit("/", 1)[1])) if "PR02407753" in url
               else (200, good(url.rsplit("/", 1)[1], status="CLOSED", principal_outstanding="0", emi=None)))
    x = loan(h, "PR02407753")
    assert (x["lms_status"], x["effective_status"], x["status_source"]) == ("FOUND", "ACTIVE", "LMS")
    assert x["lms"]["disbursed_on"] == "2024-03-15" and x["lms"]["emi"] == "11200"
    lms = next(s for s in h["sources"] if s["source"] == "LMS")
    assert lms["provider"] == "LEVIOSA" and lms["is_demo"] is False


# ---- outages: the whole source ------------------------------------------------------------------------------------
@pytest.mark.parametrize("failure, reason", [
    (httpx.ConnectTimeout("t"), "LEVIOSA_TIMEOUT"),
    (httpx.ConnectError("refused"), "LEVIOSA_UNREACHABLE"),
])
def test_down_or_timing_out_is_unavailable_and_retried_then_stops(monkeypatch, failure, reason, caplog):
    caplog.set_level(logging.WARNING)
    h, fake = run(monkeypatch, lambda url: (0, failure))
    assert h["history_status"] == "MIFIN_ONLY"
    assert {x["lms_status"] for x in h["loans"]} == {"UNAVAILABLE"}
    assert len(fake.calls) == LV.cfg()["client"]["retries"] + 1, "retried, then ONE outage stops all lookups"
    assert reason in caplog.text and TOKEN not in caplog.text and "PR024" not in caplog.text
    never_zero(h)


def test_a_5xx_is_retried_and_a_later_success_is_used(monkeypatch):
    seen = {"n": 0}

    def answer(url):
        seen["n"] += 1
        return (503, "busy") if seen["n"] == 1 else (200, good(url.rsplit("/", 1)[1]))
    h, _ = run(monkeypatch, answer)
    assert loan(h, "PR02407753")["lms_status"] == "FOUND"


@pytest.mark.parametrize("status", [401, 403, 400])
def test_a_4xx_is_not_retried_and_is_an_outage(monkeypatch, status):
    h, fake = run(monkeypatch, lambda url: (status, {"error": "no"}))
    assert len(fake.calls) == 1
    assert h["history_status"] == "MIFIN_ONLY"
    never_zero(h)


def test_not_configured_is_unavailable_never_zero(monkeypatch):
    monkeypatch.setenv("LMS_PROVIDER", "leviosa")
    monkeypatch.delenv("LEVIOSA_LMS_URL", raising=False)
    h = H.history(*CUSTOMER)
    assert {x["lms_status"] for x in h["loans"]} == {"UNAVAILABLE"}
    never_zero(h)
    monkeypatch.setenv("LEVIOSA_LMS_URL", "https://x.invalid/no-placeholder")     # a URL without {prospect_code}
    assert isinstance(L.client(), L._NotConfigured)


# ---- one loan's reply is wrong -----------------------------------------------------------------------------------
def test_a_404_is_not_found_for_that_loan_not_an_outage(monkeypatch):
    h, _ = run(monkeypatch, lambda url: (404, "no such loan") if "PR02251346" in url else (200, good(url.rsplit("/", 1)[1])))
    assert loan(h, "PR02251346")["lms_status"] == "NOT_FOUND"
    assert loan(h, "PR02407753")["lms_status"] == "FOUND"          # the others still read
    assert h["history_status"] == "PARTIAL"


def test_a_reply_that_is_not_json_confirms_nothing(monkeypatch):
    h, _ = run(monkeypatch, lambda url: (200, "<html>maintenance</html>"))
    x = loan(h, "PR02407753")
    assert x["lms_status"] == "UNREADABLE" and "LEVIOSA_RESPONSE_MALFORMED" in x["lms_problems"]
    assert x["effective_status"] == "DISBURSED" and x["status_source"] == "MIFIN"        # MIFIN's word, not LMS's
    assert x["resolved"] is False
    never_zero(h)


def test_a_reply_for_another_loan_is_never_shown_against_this_one(monkeypatch):
    h, _ = run(monkeypatch, lambda url: (200, good("PR09999999", principal_outstanding="999999")))
    x = loan(h, "PR02407753")
    assert x["lms_status"] == "UNREADABLE" and "LEVIOSA_REPLY_FOR_ANOTHER_LOAN" in x["lms_problems"]
    assert "999999" not in json.dumps(h)
    never_zero(h)


def test_an_unknown_status_word_is_unknown_and_reported(monkeypatch):
    h, _ = run(monkeypatch, lambda url: (200, good(url.rsplit("/", 1)[1], status="FROZEN")))
    x = loan(h, "PR02407753")
    assert "STATUS_UNRECOGNISED" in x["lms_problems"]
    assert x["effective_status"] != "ACTIVE"                      # never guessed live


@pytest.mark.parametrize("field, bad, problem", [
    ("emi", "-500", "EMI_NEGATIVE"),
    ("emi", "abc", "EMI_NOT_A_NUMBER"),
    ("principal_outstanding", "12,34x", "PRINCIPAL_OUTSTANDING_NOT_A_NUMBER"),
    ("dpd_max_12m", -3, "DPD_MAX_12M_NEGATIVE"),
    ("dpd_current", "many", "DPD_CURRENT_INVALID"),
    ("settled", "maybe", "SETTLED_INVALID"),
    ("disbursed_on", "31-02-2024", "DISBURSED_ON_INVALID"),
])
def test_a_bad_value_is_dropped_and_reported_never_zero(monkeypatch, field, bad, problem):
    h, _ = run(monkeypatch, lambda url: (200, good(url.rsplit("/", 1)[1], **{field: bad})))
    x = loan(h, "PR02407753")
    assert problem in x["lms_problems"], x["lms_problems"]
    assert field not in (x["lms"] or {}) and field in x["lms_unavailable_fields"]


def test_a_missing_emi_makes_the_emi_total_incomplete_not_lower(monkeypatch):
    h, _ = run(monkeypatch, lambda url: (200, good(url.rsplit("/", 1)[1], emi=None)) if "PR02407753" in url
               else (200, good(url.rsplit("/", 1)[1], status="CLOSED")))
    emi = h["aggregates"]["monthly_emi_burden"]
    assert emi["status"] == "PARTIAL" and emi["loans_missing"] == ["PR******53"]


def test_the_lms_dying_midway_keeps_what_was_read_and_asks_no_more(monkeypatch):
    seen = {"n": 0}

    def answer(url):
        seen["n"] += 1
        return (200, good(url.rsplit("/", 1)[1])) if seen["n"] == 1 else (0, httpx.ReadTimeout("t"))
    h, fake = run(monkeypatch, answer)
    statuses = [x["lms_status"] for x in h["loans"]]
    assert statuses[0] == "FOUND" and set(statuses[1:]) == {"UNAVAILABLE"}
    assert len(fake.calls) == 1 + LV.cfg()["client"]["retries"] + 1
    never_zero(h)


def test_the_token_is_sent_as_a_bearer_and_the_pr_code_is_url_encoded(monkeypatch):
    fake = Fake(lambda url: (404, "x"))
    monkeypatch.setattr(httpx, "get", fake)
    LV.HttpLeviosaLms(URL, TOKEN).loan("PR0/../1")
    url, headers = fake.calls[0]
    assert url.endswith("PR0%2F..%2F1") and headers == {"Authorization": f"Bearer {TOKEN}"}


# ---- the Leviosa demo: Leviosa-shaped replies through the REAL mapping (owner 2026-10-23: "same as MIFIN") ---------
def test_the_leviosa_demo_is_read_by_the_real_mapping(monkeypatch):
    monkeypatch.setenv("LMS_PROVIDER", "leviosa-demo")
    h = H.history(*CUSTOMER)
    lms = next(s for s in h["sources"] if s["source"] == "LMS")
    assert (lms["provider"], lms["is_demo"], lms["status"]) == ("LEVIOSA", True, "AVAILABLE")
    live = loan(h, "PR02407753")
    assert (live["effective_status"], live["status_source"]) == ("ACTIVE", "LMS")      # Leviosa "LIVE" -> ACTIVE
    assert live["lms"]["disbursed_on"] == "2024-03-15"                                  # "15-03-2024" read
    assert loan(h, "PR02251346")["lms"]["settled"] is False                             # "N" read as no
    assert h["aggregates"]["active_exposure"] == {**h["aggregates"]["active_exposure"], "status": "COMPLETE", "value": "312400"}


def test_the_leviosa_demo_never_answers_in_production(monkeypatch):
    monkeypatch.setenv("LMS_PROVIDER", "leviosa-demo")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert isinstance(L.client(), L._NotConfigured)
