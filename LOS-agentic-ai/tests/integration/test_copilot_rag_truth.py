"""
Retrieval restored, and the Copilot truthful about what it did with it.

THE LIVE FINDINGS THESE HOLD:

  RAG WAS NEVER REACHED. The deployment .env named no vector store, so every
  process got an empty in-memory Qdrant and every search failed on a missing
  collection. Configuration now points at the embedded store the demo was
  indexed into; this suite keeps its own hermetic defaults.

  A MODEL SENTENCE WAS LABELLED AS SOMETHING ELSE. A Qwen-phrased handbook or
  stage-guide answer was published as STRUCTURED or KNOWLEDGE. `LLM` means
  "a model phrased it" -- in every category.

  "I DON'T HAVE ENOUGH INFORMATION" WAS PUBLISHED AS GROUNDED.

  A STALE HOLD RODE ALONG. "Is my bank statement verified?" on a case at
  CREDIT was qualified with the review recorded back at FOS.

  A RAW STATUS TOKEN PASSED THE VALIDATOR ("verification status PASS").

  STAGE RULES BORROWED THE PRODUCT POLICY'S STATUS. They are UNCONFIRMED and
  now say so on every row they add, and can be switched off in one place.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "rag.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def memory_on_llm_off(monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    yield
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


@pytest.fixture
def demo(repo):
    from app.store import demo_seed

    demo_seed.seed(repo, force=True)
    return {c["case_id"]: c["applicant_id"] for c in demo_seed._CASES}


@pytest.fixture
def embedded_rag(tmp_path, monkeypatch, demo, repo):
    """
    A REAL embedded Qdrant store on disk, indexed with the real indexer from
    the real demo corpus -- the same shape the deployment uses, with the
    local hashing embedder in place of Ollama.
    """
    from app.knowledge import indexing, vector_store
    from app.knowledge.embeddings import HashingEmbedding

    path = tmp_path / "qdrant"
    monkeypatch.setenv("QDRANT_PATH", str(path))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")
    # The hashing embedder is not semantic: its cosine for a real question
    # sits near zero. These tests prove storage, indexing, scoping and
    # composition -- semantic quality is proven live with nomic-embed-text.
    monkeypatch.setenv("LOS_VECTOR_MIN_SCORE", "-1")
    store = vector_store.QdrantVectorStore(path=str(path))
    vector_store.set_vector_store(store)
    summary = indexing.ensure_demo_index(repo, store,
                                         embedder=HashingEmbedding(), force=True)
    yield store, summary
    vector_store.set_vector_store(None)
    client = getattr(store, "_client", None)
    if client is not None:
        client.close()


def ask(client, message, case_id, applicant_id) -> dict:
    response = client.post(COPILOT, json={"applicant_id": applicant_id,
                                          "case_id": case_id,
                                          "message": message})
    assert response.status_code == 200, response.text
    return response.json()


# ==========================================================================
# 1. RAG IS CONFIGURED -- and the suite stays hermetic
# ==========================================================================

def test_the_suite_does_not_use_the_deployment_vector_store():
    """The .env names the embedded store; the suite runs on code defaults."""
    assert os.environ.get("QDRANT_PATH") == ""
    assert os.environ.get("EMBEDDING_PROVIDER") == "hashing"


def test_the_deployment_env_names_a_store_and_matching_embeddings():
    from pathlib import Path

    env = (Path(__file__).resolve().parents[3] / ".env")
    if not env.exists():
        pytest.skip("no deployment .env on this machine")
    text = env.read_text(encoding="utf-8")
    assert "QDRANT_PATH=./runtime/qdrant_demo" in text
    assert "EMBEDDING_PROVIDER=ollama" in text
    assert "OLLAMA_URL=" in text


def test_an_embedded_store_is_indexed_and_searched(embedded_rag):
    from app.knowledge import retrieval, vector_store

    store, summary = embedded_rag
    assert store.count(vector_store.knowledge_collection()) > 0
    assert store.count(vector_store.case_collection()) > 0

    found = retrieval.process_context("What does RCU check?", stages=("RCU",))
    assert found.evidence, "the RCU stage guide was not retrieved"
    assert "RCU" in found.evidence[0].text


def test_case_retrieval_stays_inside_the_case(embedded_rag):
    from app.knowledge import retrieval
    from app.knowledge.vector_store import Scope

    found = retrieval.semantic_context(
        "why is this case under review",
        scope=Scope(app_id="DEMO-APP-002", case_id="DEMO-CASE-004",
                    stages=("FOS", "CPA", "CREDIT")))

    assert found.evidence
    # Every chunk returned is this case's own -- the store's filter, not
    # the ranking, keeps other applicants' cases out.
    cases = {(e.provenance or {}).get("case_id") for e in found.evidence}
    assert cases == {"DEMO-CASE-004"}, cases


def test_a_process_question_is_answered_from_the_retrieved_guide(
        client, embedded_rag, monkeypatch):
    """Retrieval real; the model stubbed so the test needs no Ollama."""
    from app.knowledge import grounding

    seen: dict = {}

    async def compose(question, facts, context, *_a, **_k):
        seen["process"] = [e.text for e in context.process.evidence]
        return "RCU samples files and checks whether documents are genuine."

    monkeypatch.setattr(grounding, "_generate", compose)

    body = ask(client, "What does RCU check?", "DEMO-CASE-005", "DEMO-APP-002")

    assert seen["process"], "the composer was given no retrieved guide"
    assert body["category"] == "PROCESS_KNOWLEDGE"
    assert body["response_source"] == "LLM"          # a model wrote it
    assert body["grounded"] is True
    assert "PROCESS_KNOWLEDGE" in {s.get("type") for s in body["sources"]}


def test_with_the_model_down_a_process_answer_is_not_labelled_llm(
        client, embedded_rag, monkeypatch):
    from app.knowledge import grounding

    async def down(*_a, **_k):
        return None

    monkeypatch.setattr(grounding, "_generate", down)

    body = ask(client, "What does RCU check?", "DEMO-CASE-005", "DEMO-APP-002")

    assert body["response_source"] != "LLM"


# ==========================================================================
# 2. RESPONSE SOURCE AND GROUNDED ARE TRUTHFUL
# ==========================================================================

class _Confident:
    """A retrieval result that is confident and carries nothing."""

    @staticmethod
    def make():
        from app.knowledge.grounding import GroundedContext

        class Confident(GroundedContext):
            @property
            def grounded(self):
                return True

        return Confident()


def test_a_model_phrased_knowledge_answer_says_llm(client, demo, monkeypatch):
    from app.api.routes import copilot_api
    from app.knowledge import grounding

    async def compose(*_a, **_k):
        return "An application status says where a case sits in its stage."

    monkeypatch.setattr(copilot_api.grounding, "gather",
                        lambda *a, **k: _Confident.make())
    monkeypatch.setattr(grounding, "_generate", compose)

    body = ask(client, "What is an application status?", "DEMO-CASE-008",
               "DEMO-APP-004")

    assert body["category"] == "KNOWLEDGE_ONLY"
    assert body["answer"].startswith("An application status says")
    assert body["response_source"] == "LLM"


def test_an_answer_with_no_evidence_is_not_grounded(client, demo, monkeypatch):
    from app.api.routes import copilot_api
    from app.knowledge import grounding

    async def refuse(*_a, **_k):
        return ("I don't have enough verified information to define what "
                "FOIR is based on the current evidence.")

    monkeypatch.setattr(copilot_api.grounding, "gather",
                        lambda *a, **k: _Confident.make())
    monkeypatch.setattr(grounding, "_generate", refuse)

    body = ask(client, "What is FOIR?", "DEMO-CASE-008", "DEMO-APP-004")

    assert body["grounded"] is False
    assert body["response_source"] == "LLM"


@pytest.mark.parametrize("answer,expected", [
    ("I don't have enough verified information to answer that yet.", True),
    ("I don't have enough information in the FOS knowledge base to answer that.", True),
    ("There is not enough information to decide.", True),
    ("Your PAN is verified.", False),
    ("Address Proof is still pending.", False),
])
def test_no_evidence_wording_is_recognised(answer, expected):
    from app.api.routes.copilot_api import _says_no_evidence

    assert _says_no_evidence(answer) is expected


def test_a_raw_status_token_is_rejected():
    from app.agents.applicant.validate import check_composed

    structured = ("Bank Statement is VERIFIED. These are document checks; the "
                  "issuing authority has not confirmed the document.")
    ok, reason = check_composed(
        "Bank Statement is verified as per the verification status PASS.",
        structured=structured)
    assert not ok and "internal status: PASS" in reason
    assert check_composed("Your bank statement is verified.",
                          structured=structured)[0]


# ==========================================================================
# 3. HOLDS BELONG TO THE STAGE THEY WERE MADE IN
# ==========================================================================

def test_a_document_answer_carries_no_earlier_stages_hold(client, demo):
    """DEMO-CASE-004: REVIEW recorded at FOS; the case is now at CREDIT."""
    body = ask(client, "is my bank statement verified?", "DEMO-CASE-004",
               "DEMO-APP-002")

    assert body["stage"] == "CREDIT"
    assert "under review" not in body["answer"]


def test_the_first_stages_hold_is_always_its_own(client, demo):
    """DEMO-CASE-008 is at FOS, its only stage; its REVIEW is current even
    though the decision was written a moment before the stage event."""
    body = ask(client, "What is my application status?", "DEMO-CASE-008",
               "DEMO-APP-004")

    assert body["stage"] == "FOS"
    assert "under review because" in body["answer"]


def test_hold_since_applies_only_after_a_transition(demo):
    from app.agents.los import stages

    fos = stages.resolve("DEMO-CASE-008")
    credit = stages.resolve("DEMO-CASE-004")

    assert fos.hold_since is None
    assert credit.hold_since == credit.since is not None


# ==========================================================================
# 4. STAGE REQUIREMENTS: WIRED INTO THE RUNTIME, AND HONEST ABOUT STATUS
# ==========================================================================

async def test_the_checklist_tool_is_stage_aware(demo):
    """The MCP tool itself -- the runtime path every consumer uses."""
    from app.mcp import applicant as tools

    fos = await tools.ALL_TOOLS["documents.checklist"]("DEMO-CASE-001")
    rcu = await tools.ALL_TOOLS["documents.checklist"]("DEMO-CASE-005")

    fos_slots = {e["slot"] for e in fos.result["checklist"]}
    rcu_rows = {e["slot"]: e for e in rcu.result["checklist"]}
    assert "SIGNATURE_VERIFICATION" not in fos_slots
    assert rcu_rows["SIGNATURE_VERIFICATION"]["stage"] == "RCU"
    assert rcu_rows["SIGNATURE_VERIFICATION"]["policy_status"] == "UNCONFIRMED"
    assert rcu.result["policy"]["stage"] == "RCU"
    assert rcu.result["policy"]["stage_policy_status"] == "UNCONFIRMED"


def test_a_stage_rule_row_says_it_is_unconfirmed():
    from app.agents.policy import engine

    rows = {r.slot: r for r in engine.resolve("PERSONAL_LOAN", stage="CPA").requirements}

    assert rows["PAN"].stage is None                  # the product policy's
    assert rows["INCOME_PROOF"].policy_status == "UNCONFIRMED"


def test_stage_rules_can_be_switched_off(monkeypatch, tmp_path):
    from app.agents.policy import engine, loader

    path = tmp_path / "stages.yaml"
    path.write_text("enabled: false\nstages:\n  FOS: []\n  CPA:\n"
                    "    - rule_id: X\n      section: INCOME_PROOF\n",
                    encoding="utf-8")
    monkeypatch.setenv("LOS_STAGE_REQUIREMENTS_PATH", str(path))
    loader.reload()
    try:
        slots = {r.slot for r in engine.resolve(None, stage="CPA").requirements}
        assert slots == {"PAN", "ADDRESS_PROOF"}
    finally:
        monkeypatch.delenv("LOS_STAGE_REQUIREMENTS_PATH")
        loader.reload()


def test_the_taxonomy_is_not_a_checklist():
    """Every taxonomy section exists; a FOS case needs two slots of them."""
    from app.agents.policy import engine
    from app.agents.verification import taxonomy

    assert len(taxonomy.sections()) == 6
    assert {r.slot for r in engine.resolve(None, stage="FOS").requirements} == {
        "PAN", "ADDRESS_PROOF"}


@pytest.mark.parametrize("recorded,since,current", [
    ("2026-09-22T04:56:07.600+00:00", "2026-09-22T04:56:07.541+00:00", True),
    ("2026-09-22T04:56:07.500+00:00", "2026-09-22T04:56:07.541+00:00", False),
    # The same instant: written with the transition, not made in the stage.
    ("2026-09-22T04:56:07.541+00:00", "2026-09-22T04:56:07.541+00:00", False),
    ("2026-09-22T04:56:07.541+00:00", None, True),
])
def test_a_hold_counts_only_if_decided_after_the_stage_began(recorded, since,
                                                             current):
    from app.agents.applicant.status_facts import during_stage

    assert during_stage({"recorded_at": recorded}, since) is current
