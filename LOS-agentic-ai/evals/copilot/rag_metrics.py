"""
RAG METRICS over the assistant corpus (assistant_corpus/rag.json), in process.

Not "how many were answered" -- the system must stay safe when evidence is
absent, so every metric is reported beside the false-confidence rate:

  retrieval_hit        answerable, confident, expected source among kept hits
  top1_relevance       ... and the expected source is the TOP hit
  answer_correct       the published answer carries the expected evidence and
                       none of the forbidden claims
  citation_correct     the published answer cites the expected source
  grounded             every answer sentence is taken from the handbook text
                       (or the configured policy) -- nothing composed
  no_answer_accuracy   unanswerable -> the honest no-answer, with no citation
  false_confidence     unanswerable answered confidently (MUST stay 0)
  latency              retrieval and full knowledge answer, p50 / p95

Stage-guide questions (source "stage_guide") are answered from the stage
guide, not retrieval: they count for answers, not for retrieval metrics.

  python -m evals.copilot.rag_metrics [--report out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "evals" / "copilot" / "assistant_corpus" / "rag.json"
HANDBOOK = ROOT / "knowledge" / "fos"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*`_#|—-]", " ", text)).strip().lower()


def run(report: str | None) -> int:
    os.environ.setdefault("EMBEDDING_PROVIDER", "hashing")
    os.environ.setdefault("APPLICANT_AGENT_LLM_ENABLED", "false")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.agents.applicant import knowledge_answer as ka
    from app.agents.applicant.copilot import agent

    cases = json.loads(CORPUS.read_text(encoding="utf-8"))
    handbook = _norm(" ".join(p.read_text(encoding="utf-8") for p in HANDBOOK.glob("*.md")))
    m = {k: 0 for k in ("answerable", "retrieval_cases", "retrieval_hit", "top1_relevance",
                        "answer_correct", "citation_correct", "grounded", "unanswerable",
                        "no_answer_accuracy", "false_confidence")}
    retrieval_ms, answer_ms, rows = [], [], []
    for case in cases:
        q = case["q"]
        started = time.perf_counter()
        result = ka.retrieve(q)
        retrieval_ms.append((time.perf_counter() - started) * 1000)
        sources = [str(getattr(h.chunk, "source", "") or "") for h in (result.hits if result else [])]
        confident = bool(result and result.confident)
        started = time.perf_counter()
        if case.get("source") == "stage_guide":
            # answered by the stage guide route, not by retrieval
            from app.knowledge import process_knowledge
            from app.agents.applicant.copilot.semantics.intents import stage_in

            text = process_knowledge.describe(stage_in(q) or "FOS") or ka.NO_ANSWER
            detail = {"citations": ["stage_guide"]}
        else:
            text, _src, detail = asyncio.run(agent._knowledge_reply(q, allow_model=False))
        answer_ms.append((time.perf_counter() - started) * 1000)
        body = text.split("\n\nSource:")[0]
        row = {"id": case["id"], "answerable": case["answerable"], "confident": confident,
               "sources": sources[:3], "answer": text[:400]}
        if case["answerable"]:
            m["answerable"] += 1
            expected = case["source"]
            if expected != "stage_guide":
                m["retrieval_cases"] += 1
                hit = confident and any(expected in s for s in sources)
                m["retrieval_hit"] += hit
                m["top1_relevance"] += bool(confident and sources and expected in sources[0])
                row["retrieval_hit"] = hit
            lowered = text.lower()
            correct = any(a.lower() in lowered for a in case.get("any", [])) and not any(
                x.lower() in lowered for x in case.get("lacks", []))
            m["answer_correct"] += correct
            stem = expected.split(".")[0]
            cited = expected == "stage_guide" or stem.replace("_", " ") in lowered or stem in json.dumps(
                detail.get("citations") or [])
            m["citation_correct"] += cited
            configured = any(str(c).startswith("configuration:") for c in detail.get("citations") or [])
            sentences = [s for s in re.split(r"(?<=[.!?])\s+|;\s+|\s(?=-\s+\*\*)", body) if len(s.split()) >= 4]
            grounded = configured or expected == "stage_guide" or all(
                _norm(s)[:60] in handbook for s in sentences)
            m["grounded"] += grounded
            row.update(answer_correct=correct, citation_correct=cited, grounded=grounded)
        else:
            m["unanswerable"] += 1
            honest = ka.NO_ANSWER in text and "Source:" not in text
            m["no_answer_accuracy"] += honest
            m["false_confidence"] += confident
            row.update(no_answer=honest)
        rows.append(row)

    def pct(v): return round(statistics.median(v), 1), round(sorted(v)[int(0.95 * (len(v) - 1))], 1)

    summary = {
        "retrieval_hit": f"{m['retrieval_hit']}/{m['retrieval_cases']}",
        "top1_relevance": f"{m['top1_relevance']}/{m['retrieval_cases']}",
        "answer_correct": f"{m['answer_correct']}/{m['answerable']}",
        "citation_correct": f"{m['citation_correct']}/{m['answerable']}",
        "grounded": f"{m['grounded']}/{m['answerable']}",
        "no_answer_accuracy": f"{m['no_answer_accuracy']}/{m['unanswerable']}",
        "false_confidence": f"{m['false_confidence']}/{m['unanswerable']}",
        "latency_ms": {"retrieval_p50_p95": pct(retrieval_ms), "answer_p50_p95": pct(answer_ms)},
    }
    print("RAG " + json.dumps(summary))
    for row in rows:
        bad = (row["answerable"] and not (row.get("answer_correct") and row.get("citation_correct")
                                          and row.get("grounded"))) or (
            not row["answerable"] and (row["confident"] or not row.get("no_answer")))
        if bad:
            print("FAIL", row["id"], {k: row.get(k) for k in ("confident", "retrieval_hit", "answer_correct",
                                                                "citation_correct", "grounded", "no_answer")},
                  row["answer"][:160].replace("\n", " "))
    if report:
        Path(report).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False),
                                encoding="utf-8")
    return 0 if m["false_confidence"] == 0 else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--report")
    a = p.parse_args()
    sys.exit(run(a.report))
