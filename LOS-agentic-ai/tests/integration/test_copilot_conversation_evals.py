"""
THE CONVERSATION-STATE gate: 100+ multi-turn scenarios through the real HTTP API.

Runs `python -m evals.copilot.conversations` (its own uvicorn server, JWT
keys and store, in a subprocess) and requires every scenario -- pending
clarification, yes/no versus either/or, ordinal and natural option choice,
new topic, cancellation, correction, negation, referents, replay, party
switch, multilingual, typos, stale context, security -- to pass on every
graded turn: outcome, intent, clarification, zero reads before resolution,
no retrieval, no model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_multi_turn_conversations_pass_against_the_real_http_api(tmp_path):
    report = tmp_path / "conversations.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.conversations", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    summary = "\n".join(line for line in completed.stdout.splitlines()
                        if line.startswith(("FAIL", "CONVERSATIONS", "      -")))
    assert completed.returncode == 0, summary or completed.stderr[-3000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    s = data["summary"]
    assert s["passed"] == s["total"] >= 100, summary
    assert len(s["by_category"]) >= 24
    assert s["latency_ms"]["p95"] < 2000, s["latency_ms"]
