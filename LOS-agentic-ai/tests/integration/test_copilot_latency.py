"""
THE LATENCY gate (evals/copilot/latency.py): normal requests -- case facts,
checklist, KYC, verification, knowledge, conversation, mixed -- answer end to
end within the 2-3 s target over the real HTTP API, with no model call on the
deterministic paths. The bound is generous on purpose (a shared CI machine);
the measured numbers are far below it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_normal_requests_answer_within_the_latency_target(tmp_path):
    report = tmp_path / "latency.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.latency", "--report", str(report), "--repeat", "1"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900)
    assert report.exists(), completed.stderr[-3000:]
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["overall_ms"]["p95"] <= 3000, summary
    assert summary["model_calls_per_request"] == 0, summary
    for kind in ("deterministic", "conversation", "knowledge", "verification", "mixed"):
        assert summary["by_kind_ms"][kind]["p95"] <= 3000, (kind, summary)
