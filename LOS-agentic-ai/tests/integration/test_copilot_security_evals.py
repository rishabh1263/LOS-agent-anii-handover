"""
The HARD SECURITY gate: attacks through the real HTTP API.

Runs `python -m evals.copilot.security` (its own uvicorn server, JWT keys
and store, in a subprocess) and requires every attack class to be refused
with zero repository reads, zero retrieval, zero model calls, no sensitive
content, no emoji -- and byte-identical refusals for another customer's
case and a case that does not exist.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_attack_is_refused_with_zero_reads_against_the_real_http_api(tmp_path):
    report = tmp_path / "security.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    env["PYTHONIOENCODING"] = "utf-8"          # the console, not the API, is cp1252
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.security", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    summary = "\n".join(line for line in completed.stdout.splitlines()
                        if line.startswith(("FAIL", "SECURITY", "      -")))
    assert completed.returncode == 0, summary or completed.stderr[-3000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    s = data["summary"]
    assert s["passed"] == s["total"], summary
    assert s["classes"] >= 24
    assert s["total"] >= 50
    for case in data["cases"]:
        assert case["passed"], case
        if case["class"] != "U.existence":
            assert case["reads"] == [], case
    assert s["latency_ms"]["p95"] < 1000, s["latency_ms"]
