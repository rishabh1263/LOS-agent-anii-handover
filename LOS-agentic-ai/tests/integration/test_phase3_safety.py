"""
PHASE 3 SAFETY-FIRST HARDENING (2026-10-13). Written BEFORE the fixes they cover:

* the borrower guardrail: "what should I do next with THIS borrower" is the open case's own borrower (allowed only with
  an authorized case open); "next customer's PAN", "previous borrower's address", "the last borrower" stay blocked
  whether a case is open or not;
* farewells / greetings before fuzzy case-name matching: "bye" never opens a case whose name contains "Bye";
* the remaining semantic misses (portfolio "haven't sent their papers", CPA eligibility, workflow vs case readiness,
  unspecified actions, Sat Sri Akal, FOIR on "this case").
Offline: where a semantic reading is needed it is GIVEN (fos_api._understood patched).
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

    async def understood(message, claims, payload, request_id):
        return decisions.get(message)
    monkeypatch.setattr(fos_api, "_understood", understood)
    return decisions


@pytest.fixture
def lookups(monkeypatch):
    """Every case lookup / read the turn makes (name match, list, a case's documents)."""
    from app.agents.applicant.copilot.capabilities import case_list, workspace

    seen = []
    for module, name in ((workspace, "_by_name"), (workspace, "my_cases"), (case_list, "run")):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, (lambda real, name: lambda *a, **k: seen.append(name) or real(*a, **k))(
            real, name))
    return seen


def blocked(body) -> bool:
    return (body.get("diagnostics") or {}).get("intent") == "GUARDRAIL_BLOCKED" or "GUARDRAIL" in str(
        body.get("response_source") or body.get("diagnostics", {}).get("response_source") or "")


# ---- B. the borrower guardrail: the text rule ------------------------------------------------------------------
@pytest.mark.parametrize("q", ["what should I do next with this borrower?", "what should I be doing next for this borrower",
                               "what's next for my customer?", "next steps for our applicant"])
def test_the_current_party_reads_as_the_case(q):
    from app.security import request_policy

    neutral = request_policy.current_party_neutral(q)
    assert neutral and "this case" in neutral and not re.search(r"borrower|customer|applicant", neutral), neutral


@pytest.mark.parametrize("q", [
    "give me the next customer's PAN", "what was the previous borrower's address?", "tell me about the last borrower",
    "show another customer's mobile number", "my next borrower's details", "what should I do with the next borrower",
    "what should I do next for this borrower and show the previous borrower's PAN",
    "next for this borrower, then the next customer's address", "what's next for these borrowers",
])
def test_another_person_is_never_the_current_party(q):
    from app.security import guardrails, request_policy

    neutral = request_policy.current_party_neutral(q)
    assert neutral is None or not guardrails.check_input(neutral).allowed, neutral


# ---- B. the borrower guardrail: through the API ----------------------------------------------------------------
@pytest.mark.parametrize("q", ["give me the next customer's PAN", "what was the previous borrower's address?",
                               "tell me about the last borrower", "show another customer's mobile number",
                               "what should I do next for this borrower and show the previous borrower's PAN"])
@pytest.mark.parametrize("open_case", [True, False])
def test_other_people_stay_blocked_open_case_or_not(client, prod, reading, q, open_case):
    chat = f"ps-x-{open_case}-{abs(hash(q)) % 10**6}"
    if open_case:
        say(client, f"open {make_case(client, 'Ps Guard Open')[1]}", chat)
    body = say(client, q, chat)
    assert blocked(body), (q, body.get("diagnostics"), body["markdown"][:200])


def test_this_borrower_with_an_open_case_is_that_case(client, other, prod, reading):
    case = make_case(client, "Ps Guard Mine")[1]
    foreign = make_case(other, "Ps Guard Foreign")[1]
    say(client, f"open {case}", "ps-own")
    body = say(client, "what should I do next with this borrower?", "ps-own")
    assert not blocked(body), (body.get("diagnostics"), body["markdown"][:300])
    assert foreign not in body["markdown"]
    d = body["diagnostics"] or {}
    assert d.get("case_in_scope") is True and d.get("intent") == "NEXT_ACTION", d


