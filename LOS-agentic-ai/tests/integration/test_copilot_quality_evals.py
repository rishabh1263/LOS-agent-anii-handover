"""
THE ANSWER-QUALITY gate: intent, source, facts, context, security and cost,
through the real HTTP API (evals/copilot/quality.py). A case passes only when
the capability is right, its tools are the authoritative source, the answer
carries the seeded record's value (or the honest missing-field sentence), no
clarification was asked where none was needed, no model ran offline, reads
stay within budget and each latency component stays within bounds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_answer_quality_holds_on_every_dimension_against_the_real_http_api(tmp_path):
    report = tmp_path / "quality.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.quality", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900)
    summary = "\n".join(line for line in completed.stdout.splitlines()
                        if line.startswith(("FAIL", "QUALITY")))
    assert completed.returncode == 0, summary or completed.stderr[-3000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    s = data["summary"]
    assert s["passed"] == s["total"] >= 20, summary
    assert s["model_calls"] == 0                      # offline: no model, ever
    assert s["reads"]["max"] <= 20 and s["reads"]["p50"] <= 12
    assert s["latency_ms"]["security_ms"]["p95"] < 25
