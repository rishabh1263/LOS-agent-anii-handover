"""
PHASE 2 -- THE STRUCTURED TASK (semantics/task.py). Offline: the semantic reading, where one is needed, is GIVEN
(fos_api._understood patched, as in test_audit_fixes_20261012.py); the live reading is measured by
tests/integration/test_live_task_eval.py on the held-out sets.

  unit:        scope / operation / filters / reference / correction / negation from the words + the conversation
  routing:     a portfolio follow-up keeps the operation with the new filter; a correction opens THAT row; a NO with
               softeners is NEGATION; an acknowledgement is never a hello; a settled task is never asked back as vague
  isolation:   every count / row is the caller's own; a reference resolves only to the list the caller was shown
  diagnostics: the task is codes only -- no text, no id
"""
from __future__ import annotations

import re

import pytest

from app.agents.applicant.copilot.semantics.meaning import Decision
from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401

_ID = re.compile(r"CASE-[0-9A-F]{6,}")
LISTED = ["CASE-AAAAAA111111", "CASE-BBBBBB222222", "CASE-CCCCCC333333"]


def interpret(text, decision=None, **kw):
    from app.agents.applicant.copilot.semantics import task

    return task.interpret(text, decision, **kw)


# ---- unit: the conversation signal -----------------------------------------------------------------------------
@pytest.mark.parametrize("q, op", [("hello", "greet"), ("hi there", "greet"), ("thanks", "ack"), ("ok", "ack"),
                                   ("no", "negate"), ("nahi", "negate"), ("nah not really", "negate"),
                                   ("no thanks", "negate"), ("nahi ji bas", "negate")])
def test_conversation_signals_are_kept_apart(q, op):
    t = interpret(q)
    assert (t.scope, t.operation) == ("conversation", op), t


def test_a_correction_is_not_an_acknowledgement():
    from app.agents.applicant.copilot.semantics import task

    assert task.correction_rest("no, I meant the second one") == "second one"
    assert task.correction_rest("nahi, doosra wala") == "doosra wala"
    assert task.correction_rest("no") is None and task.correction_rest("ok thanks") is None
    t = interpret("no, I meant the second one", listed=LISTED)
    assert t.correction and (t.scope, t.operation) == ("one_case", "retrieve")
    assert t.references["kind"] == "ordinal" and t.references["rows"] == [2]


# ---- unit: scope is not the subject ------------------------------------------------------------------------------
def test_one_case_and_portfolio_questions_on_the_same_words():
    from app.agents.applicant.copilot.semantics import meaning

    one = Decision(kind="case", intent="pending_documents", canonical="What is pending?", score=0.8)
    assert meaning.plural_scope("which documents are pending for this application?", one) is None
    many = meaning.plural_scope("which applications have pending documents?", one)
    assert many is not None and many.kind == "list"
    count = meaning.plural_scope("how many applications have pending documents?", one)
    assert count is not None and count.intent == "portfolio_count"
    t1 = interpret("which documents are pending for this application?", one, case_open=True)
    assert (t1.scope, t1.operation) == ("one_case", "retrieve")
    t2 = interpret("which documents are pending for this application?", one, case_open=False)
    assert (t2.scope, t2.operation) == ("one_case", "which_case") and "which_case" in t2.ambiguities


def test_a_plural_noun_alone_is_not_the_portfolio():
    from app.agents.applicant.copilot.semantics import meaning

    reading = Decision(kind="case", intent="applicant_name", canonical="What is the applicant's name?", score=0.8)
    assert meaning.plural_scope("what names do the applicants have on this file", reading) is None


@pytest.mark.parametrize("q, intent, filt", [
    ("show my pendng cases", "pending_documents", None),                       # a typo: the reading is the cue
    ("kaunsi files CPA ke liye ready hain", "cpa_readiness", "ready_for_cpa"),  # the filter the words name
])
def test_the_status_cue_comes_from_the_meaning_too(q, intent, filt):
    from app.agents.applicant.copilot.capabilities import case_list
    from app.agents.applicant.copilot.semantics import meaning

    d = meaning.plural_scope(q, Decision(kind="case", intent=intent, canonical="x", score=0.7))
    assert d is not None and d.kind == "list", d
    if filt:
        assert case_list.understand(d.canonical).filter == filt, d.canonical


@pytest.mark.parametrize("q, key", [("kitne case kyc mein atke hain", "kyc_issue"), ("kitni files atki hain", "stuck"),
                                    ("only the ready ones", "ready_for_cpa"), ("the KYC ones", "kyc_issue"),
                                    ("ones with documents pending", "documents_pending"), ("tell me a joke", None)])
def test_the_filter_named_specific_reason_first(q, key):
    from app.agents.applicant.copilot.semantics import task

    assert task.filter_in(q) == key


