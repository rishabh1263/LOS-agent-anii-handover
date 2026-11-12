"""
"OK AND AFTER THAT?" RIGHT AFTER AN "after <stage>" ANSWER (2026-10-14 English pass): the stage after the one that
answer named first -- never the case's next step. From config stage_sequence, no model.
"""

from __future__ import annotations

from app.agents.applicant.copilot.capabilities import general


def _ask(message: str, who: str = "officer-after-stage") -> str | None:
    reply = general.direct(message, {"sub": who}, None, "r", "en", case_in_scope="CASE-1")
    return reply and reply["answer"]


def test_after_that_steps_on_one_stage_at_a_time():
    assert _ask("what comes after cpa?").startswith("After CPA comes Credit")
    assert _ask("ok and after that?").startswith("After Credit comes RCU")
    assert _ask("then what").startswith("After RCU comes BOPS")
    assert _ask("what comes after hops?").startswith("After HOPS comes Disbursement")
    assert _ask("and after that?").startswith("Disbursement is the last stage")


def test_after_that_with_no_stage_answer_before_it_is_not_a_stage_answer():
    assert _ask("ok and after that?", who="officer-fresh") is None


def test_what_to_do_after_that_is_never_the_stage_order():
    _ask("what comes after cpa?", who="officer-do")
    assert _ask("iske baad kya karna hai?", who="officer-do") is None
