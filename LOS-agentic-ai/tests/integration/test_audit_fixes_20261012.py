"""
THE FOUR AUDIT FIXES (2026-10-12 post-regression audit). Offline: the semantic reading is GIVEN (fos_api._understood
patched to return the Decision meaning would return -- the live check of the reading itself is
tests/integration/test_live_audit_paraphrases.py); what is tested here is what the runtime DOES with it.

  #1 PORTFOLIO: a list / count reading is answered by the deterministic case list / case insights over the caller's
     OWN rows -- never one case's "what is pending?" + "which case?"; the count equals the stored rows.
  #2 SMALL TALK: meaning's small_talk -> the greeting reply, no case data read; thanks / no keep their own answers.
  #3 HANDBOOK PARAPHRASE: a knowledge reading retrieves with meaning's nearest catalogue example too -- the SAME
     handbook passage as the plain question; nothing found -> an honest "don't have an answer".
  #4 DIAGNOSTICS: off by default; when on, codes and names only (no text, no ids); never in production.
"""
from __future__ import annotations

import pytest

from app.agents.applicant.copilot.semantics.meaning import Decision
from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_final_fix import other  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def say(client, text, chat):
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": text, "reply_language": "en",
                                                  "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def reading(monkeypatch):
    """Make the semantic layer return `decisions[message]` (else nothing), as the live model would."""
    from app.api.routes import fos_api

    decisions: dict[str, Decision] = {}

    async def understood(message, claims, payload, request_id):
        return decisions.get(message)
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


def _canonical(intent):
    from app.agents.applicant.copilot.semantics import meaning

    return meaning.catalogue()["intents"][intent]["canonical"]


# ---- #1 portfolio ----------------------------------------------------------------------------------------------
def test_every_portfolio_intent_is_a_list_with_a_deterministic_canonical():
    from app.agents.applicant.copilot.capabilities import case_insights, case_list

    for name in ("portfolio_pending", "portfolio_count", "portfolio_kyc", "portfolio_kyc_count"):
        from app.agents.applicant.copilot.semantics import meaning

        spec = meaning.catalogue()["intents"][name]
        assert spec["kind"] == "list", name
        text = spec["canonical"]
        assert case_insights.kind_of(text) or case_list.understand(text) is not None, (name, text)


@pytest.mark.parametrize("q, intent", [("count my files that still need paperwork", "portfolio_count"),
                                       ("kitni files atki hain", "portfolio_count")])
def test_a_portfolio_count_reading_is_counted_from_the_callers_rows(client, other, prod, reading, q, intent):
    from app.agents.applicant.copilot.capabilities import case_list

    mine = [make_case(client, n)[1] for n in ("Pf Count One", "Pf Count Two", "Pf Count Three")]
    foreign = make_case(other, "Pf Not Mine")[1]
    reading[q] = Decision(kind="list", intent=intent, canonical=_canonical(intent), score=0.86, decided_by="embedding")
    md = say(client, q, f"pf-{len(q)}")["markdown"]
    assert "Which case is this about" not in md and foreign not in md, md
    total = len(case_list._owned(*_claims_and_cases(client)))                 # the caller's own stored rows
    # the count is over the caller's OWN rows, with the APPROVED filter the words name (case_list.yaml filters:
    # "paperwork" -> documents pending, "atki" -> stuck = past the stage target); never a model number
    assert f"of your {total} cases" in md, md


def _claims_and_cases(client):
    import jwt

    from app.agents.applicant.copilot.capabilities import case_list
    from app.store import get_repository

    claims = jwt.decode(client.headers["Authorization"].split()[1], options={"verify_signature": False})
    subject = case_list._subject(claims)
    return claims, get_repository().list_granted_cases(subject, limit=500)


def test_a_portfolio_list_reading_lists_cases_never_asks_which_case(client, prod, reading):
    ids = [make_case(client, n)[1] for n in ("Pf List One", "Pf List Two")]
    q = "which files are still waiting on something"
    reading[q] = Decision(kind="list", intent="portfolio_pending", canonical=_canonical("portfolio_pending"),
                          score=0.85, decided_by="embedding")
    md = say(client, q, "pf-list")["markdown"]
    assert "Which case is this about" not in md and any(i in md for i in ids), md


def test_one_cases_pending_still_asks_which_case_when_none_is_open(client, prod, reading):
    make_case(client, "Pf Single One"), make_case(client, "Pf Single Two")
    md = say(client, "what is pending", "pf-single")["markdown"]
    assert "Which case is this about" in md, md           # ONE case's question: the distinction is kept


