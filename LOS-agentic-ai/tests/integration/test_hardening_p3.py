"""
HARDENING PHASE 3 (2026-10-12): portfolio questions, follow-ups after a filtered list, greetings / farewells vs action
requests, LOS data vs handbook routing. Offline: where a semantic reading is needed it is GIVEN (fos_api._understood
patched); the live readings are measured by tests/integration/test_live_task_eval.py (dev2 / held-out C).
"""
from __future__ import annotations

import re

import pytest

from app.agents.applicant.copilot.semantics.meaning import Decision
from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401

_ID = re.compile(r"CASE-[0-9A-F]{6,}")


def say(client, text, chat, **extra):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text, "reply_language": "en",
                                                  "chat_id": chat, **extra})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def reading(monkeypatch):
    from app.api.routes import fos_api

    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    decisions: dict[str, Decision] = {}
    real = fos_api._understood

    async def understood(message, claims, payload, request_id):
        if message in decisions:
            return decisions[message]
        if message.startswith("__real__"):
            return await real(message[8:], claims, payload, request_id)
        return None
    monkeypatch.setattr(fos_api, "_understood", understood)
    return decisions


@pytest.fixture
def spies(monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_list, product_flow

    reads = []
    real_run, real_notes = case_list.run, product_flow.notifications
    monkeypatch.setattr(case_list, "run", lambda *a, **k: reads.append("case_list.run") or real_run(*a, **k))
    monkeypatch.setattr(product_flow, "notifications",
                        lambda *a, **k: reads.append("notifications") or real_notes(*a, **k))
    return reads


# ---- unit: the helpers ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("typed, fixed", [("show my oldst cases", "show my oldest cases"),
                                          ("show my pendng cases", "show my pending cases"),
                                          ("hellow", "hello")])
def test_a_typo_of_a_known_list_word_is_corrected(typed, fixed):
    from app.agents.applicant.copilot.semantics import task

    assert task.respelled(typed) == fixed


@pytest.mark.parametrize("q", ["priya sharma status", "take care", "take care of this", "thanks for the care"])
def test_a_name_or_a_real_word_is_never_corrected(q):
    """Only a MISSING / EXTRA letter towards a 5+ letter list word: "take care" once became "atke case" (stuck list)."""
    from app.agents.applicant.copilot.semantics import task

    assert task.respelled(q) == q


@pytest.mark.parametrize("q, yes", [("which applications can go to CPA now?", True),
                                    ("which of my customers still owe me documents?", True),
                                    ("show my cases", False), ("move to CPA", False),
                                    ("is this case ready for CPA?", False), ("which CASE-ABCDEF123456 files", False)])
def test_a_selection_question_over_the_cases(q, yes):
    from app.agents.applicant.copilot.semantics import task

    assert task.asks_over_cases(q) is yes


@pytest.mark.parametrize("q, kind", [("take care", "GOODBYE"), ("take care bhai", "GOODBYE"),
                                     ("namaste ji", "GREETING"), ("ram ram bhai", "GREETING"),
                                     ("helo there", "GREETING"), ("take care of this", None),
                                     ("what is pending", None)])
def test_social_messages_are_read_whole(q, kind):
    from app.agents.applicant.copilot.semantics import task

    assert task.social_kind(q) == kind


@pytest.mark.parametrize("q, ok", [("take care of this", False), ("isko kar do", False), ("thanks", True)])
def test_an_object_to_act_on_is_not_an_acknowledgement(q, ok):
    from app.agents.applicant.copilot.semantics import task

    assert task.plain_social(q) is ok


# ---- P1 portfolio -----------------------------------------------------------------------------------------------
def test_a_selection_question_is_answered_over_own_cases_never_which_case(client, other, prod, reading):
    [make_case(client, n) for n in ("Hp Sel One", "Hp Sel Two")]
    foreign = make_case(other, "Hp Sel Foreign")[1]
    q = "which applications can go to CPA now?"
    reading[q] = Decision(kind="case", intent="cpa_readiness", canonical="Is this case ready for CPA?", score=0.9)
    body = say(client, q, "hp-sel")
    md = body["markdown"]
    assert "Which case is this about" not in md and foreign not in md, md
    assert "ready for CPA" in re.sub(r"\*", "", md), md
    assert body["diagnostics"]["task"]["scope"] == "portfolio", body["diagnostics"]