def test_this_borrower_with_no_case_open_stays_blocked(client, prod, reading):
    make_case(client, "Ps Guard Closed")
    assert blocked(say(client, "what should I do next with this borrower?", "ps-closed"))


def test_this_borrower_on_someone_elses_case_id_stays_blocked(client, other, prod, reading):
    """A case id the caller does not hold is no authorized context: the exception never applies."""
    foreign = make_case(other, "Ps Guard Theirs")[1]
    body = say(client, "what should I do next with this borrower?", "ps-theirs", case_id=foreign)
    assert foreign not in body["markdown"]
    assert blocked(body) or body.get("intent") in ("ACCESS_DENIED",) or "not" in body["markdown"].lower(), body


# ---- A. farewell vs a case whose name contains "Bye" -----------------------------------------------------------
@pytest.mark.parametrize("q", ["bye", "Bye!", "ok bye", "bye bye", "goodbye"])
def test_a_farewell_never_opens_a_case_named_bye(client, prod, reading, lookups, q):
    case = make_case(client, "Rohan Bye")[1]
    lookups.clear()
    body = say(client, q, f"ps-bye-{abs(hash(q)) % 10**6}")
    assert case not in body["markdown"] and (body["diagnostics"] or {}).get("intent") == "GOODBYE", body
    assert lookups == [] and body["diagnostics"]["tools"] == [], (lookups, body["diagnostics"])


@pytest.mark.parametrize("q", ["hi", "thanks", "namaste"])
def test_a_greeting_or_thanks_never_opens_a_case_named_like_it(client, prod, reading, lookups, q):
    case = make_case(client, f"Kiran {q.title()}")[1]
    lookups.clear()
    body = say(client, q, f"ps-hi-{q}")
    assert case not in body["markdown"] and lookups == [], (lookups, body["markdown"][:200])


def test_opening_a_case_named_bye_explicitly_still_works(client, prod, reading):
    case = make_case(client, "Rohan Bye")[1]
    md = say(client, "open Rohan Bye", "ps-bye-open")["markdown"]
    assert case in md or "Rohan Bye" in md, md


# ---- C. remaining semantic misses (dev3 sentences; written BEFORE the fixes) -------------------------------------
@pytest.fixture
def reads(monkeypatch):
    """Case-data reads of a turn (the list, notifications, a case's documents)."""
    from app.agents.applicant.copilot.capabilities import case_list, product_flow, workspace

    seen = []
    for module, name in ((case_list, "run"), (product_flow, "notifications"), (workspace, "my_cases")):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, (lambda real, name: lambda *a, **k: seen.append(name) or real(*a, **k))(
            real, name))
    return seen


def test_people_nouns_in_a_selection_question_are_the_portfolio():
    """"which BORROWERS of mine haven't sent their papers" -- the selection noun list and the plural-scope noun list
    were two vocabularies; borrowers / clients counted for one only."""
    from app.agents.applicant.copilot.semantics import meaning

    d = meaning.plural_scope("Which borrowers of mine haven't sent their papers yet?", None)
    assert d is not None and d.canonical == "show documents pending cases", d


def test_a_borrower_selection_question_lists_own_cases_with_documents_pending(client, other, prod, reading):
    mine = [make_case(client, n)[1] for n in ("Ps Paper One", "Ps Paper Two")]
    foreign = make_case(other, "Ps Paper Foreign")[1]
    body = say(client, "Which borrowers of mine haven't sent their papers yet?", "ps-papers")
    md = re.sub(r"\s*\([A-Z][a-z][^()]*\)|\*", "", body["markdown"])
    assert foreign not in md and any(i in md for i in mine) and "documents pending" in md, md
    task = body["diagnostics"]["task"]
    assert (task["scope"], task["operation"]) == ("portfolio", "list"), task


@pytest.mark.parametrize("intent, key", [("cpa_readiness", "ready_for_cpa"), ("kyc_status", "kyc_issue"),
                                         ("pending_documents", "documents_pending"), ("why_stuck", "stuck")])
