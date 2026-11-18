"""
COPILOT AGENT INSIGHTS (2026-11-04; app/agents/applicant/copilot/capabilities/agent_insights.py): the words are read
in code, the facts come from each agent's own table, the reply is a template whose every number is checked.
Synthetic data only, on the disposable test server -- never the owner's database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from app.agents.applicant.copilot.capabilities import agent_insights as ai

OPEN = "CASE-SEED-LOS-DUMMY-0001"


@pytest.mark.parametrize("message,open_case,kind,agents", [
    ("What do the agents say about case CASE-SEED-LOS-DUMMY-0005?", None, "CASE_360", []),
    ("sab agents ka result batao", OPEN, "CASE_360", []),
    ("summary of agent findings for CASE-SEED-LOS-DUMMY-0001", None, "CASE_360", []),
    ("fraud risk of LOS-DUMMY-0025", None, "ONE_AGENT", ["fraud_screening"]),
    ("frad risk of LOS-DUMMY-0002", None, "ONE_AGENT", ["fraud_screening"]),          # one letter wrong
    ("case CASE-SEED-LOS-DUMMY-0007 ka fraud aur valuation kya bola?", None, "ONE_AGENT",
     ["fraud_screening", "property_valuation"]),
    ("valuer ka result", OPEN, "ONE_AGENT", ["property_valuation"]),
    ("which agents have not run on case CASE-SEED-LOS-DUMMY-0002?", None, "NOT_RUN", []),
    ("Is case CASE-SEED-LOS-DUMMY-0001 ready for credit?", None, "READINESS", []),
    ("is CASE-SEED-LOS-DUMMY-0005 good to go for credit", None, "READINESS", []),
    ("How many cases are high fraud risk?", OPEN, "PORTFOLIO", ["fraud_screening"]),
    ("kitne cases fraud me high risk hai?", None, "PORTFOLIO", ["fraud_screening"]),
    ("Which cases need attention from any agent?", None, "PORTFOLIO", []),
    ("How many dedupe checks have been done?", None, "PORTFOLIO", ["dedupe"]),
    ("can I send LOS-DUMMY-0004 to credit now?", None, "READINESS", []),          # asks whether: not the move
    ("cases jinko attention chahiye agents ke hisaab se", OPEN, "PORTFOLIO", []),
])
def test_the_officers_words_become_the_right_ask(message, open_case, kind, agents):
    ask = ai.understand(message, None, open_case)
    assert ask is not None and ask.kind == kind and ask.agents == agents, ask


@pytest.mark.parametrize("message", [
    "What is FOIR?", "cases by stage", "hi", "What file types can I upload?", "How many cases are in FOS stage?",
    "what collateral documents are needed?", "office address of applicant", "is the applicant eligible?",
    "upload the valuation report", "what is an agent?", "what is collateral verification?", "explain dedupe",
    "pending documents of this case", "show my cases", "download report",
    "send the case to credit", "send LOS-DUMMY-0004 to credit", "move this case to CPA",   # the stage move's
])
def test_what_is_not_about_the_agents_results_is_left_to_the_other_layers(message):
    assert ai.understand(message, None, OPEN) is None


def test_a_follow_up_reuses_the_last_ask_and_a_new_question_does_not():
    case = {"kind": "READINESS", "case_id": "CASE-SEED-LOS-DUMMY-0300", "agents": [], "mode": "list",
            "filter": "attention"}
    count = {"kind": "PORTFOLIO", "case_id": None, "agents": ["fraud_screening"], "mode": "count", "filter": "high"}
    assert ai.understand("which ones?", count).as_dict()["mode"] == "list"
    assert ai.understand("and the valuation details?", case).as_dict() == {
        "kind": "ONE_AGENT", "case_id": "CASE-SEED-LOS-DUMMY-0300", "agents": ["property_valuation"], "mode": "list",
        "filter": "attention"}
    assert ai.understand("How many cases are high fraud risk?", case).kind == "PORTFOLIO"   # its own question
    assert ai.understand("what is collateral verification?", case) is None                 # a definition


def test_a_number_no_fact_supports_never_passes_the_check():
    assert ai.verify("**6 of your 501 cases** are high risk: LOS-DUMMY-0001, CASE-SEED-LOS-DUMMY-0025.", {"6", "501"})
    assert not ai.verify("**7 of your 501 cases** are high risk.", {"6", "501"})
    assert ai.verify("No agent has run on CASE-SEED-LOS-DUMMY-0300 yet.", set())          # an id is not a number


# ---------------------------------------------------------------------------------------------------- on a database
@pytest.fixture
def agents_db(monkeypatch):
    """A fresh shared database with three agents' tables (synthetic rows) and the seed application table."""
    from app.agent_db import core
    from app.store.testing import _admin_dsn, _with_db

    admin = _admin_dsn()
    name = f"agdb_insights_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = _with_db(admin, name)
    at = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    with psycopg.connect(dsn) as c:
        c.execute("CREATE TABLE public.loan_applications (application_id BIGINT PRIMARY KEY, application_number TEXT)")
        c.execute("INSERT INTO public.loan_applications VALUES (1, 'LOS-DUMMY-0001'), (2, 'LOS-DUMMY-0002'), "
                  "(3, 'LOS-DUMMY-0003')")
        c.execute("CREATE SCHEMA fraud_detection")
        c.execute("CREATE TABLE fraud_detection.fraud_results (result_id BIGSERIAL PRIMARY KEY, application_number "
                  "TEXT, risk_level TEXT, is_potential_fraud BOOLEAN, created_at TIMESTAMPTZ)")
        c.execute("INSERT INTO fraud_detection.fraud_results (application_number, risk_level, is_potential_fraud, "
                  "created_at) VALUES ('LOS-DUMMY-0001', 'LOW', false, %s), ('LOS-DUMMY-0001', 'HIGH', true, %s), "
                  "('LOS-DUMMY-0002', 'HIGH', true, %s)", (at.replace(hour=8), at, at))
        c.execute("CREATE SCHEMA property_valuation")
        c.execute("CREATE TABLE property_valuation.property_valuation_agent_results (result_id BIGSERIAL PRIMARY KEY, "
                  "application_id BIGINT, agent_status TEXT, vendor_status TEXT, critical_issues_count INT, "
                  "created_at TIMESTAMPTZ)")
        c.execute("INSERT INTO property_valuation.property_valuation_agent_results (application_id, agent_status, "
                  "vendor_status, critical_issues_count, created_at) VALUES (1, 'HIGH_RISK', 'NEGATIVE', 3, %s)", (at,))
        c.execute("CREATE SCHEMA dedupe")
        c.execute("CREATE TABLE dedupe.dedupe_checks (check_id BIGSERIAL PRIMARY KEY, case_id TEXT, outcome TEXT, "
                  "customer_status TEXT, customer_type_result TEXT, checked_at TIMESTAMPTZ)")
        c.execute("INSERT INTO dedupe.dedupe_checks (case_id, outcome, customer_status, checked_at) VALUES "
                  "('CASE-SEED-LOS-DUMMY-0003', 'READY_FOR_DECISION', 'NEW', %s), "
                  "('CASE-SEED-LOS-DUMMY-0003', 'READY_FOR_DECISION', 'NEW', %s)", (at, at))
        c.commit()
    monkeypatch.setenv("LOS_AGENTIC_DSN", dsn)
    monkeypatch.setenv("COPILOT_AGENT_INSIGHTS", "true")
    ai.forget_cache()
    yield dsn
    ai.forget_cache()
    core.forget()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


