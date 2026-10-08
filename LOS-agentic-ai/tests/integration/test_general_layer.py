"""
THE GENERAL LAYER (capabilities/general.py, app/config/conversation_general.yaml) on BOTH endpoints.

  - questions that need no case never meet "which case?" (help, ok, role, stage, definitions, process, products)
  - the remembered chat context is reloaded BEFORE the layer: "ok" while a question is pending answers it,
    it is never small talk (/fos/copilot and /copilot/query)
  - the unknown-acronym rule: FNR / FTNR are logged gaps, ordinary words are never called unknown
  - the login stage is a signed, display-only claim: it never overrides a standard claim
"""

from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401

FOS, UNIVERSAL = "/api/v1/fos/copilot", "/api/v1/copilot/query"
SMALL_TALK = "Anything else?"


def fos(c, message, chat="gl"):
    r = c.post(FOS, json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en", "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def universal(c, message, chat="gl-u"):
    r = c.post(UNIVERSAL, json={"message": message, "reply_language": "en", "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


@pytest.fixture
def gaps(tmp_path, monkeypatch):
    """Unknown terms go to a temp file, never the repo's evals/knowledge_gaps.yaml."""
    from app.agents.applicant.copilot.capabilities import general

    data = dict(general.cfg())
    monkeypatch.setattr(general, "cfg", lambda: {**data, "gaps_file": str(tmp_path / "gaps.yaml")})
    return tmp_path / "gaps.yaml"


def test_no_case_questions_never_ask_which_case(client, prod, gaps):
    for name in ("Rahul Sharma", "Priya Verma"):
        make_case(client, name)
    for message in ("what can you help with?", "what is CPA", "how to move cpa", "process to verify documents",
                    "mandatory documents for Home loan", "what is my role", "what is my stage", "FNR meaning",
                    "WHAT IS NAME", "why you are not answering me"):
        md = fos(client, message)
        assert "Which case" not in md and "I don't know" not in md, (message, md)
    assert "| PAN" in fos(client, "mandatory documents for Home loan")      # the checklist table, from config
    assert "FOS requirements" not in fos(client, "what is CPA")             # the real checks, never the catch-all


def test_ok_is_small_talk_only_with_nothing_pending(client, prod, gaps):
    make_case(client, "Rahul Sharma")
    assert SMALL_TALK in fos(client, "ok", chat="ack-idle")


@pytest.mark.parametrize("ask", ["mera cases kya hai", "kyc status"])
def test_fos_ok_answers_the_pending_question(client, prod, gaps, ask):
    for name in ("Rahul Sharma", "Priya Verma"):
        make_case(client, name)
    fos(client, ask, chat="ack-fos")
    assert SMALL_TALK not in fos(client, "ok", chat="ack-fos")


def test_universal_reloads_context_before_the_general_layer(client, prod, gaps):
    for name in ("Rahul Sharma", "Priya Verma"):
        make_case(client, name)
    first = universal(client, "kyc status", chat="ack-u")
    assert "Which case" in first, first
    assert SMALL_TALK not in universal(client, "ok", chat="ack-u")
    # and with nothing pending, the same endpoint does answer small talk
    assert SMALL_TALK in universal(client, "ok", chat="ack-u-idle")


def test_unknown_acronym_rule(client, prod, gaps):
    import yaml

    assert "not in the knowledge base yet" in fos(client, "FNR meaning")
    assert "not in the knowledge base yet" in fos(client, "ftnr meaning")
    for ordinary in ("what is loan", "what is pending", "status kya hai"):
        assert "not in the knowledge base yet" not in fos(client, ordinary), ordinary
    assert set(yaml.safe_load(gaps.read_text(encoding="utf-8"))["terms"]) == {"FNR", "FTNR"}


@pytest.mark.parametrize("phrase", ["what are my cases", "mera cases kya hai", "show all my cases"])
def test_multi_word_list_phrases(client, prod, phrase):
    for name in ("Rahul Sharma", "Priya Verma"):
        make_case(client, name)
    assert "Showing 1-2 of 2" in fos(client, phrase, chat=f"list-{phrase}")


def test_login_stage_is_a_display_only_claim(make_token, prod):
    from fastapi.testclient import TestClient

    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES, stage='FOS')}"})
    assert "You signed in for the FOS" in fos(c, "what is my stage", chat="stage")


def test_dev_idp_extra_claims_never_override(monkeypatch):
    import jwt

    from app.security import dev_idp

    token = dev_idp.issue_access_token(subject="officer-a", issuer="iss", audience="aud",
                                       extra_claims={"stage": "FOS", "sub": "someone-else", "scope": "admin",
                                                     "roles": ["admin"], "role": "admin"})
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == "officer-a" and claims["stage"] == "FOS"
    assert "scope" not in claims and "roles" not in claims and claims["role"] == "fos"
    blank = jwt.decode(dev_idp.issue_access_token(subject="o", issuer="i", audience="a", extra_claims={"stage": ""}),
                       options={"verify_signature": False})
    assert "stage" not in blank
