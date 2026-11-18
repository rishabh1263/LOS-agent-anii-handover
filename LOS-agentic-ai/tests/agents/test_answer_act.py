"""
THE ANSWER ACT (2026-11-04, semantics/answer_act.py): what KIND of answer a question wants, learned from
copilot_answer_acts.yaml (models/answer_act.npz). The suite runs without Ollama, so this is the n-gram half of the
model -- the same file, deterministic.
"""

from __future__ import annotations

import pytest
import yaml

from app.agents.applicant.copilot.semantics import answer_act


@pytest.mark.parametrize("message,act", [
    ("but after doing all this case can be move to cpa?", "CONDITIONAL"),
    ("ye sab upload karne ke baad cpa ja sakta hai?", "CONDITIONAL"),
    ("Why this case cant be move to cpa?", "WHY_NOT"),
    ("case cpa me kyu nahi ja raha", "WHY_NOT"),
    ("why was the PAN rejected?", "WHY"),
    ("is the PAN uploaded?", "YES_NO"),
    ("how many documents are uploaded?", "COUNT"),
    ("which documents are pending?", "LIST"),
    ("how do I add a co-applicant?", "HOW"),
    ("what is FOIR?", "DEFINE"),
])
def test_the_model_reads_the_kind_of_question(message, act):
    got = answer_act.classify(message)
    assert got is not None and got.name == act and got.sure, got


def test_no_model_file_is_no_act(monkeypatch, tmp_path):
    cfg = dict(answer_act._cfg())
    cfg["model"] = {**cfg.get("model", {}), "path": str(tmp_path / "missing.npz")}
    monkeypatch.setattr(answer_act, "_cfg", lambda: cfg)
    monkeypatch.setattr(answer_act, "_MODEL", {"mtime": None, "model": None})
    monkeypatch.setattr(answer_act, "_CACHE", {})
    assert answer_act.classify("is the PAN uploaded?") is None and answer_act.sure_act("x") is None


def test_the_held_out_set_is_never_trained_on():
    held = yaml.safe_load(open(answer_act.ROOT / "evals/copilot/answer_act_heldout.yaml", encoding="utf-8"))["items"]
    trained = {answer_act.clean(t) for t, _ in answer_act.examples()}
    assert not [i["q"] for i in held if answer_act.clean(i["q"]) in trained]


# "i have one more query to ask?" got the clarify menu (owner, 2026-11-04): a question announced, nothing asked yet,
# is the learned act PREFACE -> "Sure, go ahead" in the officer's language; a real question is never swallowed.
@pytest.mark.parametrize("message", ["i have one more query to ask?", "ek aur sawal hai", "can I ask something"])
def test_a_question_announced_is_answered_go_ahead(message):
    from app.agents.applicant import conversation

    turn = conversation.classify(message)
    assert turn is not None and turn.kind == conversation.PREFACE
    text, _ = conversation.reply(turn.kind, turn.language)
    assert "?" in text and ("ahead" in text.lower() or "poochhiye" in text.lower())


@pytest.mark.parametrize("message", ["I have a question about PAN status", "what is pending", "why is KYC on hold"])
def test_a_real_question_is_never_taken_as_a_preface(message):
    from app.agents.applicant import conversation

    turn = conversation.classify(message)
    assert turn is None or turn.kind != conversation.PREFACE