MINE = ["CASE-SEED-LOS-DUMMY-0001", "CASE-SEED-LOS-DUMMY-0002", "CASE-SEED-LOS-DUMMY-0003",
        "CASE-SEED-LOS-DUMMY-0004"]


def _ask(monkeypatch, message, owned=MINE, open_case=None):
    monkeypatch.setattr(ai, "owned_cases", lambda claims: list(owned))
    return ai.answer(message, {"sub": "officer-t"}, "req-t", open_case=open_case)


def test_one_case_across_every_agent_states_each_latest_result_and_the_readiness(agents_db, monkeypatch):
    r = _ask(monkeypatch, "What do the agents say about LOS-DUMMY-0001?")
    a = r["answer"]
    assert r["intent"] == "AGENT_INSIGHTS" and "Fraud screening: **HIGH**" in a          # the LATEST run, not LOW
    assert "Property valuation: **HIGH_RISK**" in a and "critical issues: 3" in a
    assert "Credit readiness: not ready" in a and "Dedupe" in a.split("Not run yet:")[1]
    assert r["agent_insights"]["readiness"] == "NOT_READY" and not r["agent_insights"].get("verify_failed")


def test_the_portfolio_counts_and_lists_only_the_officers_own_cases(agents_db, monkeypatch):
    r = _ask(monkeypatch, "How many cases are high fraud risk?")
    assert "**2 of your 4 cases** are high risk (Fraud screening)" in r["answer"]
    r = _ask(monkeypatch, "List the potential fraud cases", owned=MINE[1:])               # 0001 is not theirs now
    assert "LOS-DUMMY-0002" in r["answer"] and "LOS-DUMMY-0001" not in r["answer"]
    r = _ask(monkeypatch, "How many dedupe checks have been done?")
    assert "**1 of your 4 cases** has a result (Dedupe). 2 checks in all." in r["answer"]


def test_a_case_that_is_not_the_officers_is_not_found_and_shows_nothing(agents_db, monkeypatch):
    r = _ask(monkeypatch, "fraud risk of LOS-DUMMY-0001", owned=["CASE-SEED-LOS-DUMMY-0004"])
    assert "couldn't find CASE-SEED-LOS-DUMMY-0001 among your cases" in r["answer"]
    assert "HIGH" not in r["answer"] and r["agent_insights"]["found"] is False


def test_an_agent_whose_table_is_missing_is_not_run_never_a_failure(agents_db, monkeypatch):
    # collateral / office / residence / eligibility / documents have no table in this database
    r = _ask(monkeypatch, "agents result for CASE-SEED-LOS-DUMMY-0004")
    assert r["intent"] == "AGENT_INSIGHTS" and "No agent has run on CASE-SEED-LOS-DUMMY-0004 yet" in r["answer"]


def test_no_agents_database_leaves_the_turn_to_the_other_layers(monkeypatch):
    from app.agent_db import core

    monkeypatch.setenv("COPILOT_AGENT_INSIGHTS", "true")
    monkeypatch.setattr(core, "shared_dsn", lambda: None)
    assert _ask(monkeypatch, "fraud risk of LOS-DUMMY-0001") is None


def test_an_agents_database_that_cannot_be_read_is_said_plainly(agents_db, monkeypatch):
    def down(*a, **k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(ai, "_connect", down)
    r = _ask(monkeypatch, "fraud risk of LOS-DUMMY-0001")
    assert "can't be read right now" in r["answer"] and r["agent_insights"]["unavailable"] is True