def test_a_typo_list_request_lists_own_cases(client, other, prod, reading):
    mine = [make_case(client, n)[1] for n in ("Hp Typo One", "Hp Typo Two")]
    foreign = make_case(other, "Hp Typo Foreign")[1]
    md = say(client, "show my oldst cases", "hp-typo")["markdown"]
    # the OLDEST first: in a shared store the cases just made are the newest, so they need not be on page 1 -- the
    # list must be the caller's own, sorted oldest first (the typo understood)
    shown = _ID.findall(md)
    assert "Which case is this about" not in md and foreign not in md and shown and "oldest" in md, md
    assert len(mine) == 2


def test_a_one_case_question_with_a_case_open_stays_one_case(client, prod, reading):
    case = make_case(client, "Hp One Case")[1]
    say(client, f"open {case}", "hp-one")
    q = "is this case ready for CPA?"
    reading[q] = Decision(kind="case", intent="cpa_readiness", canonical="Is this case ready for CPA?", score=0.9)
    d = say(client, q, "hp-one")["diagnostics"]
    assert d["task"]["scope"] == "one_case" and d["layer"] != "case_insights(meaning)", d


# ---- P2 follow-up after a filtered list -------------------------------------------------------------------------
def test_the_ones_with_documents_pending_after_a_stuck_list_is_that_filtered_list(client, other, prod, reading):
    mine = [make_case(client, n)[1] for n in ("Hp Fu One", "Hp Fu Two", "Hp Fu Three")]
    foreign = make_case(other, "Hp Fu Foreign")[1]
    say(client, "show my cases", "hp-fu")
    say(client, "show the stuck cases", "hp-fu")
    body = say(client, "and the ones with documents pending?", "hp-fu")
    md = re.sub(r"\s*\([A-Z][a-z][^()]*\)|\*", "", body["markdown"])
    assert "documents pending" in md and foreign not in md and any(i in md for i in mine), md
    task = body["diagnostics"]["task"]
    assert (task["scope"], task["operation"], task["filters"]) == ("portfolio", "list", ["documents_pending"]), task


def test_after_a_list_a_new_question_is_not_a_filter(client, prod, reading):
    [make_case(client, n) for n in ("Hp New One", "Hp New Two")]
    say(client, "show my cases", "hp-new")
    d = say(client, "What documents are mandatory?", "hp-new")["diagnostics"]
    assert (d["task"] or {}).get("references", {}).get("kind") != "previous_task", d


def test_an_ordinal_after_the_filtered_list_is_a_row_of_it(client, prod, reading):
    [make_case(client, n) for n in ("Hp Ord One", "Hp Ord Two", "Hp Ord Three")]
    say(client, "show my cases", "hp-ord")
    listed = list(dict.fromkeys(_ID.findall(say(client, "and the ones with documents pending?", "hp-ord")["markdown"])))
    assert len(listed) >= 2, listed
    md = say(client, "open the second one", "hp-ord")["markdown"]
    assert md.lstrip().startswith(listed[1]), (listed, md[:120])


# ---- P3 greetings / farewell / action request -------------------------------------------------------------------
@pytest.mark.parametrize("q", ["namaste ji", "ram ram bhai", "helo there"])
def test_indian_and_typo_greetings_are_a_hello_and_read_no_case(client, prod, reading, spies, q):
    make_case(client, "Hp Greet One")
    spies.clear()
    body = say(client, q, f"hp-g-{len(q)}")
    assert body["markdown"].split()[0].rstrip("!.,?") in ("Hello", "Hi", "Good", "Namaste"), body["markdown"]
    assert spies == [] and body["diagnostics"]["tools"] == [], (spies, body["diagnostics"])