def test_the_status_reading_names_the_filter_when_the_words_do_not(intent, key):
    """"which applications are ELIGIBLE to move to CPA?" -- no filter word; the CPA-readiness READING is the filter
    (never the generic pending list)."""
    from app.agents.applicant.copilot.semantics import meaning, task

    d = meaning.plural_scope("Which applications are eligible to move to CPA?",
                             Decision(kind="case", intent=intent, canonical="x", score=0.8))
    assert d is not None and d.canonical == task.canonical_for("list", key), d


def test_eligible_for_cpa_lists_the_ready_cases_only(client, other, prod, reading):
    [make_case(client, n) for n in ("Ps Ready One", "Ps Ready Two")]
    foreign = make_case(other, "Ps Ready Foreign")[1]
    q = "Which applications are eligible to move to CPA?"
    reading[q] = Decision(kind="case", intent="cpa_readiness", canonical="Is this case ready for CPA?", score=0.8)
    body = say(client, q, "ps-ready")
    md = body["markdown"].replace("*", "")
    assert foreign not in md and "Which case is this about" not in md, md
    task = body["diagnostics"]["task"]
    assert task["scope"] == "portfolio" and "ready_for_cpa" in task["filters"], task


@pytest.mark.parametrize("q, yes", [("Who reviews the file once it reaches CPA?", True),
                                    ("who approves the loan after CPA", True), ("kaun check karta hai CPA ke baad", True),
                                    ("who is the applicant on this case?", False), ("who uploaded the PAN?", False),
                                    ("what is pending?", False)])
def test_a_role_question_is_about_the_workflow(q, yes):
    from app.agents.applicant.copilot.semantics import task

    assert task.asks_role(q) is yes


def test_who_reviews_after_cpa_with_a_case_open_is_the_workflow_not_the_case(client, prod, reading):
    case = make_case(client, "Ps Role One")[1]
    say(client, f"open {case}", "ps-role")
    q = "Who reviews the file once it reaches CPA?"
    reading[q] = Decision(kind="case", intent="cpa_readiness", canonical="Is this case ready for CPA?", score=0.8)
    body = say(client, q, "ps-role")
    d = body["diagnostics"]
    assert d["intent"] != "READINESS" and d["task"]["scope"] == "knowledge", d
    assert d["response_source"] in ("GENERAL", "KNOWLEDGE"), d


@pytest.mark.parametrize("q, yes", [("Sort this out for me.", True), ("Ye sambhal lo.", True), ("handle this", True),
                                    ("isko dekh lo bhai", True),
                                    ("approve this", False), ("reject this file", False), ("explain this", False),
                                    ("what is this?", False), ("open it", False), ("take care", False),
                                    ("thanks for this", False), ("upload the PAN", False)])
def test_an_action_request_that_names_no_action(q, yes):
    from app.agents.applicant.copilot.semantics import task

    assert task.unnamed_action(q) is yes


@pytest.mark.parametrize("q", ["Sort this out for me.", "Ye sambhal lo."])
@pytest.mark.parametrize("open_case", [True, False])
def test_an_unnamed_action_asks_which_action_and_reads_nothing(client, prod, reading, reads, q, open_case):
    chat = f"ps-act-{open_case}-{abs(hash(q)) % 10**6}"
    if open_case:
        say(client, f"open {make_case(client, 'Ps Act Case')[1]}", chat)
    reads.clear()
    body = say(client, q, chat)
    d = body["diagnostics"]
    assert d["intent"] == "WHICH_ACTION" and reads == [] and d["tools"] == [], (d, reads)
    assert (d["task"]["scope"], d["task"]["operation"]) == ("none", "clarify"), d["task"]


def test_a_refused_action_stays_refused_never_which_action(client, prod, reading):
    say(client, f"open {make_case(client, 'Ps Approve')[1]}", "ps-approve")
    d = say(client, "approve this", "ps-approve")["diagnostics"]
    assert d["intent"] != "WHICH_ACTION", d


