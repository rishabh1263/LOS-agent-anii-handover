"""
Two numbers, hard gates, and a reason for every non-pass.

THE STATE THIS REPLACES. A bank statement that did not pass came back as:

    verification: REVIEW
    reason_codes: []

The verdict was usually right. It was simply unexplained -- a field officer
was told a document needed a person to look at it and nothing about what to
look at, and a queue had nothing to route on.

THE DISTINCTION THE TWO NUMBERS EXIST FOR. `verification_score` is how much
of what should have been established was. `verification_confidence` is how
far that answer can be relied on. A document that plainly fails every check
scores 0 with HIGH confidence; one whose checks could not run scores 0 with
LOW confidence. Those are different situations and a single number hides it.

AND NEITHER IS A RISK SCORE. No figure on a bank statement moves either one.
"""

from __future__ import annotations

import pytest

from app.agents.verification import scoring
from app.agents.verification.scoring import Check, Outcome


def ok(name, weight=1.0):
    return Check(name=name, outcome=Outcome.OK, weight=weight)


def bad(name, weight=1.0, code="X_BAD", gate=False, verdict=scoring.FAIL):
    return Check(name=name, outcome=Outcome.BAD, weight=weight,
                 reason_code=code, reason=f"{name} failed",
                 hard_gate=gate, gate_verdict=verdict)


def unknown(name, weight=1.0, code="X_UNKNOWN", gate=False,
            verdict=scoring.FAIL):
    return Check(name=name, outcome=Outcome.UNKNOWN, weight=weight,
                 reason_code=code, reason=f"{name} inconclusive",
                 hard_gate=gate, gate_verdict=verdict)


# ==========================================================================
# THE TWO NUMBERS
# ==========================================================================

def test_everything_conclusive_and_good_scores_full_marks():
    result = scoring.assess("PAN", [ok("a", 2), ok("b", 1)])

    assert result.status == scoring.PASS
    assert result.score == 100
    assert result.confidence == 100
    assert result.reason_codes == []


def test_a_clean_failure_scores_zero_with_high_confidence():
    """
    The document plainly fails. That is a RELIABLE finding, and confidence
    has to say so -- otherwise a reviewer learns to discount real failures.
    """
    result = scoring.assess("PAN", [bad("a", 2), bad("b", 1)])

    assert result.score == 0
    assert result.confidence == 100
    assert result.reason_codes


def test_checks_that_could_not_run_score_zero_with_low_confidence():
    """
    The same score, a completely different situation. Nothing was
    established either way.
    """
    result = scoring.assess("PAN", [unknown("a", 2), unknown("b", 1)])

    assert result.score == 0
    assert result.confidence == 0
    assert result.status == scoring.REVIEW


def test_score_and_confidence_come_apart():
    """Half the checks passed, the rest were inconclusive."""
    result = scoring.assess("PAN", [ok("a"), unknown("b")])

    assert result.score == 50
    assert result.confidence == 50
    assert result.score != 100 and result.confidence != 100


@pytest.mark.parametrize("checks", [
    [ok("a")], [bad("a")], [unknown("a")], [ok("a"), bad("b"), unknown("c")],
    [],
])
def test_both_numbers_stay_in_range(checks):
    result = scoring.assess("PAN", checks)
    assert 0 <= result.score <= 100
    assert 0 <= result.confidence <= 100


# ==========================================================================
# EVERY NON-PASS EXPLAINS ITSELF
# ==========================================================================

def test_a_review_always_carries_a_reason_code():
    result = scoring.assess("PAN", [ok("a"), unknown("b")])
    assert result.status == scoring.REVIEW
    assert result.reason_codes
    assert result.reasons


def test_a_review_with_no_named_reason_still_gets_one():
    """
    THE EMPTY-REASON BUG, closed at the layer that could produce it.

    A check that fails without naming a code must not produce a silent
    REVIEW.
    """
    nameless = Check(name="mystery", outcome=Outcome.UNKNOWN, weight=1.0)
    result = scoring.assess("PAN", [nameless])

    assert result.status == scoring.REVIEW
    assert result.reason_codes == ["VERIFICATION_INCONCLUSIVE"]
    assert result.reasons


