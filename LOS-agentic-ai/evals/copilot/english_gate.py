"""
THE ENGLISH GATE (2026-10-28) -- one command that says whether the chatbot's English got better or worse:

    python -m evals.copilot.english_gate --tag before          # with the LOS server (8010) running, auth off (dev)
    python -m evals.copilot.english_gate --tag after --compare before

Runs, one after the other (never in parallel: they share one CPU and one model):
    1. ANSWERS  agent_eval on the FROZEN English held-out conversations (agent_heldout_en.yaml; its md5 is checked)
                and on the English dev conversations (agent_dev_en.yaml, the set tuning may look at)
    2. ROUTING  understand_eval on the held-out meaning sets (e-h): correct, not understood, confident-and-wrong
Writes runs/english_gate_<tag>.json; --compare prints the change per number. A drop on the held-out set is a failed
gate (exit 1). Never tuned on: held-out sentences are never copied into config (they would stop measuring).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "runs"
FROZEN_MD5 = "e058a51589bd86944db5810275a31493"          # agent_heldout_en.yaml as written 2026-10-13
PAN = ROOT / "samples" / "documents" / "demo_pan_rahul_sharma.png"


def _py(*args: str, timeout: int = 3600) -> int:
    return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, timeout=timeout,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def _answers(name: str) -> dict:
    _py("evals.copilot.agent_eval", "--set", name, "--pan", str(PAN))
    r = json.loads((RUNS / f"agent_eval_{name}.json").read_text(encoding="utf-8"))
    return {"turns": r["turns"], "correct": r["correct"], "pct": round(100 * r["correct"] / max(1, r["turns"]), 1),
            "p50_s": r["latency"]["p50"], "p95_s": r["latency"]["p95"], "max_s": r["latency"]["max"],
            "misses": [{"q": row["q"], "why": row["why"]} for row in r["rows"] if not row["ok"]]}


def _routing() -> dict:
    _py("evals.copilot.understand_eval", "--sets", "e,f,g,h", "--tag", "gate")
    files = sorted(RUNS.glob("understand_eval_*_gate.json"), key=lambda p: p.stat().st_mtime)
    r = json.loads(files[-1].read_text(encoding="utf-8"))
    total = {"n": 0, "correct": 0, "not_understood": 0, "clear_wrong": 0, "to_model": 0, "model_failed": 0}
    for s in "efgh":
        x = r[s]
        total["n"] += x["n"]
        total["correct"] += int(str(x["correct"]).split("/")[0])
        for k in ("not_understood", "clear_wrong", "to_model", "model_failed"):
            total[k] += x[k]
    total["pct"] = round(100 * total["correct"] / max(1, total["n"]), 1)
    return total


def _routing_valid(v: dict, max_failed_share: float) -> bool:
    """A routing run where most model calls failed measured the CPU, not the understanding (2026-10-28: the gate's
    run, with the machine busy, failed 110 of 118 model calls -- 73% -- while the same set on an idle machine scored
    13 points higher)."""
    return v.get("to_model", 0) == 0 or v.get("model_failed", 0) / v["to_model"] <= max_failed_share


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--compare", default="")
    ap.add_argument("--skip-routing", action="store_true")
    ap.add_argument("--max-failed-share", type=float, default=0.25,
                    help="routing runs with a larger share of failed model calls are reported as not measured")
    args = ap.parse_args()
    held = ROOT / "evals" / "copilot" / "agent_heldout_en.yaml"
    if hashlib.md5(held.read_bytes()).hexdigest() != FROZEN_MD5:
        print("agent_heldout_en.yaml CHANGED since it was frozen -- the gate would not measure anything; restore it")
        return 2
    out = {"heldout_en": _answers("heldout_en"), "dev_en": _answers("dev_en")}
    if not args.skip_routing:
        out["routing"] = _routing()
    RUNS.mkdir(exist_ok=True)
    (RUNS / f"english_gate_{args.tag}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for k in ("heldout_en", "dev_en"):
        v = out[k]
        print(f"{k:<11} {v['correct']}/{v['turns']} ({v['pct']}%)  p50 {v['p50_s']}s  p95 {v['p95_s']}s  max {v['max_s']}s")
    if "routing" in out:
        v = out["routing"]
        v["valid"] = _routing_valid(v, args.max_failed_share)
        print(f"routing     {v['correct']}/{v['n']} ({v['pct']}%)  not understood {v['not_understood']}  "
              f"confident-and-wrong {v['clear_wrong']}  model failed {v.get('model_failed', '?')}/{v.get('to_model', '?')}"
              + ("" if v["valid"] else "  -- NOT MEASURED: most model calls failed (machine busy?); run it again idle"))
    failed = False
    if args.compare:
        old = json.loads((RUNS / f"english_gate_{args.compare}.json").read_text(encoding="utf-8"))
        print(f"\nvs {args.compare}:")
        for k in ("heldout_en", "dev_en"):
            print(f"  {k:<11} {old[k]['pct']}% -> {out[k]['pct']}%   p50 {old[k]['p50_s']}s -> {out[k]['p50_s']}s")
        if "routing" in out and "routing" in old and out["routing"]["valid"] and old["routing"].get("valid", True):
            print(f"  routing     {old['routing']['pct']}% -> {out['routing']['pct']}%   confident-and-wrong "
                  f"{old['routing']['clear_wrong']} -> {out['routing']['clear_wrong']}")
        failed = out["heldout_en"]["pct"] < old["heldout_en"]["pct"]
        print("\nGATE:", "FAILED (held-out English got worse)" if failed else "passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
