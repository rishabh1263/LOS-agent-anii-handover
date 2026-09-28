"""
The Credit Underwriting HTTP gate: real uvicorn server, real RS256 JWTs.

Runs `python -m evals.credit.http_e2e` in a SUBPROCESS (its own server,
environment and signing keys, isolated from this test process) and requires
every scenario to pass: success, 401, wrong scope, wrong stage, missing /
foreign case (identical 403), provider failure, partial and insufficient
evidence, replay idempotency, a body that tries to carry a caller, OpenAPI,
audit without token material -- plus measured warm latency.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_credit_underwriting_passes_against_the_real_http_api(tmp_path):
    report = tmp_path / "credit_http.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    completed = subprocess.run(
        [sys.executable, "-m", "evals.credit.http_e2e", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    summary = "\n".join(line for line in completed.stdout.splitlines()
                        if line.startswith(("FAIL", "HTTP E2E", "LATENCY")))
    assert completed.returncode == 0, summary or completed.stderr[-3000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["results"] and all(ok for _name, ok, _detail in data["results"]), summary
    latency = data["latency_ms"]
    # An engineering sanity bound, not an SLO: the agent's own path is
    # deterministic reads + rules (the memo model is down in this run).
    assert latency["warm_p95"] < 2000, latency
