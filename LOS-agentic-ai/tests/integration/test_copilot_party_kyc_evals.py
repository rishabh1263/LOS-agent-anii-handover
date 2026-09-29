"""
THE PARTY / CO-APPLICANT / KYC gate (evals/copilot/party_kyc.py), through the
real HTTP API: every co-applicant field from the co-applicant's own record,
KYC per person with its three honest outcomes, party context across turns
(explicit "my" always wins), compound questions split per party, missing
fields said honestly, and every cross-party or bulk request refused with zero
reads. The attack questions in samples/documents/*.csv are asked as typed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_party_and_kyc_conversations_hold_against_the_real_http_api(tmp_path):
    report = tmp_path / "party_kyc.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.party_kyc", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1200)
    assert report.exists(), completed.stderr[-3000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    s = data["summary"]
    failed = "\n".join(f"{r['case']}: {r['question']} -> {r['problems']}"
                       for r in data["rows"] if not r["passed"])
    assert s["passed"] == s["total"], failed
    counts = {tag: int(v.split("/")[1]) for tag, v in s["by_tag"].items()}
    assert counts["co"] >= 30 and counts["kyc"] >= 30 and counts["followup"] >= 20
    assert counts["compound"] >= 15 and counts["missing"] >= 15 and counts["hinglish"] >= 15
    assert counts["security"] >= 10 and counts["csv"] >= 50
    assert s["qwen_calls"] == 0
    assert s["latency_ms"]["p95"] < 2000, s["latency_ms"]
