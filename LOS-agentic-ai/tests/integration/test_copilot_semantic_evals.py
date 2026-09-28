"""
The SEMANTIC UNDERSTANDING gate: paraphrases through the real HTTP API.

Runs `python -m evals.copilot.semantic` (its own uvicorn server, JWT keys
and store, in a subprocess) and requires every case -- seen, unseen,
multilingual, the two-turn "this stage" follow-up and the cross-customer
refusal -- to pass on every graded dimension, with the understanding model
never called for a confidently parsed question.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_semantic_understanding_passes_against_the_real_http_api(tmp_path):
    report = tmp_path / "semantic.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    env["PYTHONIOENCODING"] = "utf-8"          # the console, not the API, is cp1252
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.semantic", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    summary = "\n".join(line for line in completed.stdout.splitlines()
                        if line.startswith(("FAIL", "SEMANTIC", "      -")))
    assert completed.returncode == 0, summary or completed.stderr[-3000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    s = data["summary"]
    assert s["passed"] == s["total"], summary
    assert s["unseen_passed"] == s["unseen_total"] >= 50
    assert s["questions"] >= 80
    # Offline the model is unreachable: understanding never depended on it.
    assert s["qwen_understanding_calls"] == 0
    assert s["latency_ms"]["p95"] < 3000, s["latency_ms"]
