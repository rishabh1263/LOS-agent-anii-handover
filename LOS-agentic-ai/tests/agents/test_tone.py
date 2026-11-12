"""
THE TONE MODEL (2026-11-06, semantics/tone.py): abuse is LEARNED from copilot_tone.yaml, not listed -- "hatt saale?"
got the options menu because no listed word was in it. The suite runs without Ollama, so this is the n-gram half of
the model file; the boundary needs a SURE reading, so an ordinary question is never refused on a guess.
"""

from __future__ import annotations

import pytest
import yaml

from app.agents.applicant.copilot.capabilities import abuse_guard
from app.agents.applicant.copilot.semantics import tone


@pytest.mark.parametrize("message", ["bhai pending kya hai", "what is the status of this case", "hello",
                                     "kill the old request and start again", "why is it pending?",
                                     "the applicant's name is Saleem"])
def test_ordinary_messages_are_never_refused(message, monkeypatch):
    monkeypatch.setenv("COPILOT_ABUSE_GUARD", "true")          # the suite pins the guard off; on, as in the service
    assert abuse_guard.screen(message, "tone-test-" + message, "req") is None


def test_a_sure_abusive_reading_gets_the_boundary_without_naming_a_word(monkeypatch):
    monkeypatch.setenv("COPILOT_ABUSE_GUARD", "true")
    monkeypatch.setattr(tone, "is_abusive", lambda m: True)
    reply = abuse_guard.screen("hatt saale?", "tone-test-boundary", "req")
    assert reply is not None and reply["intent"] == "ABUSIVE" and "**" not in reply["answer"]
    assert reply["tools_invoked"] == [] and reply["guardrail"]["category"] == "ABUSE"


def test_no_model_file_leaves_the_lexicon_alone(monkeypatch, tmp_path):
    cfg = dict(tone._cfg())
    cfg["model"] = {**cfg.get("model", {}), "path": str(tmp_path / "missing.npz")}
    monkeypatch.setattr(tone, "_cfg", lambda: cfg)
    monkeypatch.setattr(tone, "_MODEL", {"mtime": None, "model": None})
    monkeypatch.setattr(tone, "_CACHE", {})
    assert tone.classify("hatt saale?") is None and tone.is_abusive("hatt saale?") is False


def test_the_held_out_set_is_never_trained_on():
    held = yaml.safe_load(open(tone.ROOT / "evals/copilot/tone_heldout.yaml", encoding="utf-8"))["items"]
    trained = {tone.clean(t) for t, _ in tone.examples()}
    assert not [i["q"] for i in held if tone.clean(i["q"]) in trained]
