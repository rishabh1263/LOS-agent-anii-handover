"""
THE CURATED KNOWLEDGE (OKF-style front-matter) IS IMPLEMENTED, NOT ONLY SUPPORTED (2026-10-04).

  - every item declares an id, type, title, description, domain and language
  - ids are unique; every `related` id resolves; every `derived_from` file exists
  - metadata reaches every chunk the retriever indexes
  - only CURRENT items inside their effective window are indexed or returned
  - a question naming a product never gets another product's checklist
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import knowledge
from app.agents.applicant import knowledge_answer
from app.knowledge.markdown_repo import MarkdownKnowledgeRepository, in_effect, knowledge_metadata

ROOT = Path(__file__).resolve().parents[2]
CORPUS = sorted((ROOT / "knowledge" / "fos").glob("*.md"))
REQUIRED = ("id", "knowledge_type", "title", "description", "domain", "language")


def _meta(path: Path) -> dict:
    return knowledge_metadata("FOS", path.name, path.read_text(encoding="utf-8"))[0]


@pytest.fixture(autouse=True)
def _real_corpus(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")
    knowledge.set_repository(None)
    yield
    knowledge.set_repository(None)


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_every_item_declares_its_metadata(path):
    meta = _meta(path)
    assert meta["metadata_source"] == "DECLARED"
    assert all(meta.get(key) for key in REQUIRED), [k for k in REQUIRED if not meta.get(k)]


def test_ids_are_unique_and_links_resolve():
    metas = [_meta(p) for p in CORPUS]
    ids = [m["id"] for m in metas]
    assert len(ids) == len(set(ids))
    broken = [(m["id"], r) for m in metas for r in m.get("related") or [] if r not in ids]
    assert not broken, broken


def test_every_derived_from_file_exists():
    missing = [(m["id"], f) for m in map(_meta, CORPUS) for f in m.get("derived_from") or []
               if not (ROOT / f).exists()]
    assert not missing, missing


def test_metadata_reaches_every_indexed_chunk():
    chunks = knowledge.get_repository().chunks("FOS")
    assert chunks and all(c.metadata.get("id") and c.metadata.get("status") == "CURRENT" for c in chunks)


def test_superseded_or_expired_items_are_never_indexed(tmp_path):
    stage = tmp_path / "fos"
    stage.mkdir()
    body = "# Address proof\n\nA utility bill is accepted as address proof for every product.\n" * 3
    (stage / "old.md").write_text("---\nid: x.old\nstatus: SUPERSEDED\n---\n" + body, encoding="utf-8")
    (stage / "expired.md").write_text("---\nid: x.exp\neffective_to: 2020-01-01\n---\n" + body, encoding="utf-8")
    (stage / "future.md").write_text("---\nid: x.fut\neffective_from: 2999-01-01\n---\n" + body, encoding="utf-8")
    (stage / "current.md").write_text("---\nid: x.cur\n---\n# Current\n\nOnly a passport is accepted here.\n",
                                      encoding="utf-8")
    sources = {c.source for c in MarkdownKnowledgeRepository(tmp_path).chunks("FOS")}
    assert sources == {"current.md"}
    assert not in_effect({"status": "RETIRED"}) and in_effect({"effective_from": "2000-01-01"})


def test_a_product_question_gets_only_that_products_checklist():
    home = knowledge_answer.retrieve("Which documents are mandatory for a home loan?")
    assert home is not None and home.hits
    assert all("personal_loan" not in h.chunk.source for h in home.hits)
    personal = knowledge_answer.retrieve("Which documents does a personal loan always need?")
    assert personal is not None and personal.hits
    assert all("home_loan" not in h.chunk.source for h in personal.hits)
    assert personal.hits[0].chunk.source == "document_requirements_personal_loan.md"


def test_a_question_naming_no_product_is_not_filtered():
    result = knowledge_answer.retrieve("What can be used as address proof?")
    assert result is not None and result.confident and result.hits[0].chunk.source == "address_proof.md"
