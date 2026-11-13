"""
PLANNER BENCHMARK (2026-11-02): the planner's intent / tool / argument accuracy and latency per model, on the 115
labelled messages of planner_set.yaml -- against the LIVE local model (never a mock).

    python -m evals.copilot.planner_bench [--models qwen3:4b-instruct-2507-q4_K_M,phi4-mini:latest] [--out out.json]

The first call per model is a warm-up (model load, prompt cache) and is not scored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

import yaml

SET = Path(__file__).with_name("planner_set.yaml")


def items() -> list[dict]:
    data = yaml.safe_load(SET.read_text(encoding="utf-8"))
    return [{**it, "group": g} for g, lst in data.items() for it in lst]


def _history(it: dict) -> list[dict[str, str]]:
    out = []
    for user, assistant in it.get("history") or []:
        out += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
    return out


async def bench(model: str) -> dict:
    from app.agents.applicant.copilot.planner import plan as P
    from app.llm import turn_budget

    # WARM-UP (not scored): loads the model and puts the fixed system prompt in Ollama's prompt cache -- a cold
    # 800-token prompt takes ~13 s on a CPU host; a deployment does this once at start-up
    spec, saved, cap = P.cfg(), P.cfg().get("timeout_seconds"), turn_budget._max_seconds
    spec["timeout_seconds"] = 90.0
    turn_budget._max_seconds = lambda: 0.0                    # the warm-up only: no per-turn cap
    turn_budget.begin()
    try:
        await P.plan("hello", model=model)
    finally:
        spec["timeout_seconds"], turn_budget._max_seconds = saved, cap
    rows = []
    for it in items():
        turn_budget.begin()                                   # one chat turn per message, as the API does
        p = await P.plan(it["q"], _history(it), case_open=bool(it.get("case")), model=model)
        want_args = it.get("args") or {}
        args_ok = all(str(p.args.get(k)) == str(v) for k, v in want_args.items())
        rows.append({"group": it["group"], "q": it["q"], "want": [it["intent"], it["tool"], want_args],
                     "got": [p.intent, p.tool, p.args], "error": p.error, "ms": p.ms,
                     "intent_ok": p.intent == it["intent"], "tool_ok": p.tool == it["tool"],
                     "all_ok": p.intent == it["intent"] and p.tool == it["tool"] and args_ok})
    lat = sorted(r["ms"] for r in rows)
    groups: dict[str, list[bool]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r["all_ok"])
    return {"model": model, "n": len(rows), "intent_acc": sum(r["intent_ok"] for r in rows) / len(rows),
            "tool_acc": sum(r["tool_ok"] for r in rows) / len(rows),
            "all_acc": sum(r["all_ok"] for r in rows) / len(rows),
            "errors": sum(1 for r in rows if r["error"]),
            "by_group": {k: f"{sum(v)}/{len(v)}" for k, v in groups.items()},
            "latency_ms": {"p50": statistics.median(lat), "p95": lat[int(0.95 * len(lat)) - 1], "max": lat[-1]},
            "rows": rows}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--models", default="qwen3:4b-instruct-2507-q4_K_M")
    p.add_argument("--out", default="planner_bench.json")
    a = p.parse_args()
    reports = [asyncio.run(bench(m.strip())) for m in a.models.split(",") if m.strip()]
    Path(a.out).write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    for r in reports:
        print(json.dumps({k: v for k, v in r.items() if k != "rows"}, indent=1))
        for row in r["rows"]:
            if not row["all_ok"]:
                print("  MISS", row["group"], repr(row["q"]), "want", row["want"], "got", row["got"], row["error"] or "")


if __name__ == "__main__":
    main()
