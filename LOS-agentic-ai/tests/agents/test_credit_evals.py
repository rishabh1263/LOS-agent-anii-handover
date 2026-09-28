"""
The Credit Underwriting Agent's 15-case agentic eval suite, as a test gate.

Graded by the COMMON eval runner (app/agents/runtime/evals): trajectory, tool
selection, forbidden / unnecessary / duplicate tools, bounded execution,
retries, provenance, PII, latency, output contract -- plus credit-specific
checks. Every case must pass on every dimension.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.agents.runtime.evals import runner
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository
from evals.credit.suite import CASES, execute_in_process


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDIT_MEMO_LLM_ENABLED", "false")
    monkeypatch.setenv("AGENT_RUN_AUDIT_ENABLED", "false")
    from app.agents.credit.bureau import demo as demo_bureau

    repo = SQLiteRepository(tmp_path / "eval.sqlite3")
    repo.initialise()
    set_repository(repo)
    with patch("app.agents.los.config.case_memory_enabled", return_value=True):
        yield repo
    set_repository(None)
    demo_bureau.clear_assignments()


@pytest.mark.parametrize("case, setup", CASES, ids=[c.case_id for c, _ in CASES])
def test_credit_eval_case(case, setup, repository):
    run = asyncio.run(execute_in_process(case, setup, repository))
    result = runner.evaluate(run, case.expect, case_id=case.case_id)
    assert result.passed, [(f.dimension, f.detail) for f in result.failures()]
    # every generic dimension was actually graded
    graded = {c.dimension for c in result.checks}
    for dimension in ("final_output_contract", "forbidden_tool_prevention",
                      "duplicate_tool_prevention", "bounded_execution", "retry_behaviour",
                      "model_calls", "latency", "trajectory_correctness", "no_hallucination",
                      "pii_handling", "provenance_completeness", "agent_specific"):
        assert dimension in graded, dimension


def test_the_suite_covers_the_fifteen_required_cases():
    assert [c.case_id for c, _ in CASES] == [
        "clean_salaried", "adverse_bureau", "weak_repayment", "banking_anomaly",
        "income_discrepancy", "missing_bank_statement", "missing_salary_slip",
        "itr_only_self_employed", "missing_bureau", "bureau_timeout", "co_applicant",
        "contradictory_evidence", "insufficient_evidence", "all_evidence_available",
        "provider_unavailable"]


def test_a_broken_expectation_is_caught(repository):
    """The graders are not vacuous: a wrong expected status fails."""
    from dataclasses import replace

    from evals.credit.suite import status_is

    case, setup = CASES[0]
    run = asyncio.run(execute_in_process(case, setup, repository))
    wrong = replace(case.expect, output_checks=(status_is("DATA_INSUFFICIENT"),),
                    unnecessary_tools=("application.get",))
    failed = {f.dimension for f in runner.evaluate(run, wrong).failures()}
    assert {"agent_specific", "unnecessary_tool_prevention"} <= failed