# ---- unit: references and follow-ups ---------------------------------------------------------------------------
@pytest.mark.parametrize("q, row", [("why is the first one pending?", 1), ("teesre wale ka status kya hai", 3),
                                    ("what's the loan amount on the second one?", 2)])
def test_a_row_of_the_list_shown(q, row):
    t = interpret(q, listed=LISTED)
    assert (t.scope, t.operation, t.references.get("rows")) == ("one_case", "retrieve", [row]), t


def test_a_reference_without_a_list_resolves_to_nothing():
    t = interpret("why is the first one pending?", listed=[])
    assert t.references.get("kind") != "ordinal", t


def test_compare_rows_and_paging_are_portfolio():
    assert (interpret("compare the first two", listed=LISTED).operation) == "compare"
    t = interpret("next two", listed=LISTED, list_query={"page": 0, "size": 5})
    assert (t.scope, t.operation) == ("portfolio", "page"), t


@pytest.mark.parametrize("prev_op, q, op, key", [
    ("count", "what about the stuck ones?", "count", "stuck"),
    ("count", "no, I meant the KYC ones", "count", "kyc_issue"),
    ("count", "and how many are ready for CPA?", "count", "ready_for_cpa"),
    ("list", "only the ones with documents pending", "list", "documents_pending"),
])
def test_a_portfolio_followup_keeps_the_operation_with_the_new_filter(prev_op, q, op, key):
    from app.agents.applicant.copilot.capabilities import case_insights, case_list
    from app.agents.applicant.copilot.semantics import task

    prev = {"scope": "portfolio", "operation": prev_op, "filters": ["documents_pending"]}
    t = interpret(q, previous=prev)
    assert (t.scope, t.operation, t.filters, t.references.get("kind")) == ("portfolio", op, [key], "previous_task"), t
    canonical = task.canonical_for(op, key)
    if op == "count":
        assert case_insights.kind_of(canonical) == "COUNT" and task.filter_in(canonical) == key, canonical
    else:
        assert case_list.understand(canonical).filter == key, canonical


def test_no_portfolio_followup_after_a_one_case_question():
    t = interpret("what about the stuck ones?", previous={"scope": "one_case", "operation": "retrieve"})
    assert t.references.get("kind") != "previous_task", t


@pytest.mark.parametrize("kind, scope, op", [("knowledge", "knowledge", "explain"), ("policy", "knowledge", "explain"),
                                             ("decision", "none", "refuse"), ("out_of_scope", "none", "refuse"),
                                             ("clarify_action", "none", "clarify"), ("ack", "conversation", "ack")])
def test_the_semantic_kinds_map_to_scope_and_operation(kind, scope, op):
    t = interpret("some unseen wording", Decision(kind=kind, intent="x", score=0.7), case_open=True)
    assert (t.scope, t.operation) == (scope, op), t


def test_unknown_stays_unknown_and_public_has_no_ids():
    t = interpret("zzz qqq", None)
    assert (t.scope, t.operation) == ("unresolved", "unknown") and "not_understood" in t.ambiguities
    t = interpret("why is the first one pending?", listed=LISTED)
    assert "CASE-" not in str(t.public()), t.public()


# ---- routing through the API ------------------------------------------------------------------------------------
def say(client, text, chat, **extra):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text, "reply_language": "en",
                                                  "chat_id": chat, **extra})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def reading(monkeypatch):
    from app.api.routes import fos_api

    decisions: dict[str, Decision] = {}

    async def understood(message, claims, payload, request_id):
        return decisions.get(message)
    monkeypatch.setattr(fos_api, "_understood", understood)
    return decisions


def test_a_portfolio_followup_counts_the_new_filter_over_own_rows(client, other, prod, reading):
    mine = [make_case(client, n)[1] for n in ("Tk Fu One", "Tk Fu Two")]
    foreign = make_case(other, "Tk Fu Foreign")[1]
    first = say(client, "how many cases have a KYC issue?", "tk-fu")["markdown"]
    assert re.search(r"\b\d+ of your \d+ cases\b", first), first
    md = say(client, "what about the stuck ones?", "tk-fu")["markdown"]
    assert re.search(r"\b\d+ of your \d+ cases: past the stage target", md), md
    assert foreign not in md and "Which case is this about" not in md, md
    assert mine


def test_a_correction_opens_the_other_row_of_the_list(client, other, prod, reading):
    [make_case(client, n) for n in ("Tk Cr One", "Tk Cr Two", "Tk Cr Three")]
    make_case(other, "Tk Cr Foreign")
    listed = list(dict.fromkeys(_ID.findall(say(client, "show my cases", "tk-cr")["markdown"])))
    assert len(listed) >= 2
    say(client, "open the first one", "tk-cr")
    md = say(client, "no, I meant the second one", "tk-cr")["markdown"]
    assert md.lstrip().startswith(listed[1]), (listed[:2], md[:200])


