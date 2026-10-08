"""
THE RELEASE GATE: the golden set (evals/general_questions.yaml) and the FROZEN held-out set (evals/heldout/), one
chat per item, NO case open, two cases on file. Run by scripts/eval_gate.py (EVAL_RUN=1); skipped in the ordinary
suite. EVAL_MODEL=1 lets the LLM rewrite run against the real Ollama; otherwise the model is off (the fast lane only).

Per reply: correct | WRONG | unknown | clarify | case | list | decline | not_configured, the latency of the judged
(last) turn, and whether the model rewrote the question. WRONG = an answer whose content is not what was asked
(a `must` word missing), a number where "not configured" was expected, an answer where "not in the knowledge base"
was expected, or a credit decision made. WRONG must stay 0.
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
    # a guardrail refusal: a MISS (the question was not answered), never a wrong fact
    ("refused", re.compile(r"I can't provide other customers|I can't share internal|can't help with that request", re.I)),
    ("case", re.compile(r"Which case is this about|Open a case first", re.I)),
    ("list", re.compile(r"^Showing \d+-\d+ of \d+", re.I | re.M)),
    ("unknown", re.compile(r"I don't know|not in the knowledge base|isn't in the knowledge base|don't have (?:that|this) "
                           r"information|I can only help with|don't have an answer", re.I)),
    ("not_configured", re.compile(r"^- [^\n]*not set in the eligibility|^No [^.\n]+ is configured|is \*\*not configured\*\*"
                                  r"|not configured in this system", re.I | re.M)),
    ("clarify", re.compile(r"^Do you mean|Whose name do you mean|For which product", re.I | re.M)),
    ("decline", re.compile(r"I don't make credit decisions|I can't approve, reject", re.I)),
]


def classify(md: str) -> str:
    for name, pattern in CLASSES:
        if pattern.search(md):
            return name
    return "answer"


def judge(item: dict, md: str, status: int) -> str:
    """correct / WRONG / or the miss class (unknown, clarify, case, list, decline, not_configured, error)."""
    if status != 200:
        return "error"
    got, expect = classify(md), item["expect"]
    missing = [w for w in item.get("must") or [] if w.lower() not in md.lower()]
    if expect in ("answer", "list"):
        if got == expect or (expect == "answer" and got in ("not_configured", "list") and not missing):
            return "correct" if not missing else "WRONG"
        return got
    if expect == got:
        return "correct"
    if expect in ("not_configured", "unknown") and got == "answer":
        return "WRONG"                       # a guessed number / an answer the knowledge base does not hold
    if expect == "decline" and got in ("answer", "list"):
        return "WRONG"                       # a credit decision is never answered
    if {expect, got} <= {"decline", "not_configured"}:
        return "correct"                     # both say who decides, neither guesses
    return got


def _items(name: str) -> list[dict]:
    if name == "golden":
        bank = yaml.safe_load((ROOT / "evals" / "general_questions.yaml").read_text(encoding="utf-8"))["categories"]
        return [{"id": f"G{c[:3]}{n:03d}", **i} for c, items in bank.items() for n, i in enumerate(items)]
    path = ROOT / "evals" / "heldout" / f"{name}.yaml"
    frozen = (path.with_suffix(".sha256")).read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen, f"{path.name} changed after it was frozen"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["items"]


@pytest.fixture
def gaps(tmp_path, monkeypatch):
    from app.agents.applicant.copilot.capabilities import general

    data = dict(general.cfg())
    monkeypatch.setattr(general, "cfg", lambda: {**data, "gaps_file": str(tmp_path / "gaps.yaml"),
                                                 "llm_rewrite": {**(data.get("llm_rewrite") or {}),
                                                                 "log_file": str(tmp_path / "rewrites.jsonl")}})


@pytest.mark.parametrize("name", [n.strip() for n in os.getenv("EVAL_SETS", "golden,heldout_v1").split(",")])
def test_eval_gate(client, prod, gaps, monkeypatch, name):
    from app.agents.applicant.copilot.capabilities import safety
    from app.agents.applicant.copilot.semantics import question_rewrite

    monkeypatch.setenv("COPILOT_LLM_REWRITE", "true" if os.getenv("EVAL_MODEL") == "1" else "false")
    question_rewrite.clear_cache()
    for person in ("Rahul Sharma", "Priya Verma"):
        make_case(client, person)
    rows = []
    for item in _items(name):
        turns = item.get("turns") or [item["q"]]
        used_before = question_rewrite.STATS["used"]
        for message in turns:
            safety.reset()
            started = time.perf_counter()
            r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message,
                                                          "reply_language": "en", "chat_id": f"eval-{item['id']}"})
            ms = (time.perf_counter() - started) * 1000
        md = r.json().get("markdown", r.text) if r.status_code == 200 else f"HTTP {r.status_code}"
        rows.append({"id": item["id"], "q": " -> ".join(turns), "expect": item["expect"],
                     "result": judge(item, md, r.status_code), "ms": round(ms, 1),
                     "model": question_rewrite.STATS["used"] > used_before,
                     "reply": " ".join(ln for ln in md.split("\n") if ln.strip() and not ln.startswith("|"))[:200]})
    _report(name, rows)
    if os.getenv("EVAL_GATE") == "1":
        wrong = [r for r in rows if r["result"] == "WRONG"]
        assert not wrong, f"{len(wrong)} WRONG in {name}: " + "; ".join(f"{r['id']} {r['q']!r}" for r in wrong)


def _report(name: str, rows: list[dict]) -> None:
    label = os.getenv("EVAL_LABEL", "model" if os.getenv("EVAL_MODEL") == "1" else "fast")
    count = lambda k: sum(1 for r in rows if r["result"] == k)  # noqa: E731
    lat = sorted(r["ms"] for r in rows)
    p95 = lat[max(0, int(round(0.95 * len(lat))) - 1)] if lat else 0
    summary = {"set": name, "label": label, "total": len(rows), "correct": count("correct"), "WRONG": count("WRONG"),
               "unknown": count("unknown"), "clarify": count("clarify"), "case": count("case"),
               "other_miss": len(rows) - sum(count(k) for k in ("correct", "WRONG", "unknown", "clarify", "case")),
               "p50_ms": round(statistics.median(lat), 1) if lat else 0, "p95_ms": p95,
               "model_pct": round(100 * sum(1 for r in rows if r["model"]) / max(1, len(rows)), 1)}
    out = ROOT / "runs" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}_{label}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1,
                                                          ensure_ascii=False), encoding="utf-8")
    lines = [f"# {name} ({label})", "", "```", json.dumps(summary, indent=1), "```", "",
             "| id | result | ms | model | question | reply |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['result']} | {r['ms']} | {'Y' if r['model'] else ''} | {r['q']} | "
              f"{r['reply'].replace('|', '/')} |" for r in rows]
    (out / f"{name}_{label}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