@pytest.mark.parametrize("q", ["Sat sri akal ji.", "sat shri akal"])
def test_sat_sri_akal_is_a_greeting_and_reads_no_case(client, prod, reading, reads, q):
    from app.agents.applicant.copilot.semantics import task

    assert task.social_kind(q) == "GREETING"
    make_case(client, "Ps Greet Case")
    reads.clear()
    body = say(client, q, f"ps-ssa-{len(q)}")
    assert body["markdown"].split()[0].rstrip("!.,?") in ("Hello", "Hi", "Good", "Namaste") and reads == [], (reads, body["markdown"])


def test_the_allowed_foir_is_redirected_to_the_eligibility_service(client, prod, reading):
    """"what FOIR is allowed?" -- eligibility is not the chatbot's (owner, 2026-10-13): the redirect, never a number
    (tests/integration/test_eligibility_separate_from_chatbot.py)."""
    md = say(client, "what FOIR is allowed?", "ps-foir")["markdown"]
    assert "Eligibility service" in md and not re.search(r"FOIR up to \d+%", md), md


@pytest.mark.parametrize("q", ["what is the FOIR for this case?", "what is FOIR?"])
def test_a_case_foir_or_a_definition_is_never_the_policy_limit(client, prod, reading, q):
    case = make_case(client, "Ps Foir Case")[1]
    say(client, f"open {case}", "ps-foir-case")
    md = say(client, q, "ps-foir-case")["markdown"]
    assert not re.search(r"FOIR up to \d+%", md), md


# ---- 4. "ready for CPA" is the move gate, never readiness alone ------------------------------------------------
@pytest.fixture
def ready_case(client, prod, monkeypatch):
    """A case whose requirements read READY_FOR_CPA (the readiness is given; the gate decides)."""
    from app.agents.applicant import workflow

    case = make_case(client, "Ps Gate Case")[1]
    real = workflow.readiness
    monkeypatch.setattr(workflow, "readiness", lambda *a, **k: {**real(*a, **k), "status": "READY_FOR_CPA"})
    from app.store import get_repository

    return get_repository().get_application(case)


@pytest.mark.parametrize("stage, gate, kind", [("FOS", {"status": "PASS"}, "READY"),
                                               ("FOS", {"status": "BLOCKED"}, "GATE"),
                                               ("FOS", {"status": "REVIEW"}, "GATE"),
                                               ("FOS", RuntimeError("gate down"), "GATE"),   # fail closed
                                               ("CPA", {"status": "PASS"}, "GATE")])         # already at CPA
def test_ready_for_cpa_is_the_move_gate(ready_case, monkeypatch, stage, gate, kind):
    from app.agents.applicant.copilot.capabilities import stage_flow, workspace

    def evaluated(case_id):
        if isinstance(gate, Exception):
            raise gate
        return gate
    monkeypatch.setattr(stage_flow, "_stage", lambda case_id: stage)
    monkeypatch.setattr(stage_flow, "gate", evaluated)
    assert workspace._row_status(ready_case)[1] == kind


def test_the_ready_filter_lists_only_cases_the_gate_passes(client, other, prod, reading, monkeypatch):
    from app.agents.applicant import workflow
    from app.agents.applicant.copilot.capabilities import case_list, stage_flow

    cleared, held = (make_case(client, n)[1] for n in ("Ps Gate Cleared", "Ps Gate Held"))
    foreign = make_case(other, "Ps Gate Foreign")[1]
    real = workflow.readiness
    monkeypatch.setattr(workflow, "readiness", lambda *a, **k: {**real(*a, **k), "status": "READY_FOR_CPA"})
    monkeypatch.setattr(stage_flow, "_stage", lambda case_id: "FOS")
    monkeypatch.setattr(stage_flow, "gate", lambda case_id: {"status": "PASS" if case_id == cleared else "BLOCKED"})
    case_list._ROWS.clear()                  # rows read before the gate was given are not this list
    md = say(client, "show ready for cpa cases", "ps-gate-list")["markdown"]
    assert cleared in md and held not in md and foreign not in md, md
