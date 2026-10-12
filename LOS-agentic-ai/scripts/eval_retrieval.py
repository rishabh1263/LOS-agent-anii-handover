"""
RETRIEVAL BASELINE -- dense (nomic-embed-text via Ollama) vs BM25 on the labelled RAG corpus.

    python scripts/eval_retrieval.py [--out runs/eval/retrieval.json]

The corpus is evals/copilot/assistant_corpus/rag.json: every answerable case that names the handbook file it must
come from (`source`; stage-guide answers excluded -- they are not retrieval). Per retriever: P@1 (the first passage
is from the expected file), MRR@5, hit@5, and per-query latency (median / p95 over the warm runs; the first pass,
which embeds the corpus, is reported separately). Off-topic questions (answerable: false) report how often the
retriever was CONFIDENT anyway -- a false confidence the answer layer must then refuse.

It re-measures the figures quoted in app/knowledge/retriever.py (41 questions, P@1 0.805 / MRR@5 0.854 / 35 ms)
on the corpus that exists in the repository today; the two sets differ, so the numbers are compared, not equated.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CORPUS = ROOT / "evals" / "copilot" / "assistant_corpus" / "rag.json"


def _rank(sources: list[str], accepted: list[str]) -> int | None:
    for i, s in enumerate(sources[:5], start=1):
        if any(a in s for a in accepted):
            return i
    return None


def _measure(name: str, retriever, cases: list[dict], runs: int) -> dict:
    answerable = [c for c in cases if c["answerable"] and c.get("source") not in (None, "stage_guide")]
    off_topic = [c for c in cases if not c["answerable"]]
    first_ms = []
    started = time.perf_counter()
    retriever.retrieve("warm up", "FOS", limit=5)
    first_ms.append((time.perf_counter() - started) * 1000)
    latencies, ranks = [], []
    for run in range(runs):
        for case in answerable:
            started = time.perf_counter()
            result = retriever.retrieve(case["q"], "FOS", limit=5)
            latencies.append((time.perf_counter() - started) * 1000)
            if run == 0:
                sources = [str(getattr(h.chunk, "source", "") or "") for h in (result.hits or [])]
                ranks.append(_rank(sources, [case["source"]]))
    confident_off = 0
    for case in off_topic:
        result = retriever.retrieve(case["q"], "FOS", limit=5)
        confident_off += bool(result and result.confident)
    n = len(answerable)
    latencies.sort()
    return {
        "retriever": name, "questions": n, "off_topic": len(off_topic),
        "p_at_1": round(sum(1 for r in ranks if r == 1) / n, 3),
        "mrr_at_5": round(sum(1 / r for r in ranks if r) / n, 3),
        "hit_at_5": round(sum(1 for r in ranks if r) / n, 3),
        "latency_ms": {"median": round(statistics.median(latencies), 1),
                       "p95": round(latencies[int(0.95 * (len(latencies) - 1))], 1), "n": len(latencies)},
        "first_query_ms": round(first_ms[0], 1),
        "confident_on_off_topic": f"{confident_off}/{len(off_topic)}",
        "misses": [c["id"] for c, r in zip(answerable, ranks) if r != 1],
    }


def main(out: Path, runs: int) -> None:
    from app.knowledge import default_threshold, get_repository, vector_threshold
    from app.knowledge.retriever import EmbeddingRetriever, LexicalRetriever

    cases = json.loads(CORPUS.read_text(encoding="utf-8"))
    repository = get_repository()
    report = {"corpus": str(CORPUS.relative_to(ROOT)), "runs": runs, "results": []}
    report["results"].append(_measure("bm25", LexicalRetriever(repository, default_threshold=default_threshold()),
                                      cases, runs))
    os.environ.setdefault("EMBEDDING_PROVIDER", "ollama")
    from app.knowledge.embeddings import get_query_embedder, provider_name

    if provider_name() == "ollama":
        dense = EmbeddingRetriever(repository, get_query_embedder(), default_threshold=vector_threshold())
        report["results"].append(_measure("dense_nomic", dense, cases, runs))
    else:
        report["dense_skipped"] = f"provider {provider_name()} is not ollama"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for r in report["results"]:
        print(f"{r['retriever']:12s} n={r['questions']} P@1={r['p_at_1']} MRR@5={r['mrr_at_5']} "
              f"hit@5={r['hit_at_5']} median={r['latency_ms']['median']} ms p95={r['latency_ms']['p95']} ms "
              f"first={r['first_query_ms']} ms off-topic-confident={r['confident_on_off_topic']}")
    print("written", out)


def calibrate(out: Path) -> None:
    """
    THRESHOLD SWEEP for the dense retriever: at each threshold, how many answerable questions are confidently
    answered from the right file (recall@1 among confident), how many confident answers are wrong (precision), and
    how many off-topic questions are confidently answered (false confidence). Scores are computed once per query.
    """
    from app.knowledge import get_repository, vector_threshold
    from app.knowledge.embeddings import get_query_embedder
    from app.knowledge.retriever import EmbeddingRetriever

    cases = json.loads(CORPUS.read_text(encoding="utf-8"))
    dense = EmbeddingRetriever(get_repository(), get_query_embedder(), default_threshold=0.0)
    rows = []
    for case in cases:
        if case.get("source") == "stage_guide":
            continue
        result = dense.retrieve(case["q"], "FOS", limit=1, threshold=0.0)
        top = result.hits[0] if result.hits else None
        rows.append({"answerable": case["answerable"], "score": top.score if top else 0.0,
                     "right": bool(top and case.get("source") and case["source"] in str(top.chunk.source))})
    sweep = []
    for t in [round(0.30 + 0.02 * i, 2) for i in range(26)]:
        ans = [r for r in rows if r["answerable"]]
        off = [r for r in rows if not r["answerable"]]
        conf_ans = [r for r in ans if r["score"] >= t]
        sweep.append({"threshold": t,
                      "recall_at_1": round(sum(r["right"] for r in conf_ans) / len(ans), 3),
                      "precision": round(sum(r["right"] for r in conf_ans) / len(conf_ans), 3) if conf_ans else None,
                      "false_confident_off_topic": f"{sum(r['score'] >= t for r in off)}/{len(off)}",
                      "abstained_answerable": f"{len(ans) - len(conf_ans)}/{len(ans)}"})
    report = {"configured_vector_threshold": vector_threshold(),
              "answerable_scores": sorted(round(r["score"], 3) for r in rows if r["answerable"]),
              "off_topic_scores": sorted(round(r["score"], 3) for r in rows if not r["answerable"]),
              "sweep": sweep}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("configured", report["configured_vector_threshold"])
    print("answerable top-1 scores", report["answerable_scores"])
    print("off-topic  top-1 scores", report["off_topic_scores"])
    for s in sweep:
        print(s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="runs/eval/retrieval.json")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--calibrate", action="store_true", help="sweep the dense threshold instead")
    args = parser.parse_args()
    if args.calibrate:
        calibrate(ROOT / "runs/eval/dense_calibration.json")
    else:
        main(ROOT / args.out, args.runs)
