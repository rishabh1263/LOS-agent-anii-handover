"""
The dedupe screen does not wait for the model (owner 2026-10-23): the answer carries 3 deterministic reason points at
once; the model's 3 points come from GET /api/v1/credit/dedupe/{request_id}/reason, checked like the summary, bound
to the caller who ran the check, else the deterministic points. Synthetic data only.
"""

from __future__ import annotations

import pytest

from app.agents.dedupe import history_summary as S


def _result(rec="HOLD_FOR_REVIEW", request_id="ddp_" + "a" * 32):
    return {
        "request_id": request_id, "case_id": "C-1", "outcome": "REVIEW_REQUIRED",
        "reason_codes": ["LOS_IDENTITY_CONFLICT"], "next_actions": ["Proceed with onboarding.", "Check the other record."],
        "history": {"customer": {"match": "CONFIRMED_MATCH", "customer_ref": "AP******18"},
                    "history_status": "COMPLETE", "loan_count": 2,
                    "loans": [{"prospect_ref": "PR******53", "scheme": "HL", "effective_status": "ACTIVE"},
                              {"prospect_ref": "PR******46", "scheme": "LAP", "effective_status": "CLOSED"}],
                    "aggregates": {"active_loans": 1}},
        "evaluation": {"outcome": "REVIEW_REQUIRED", "reason_codes": [], "next_actions": [], "rules": []},
        "dedupe": {"invoked": True, "match_reasons": ["MOBILE_MATCHES_EXISTING_CUSTOMER", "IDENTIFIER_ON_OTHER_CUSTOMER_RECORD_IN_LOS"],
                   "parties": [], "workflow_recommendation": {"recommendation": rec, "reason": "r",
                                                             "next_action": "Review the other LOS record"}},
    }


@pytest.fixture(autouse=True)
def clean():
    S._REASONS.clear()
    yield
    S._REASONS.clear()


def test_three_deterministic_points_say_why_and_what_next():
    pts = S.reason_points(_result())
    assert len(pts) == 3
    assert "held for an officer's review" in pts[0]
    assert "identifier on other customer record in los" in pts[1]
    assert pts[2].startswith("Next: Review the other LOS record")
    assert not any("Proceed" in p for p in pts), "never 'proceed' beside a hold"


def test_the_dedupe_answer_does_not_wait_for_the_model(monkeypatch):
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: True)
    called = []
    monkeypatch.setattr(S, "_ollama", lambda f: called.append(1) or "x")
    out = S.explain(_result())
    assert called == [] and out["fallback_reason"] == "LLM_DEFERRED" and out["llm_reason_pending"] is True
    assert len(out["points"]) == 3


def test_model_down_means_nothing_pending(monkeypatch):
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: False)
    out = S.explain(_result())
    assert out["fallback_reason"] == "LLM_UNAVAILABLE" and out["llm_reason_pending"] is False


def test_the_models_points_are_used_when_they_pass():
    r = _result()
    S.remember(r, "officer-1")
    good = ["The case is held for review.", "The mobile is on another customer record in the LOS.",
            "Review the other LOS record next."]
    out = S.reason(r["request_id"], "officer-1", generator=lambda f: good)
    assert out == {"points": good, "source": "LLM", "fallback_reason": None, "recommendation": "HOLD_FOR_REVIEW"}
    assert S.reason(r["request_id"], "officer-1", generator=lambda f: 1 / 0)["source"] == "LLM", "answered once"


@pytest.mark.parametrize("bad, why", [
    (["one", "two"], "expected 3 points"),
    (["Held.", "PAN ABCPD1234K is on file.", "Review."], "PAN"),
    (["Held.", "Full code AP00745218 found.", "Review."], "AP/PR code"),
    (["Held.", "The customer has no loans.", "Review."], "no loans"),
])
def test_bad_model_points_fall_back_to_the_deterministic_ones(bad, why):
    r = _result()
    S.remember(r, "officer-1")
    out = S.reason(r["request_id"], "officer-1", generator=lambda f: bad)
    assert out["source"] == "DETERMINISTIC" and out["fallback_reason"].startswith("LLM_REJECTED"), why
    assert out["points"] == S.reason_points(r)


def test_a_model_error_falls_back():
    r = _result()
    S.remember(r, "officer-1")
    out = S.reason(r["request_id"], "officer-1", generator=lambda f: (_ for _ in ()).throw(TimeoutError()))
    assert out["source"] == "DETERMINISTIC" and out["fallback_reason"] == "LLM_ERROR:TimeoutError"


def test_only_the_caller_who_ran_the_check_gets_its_reason():
    r = _result()
    S.remember(r, "officer-1")
    with pytest.raises(S.ReasonNotFound):
        S.reason(r["request_id"], "officer-2", generator=lambda f: ["a.", "b.", "c."])
    with pytest.raises(S.ReasonNotFound):
        S.reason("ddp_" + "b" * 32, "officer-1", generator=lambda f: ["a.", "b.", "c."])


def test_the_kept_facts_carry_no_identifier():
    r = _result()
    S.remember(r, "officer-1")
    text = str(S._REASONS[r["request_id"]]["facts"])
    for secret in ("ABCPD1234K", "9876501234", "AP00745218"):
        assert secret not in text


def test_the_cache_is_bounded(monkeypatch):
    real = S._cfg()
    monkeypatch.setattr(S, "_cfg", lambda: {**real, "reason_cache_max": 3})
    for i in range(5):
        S.remember(_result(request_id=f"ddp_{i:032d}"), "officer-1")
    assert len(S._REASONS) == 3 and "ddp_" + "4".zfill(32) in S._REASONS
