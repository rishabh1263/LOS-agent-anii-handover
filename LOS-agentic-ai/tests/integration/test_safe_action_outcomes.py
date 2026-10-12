"""
A confirmed action is reported as the STORE shows it (master prompt §5, 2026-10-10): success only when read back,
a timeout reconciled from the stored stage, an unreadable outcome said to be unknown, a second click of the same
Confirm never acting twice. Synthetic applicants; the gate is forced open as in test_master_15b_stage_flow.
"""
from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_master_15b_stage_flow import FOS, gate_passes, link, stage_of, turn  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401


@pytest.fixture
def proposal(client, prod, gate_passes, monkeypatch):
    from app.agents.applicant.copilot.capabilities import stage_flow

    monkeypatch.setattr(stage_flow, "problems", lambda case_id, lang: [])
    _, case_id = make_case(client, "Priya Verma")
    turn(client, f"open {case_id}", chat="sa")
    md = turn(client, "Move to CPA", chat="sa")
    assert "Move it to CPA?" in md
    return case_id, md


def test_a_timeout_after_the_move_committed_is_reported_as_moved(client, proposal, monkeypatch):
    from app.agents.los import stage_lifecycle

    case_id, md = proposal
    real = stage_lifecycle.transition

    def committed_then_timed_out(*args, **kwargs):
        real(*args, **kwargs)
        raise TimeoutError("connection dropped after COMMIT")

    monkeypatch.setattr(stage_lifecycle, "transition", committed_then_timed_out)
    moved = link(client, md, "Confirm", chat="sa")
    assert "moved from FOS" in moved and stage_of(case_id) == "CPA", moved


def test_a_timeout_before_the_move_is_reported_as_not_moved(client, proposal, monkeypatch):
    from app.agents.los import stage_lifecycle

    case_id, md = proposal

    def timed_out(*args, **kwargs):
        raise TimeoutError("no answer from the store")

    monkeypatch.setattr(stage_lifecycle, "transition", timed_out)
    answer = link(client, md, "Confirm", chat="sa")
    assert "was not moved" in answer and "moved from" not in answer and stage_of(case_id) == "FOS", answer


def test_an_outcome_that_cannot_be_read_back_is_unknown_never_success(client, proposal, monkeypatch):
    from app.agents.los import stage_lifecycle, stages

    case_id, md = proposal

    def timed_out(*args, **kwargs):
        raise TimeoutError("no answer from the store")

    def unreadable(*args, **kwargs):
        raise ConnectionError("store unreachable")

    monkeypatch.setattr(stage_lifecycle, "transition", timed_out)
    monkeypatch.setattr(stages, "resolve", unreadable)
    answer = link(client, md, "Confirm", chat="sa")
    assert "could not confirm" in answer and "moved from" not in answer, answer


def test_a_second_click_of_the_same_confirm_does_not_act_twice(client, proposal, monkeypatch):
    from app.agents.los import stage_lifecycle

    case_id, md = proposal
    calls = []
    real = stage_lifecycle.transition

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(stage_lifecycle, "transition", counted)
    first = link(client, md, "Confirm", chat="sa")
    second = link(client, md, "Confirm", chat="sa")
    assert "moved from FOS" in first and len(calls) == 1, (first, second)
    assert "no longer pending" in second.replace("**", "") and "moved from" not in second, second
    assert stage_of(case_id) == "CPA"


def test_an_edit_whose_value_does_not_read_back_is_not_claimed(client, prod, monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_form

    _, case_id = make_case(client, "Rahul Sharma")
    turn(client, f"open {case_id}", chat="ev")
    proposal = turn(client, "loan amount 600000 karo", chat="ev")
    real_values = case_form.values_of
    monkeypatch.setattr(case_form, "save", lambda *a, **k: {"outcome": "SAVED", "changed": {}, "current": {}})
    monkeypatch.setattr(case_form, "values_of", lambda cid: {**real_values(cid)})   # the write never happened
    answer = link(client, proposal, "Confirm", chat="ev")
    assert "could not confirm" in answer and "Updated" not in answer, answer
