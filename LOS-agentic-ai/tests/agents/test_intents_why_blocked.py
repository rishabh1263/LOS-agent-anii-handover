"""
WHY A CASE CAN'T GO TO A STAGE (2026-11-04, owner's chat): "Why this case cant be move to cpa?" was answered "The
application has not been at the CPA stage" -- the stage-history rule took "move to cpa". A WHY + a NOT + a MOVE about a
named stage asks what blocks it (READINESS), in broken English and Hinglish too; the stage questions keep their route.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.semantics import intents
from app.agents.applicant.copilot.semantics.intents import Intent


@pytest.mark.parametrize("message", [
    "Why this case cant be move to cpa?",
    "why can't this case move to CPA?",
    "why this case cannot move to cpa",
    "why is my case not moving to credit",
    "case cpa me kyu nahi ja raha",
    "cpa me kyun nahi gaya ye case",
    "ye case cpa kyu nahi bhej sakte",
])
def test_why_it_cannot_go_to_a_stage_is_what_blocks_it(message):
    c = intents.classify(message)
    assert c.intent is Intent.READINESS and c.matched_on == "why_blocked_from_stage", c


@pytest.mark.parametrize("message,intent", [
    ("when did it move to CPA?", Intent.APPLICATION_STAGE),
    ("why did the case go to RCU?", Intent.APPLICATION_STAGE),
    ("why is my case in RCU?", Intent.APPLICATION_STAGE),
    ("which stage is this case in?", Intent.APPLICATION_STAGE),
    ("what happens at CPA stage?", Intent.STAGE_PROCESS),
    ("is this case ready for CPA?", Intent.READINESS),
])
def test_the_stage_questions_keep_their_route(message, intent):
    assert intents.classify(message).intent is intent


@pytest.mark.parametrize("message", [
    "but after doing all this case can be move to cpa?",
    "once these are uploaded will it go to cpa",
    "ye sab upload karne ke baad cpa ja sakta hai?",
    "if I upload all this can it move to credit",
])
def test_once_done_can_it_go_is_a_readiness_question_for_this_case(message):
    c = intents.classify(message)
    assert c.intent is Intent.READINESS and c.matched_on == "once_done_to_stage", c


def test_portfolio_questions_stay_portfolio():
    assert intents.classify("how many cases are in CPA").intent is not Intent.READINESS


@pytest.mark.parametrize("message", [
    "but after doing all this case can be move to cpa?",
    "ye sab upload karne ke baad cpa ja sakta hai?",
    "Why this case cant be move to cpa?",
    "case cpa me kyu nahi ja raha",
])
def test_the_semantic_frame_does_not_take_them_back(message):
    # the full understanding (frame included) keeps the readiness route -- the live chat took the frame's NEXT_ACTION
    assert intents.understand(message, has_case=True).intent is Intent.READINESS


# "Why it is pending?" got the case timeline and "no individual findings" (owner, 2026-11-06): why the CASE is
# pending is its readiness blockers; a review state keeps the recorded reason; a document keeps its own status
@pytest.mark.parametrize("message", ["Why it is pending?", "why is my case pending", "case pending kyu hai"])
def test_why_the_case_is_pending_is_its_blockers(message):
    c = intents.understand(message, has_case=True)
    assert c.intent is Intent.READINESS and c.matched_on == "why_pending", c


@pytest.mark.parametrize("message,intent", [
    ("Why is my application under review?", Intent.CASE_HISTORY),
    ("why are my documents still pending?", Intent.DOCUMENTS_PENDING),
    ("why is my PAN pending", Intent.DOCUMENT_VERIFICATION),
])
def test_review_and_document_questions_keep_their_route(message, intent):
    assert intents.understand(message, has_case=True).intent is intent
