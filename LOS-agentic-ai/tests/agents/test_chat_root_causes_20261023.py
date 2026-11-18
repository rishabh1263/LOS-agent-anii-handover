"""Live English eval review, 2026-10-23: each root cause behind a wrong (or right-by-luck) answer, pinned."""
from __future__ import annotations


def test_a_pronoun_one_is_not_a_list_size():
    from app.agents.applicant.copilot.capabilities import case_list

    assert case_list.understand("which one should i do first") is None        # was "show the first 1 case"
    assert case_list.understand("show top two cases").size == 2               # a number word beside a case noun
    assert case_list.understand("top 3").size == 3                            # digits unchanged


def test_one_named_document_asks_what_it_accepts():
    from app.agents.applicant.copilot.semantics import semantic_frame

    for said in ("what can he give for address proof", "what documents can he give for address proof"):
        assert semantic_frame.route(semantic_frame.parse(said)) == "FOS_KNOWLEDGE"


def test_who_works_a_named_stage_is_the_stage_guide():
    from app.agents.applicant.copilot.semantics import intents

    assert intents.classify("who handles cpa?").intent is intents.Intent.STAGE_PROCESS
    assert intents.classify("who is the applicant").intent is not intents.Intent.STAGE_PROCESS


def test_a_document_ordinal_is_the_listed_document():
    from app.agents.applicant.copilot.conversation.followup import ordinal_document

    listed = ["ADDRESS_PROOF", "SIGNATURE"]
    assert ordinal_document("what documents can he give for the first one", listed) \
        == "what documents can he give for address proof"
    assert ordinal_document("and the last one?", listed) == "and signature?"
    assert ordinal_document("show the first case", listed) is None             # a case ordinal is the case list's
    assert ordinal_document("the first one", ["ADDRESS_PROOF"]) is None         # one listed document is "it"


def test_time_phrases_ignore_determiners():
    from app.agents.applicant.copilot.capabilities import timeline

    assert timeline.asked("when was this case created") == "CREATED"
    assert timeline.asked("what stage is it in and since when") == "DAYS"
    assert timeline.asked("kal wala verify hua?") == "DAY:-1"
