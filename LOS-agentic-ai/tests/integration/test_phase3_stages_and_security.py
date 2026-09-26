"""
Phase 3 final sprint: the Universal Copilot at EVERY stage, answering stage-
aware questions from the authoritative stage resolver and never inventing a
downstream result -- and the final security audit fixes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"
ROOT = Path(__file__).resolve().parents[2]
ALL_STAGES = ("FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT")

#: Claims no answer may make unless an authoritative provider returned them.
#: None exists in this build for credit, RCU, decision or fraud.
FAKE_INTELLIGENCE = re.compile(
    r"\b(ai|agent|system)\s+(has\s+)?(checked|approved|cleared|passed)\b"
    r"|\bdecision\s+agent\s+approved\b|\brcu\s+(has\s+)?cleared\b"
    r"|\bfraud\s+agent\s+passed\b|\bcredit\s+(is\s+|has\s+been\s+)?approved\b"
    r"|\bloan\s+(is\s+|has\s+been\s+)?(approved|sanctioned|disbursed)\b"
    r"|\bcibil\s+score\s+(is|of)\b",
    re.IGNORECASE)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "p3.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def demo(repo):
    from app.store import demo_seed

    demo_seed.seed(repo, force=True)
    return {c["stage"]: (c["case_id"], c["applicant_id"])
            for c in reversed(demo_seed._CASES)}


@pytest.fixture
def client(make_token):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = f"Bearer {make_token(scopes=['los.read'])}"
    return c


def ask(client, message, case_id, applicant_id, **extra):
    r = client.post(COPILOT, json={"case_id": case_id, "applicant_id": applicant_id,
                                   "message": message, **extra})
    assert r.status_code == 200, r.text
    return r.json()


STAGE_QUESTIONS = [
    "What stage am I in?", "Where is my application?", "What happens next?",
    "What is pending at this stage?", "Why hasn't it moved?",
    "What changed after CPA?", "What do I need for CREDIT?",
    "Is it ready for RCU?", "What is blocking me?", "What should I do now?",
    "Has the credit check been done?", "Did RCU clear my file?",
    "Is my loan approved?", "When will the loan be disbursed?",
]


@pytest.mark.parametrize("stage", ALL_STAGES)
@pytest.mark.parametrize("question", STAGE_QUESTIONS)
def test_stage_aware_questions_are_answered_honestly_at_every_stage(
        client, demo, stage, question):
    case_id, applicant_id = demo[stage]
    body = ask(client, question, case_id, applicant_id)
    # THE STAGE IS THE RECORD'S, whatever the question names.
    assert body["stage"] == stage, (question, body["stage"])
    # NO FAKE INTELLIGENCE: no invented credit / RCU / decision / fraud result.
    assert not FAKE_INTELLIGENCE.search(body["answer"]), (stage, question, body["answer"])
    # Deterministic: no model wrote a stage-aware case answer here.
    assert body["response_source"] != "LLM"
    assert body["answer"].strip()


@pytest.mark.parametrize("stage", ["CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT"])
def test_a_stage_without_a_provider_says_so_rather_than_answering(client, demo, stage):
    """Downstream capability that does not exist is reported, not simulated."""
    from app.agents.los import stage_registry
    from app.agents.los.stages import LosStage

    case_id, applicant_id = demo[stage]
    capabilities = stage_registry.capabilities_for(LosStage(stage))
    assert "case_facts" not in capabilities.capabilities
    body = ask(client, "Is this case fraudulent?", case_id, applicant_id)
    assert body["stage"] == stage
    assert not FAKE_INTELLIGENCE.search(body["answer"])
    assert body["response_source"] != "LLM"


def test_every_stage_is_registered_and_none_claims_an_unbuilt_provider():
    from app.agents.los import stage_registry
    from app.agents.los.stages import LosStage

    for stage in ALL_STAGES:
        entry = stage_registry.capabilities_for(LosStage(stage))
        for capability in entry.capabilities:
            assert capability in stage_registry.PROVIDERS, (stage, capability)


# ==========================================================================
# SECURITY AUDIT FIXES
# ==========================================================================

def test_the_local_identity_is_read_only_by_default(monkeypatch):
    from app.security import auth

    monkeypatch.delenv("AUTH_LOCAL_SCOPES", raising=False)
    assert auth.local_claims()["scope"] == "los.read"
    monkeypatch.setenv("AUTH_LOCAL_SCOPES", "los.read los.write")
    assert auth.local_claims()["scope"] == "los.read los.write"


def test_no_credential_ships_in_example_or_config_files():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"^DUMMY_PASSWORD_HASH=\S+", example, re.MULTILINE)
    assert "Dev@123" not in example
    assert "Dev@123" not in (ROOT / "app/api/routes/auth_api.py").read_text(encoding="utf-8")
    for path in (ROOT / "app/config").glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}\.", text), path
        assert "BEGIN PRIVATE KEY" not in text and "BEGIN RSA" not in text, path
        assert not re.search(r"(?im)^\s*(password|secret|api_key|token)\s*:\s*\S+", text), path


def test_the_standalone_dev_token_server_has_no_default_secret():
    source = (ROOT / "auth_provider_dev.py").read_text(encoding="utf-8")
    assert "change-me-local-only" not in source
    assert "_refuse_outside_development()" in source


def test_startup_validates_jwt_settings_and_fails_closed_outside_development():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "validate_auth_configuration()" in source
    assert "_auth.validate_auth_mode()" in source


def test_production_refuses_auth_off(monkeypatch):
    from app.security import auth

    monkeypatch.delenv("ENVIRONMENT", raising=False)          # unset = production
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with pytest.raises(RuntimeError):
        auth.validate_auth_mode()
    assert auth.auth_enabled() is True
