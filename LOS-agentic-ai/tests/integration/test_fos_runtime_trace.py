"""
What actually executes, recorded rather than assumed.

WHY THIS FILE EXISTS. A diagram showing Copilot -> Orchestrator -> Agent
-> MCP is a claim about runtime, and the only way to know whether it
holds is to watch a request go through. Every layer here is instrumented
and the trace is asserted: which route took the request, whether the
intent was inferred or named, which tools ran, and whether the
orchestrator was involved at all.

WHAT THIS PINS, AND IT INCLUDES THINGS THAT ARE NOT YET TRUE:

  A named FOS action reaches its capability without the classifier.
  A typed question reaches the classifier and the same capability.
  Both doors reach the SAME tool.
  Intake runs no cross-document, income or affordability analysis.
  The ordinary document pipeline does NOT go through the orchestrator.
  The capability layer is in-process Python, not an MCP protocol server.

The last two are recorded as the current state, not as the target. A
test that asserted the target would fail today and be deleted; a test
that asserts the truth fails the day the truth changes, which is when
somebody should look.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import config as agent_config
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

FOS_SCOPES = [
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()
    repository = SQLiteRepository(tmp_path / "trace.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=FOS_SCOPES)}"})
    return c


@pytest.fixture
def case(client):
    body = client.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": "Trace Subject", "mobile": "9876543210",
                      "date_of_birth": "1990-04-12", "address": "Mumbai"},
        "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000},
    }).json()
    return body["applicant_id"], body["case_id"]


@pytest.fixture
def trace(monkeypatch):
    """
    Every layer, recorded as it runs.

    Wrapped rather than replaced: the real function still executes, so
    the trace describes a request that genuinely happened.
    """
    from app.agents.applicant import agent
    from app.orchestration import graph

    log: dict[str, list] = {"classified": [], "tools": [], "orchestrator": []}

    original_classify = agent.classify

    def watched_classify(message):
        log["classified"].append(message)
        return original_classify(message)

    original_tools = agent._call_tools

    async def watched_tools(plan, **kwargs):
        log["tools"].extend(plan)
        return await original_tools(plan, **kwargs)

    original_run = graph.run_agent

    async def watched_run(*args, **kwargs):
        log["orchestrator"].append(kwargs.get("agent_id") or args[0])
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(agent, "classify", watched_classify)
    monkeypatch.setattr(agent, "_call_tools", watched_tools)
    monkeypatch.setattr(graph, "run_agent", watched_run)
    return log


# ==========================================================================
# 1. A NAMED ACTION
# ==========================================================================


def test_a_dropdown_action_reaches_its_tool_without_being_understood(
        client, case, trace):
    """
    route -> agent -> capability. No classifier in the chain.
    """
    applicant_id, case_id = case

    response = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "GET_DOCUMENT_CHECKLIST"})

    assert response.status_code == 200
    assert trace["classified"] == [], "the action was sent through the classifier"
    assert "documents.checklist" in trace["tools"]


def test_the_business_read_takes_the_same_path(client, case, trace):
    applicant_id, case_id = case

    response = client.get(f"/api/v1/fos/checklist/{case_id}",
                          params={"applicant_id": applicant_id})

    assert response.status_code == 200
    assert trace["classified"] == []
    assert "documents.checklist" in trace["tools"]


# ==========================================================================
# 2. A TYPED QUESTION
# ==========================================================================


def test_a_typed_question_is_classified_and_reaches_the_same_tool(
        client, case, trace):
    """
    THE TWO DOORS MEET AT THE CAPABILITY. A question is understood
    first; a button is not. Both end at `documents.checklist`.
    """
    applicant_id, case_id = case

    response = client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "CUSTOM_QUERY",
        "message": "What documents are required for this case?"})

    assert response.status_code == 200
    assert trace["classified"], "a typed question skipped the classifier"
    assert "documents.checklist" in trace["tools"]


def test_the_universal_copilot_reaches_the_same_agent(client, case, trace):
    """
    `/api/v1/copilot/query` and `/api/v1/fos/copilot` are two doors into
    one agent -- the same `answer_question`, the same tools.
    """
    applicant_id, case_id = case

    response = client.post("/api/v1/copilot/query", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "message": "What documents are required for this case?"})

    assert response.status_code == 200
    assert trace["classified"]
    assert "documents.checklist" in trace["tools"]


# ==========================================================================
# 3. WHAT THE CAPABILITY LAYER ACTUALLY IS
# ==========================================================================


def test_the_capability_layer_is_in_process_python(client, case, trace):
    """
    RECORDED AS IT IS, NOT AS THE DIAGRAM DRAWS IT. `app/mcp/` provides
    typed contracts, scopes and audit, and the agent calls the tool
    functions in-process. No MCP protocol server is mounted: this is an
    MCP-STYLE capability layer, not an MCP runtime, and the distinction
    matters to anyone reading the architecture.
    """
    import inspect

    from app.mcp import applicant as tools

    handler = tools.ALL_TOOLS["documents.checklist"]

    assert inspect.iscoroutinefunction(handler)
    assert handler.__module__ == "app.mcp.applicant"


def test_no_mcp_protocol_server_is_mounted():
    """
    `app/mcp/server.py` exists and is never served. Its only importer
    chain ends at `orchestration/orchestrator.py`, which has no callers.
    """
    import main

    paths = set(main.app.openapi()["paths"])

    assert not any("/mcp" in path for path in paths)


def test_the_ordinary_copilot_path_does_not_use_the_orchestrator(
        client, case, trace):
    """
    The registry-driven graph is real and is used by the specialists,
    by document extraction and by eligibility. A copilot read is not one
    of them: it goes route -> agent -> tool.
    """
    applicant_id, case_id = case

    client.post("/api/v1/fos/copilot", json={
        "applicant_id": applicant_id, "case_id": case_id,
        "action": "GET_DOCUMENTS"})

    assert trace["orchestrator"] == []


# ==========================================================================
# 4. INTAKE IS INTAKE
# ==========================================================================


def test_an_upload_runs_the_pipeline_with_the_intake_boundary_set(
        client, case, monkeypatch):
    """
    WATCHED AT THE CALL SITE. Rather than trusting the flags are still
    written in the source, the pipeline is wrapped and the arguments it
    was actually called with are asserted.
    """
    from app.api.routes import fos_api

    seen: dict[str, object] = {}
    original = fos_api  # module-level import inside the handler

    from app.agents.los import flow

    async def watched(documents, **kwargs):
        seen.update(kwargs)
        return await original_process(documents, **kwargs)

    original_process = flow.process_application
    monkeypatch.setattr(flow, "process_application", watched)

    applicant_id, case_id = case
    client.post("/api/v1/fos/documents",
                data={"applicant_id": applicant_id, "case_id": case_id,
                      "action": "UPLOAD_DOCUMENT"},
                files={"files": ("pan.txt", b"not a real document", "text/plain")})

    assert seen.get("cross_document_checks") is False, \
        "intake ran cross-document identity checks"
    assert seen.get("financial_analysis") is False, \
        "intake ran income analysis"
    assert seen.get("summarise") is False


def test_intake_publishes_no_downstream_verdicts(client, case):
    """
    Whatever the pipeline made of the file, the response carries no
    KYC, income, eligibility or risk verdict: those stages did not run
    and an intake response must not imply that they did.
    """
    applicant_id, case_id = case

    body = client.post("/api/v1/fos/documents",
                       data={"applicant_id": applicant_id, "case_id": case_id,
                             "action": "UPLOAD_DOCUMENT"},
                       files={"files": ("pan.txt", b"not a real document",
                                        "text/plain")}).json()

    assert body.get("kyc") in (None, {}, [])
    for downstream in ("income_consistency", "eligibility", "risk", "foir"):
        assert downstream not in body
