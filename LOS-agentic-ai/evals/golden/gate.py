"""
THE QUALITY GATE (FOS plan 9.7 / STEP 10a) -- run before a commit:

    python -m evals.golden.gate                 # compare with evals/golden/baseline.json, exit 1 on a regression
    python -m evals.golden.gate --record        # record the current scores as the new baseline

1. the FOS-plan golden set (evals/golden/*.yaml via evals.golden.run): FAIL when accuracy drops by more than
   `max_accuracy_drop` points, or the wrong-answer / wrong-data rate rises at all;
2. every copilot eval file (tests/integration/test_copilot_*evals.py), ONE PER PROCESS -- run together in one
   process they hang (open item, FOS plan 9.7) -- FAIL on any failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("baseline.json")
MAX_ACCURACY_DROP = 1.0


def _evals() -> dict[str, str]:
    import os

    # the eval files run on the CODE DEFAULTS: the golden run's EVAL_OVERRIDE_* (every plan flag on) must not leak
    env = {k: v for k, v in os.environ.items() if not k.startswith("EVAL_OVERRIDE_")}
    out = {}
    for path in sorted((ROOT / "tests" / "integration").glob("test_copilot_*evals.py")):
        started = time.perf_counter()
        r = subprocess.run([sys.executable, "-m", "pytest", str(path), "-q", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, timeout=900, env=env)
        tail = (r.stdout.strip().splitlines() or ["?"])[-1]
        out[path.name] = ("PASS" if r.returncode == 0 else "FAIL") + f" ({time.perf_counter() - started:.0f}s) {tail}"
        print(path.name, out[path.name][:120])
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--skip-evals", action="store_true")
    args = parser.parse_args()
    from evals.golden import run as golden

    summary = golden.run(str(ROOT / "runs" / "golden" / "report.json"))
    evals = {} if args.skip_evals else _evals()
    current = {"golden": summary, "evals": evals, "recorded": time.strftime("%Y-%m-%d %H:%M")}
    if args.record:
        BASELINE.write_text(json.dumps(current, indent=2), encoding="utf-8")
        print(f"baseline recorded -> {BASELINE}")
        return 0
    if not BASELINE.exists():
        print("no baseline yet: run with --record first")
        return 1
    base = json.loads(BASELINE.read_text(encoding="utf-8"))["golden"]
    problems = []
    if summary["accuracy"] < base["accuracy"] - MAX_ACCURACY_DROP:
        problems.append(f"accuracy {summary['accuracy']} < baseline {base['accuracy']} - {MAX_ACCURACY_DROP}")
    for key in ("wrong_answer_rate", "wrong_data_rate"):
        if summary[key] > base[key]:
            problems.append(f"{key} {summary[key]} > baseline {base[key]}")
    problems += [f"{name}: {result}" for name, result in evals.items() if result.startswith("FAIL")]
    print("\nGATE:", "PASS" if not problems else "FAIL")
    for p in problems:
        print(" -", p)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
