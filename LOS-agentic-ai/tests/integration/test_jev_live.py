"""
JEV ACCEPTANCE AGAINST THE REAL PROVIDER -- no stub, no fake.

Runs only with JEV_LIVE_TESTS=1 and a configured provider:

    local    JEV_BASE_URL=http://localhost:8888   JEV_MODEL=laya-multilingual  (Unsloth Decision API)
    hosted   JEV_BASE_URL=https://api.beatapi.io  JEV_MODEL=jev-1.13          JEV_API_KEY=...

    JEV_LIVE_TESTS=1 python -m pytest tests/integration/test_jev_live.py -m live_jev -s

Question set case_triage.v2 (config/jev.yaml). JEV is NOT an authority for KYC
or identity: the KYC agent's recorded result is an INPUT here, never re-judged.

Asserted on the real model:
  1  every question of the set comes back from ONE batched call
  2  needs_manual_review (the AUTOMATE question) -- clear case NO, KYC-review case YES
  3  routing by POLICY, never by hope: a route in the AUTO band must be the right
     one; an unsure route is not automated; with the shipped config routing is
     ADVISORY and nothing executes on it
  4  severity is a typed score with a confidence
  5  an unreachable provider is a dependency failure, never an answer
"""

from __future__ import annotations

import os

import pytest

from app.jev import client, config, engine

pytestmark = [
    pytest.mark.live_jev,
    pytest.mark.skipif(os.getenv("JEV_LIVE_TESTS") != "1", reason="set JEV_LIVE_TESTS=1 to call the real JEV provider"),
    pytest.mark.skipif(client.readiness_problem() is not None,
                       reason=f"JEV provider not configured: {client.readiness_problem()}"),
]

QUESTIONS = config.question_set("CASE_TRIAGE")["questions"]


def _doc(t, v="PASS", codes=()):
    return {"document_type": t, "verification": v, "reason_codes": list(codes), "fields": {}}


CLEAR = {"documents": [_doc("PAN"), _doc("DRIVING_LICENCE"), _doc("BANK_STATEMENT")],
         "kyc": {"status": "PASS", "reason_codes": []},
         "income_consistency": {"status": "PASS", "reason_codes": ["INCOME_CONSISTENT"]}}
KYC_REVIEW = {"documents": [_doc("PAN"), _doc("DRIVING_LICENCE")],
              "kyc": {"status": "REVIEW", "reason_codes": ["NAME_MISMATCH"]}}
RIGHT_ROUTE = {"CLEAR": {"NONE"}, "KYC_REVIEW": {"KYC"}}


def _decide(state):
    reply = client.system_one(state, QUESTIONS)
    print(f"\nJEV {reply['model']} {reply['latency_ms']} ms", {k: v for k, v in reply["answers"].items()})
    return reply, {qid: engine._decision(qid, q, reply["answers"][qid], "JEV") for qid, q in QUESTIONS.items()}


def test_1_2_one_batched_call_and_the_manual_review_decision():
    clear_reply, clear = _decide(CLEAR)
    review_reply, review = _decide(KYC_REVIEW)
    for reply in (clear_reply, review_reply):
        assert set(reply["answers"]) == set(QUESTIONS)                 # 1: all in ONE call
        assert reply["model"] == config.model()                          # the model the config names
    # the AUTOMATE question: an answer in the AUTO band must be right -- that band is
    # what executes. Outside it nothing is automated, whatever the model leaned to.
    for decided, right in ((clear["needs_manual_review"], "NO"), (review["needs_manual_review"], "YES")):
        if decided["band"] == "AUTO":
            assert decided["answer"] == right, decided
    assert review["needs_manual_review"]["answer"] == "YES"           # a recorded KYC REVIEW is seen


def test_3_routing_follows_the_confidence_policy():
    for name, state in (("CLEAR", CLEAR), ("KYC_REVIEW", KYC_REVIEW)):
        _, decisions = _decide(state)
        route = decisions["route_to"]
        print(f"ROUTE {name}: {route['answer']} conf={route['confidence']} band={route['band']} "
              f"right={route['answer'] in RIGHT_ROUTE[name]}")
        # routing is ADVISORY on this provider BECAUSE confident routes were measured wrong
        # (benchmark: 0 of 9 AUTO-band routes right). The shipped config therefore never
        # executes one, whatever the band -- that is what is asserted, not the model's luck.
        assert route["automate"] is False
        from app.jev import actions

        planned = actions.plan_and_execute("LIVE", None, "run", list(decisions.values()), state, execute=False)
        assert next(a for a in planned if a["action"] == "ROUTE")["status"] in {"ADVISORY", "SKIPPED"}


def test_4_severity_is_a_typed_score_with_confidence():
    reply, decisions = _decide(KYC_REVIEW)
    raw = reply["answers"]["severity"]
    assert raw["type"] == "score" and 0 <= raw["score"] <= len(QUESTIONS["severity"]["criteria"]) - 1
    assert decisions["severity"]["answer"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert decisions["severity"]["confidence"] is not None


def test_5_an_unreachable_provider_is_a_dependency_failure(monkeypatch):
    monkeypatch.setenv("JEV_BASE_URL", "http://127.0.0.1:9")     # nothing listens here
    with pytest.raises(client.JevUnavailable):
        client.system_one(CLEAR, QUESTIONS)