def test_a_pass_needs_no_reason():
    result = scoring.assess("PAN", [ok("a")])
    assert result.reason_codes == []
    assert result.reasons == []


def test_nothing_checked_is_not_a_pass():
    result = scoring.assess("PAN", [])
    assert result.status == scoring.SKIPPED


# ==========================================================================
# HARD GATES OUTRANK THE SCORE
# ==========================================================================

def test_a_hard_gate_failure_beats_a_high_score():
    """
    Nine clean checks and a wrong document type is still a wrong document.

    A weighted score that can talk its way past a type mismatch is one that
    will eventually admit the wrong document.
    """
    checks = [ok(f"c{i}", 1) for i in range(9)]
    checks.append(bad("type", 1, code="DOCUMENT_TYPE_MISMATCH", gate=True))

    result = scoring.assess("PAN", checks)

    assert result.status == scoring.FAIL
    assert result.reason_codes == ["DOCUMENT_TYPE_MISMATCH"]
    assert result.score == 90, "the score is still reported honestly"


def test_a_gate_that_could_not_be_evaluated_reviews_rather_than_fails():
    """
    THE DISTINCTION THIS WHOLE MODULE EXISTS FOR.

    A gate the parser could not evaluate is not a gate the document failed.
    Treating "we could not check" as "it is wrong" turns a parser limitation
    into an accusation about the customer's document.
    """
    result = scoring.assess("BANK_STATEMENT", [
        ok("readable"),
        unknown("integrity", code="BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE",
                gate=True, verdict=scoring.FAIL),
    ])

    assert result.status == scoring.REVIEW
    assert result.status != scoring.FAIL
    assert "BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE" in result.reason_codes


def test_a_gate_may_resolve_to_review_by_design():
    result = scoring.assess("BANK_STATEMENT", [
        unknown("readable", code="DOCUMENT_REQUIRES_OCR", gate=True,
                verdict=scoring.REVIEW),
    ])
    assert result.status == scoring.REVIEW


def test_a_bad_non_gate_check_reviews_rather_than_failing():
    """Only a gate may fail a document outright."""
    result = scoring.assess("PAN", [ok("a"), bad("b")])
    assert result.status == scoring.REVIEW


# ==========================================================================
# WEIGHTS ARE CONFIGURATION
# ==========================================================================

def test_weights_come_from_configuration_when_present(monkeypatch):
    from app.services import verification_config

    monkeypatch.setattr(
        verification_config, "scoring_config",
        lambda: {"documents": {
            "BANK_STATEMENT": {"weights": {"integrity": 9.0, "period": 1.0}}
        }},
    )

    result = scoring.assess("BANK_STATEMENT",
                            [ok("integrity"), unknown("period")])

    # 9 of 10 weight earned, not 1 of 2.
    assert result.score == 90


def test_a_type_with_no_configured_weights_still_works():
    """A verifier must work before anybody writes a weights block."""
    result = scoring.assess("SOMETHING_NEW", [ok("a", 3), unknown("b", 1)])
    assert result.score == 75


def test_not_applicable_checks_are_excluded_entirely():
    result = scoring.assess("PAN", [
        ok("a"),
        Check(name="b", outcome=Outcome.NOT_APPLICABLE, weight=99.0),
    ])
    assert result.score == 100


# ==========================================================================
# WHAT A CALLER IS TOLD
# ==========================================================================

def test_the_public_view_carries_no_internals():
    result = scoring.assess("PAN", [ok("a"), unknown("b", code="X")])
    published = result.public()

    assert set(published) == {
        "verification_score", "verification_confidence",
        "reason_codes", "reasons",
    }
    # Check names, weights and outcomes describe how the service is built.
    import json

    blob = json.dumps(published)
    assert "weight" not in blob
    assert "hard_gate" not in blob
