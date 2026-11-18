"""
POST /api/v1/credit/dedupe/all -- every parameter at once (sir, 2026-10-24; app/agents/dedupe/all_parameters.py).
SYNTHETIC DEMO DATA ONLY (app/config/demo/mifin_demo.yaml): PAN BCDPM5678N and mobile 9876502222 are one customer
(AP00745218); mobile 9876501111 is another (AP00731942).
"""
from __future__ import annotations

import json

import pytest

from tests.integration.test_customer_history import CASE, _client, env, repo  # noqa: F401


def _all(make_token, **values):
    return _client(make_token).post("/api/v1/credit/dedupe/all", json={"case_id": CASE, "customer_type": "Existing",
                                                                       **values})


def test_two_identifiers_of_one_customer_agree(repo, make_token):
    r = _all(make_token, pan="BCDPM5678N", mobile="9876502222")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checked"] == ["PAN", "MOBILE"]
    assert {m["parameter"] for m in body["missing"]} == {"AP_CODE", "PR_CODE"}         # said, never guessed
    assert body["consistency"]["status"] == "SAME_CUSTOMER"
    assert body["consistency"]["customers"] == ["AP00745218"]
    assert all(row["source"] == "ENTERED" for row in body["rows"])
    assert "PARAMETERS_POINT_TO_DIFFERENT_CUSTOMERS" not in body["combined"]["reason_codes"]


def test_parameters_naming_different_customers_hold_the_case(repo, make_token):
    body = _all(make_token, pan="BCDPM5678N", mobile="9876501111").json()
    assert body["consistency"]["status"] == "CONFLICT"
    assert body["consistency"]["customers"] == ["AP00731942", "AP00745218"]
    assert body["combined"]["recommendation"] == "HOLD_FOR_REVIEW"
    assert body["combined"]["reason_codes"][0] == "PARAMETERS_POINT_TO_DIFFERENT_CUSTOMERS"


def test_the_combined_answer_is_the_most_serious_row(repo, make_token):
    body = _all(make_token, pan="BCDPM5678N", mobile="9876502222").json()
    recs = [row["recommendation"] for row in body["rows"]]
    order = ["BLOCK_APPLICATION", "UNABLE_TO_DECIDE", "HOLD_FOR_REVIEW", "PROCEED_TO_NEXT_STAGE"]
    assert body["combined"]["recommendation"] == min(recs, key=order.index)


def test_values_are_never_echoed_in_full(repo, make_token):
    r = _all(make_token, pan="BCDPM5678N", mobile="9876502222")
    rows = [{k: v for k, v in row.items() if k != "result"} for row in r.json()["rows"]]
    shown = json.dumps(rows)
    assert "BCDPM5678N" not in shown and "9876502222" not in shown
    assert rows[0]["value_shown"].endswith("678N") and rows[1]["value_shown"] == "98******22"


def test_nothing_to_check_is_refused_and_an_unknown_case_is_not_found(repo, make_token):
    assert _all(make_token).status_code == 422                                     # no value anywhere
    r = _client(make_token).post("/api/v1/credit/dedupe/all", json={"case_id": "CASE-NOPE", "pan": "BCDPM5678N"})
    assert r.status_code in (403, 404)


def test_a_bad_value_is_refused_like_a_single_check(repo, make_token):
    assert _all(make_token, pan="NOT-A-PAN").status_code == 422
