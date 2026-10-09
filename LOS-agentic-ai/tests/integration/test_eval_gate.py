"""
THE RELEASE GATE / MEASUREMENT (Smart Bot plan section 9): the golden set (evals/general_questions.yaml) and the FROZEN
held-out sets (evals/heldout/*.yaml, checksum-verified). Run by scripts/eval_gate.py (EVAL_RUN=1); skipped in the suite.

  EVAL_MODEL=0  "current" -- meaning, the model chooser, the general model answer, the rewrite and dense retrieval OFF
  EVAL_MODEL=1  "new"     -- all ON against the REAL local models (nomic-embed-text + qwen2.5:3b)

A set with `fixture: kyc_failed` opens that case (documents, a KYC name mismatch) in every chat before the question.
Per reply: correct | WRONG | not_available | clarify | case | small_talk | out_of_scope | decline | error.
WRONG = an answer whose content is not what was asked (`must` all / `must_any` one missing), a number or an answer where
"not available" was expected, or a credit decision made. WRONG must stay 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import time
from pathlib import Path

import pytest
import yaml

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.getenv("EVAL_RUN") != "1", reason="the release gate runs via scripts/eval_gate.py")

CLASSES = [
    ("refused", re.compile(r"I can't provide other customers|I can't share internal|can't help with that request", re.I)),
    ("case", re.compile(r"Which case is this about|Open a case first", re.I)),
    ("decline", re.compile(r"I don't make credit decisions|I can't approve, reject", re.I)),
    ("out_of_scope", re.compile(r"outside what I can help with|I can only help with", re.I)),
    ("not_available", re.compile(r"I don't know|not in the knowledge base|isn't in the knowledge base|don't have (?:that|this) "
                                 r"information|don't have an answer|not configured|^- [^\n]*not set in the eligibility|"
                                 r"isn't recorded|not recorded|has not been recorded|does not fetch|no .{0,40} is recorded|"
                                 r"^No\b[^.\n]* is configured",
                                 re.I | re.M)),
    ("clarify", re.compile(r"^Do you mean|Whose name do you mean|For which product|which one do you mean", re.I | re.M)),
    ("small_talk", re.compile(r"^(Okay|Sure|Got it|You're welcome)\b.*Anything else|^I can help with your cases", re.I | re.M)),
    ("list", re.compile(r"^Showing \d+-\d+ of \d+", re.I | re.M)),
]


def classify(md: str) -> str:
    for name, pattern in CLASSES:
        if pattern.search(md):
            return name
    return "answer"


def _content_ok(item: dict, md: str) -> bool:
    low = md.lower()
    if any(w.lower() not in low for w in item.get("must") or []):
        return False
    anyof = item.get("must_any") or []
    return not anyof or any(w.lower() in low for w in anyof)


def judge(item: dict, md: str, status: int) -> str:
    if status != 200:
        return "error"
    got, expect, ok = classify(md), item["expect"], _content_ok(item, md)
    if expect == "options":
        # ONE short follow-up question with 2-4 option links -- not "which case?", not "I don't know"
        options = md.count("](ask:")
        if got in ("case", "not_available", "out_of_scope"):
            return got
        return "correct" if "?" in md and 2 <= options <= 4 else "no_options"
    if expect in ("unknown", "not_configured"):
        expect = "not_available"
    if expect in ("answer", "list"):
        if got in ("answer", "list", "not_available", "clarify") and ok and got != "clarify":
            return "correct"                 # the content asked for is there (a "not set" line can carry it too)
        if got in ("answer", "list"):
            return "WRONG"                   # answered, but not what was asked
        return got
    if expect == "general_or_unknown":
        if got == "not_available":
            return "correct"
        if got in ("answer", "list"):
            return "correct" if ok else "WRONG"
        return got
    if expect == got:
        return "correct"
    if expect == "not_available" and got in ("decline",):
        return "correct"                     # says who decides, never guesses
    if expect == "decline" and got == "not_available":
        return "correct"
    if expect == "not_available" and got in ("answer", "list"):
        return "WRONG"                       # an answer where nothing is recorded / configured
    if expect == "decline" and got in ("answer", "list"):
        return "WRONG"                       # a credit decision is never answered
    return got


def _items(name: str) -> tuple[list[dict], str | None]:
    if name == "golden":
        bank = yaml.safe_load((ROOT / "evals" / "general_questions.yaml").read_text(encoding="utf-8"))["categories"]
        return [{"id": f"G{c[:3]}{n:03d}", **i} for c, items in bank.items() for n, i in enumerate(items)], None
    path = ROOT / "evals" / "heldout" / f"{name}.yaml"
    frozen = (path.with_suffix(".sha256")).read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen, f"{path.name} changed after it was frozen"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["items"], data.get("fixture")


@pytest.fixture
def gaps(tmp_path, monkeypatch):
    from app.agents.applicant.copilot.capabilities import general

    data = dict(general.cfg())
    monkeypatch.setattr(general, "cfg", lambda: {**data, "gaps_file": str(tmp_path / "gaps.yaml"),
                                                 "llm_rewrite": {**(data.get("llm_rewrite") or {}),
                                                                 "log_file": str(tmp_path / "rewrites.jsonl")}})


def _model_mode(monkeypatch) -> bool:
    on = os.getenv("EVAL_MODEL") == "1"
    for flag, value in (("COPILOT_MEANING", on), ("COPILOT_GENERAL_LLM", on), ("COPILOT_LLM_REWRITE", on)):
        monkeypatch.setenv(flag, "true" if value else "false")
    monkeypatch.setenv("KNOWLEDGE_BACKEND", "vector" if on else "lexical")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama" if on else "hashing")
    from app.knowledge import set_repository
    from app.agents.applicant.copilot.semantics import meaning, question_rewrite

    set_repository(None)                      # the retriever is rebuilt for this mode
    meaning._BANK["key"] = None
    question_rewrite.clear_cache()
    return on


def _kyc_failed_case(client, store) -> str:
    """The fixture of tests/integration/test_master_6_kyc_ab.py, built here (documents + a KYC name mismatch)."""
    from app.store.models import CaseFinding, Document, DocumentStatus, FindingKind

    a, c = make_case(client, "Rahul Sharma")
    for kind, name in (("PAN", "RAHUL SHARMA"), ("DRIVING_LICENCE", "RAHUL SHARMA"), ("BANK_STATEMENT", "ROHIT VERMA")):
        doc_id = f"{c}:{a}:{kind.lower()}"
        store.save_document(Document(document_id=doc_id, case_id=c, applicant_id=a, party_id=a, document_type=kind,
                                     status=DocumentStatus.VERIFIED, verification_status="PASS"))
        store.save_finding(CaseFinding(finding_id=f"x-{kind}", case_id=c, party_id=a, document_id=doc_id,
                                       source_id=f"{kind.lower()}.jpg", finding_kind=FindingKind.EXTRACTION,
                                       status="PASS", payload={"fields": {"name": name}}, content_hash=f"x-{kind}"))
    store.save_finding(CaseFinding(
        finding_id="k", case_id=c, party_id=a, finding_kind=FindingKind.KYC, status="FAIL",
        reason_codes=["NAME_MISMATCH"], content_hash="k",
        payload={"fields": [{"field": "NAME", "status": "FAIL", "sources": [
            {"document_type": "PAN", "value": "RAHUL SHARMA"}, {"document_type": "DRIVING_LICENCE", "value": "RAHUL SHARMA"},
            {"document_type": "BANK_STATEMENT", "value": "ROHIT VERMA"}]}]}))
    return c


def _model_calls() -> int:
    from app.agents.applicant.copilot.capabilities import general
    from app.agents.applicant.copilot.semantics import meaning, question_rewrite

    return (meaning.STATS["model"] + meaning.STATS["model_failed"] + question_rewrite.STATS["called"]
            + general.GENERAL_LLM_STATS["called"])


@pytest.mark.parametrize("name", [n.strip() for n in os.getenv("EVAL_SETS", "golden,heldout_v1").split(",")])
def test_eval_gate(client, prod, gaps, monkeypatch, _store, name):
    from app.agents.applicant.copilot.capabilities import safety
    from app.llm.memory import free_gb

    _model_mode(monkeypatch)
    items, fixture = _items(name)
    case_id = _kyc_failed_case(client, _store) if fixture == "kyc_failed" else None
    make_case(client, "Priya Verma")
    if not case_id:
        make_case(client, "Rahul Sharma")
    rows, ram = [], []
    for item in items:
        chat = f"eval-{name}-{item['id']}"
        if case_id and not item.get("no_case"):
            client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": f"open {case_id}",
                                                     "chat_id": chat})
        turns = item.get("turns") or [item["q"]]
        calls_before = _model_calls()
        for message in turns:
            safety.reset()
            started = time.perf_counter()
            r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message,
                                                          "reply_language": "en", "chat_id": chat})
            ms = (time.perf_counter() - started) * 1000
        md = r.json().get("markdown", r.text) if r.status_code == 200 else f"HTTP {r.status_code}"
        free = free_gb()
        if free is not None:
            ram.append(free)
        rows.append({"id": item["id"], "q": " -> ".join(turns), "expect": item["expect"],
                     "result": judge(item, md, r.status_code), "ms": round(ms, 1),
                     "model": _model_calls() > calls_before,
                     "reply": " ".join(ln for ln in md.split("\n") if ln.strip() and not ln.startswith("|"))[:240]})
    _report(name, rows, ram)
    if os.getenv("EVAL_GATE") == "1":
        wrong = [r for r in rows if r["result"] == "WRONG"]
        assert not wrong, f"{len(wrong)} WRONG in {name}: " + "; ".join(f"{r['id']} {r['q']!r}" for r in wrong)


def _report(name: str, rows: list[dict], ram: list[float]) -> None:
    label = os.getenv("EVAL_LABEL", "new" if os.getenv("EVAL_MODEL") == "1" else "current")
    count = lambda k: sum(1 for r in rows if r["result"] == k)  # noqa: E731
    lat = sorted(r["ms"] for r in rows)
    p95 = lat[max(0, int(round(0.95 * len(lat))) - 1)] if lat else 0
    summary = {"set": name, "label": label, "total": len(rows), "correct": count("correct"), "WRONG": count("WRONG"),
               "not_available": count("not_available"), "clarify": count("clarify"), "case": count("case"),
               "other_miss": len(rows) - sum(count(k) for k in ("correct", "WRONG", "not_available", "clarify", "case")),
               "correct_pct": round(100 * count("correct") / max(1, len(rows)), 1),
               "p50_ms": round(statistics.median(lat), 1) if lat else 0, "p95_ms": p95,
               "model_pct": round(100 * sum(1 for r in rows if r["model"]) / max(1, len(rows)), 1),
               "min_free_ram_gb": round(min(ram), 2) if ram else None}
    out = ROOT / "runs" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}_{label}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1,
                                                          ensure_ascii=False), encoding="utf-8")
    lines = [f"# {name} ({label})", "", "```", json.dumps(summary, indent=1), "```", "",
             "| id | result | ms | model | question | reply |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['result']} | {r['ms']} | {'Y' if r['model'] else ''} | {r['q']} | "
              f"{r['reply'].replace('|', '/')} |" for r in rows]
    (out / f"{name}_{label}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
