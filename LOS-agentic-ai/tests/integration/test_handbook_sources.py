"""
P1-B (2026-10-18, contract 1.1): A HANDBOOK ANSWER KEEPS ITS SOURCES TO THE API.

`"with_sources": true` -> `sources: [{type, document, section, title}]` on a knowledge reply: only what retrieval
cited for that answer, only a real handbook file and a real heading of it. Never on a case answer or a refusal; never
without the opt-in (old clients get exactly {request_id, markdown, tts}).
"""

from __future__ import annotations

import json
import re

import pytest

from app.agents.applicant.copilot.answering import contract
from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

CHAT = "/api/v1/fos/copilot"
SHAPE = {"type", "document", "section", "title"}


def _ask(client, message: str, **extra):
    body = {"action": "CUSTOM_QUERY", "message": message, "chat_id": "src", "reply_language": "en", **extra}
    r = client.post(CHAT, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- through the API ------------------------------------------------------------------------------------------------

HANDBOOK_Q = "what is a soft enquiry?"           # answered from the handbook (los_glossary.md), not from config


def test_a_handbook_answer_carries_the_source_it_was_built_from(client, prod):
    body = _ask(client, HANDBOOK_Q, with_sources=True)
    assert body["sources"] == [{"type": "handbook", "document": "los_glossary", "section": "Soft enquiry",
                                "title": "FOS handbook -- Los glossary (Soft enquiry)"}]
    assert "soft enquiry" in body["markdown"].lower()          # the section cited is the one the answer reads


def test_an_answer_from_the_product_configuration_gets_no_handbook_source(client, prod):
    body = _ask(client, "what can be used as address proof?", with_sources=True)
    assert all(s["document"] != "address_proof" or s["type"] == "configuration" for s in body.get("sources") or [])


def test_without_the_opt_in_the_reply_is_exactly_the_old_contract(client, prod):
    assert set(_ask(client, HANDBOOK_Q)) == {"request_id", "markdown", "tts"}


@pytest.mark.parametrize("message", ["what is the capital of france?", "hello", "show my cases"])
def test_a_refusal_small_talk_or_case_answer_gets_no_sources(client, prod, message):
    make_case(client, "Rahul Sharma")
    assert "sources" not in _ask(client, message, with_sources=True)


def test_a_case_answer_never_gets_a_handbook_source(client, prod):
    applicant_id, case_id = make_case(client, "Rahul Sharma")
    body = _ask(client, "what is the application status?", with_sources=True, applicant_id=applicant_id,
                case_id=case_id)
    assert "sources" not in body


def test_the_streamed_final_reply_keeps_the_sources(client, prod):
    r = client.post(CHAT, json={"action": "CUSTOM_QUERY", "message": HANDBOOK_Q,
                                "chat_id": "src-s", "reply_language": "en", "stream": True, "with_sources": True})
    finals = [b for b in r.text.split("\n\n") if b.startswith("event: final")]
    assert finals
    final = json.loads(re.search(r"^data: (.+)$", finals[0], re.M).group(1))
    assert final["sources"][0]["section"] == "Soft enquiry"


def test_the_markdown_is_unchanged_by_the_opt_in(client, prod):
    plain = _ask(client, HANDBOOK_Q)
    sourced = _ask(client, HANDBOOK_Q, with_sources=True)
    assert plain["markdown"] == sourced["markdown"]


# ---- the rules, on made-up replies ----------------------------------------------------------------------------------

def _knowledge(*citations, **extra):
    return {"intent": "GENERAL_KNOWLEDGE", "query_type": "PROCESS_KNOWLEDGE", "citations": list(citations), **extra}


def test_several_sources_are_kept_in_order_and_deduplicated():
    out = contract.sources(_knowledge("address_proof.md#Address proof", "los_glossary.md#Address proof",
                                      "address_proof.md#Address proof"))
    assert [(s["document"], s["section"]) for s in out] == [("address_proof", "Address proof"),
                                                           ("los_glossary", "Address proof")]


@pytest.mark.parametrize("citation", ["made_up.md#Anything", "../app/config/guardrails.yaml", "..\\secrets.md",
                                      "/etc/passwd", "address_proof.txt#Address proof", "https://evil.example/x.md",
                                      "", "fos/../../main.py"])
def test_a_source_that_is_not_a_real_handbook_file_is_never_shown(citation):
    assert contract.sources(_knowledge(citation)) == []


def test_only_the_passages_the_answer_was_built_from_are_shown():
    reply = _knowledge("los_glossary.md#Soft enquiry", "los_glossary.md#Hard enquiry",
                       answered_from=["los_glossary.md#Soft enquiry"])
    assert [s["section"] for s in contract.sources(reply)] == ["Soft enquiry"]
    agent = {"knowledge": {"grounded": True, "sources": ["cibil.md", "faq.md"]}}
    assert [s["document"] for s in contract.sources(agent)] == ["cibil"]      # the agent's answer reads the top one


def test_a_section_the_file_does_not_have_is_dropped_not_invented():
    out = contract.sources(_knowledge("address_proof.md#Interest rates for gold loans"))
    assert out == [{"type": "handbook", "document": "address_proof", "section": None,
                    "title": "FOS handbook -- Address proof"}]


@pytest.mark.parametrize("intent", ["UNKNOWN", "UNKNOWN_TERM", "NOT_IN_SCOPE", "GUARDRAIL_BLOCKED", "OUT_OF_SCOPE"])
def test_a_refusal_carrying_citations_still_shows_none(intent):
    assert contract.sources(_knowledge("address_proof.md#Address proof", intent=intent)) == []
    assert contract.sources(_knowledge("address_proof.md#Address proof", refused=True)) == []


def test_the_agent_path_is_shown_only_when_grounded():
    grounded = {"knowledge": {"grounded": True, "sources": ["cibil.md"]}}
    assert contract.sources(grounded)[0]["document"] == "cibil"
    assert contract.sources({"knowledge": {"grounded": False, "sources": ["cibil.md"]}}) == []
    assert contract.sources({"knowledge": None}) == [] and contract.sources({"intent": "CASE_LIST"}) == []


def test_a_configured_fact_is_labelled_as_configuration_and_a_bad_kind_is_dropped():
    out = contract.sources(_knowledge("configuration:accepted_documents", "configuration:../x", "configuration:a b"))
    assert out == [{"type": "configuration", "document": "accepted_documents", "section": None,
                    "title": "Configured document policy"}]


def test_at_most_the_configured_number_and_off_means_none(monkeypatch):
    many = _knowledge(*[f"{d}.md" for d in ("address_proof", "cibil", "faq", "loan_terms", "los_glossary")])
    assert len(contract.sources(many)) == int((contract.cfg().get("sources") or {}).get("max", 3))
    monkeypatch.setitem(contract.cfg()["sources"], "enabled", False)
    assert contract.sources(many) == []


def test_non_string_citations_are_ignored():
    assert contract.sources(_knowledge(None, 7, {"x": 1}, object())) == []


# ---- the schema -----------------------------------------------------------------------------------------------------

def test_the_request_schema_offers_with_sources():
    import main

    props = main.app.openapi()["components"]["schemas"]["CopilotRequest"]["properties"]
    assert props["with_sources"]["type"] == "boolean" and props["with_sources"]["default"] is False
