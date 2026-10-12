"""
"case" ALONE ON AN OPEN CASE is answered with its status, as "Case?" already was (owner rule: one word on an open case
is answered, 2026-10-11); the options an officer is shown are questions, never an intent's internal name.
The meaning model is replaced by its live reading of "case" (the status topic); no data is read.
"""
from types import SimpleNamespace

import pytest

from app.agents.applicant.copilot.capabilities import general, vague
from app.agents.applicant.copilot.semantics import meaning


@pytest.fixture
def reads_as_status(monkeypatch):
    # the LIVE nomic ranking of "case" (2026-10-11): case_history first, the status topic third
    monkeypatch.setattr(meaning, "rank", lambda message: [("case_history", 0.809), ("ambiguous_action", 0.782),
                                                          ("case_status", 0.773), ("applicant_name", 0.753)])


@pytest.mark.parametrize("word", ["case", "Case", "file", "application"])
def test_one_word_on_an_open_case_is_its_status(reads_as_status, word):
    state = SimpleNamespace(flow={})
    assert vague.contextual(word, state, case_open=True) == "What is the status of this case?"


def test_no_case_open_still_asks(reads_as_status):
    assert vague.contextual("case", SimpleNamespace(flow={}), case_open=False) is None


def test_an_unrelated_word_of_the_topic_still_gets_options(reads_as_status):
    # "details" reads into the same topic but is not one of its own words: asked, never guessed
    assert vague.contextual("details", SimpleNamespace(flow={}), case_open=True) is None


def test_the_options_shown_are_questions_not_internal_names():
    for topic in (general.cfg().get("vague") or {}).get("case_topics") or []:
        for option in topic.get("options") or []:
            assert option[:1].isupper() and option.rstrip().endswith("?"), option
