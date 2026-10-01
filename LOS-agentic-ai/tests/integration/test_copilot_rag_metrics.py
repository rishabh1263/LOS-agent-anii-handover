"""
THE RAG METRICS gate (evals/copilot/rag_metrics.py): answerable questions are
retrieved, answered with the expected evidence, cited and grounded; an
unanswerable one gets the honest no-answer. False confidence must stay zero --
the metric is not "how many were answered".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ratio(value: str) -> tuple[int, int]:
    got, total = value.split("/")
    return int(got), int(total)


def test_rag_is_grounded_cited_and_never_falsely_confident(tmp_path):
    report = tmp_path / "rag.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.rag_metrics", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900)
    assert report.exists(), completed.stderr[-3000:]
    summary = json.loads(report.read_text(encoding="utf-8"))["summary"]
    assert _ratio(summary["false_confidence"])[0] == 0, summary
    for key in ("retrieval_hit", "answer_correct", "citation_correct", "grounded", "no_answer_accuracy"):
        got, total = _ratio(summary[key])
        assert got == total, (key, summary)
