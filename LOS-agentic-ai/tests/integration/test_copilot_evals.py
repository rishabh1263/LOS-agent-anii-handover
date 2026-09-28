"""
The Copilot EVAL GATE: the golden dataset against the real HTTP API.

Runs `python -m evals.copilot.run` (offline: no model, so every composition
path must fall back within budget) in a SUBPROCESS -- its own uvicorn server,
environment and JWT keys, isolated from this test process -- and requires
every golden case, every security case and every OpenAPI/contract check to
pass. The live variant (`--live`, real qwen2.5:3b) adds the latency eval and
is run on demand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_golden_dataset_passes_against_the_real_http_api(tmp_path):
    report = tmp_path / "eval.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST", "COPILOT_SERVICE_SCOPE_ACCESS"))}
    completed = subprocess.run(
        [sys.executable, "-m", "evals.copilot.run", "--report", str(report)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    summary = "\n".join(line for line in completed.stdout.splitlines()
                        if line.startswith(("GOLDEN", "BY CATEGORY", "OPENAPI", "SECURITY",
                                            "  FAIL")))
    assert completed.returncode == 0, summary or completed.stderr[-3000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    golden = data["golden"]
    assert len(golden) >= 100
    assert all(case["passed"] for case in golden), summary
    security = [c for c in golden if "G" in c["tags"]]
    assert len(security) >= 40 and all(c["passed"] for c in security)
    # Trajectory discipline, proven across the whole set.
    assert all(c["qwen_calls"] <= 1 for c in golden)
    refused = [c for c in golden if c["intent"] == "GUARDRAIL_BLOCKED"]
    assert refused and all(c["reads"] == 0 and c["retrievals"] == 0 and c["qwen_calls"] == 0
                           and not c["tools"] for c in refused)
    assert all(ok for _name, ok, _detail in data["contract"])


def test_the_golden_dataset_is_well_formed():
    import yaml

    data = yaml.safe_load((ROOT / "evals/copilot/golden.yaml").read_text(encoding="utf-8"))
    ids = [c["id"] for c in data["cases"]]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    categories = {t for c in data["cases"] for t in c.get("tags") or []}
    assert set("ABCDEFGHIJKLM") <= categories      # every eval category is covered
    for case in data["cases"]:
        assert "expect" in case and ("message" in case or "raw" in case), case["id"]
        if case.get("follow"):
            assert case["follow"] in ids[:ids.index(case["id"])], case["id"]
