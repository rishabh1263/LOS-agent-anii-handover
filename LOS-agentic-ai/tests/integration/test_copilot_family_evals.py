"""
THE SEMANTIC FAMILY gate (evals/copilot/families.py), through the real HTTP
API: case vs knowledge by framing (not by "my" / "I"), knowledge answers with
their citation and honest no-answers, parties in any case with unknown people
refused and claims never adopted, conversational continuations, and field
precision. Every family must pass in full.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_semantic_families_pass_against_the_real_http_api(tmp_path):
    report = tmp_path / "families.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.families", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1200)
    assert report.exists(), completed.stderr[-3000:]
    summary = json.loads(report.read_text(encoding="utf-8"))["summary"]
    failures = "\n".join(line for line in completed.stdout.splitlines() if line.startswith("FAIL"))
    assert summary["passed"] == summary["total"], failures or summary
    for family in ("case_vs_knowledge", "rag", "party", "conversation", "field"):
        assert family in summary["by_family"], summary
