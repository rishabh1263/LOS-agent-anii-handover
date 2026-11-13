"""simple/pipeline: the fast lane answers only when the structural reader agrees with the question it would ask."""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.simple import pipeline


@pytest.mark.parametrize("message, canonical", [
    ("how many are pending", "How long has this case been in this stage?"),      # count of pending != time in stage
    ("why are they pending?", "Why is this case stuck?"),                        # blockers, not the case history
    ("बैंक स्टेटमेंट अपलोड हुआ क्या?", "What is pending?"),                         # the named document was lost
])
def test_a_wrong_reading_is_left_to_the_engine(message, canonical):
    assert pipeline.disagrees(message, canonical)


@pytest.mark.parametrize("message, canonical", [
    ("what is pending", "What is pending?"),
    ("kitne documents baaki hain", "What is pending?"),
    ("is this case ready for CPA?", "Is this case ready for CPA?"),
    ("has the PAN been uploaded?", "What is the status of the PAN?"),
    ("tenure kya hai", "what is the recorded tenure"),
])
def test_an_agreeing_reading_is_answered_here(message, canonical):
    assert not pipeline.disagrees(message, canonical)


def test_switched_off(monkeypatch):
    monkeypatch.setattr(pipeline, "cfg", lambda: {"agreement": {"enabled": False}})
    assert not pipeline.disagrees("how many are pending", "How long has this case been in this stage?")


def test_months_of_the_loan_is_the_tenure():
    from app.agents.applicant.copilot.answering import profile

    assert profile.detect("is case ka loan kitne mahine ka hai").fields == ["tenure_months"]
    assert profile.detect("loan kitna hai").fields == ["loan_amount"]


@pytest.mark.parametrize("message", ["ek sawal hai", "quick question", "no, I meant the tenure", "how many are pending",
                                     "why are they pending?"])
def test_deterministic_readers_are_sure(message):
    assert pipeline.structural_sure(message)


@pytest.mark.parametrize("message", ["more", "and?"])
def test_weak_readings_are_not_sure(message):
    assert not pipeline.structural_sure(message)


@pytest.mark.parametrize("message", ["what is the capital of France", "who won the cricket match", "write me a poem"])
def test_off_topic_is_never_sure_about_the_case(message):
    assert not pipeline.structural_sure(message)


def test_the_same_question_for_the_co_applicant(monkeypatch):
    monkeypatch.setattr(pipeline, "last_intent", lambda chat: "pending_documents")
    frame = pipeline.read("and for the co-applicant?", "chat-1")
    assert frame is not None and "co-applicant" in frame.canonical.lower() and frame.by == "followup"
    monkeypatch.setattr(pipeline, "last_intent", lambda chat: None)
    assert pipeline.read("and for the co-applicant?", "chat-1") is None or \
        pipeline.read("and for the co-applicant?", "chat-1").by != "followup"
