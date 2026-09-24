"""
The live demo, run as a test so it uses the real stack.

Not part of the suite's guarantees -- it prints. Run explicitly:

    pytest tests/integration/_demo_showcase.py -s
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.knowledge import indexing
from app.knowledge.embeddings import get_embedder
from app.knowledge.vector_store import QdrantVectorStore, set_vector_store
from app.store import demo_seed, set_repository
from app.store.sqlite_repo import SQLiteRepository

ENDPOINT = "/api/v1/copilot/query"


@pytest.fixture(autouse=True)
def demo_env(monkeypatch):
    monkeypatch.setattr("app.agents.los.config.case_memory_enabled",
                        lambda: True)
    yield


@pytest.fixture
def client(make_token, tmp_path):
    import main

    repo = SQLiteRepository(tmp_path / "demo.sqlite3")
    repo.initialise()
    set_repository(repo)
    demo_seed.seed(repo)

    embedder = get_embedder()
    store = QdrantVectorStore(url=None)
    summary = indexing.index_demo(repo, store, embedder=embedder)
    set_vector_store(store)

    print("\n" + "=" * 74)
    print(f"  provider : {type(embedder).__name__}")
    print(f"  indexed  : {summary.case_chunks} case + "
          f"{summary.knowledge_chunks} process chunks, "
          f"{len(summary.stages)} stages")
    print("=" * 74)

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    yield c

    set_vector_store(None)
    set_repository(None)


def show(client, label, **body):
    response = client.post(ENDPOINT, json=body)
    print(f"\n### {label}")
    print(f"    request  {json.dumps({k: v for k, v in body.items()})}")
    print(f"    status   {response.status_code}")

    payload = response.json()
    if response.status_code != 200:
        print(f"    detail   {json.dumps(payload.get('detail'))}")
        return

    print(f"    stage    {payload.get('stage')} "
          f"({payload.get('stage_resolution')})")
    print(f"    category {payload.get('category')} / {payload.get('intent')}")
    print(f"    grounded {payload.get('grounded')}")
    print(f"    answer   {payload.get('answer')}")
    for item in (payload.get("sources") or [])[:4]:
        trimmed = {k: v for k, v in item.items() if v}
        print(f"      source {json.dumps(trimmed)}")


def test_demo(client):
    show(client, "1. CASE QUESTION",
         message="Why is this case under review?",
         applicant_id="DEMO-APP-002", case_id="DEMO-CASE-005", stage="FOS")

    show(client, "2. PROCESS QUESTION",
         message="What does the RCU stage check?",
         applicant_id="DEMO-APP-002", case_id="DEMO-CASE-005", stage="RCU")

    show(client, "3. MIXED QUESTION",
         message="Why is this case under review and what does the RCU "
                 "stage check?",
         applicant_id="DEMO-APP-002", case_id="DEMO-CASE-005", stage="RCU")

    show(client, "4. JOURNEY QUESTION",
         message="What happened to this case from FOS to RCU?",
         applicant_id="DEMO-APP-002", case_id="DEMO-CASE-005", stage="RCU")

    show(client, "5. UNAUTHORIZED CASE",
         message="Why is this case under review?",
         applicant_id="DEMO-APP-001", case_id="DEMO-CASE-005")