def test_take_care_is_a_farewell_never_options(client, prod, reading, spies):
    case = make_case(client, "Hp Bye One")[1]
    say(client, f"open {case}", "hp-bye")
    spies.clear()
    body = say(client, "take care", "hp-bye")
    assert body["diagnostics"]["intent"] == "GOODBYE" and spies == [], (body["diagnostics"], body["markdown"])


def test_take_care_of_this_asks_which_action(client, prod, reading):
    case = make_case(client, "Hp Act One")[1]
    say(client, f"open {case}", "hp-act")
    q = "take care of this"
    reading[q] = Decision(kind="ack", intent="acknowledgement", canonical="ok", score=0.6, decided_by="model",
                          candidates=[("ambiguous_action", 0.617), ("next_step", 0.593), ("acknowledgement", 0.565)])
    d = say(client, q, "hp-act")["diagnostics"]
    assert d["intent"] == "WHICH_ACTION", d
    assert (d["task"]["scope"], d["task"]["operation"]) == ("none", "clarify"), d["task"]


# ---- P4 LOS data vs handbook ------------------------------------------------------------------------------------
def test_after_cpa_with_a_case_open_is_the_workflow_answer(client, prod, reading):
    case = make_case(client, "Hp Flow One")[1]
    say(client, f"open {case}", "hp-flow")
    q = "What happens after a case goes to CPA?"
    reading[q] = Decision(kind="process", intent="process_stages", canonical="what are the stages of a loan",
                          score=0.94)
    body = say(client, q, "hp-flow")
    d = body["diagnostics"]
    # the APPROVED workflow answer (general layer or the engine's FOS knowledge), never the case's own data
    assert d["task"]["scope"] == "knowledge" and d["response_source"] in ("GENERAL", "KNOWLEDGE"), d
    assert d["query_type"] in ("PROCESS_KNOWLEDGE", "CONVERSATION") and "Credit" in body["markdown"], body["markdown"]


def test_a_document_count_is_counted_from_own_document_views(client, other, prod, reading):
    mine = [make_case(client, n)[1] for n in ("Hp Doc One", "Hp Doc Two")]
    foreign = make_case(other, "Hp Doc Foreign")[1]
    body = say(client, "How many applications are missing the bank statement?", "hp-doc")
    md = body["markdown"].replace("*", "")
    assert re.search(r"\b\d+ of your \d+ cases: Bank Statement still pending", md), md
    assert foreign not in md and any(i in md for i in mine), md
    assert body["diagnostics"]["tools"] == ["case.list", "documents.get"], body["diagnostics"]


def test_a_document_question_on_one_case_is_not_a_count(client, prod, reading):
    case = make_case(client, "Hp Doc Single")[1]
    say(client, f"open {case}", "hp-docs")
    md = say(client, "is the bank statement missing?", "hp-docs")["markdown"]
    assert not re.search(r"\b\d+ of your \d+ cases\b", md), md


@pytest.mark.parametrize("q, kind, portfolio", [
    ("which of my customers still owe me documents?", None, True),
    ("which of my customers still owe me documents?", "knowledge", True),       # the officer's OWN cases
    ("which documents do customers need for a home loan?", "knowledge", False),  # a handbook question stays one
    ("what documents are required for applications?", None, False),             # no selection over the cases
    ("which documents are pending for this application?", "case", False),       # ONE case pointed at
])
def test_portfolio_scope_on_selection_questions(q, kind, portfolio):
    from app.agents.applicant.copilot.semantics import meaning

    d = Decision(kind=kind, intent="knowledge_documents" if kind == "knowledge" else "pending_documents",
                 score=0.8) if kind else None
    assert (meaning.plural_scope(q, d) is not None) is portfolio


def test_an_acknowledgement_is_never_skipped_as_social():
    """Held-out C regression (cA2 "thik hai ji" -> NOT_IN_SCOPE): only GREETING / GOODBYE bypass meaning."""
    from app.agents.applicant.copilot.semantics import task

    assert task.social_kind("thik hai ji") is None and task.social_kind("thanks a lot") is None
    assert task.social_kind("namaste ji") == "GREETING" and task.social_kind("take care") == "GOODBYE"