def test_a_no_with_softeners_is_negation_not_a_question(client, prod, reading, monkeypatch):
    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    case = make_case(client, "Tk Neg One")[1]
    say(client, f"open {case}", "tk-neg")
    say(client, "what is pending", "tk-neg")
    body = say(client, "nah not really", "tk-neg")
    assert body["diagnostics"]["outcome"] == "NEGATION", body["diagnostics"]
    assert "Still pending" not in body["markdown"], body["markdown"]


@pytest.fixture
def spies(monkeypatch):
    from app.agents.applicant.copilot.capabilities import case_list, product_flow

    reads = []
    real_run, real_notes = case_list.run, product_flow.notifications
    monkeypatch.setattr(case_list, "run", lambda *a, **k: reads.append("case_list.run") or real_run(*a, **k))
    monkeypatch.setattr(product_flow, "notifications",
                        lambda *a, **k: reads.append("notifications") or real_notes(*a, **k))
    return reads


@pytest.mark.parametrize("q, canonical, intent", [("great, thanks a lot", "thanks", "THANKS"),
                                                  ("theek hai samajh gaya", "ok", "ACKNOWLEDGEMENT")])
def test_an_acknowledgement_reading_is_never_a_hello(client, prod, reading, spies, monkeypatch, q, canonical, intent):
    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    make_case(client, "Tk Ack One")
    spies.clear()
    reading[q] = Decision(kind="ack", intent="thanks" if canonical == "thanks" else "acknowledgement",
                          canonical=canonical, score=0.8)
    body = say(client, q, f"tk-ack-{len(q)}")
    assert body["diagnostics"]["intent"] == intent and spies == [], (body["diagnostics"], spies)
    assert not body["markdown"].startswith("Hello"), body["markdown"]


def test_a_short_greeting_read_by_meaning_is_not_asked_back(client, prod, reading):
    reading["namaste ji"] = Decision(kind="small_talk", intent="small_talk", score=0.8)
    md = say(client, "namaste ji", "tk-namaste")["markdown"]
    assert md.split()[0].rstrip("!.,?") in ("Hello", "Hi", "Good", "Namaste"), md


def test_next_two_after_a_list_pages_it_never_asks_what_you_mean(client, prod, reading):
    [make_case(client, f"Tk Pg {i}") for i in range(3)]
    say(client, "show my cases", "tk-pg")
    md = say(client, "next two", "tk-pg")["markdown"]
    assert "What do you mean" not in md and "Pick one" not in md, md


def test_the_diagnostics_carry_the_task_as_codes_only(client, prod, reading, monkeypatch):
    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    case = make_case(client, "Tk Diag Secret")[1]
    say(client, "show my cases", "tk-dg")
    d = say(client, "why is the first one pending?", "tk-dg")["diagnostics"]
    assert d["task"]["scope"] == "one_case" and d["task"]["references"]["kind"] == "ordinal", d
    flat = str(d)
    assert case not in flat and "CASE-" not in flat and "Secret" not in flat and "pending?" not in flat, d


def test_task_off_leaves_every_message_as_typed(client, prod, reading, monkeypatch):
    from app.agents.applicant.copilot.semantics import task

    monkeypatch.setattr(task, "enabled", lambda: False)
    say(client, "how many cases have a KYC issue?", "tk-off")
    md = say(client, "what about the stuck ones?", "tk-off")["markdown"]
    assert not re.search(r"\b\d+ of your \d+ cases: past the stage target", md), md


@pytest.mark.parametrize("q, ok", [("great, thanks a lot", True), ("theek hai samajh gaya", True), ("namaste ji", True),
                                   ("wats pendng here", False), ("is it done?", False), ("kya status hai", False)])
def test_a_question_is_never_read_as_small_talk(q, ok):
    """A typo'd question ("wats pendng here") read as "ok" by the embeddings is a QUESTION (held-out A, S08)."""
    from app.agents.applicant.copilot.semantics import task

    assert task.plain_social(q) is ok


def test_an_ack_reading_of_a_question_goes_on_as_typed(client, prod, reading, monkeypatch):
    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    case = make_case(client, "Tk Typo One")[1]
    say(client, f"open {case}", "tk-typo")
    reading["wats pendng here"] = Decision(kind="ack", intent="acknowledgement", canonical="ok", score=0.7)
    d = say(client, "wats pendng here", "tk-typo")["diagnostics"]
    assert d["intent"] not in ("ACKNOWLEDGEMENT", "THANKS"), d