# ---- #2 small talk --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("q", ["hey there, good evening", "hello ji kaise ho"])
def test_a_small_talk_reading_is_a_greeting_and_reads_no_case(client, prod, reading, spies, q):
    ids = [make_case(client, "St Greet One")[1]]
    spies.clear()
    reading[q] = Decision(kind="small_talk", intent="small_talk", score=0.76, decided_by="embedding")
    md = say(client, q, f"st-{len(q)}")["markdown"]
    assert md.split()[0].rstrip("!.,?") in ("Hello", "Hi") and spies == [] and not any(i in md for i in ids), (spies, md)


@pytest.mark.parametrize("q, starts", [("Hii", ("Hello", "Hi")), ("thanks", ("Okay", "Sure")), ("no", ("Okay", "Sure"))])
def test_greeting_thanks_and_no_keep_their_own_answers(client, prod, reading, spies, q, starts):
    make_case(client, "St Keep One")
    spies.clear()
    reading[q] = Decision(kind="small_talk", intent="small_talk", score=0.9, decided_by="embedding")
    md = say(client, q, f"keep-{q}")["markdown"]
    assert md.split()[0].rstrip("!.,?") in starts and spies == [], (spies, md)


def test_no_keeps_its_negation_outcome():
    """'no' is never re-read as an acknowledgement by the conversation contract (evals h5)."""
    from app.agents.applicant.copilot.capabilities import general

    assert general._only("no", "negation") and general._only("nahi", "negation") and not general._only("ok", "negation")


# ---- #3 handbook paraphrase -----------------------------------------------------------------------------------
def test_a_knowledge_paraphrase_reaches_the_same_handbook_passage(client, prod, reading, monkeypatch):
    from app.agents.applicant.copilot.semantics import meaning

    plain = say(client, "what is the minimum bank statement period?", "kn-plain")["markdown"]
    q = "how long a statement history do we need from the customer"
    reading[q] = Decision(kind="knowledge", intent="knowledge_documents", score=0.75, decided_by="embedding")
    monkeypatch.setattr(meaning, "nearest_example",
                        lambda message, intent, embedder=None: "how many months of bank statement are needed")
    md = say(client, q, "kn-para")["markdown"]
    assert "not configured" in plain, plain
    assert "not configured" in md and "bank statement" in md.lower(), md        # the same handbook statement
    assert not any(ch.isdigit() for ch in md.split("\n")[0].replace("CPA", "")), md   # no invented period


def test_a_knowledge_paraphrase_with_no_evidence_abstains(client, prod, reading, monkeypatch):
    from app.agents.applicant.copilot.semantics import meaning

    q = "what is the colour of the sanction letter envelope"
    reading[q] = Decision(kind="knowledge", intent="knowledge_term", score=0.62, decided_by="embedding")
    monkeypatch.setattr(meaning, "nearest_example", lambda message, intent, embedder=None: None)
    md = say(client, q, "kn-none")["markdown"]
    assert "don't have an answer" in md, md


@pytest.mark.parametrize("q", ["what is FOIR?", "FOIR ka matlab kya hai"])
def test_foir_paraphrases_answer_from_the_glossary(client, prod, q):
    assert "Fixed Obligation to Income Ratio" in say(client, q, f"foir-{len(q)}")["markdown"]


# ---- #4 diagnostics -------------------------------------------------------------------------------------------
def test_diagnostics_are_off_by_default(client, prod, monkeypatch):
    monkeypatch.delenv("COPILOT_TURN_DIAGNOSTICS", raising=False)
    assert "diagnostics" not in say(client, "Hii", "dg-off")


def test_diagnostics_when_on_say_the_layer_and_hold_no_text_or_ids(client, prod, monkeypatch):
    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    case = make_case(client, "Dg Secret Name")[1]
    say(client, f"open {case}", "dg-on")
    body = say(client, "what is pending", "dg-on")
    d = body["diagnostics"]
    assert set(d) >= {"layer", "intent", "tools", "retrieval", "meaning", "case_in_scope"} and d["case_in_scope"], d
    flat = str(d)
    assert case not in flat and "Secret" not in flat and "what is pending" not in flat, d
    greet = say(client, "Hii", "dg-greet")["diagnostics"]
    assert greet["layer"] == "workspace" and greet["intent"] == "GREETING" and greet["tools"] == [], greet


def test_diagnostics_never_in_production(monkeypatch):
    from app.observability import turn_diagnostics

    monkeypatch.setenv("COPILOT_TURN_DIAGNOSTICS", "true")
    monkeypatch.setattr("app.agents.fraud_risk.config.environment", lambda: "production")
    assert turn_diagnostics.enabled() is False
