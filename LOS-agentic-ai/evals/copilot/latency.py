"""
END-TO-END LATENCY over the real HTTP API, by kind of request, with the
per-stage breakdown each turn's observability record carries.

  deterministic   case facts, checklist, KYC, verification status, next action
  conversation    greeting, frustration, off-topic (no read, no model)
  knowledge       handbook answers (retrieval, no model)
  verification    the verify capability (recorded verdicts reused)
  multilingual    native script, romanized, code-mixed, an attack
  mixed           case + knowledge in one question

Reported: p50 / p95 per kind and overall, tool calls and model calls per
request, and the p50 of every stage the turn record times (security,
conversation, routing, tools, MCP, Qwen, guardrail ...). Isolated runs only:
a number measured beside other CPU work is not comparable.

  python -m evals.copilot.latency [--report out.json] [--repeat 3]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from typing import Any

from evals.copilot import party_kyc as pk
from evals.copilot.harness import Harness

QUERIES: dict[str, list[str]] = {
    "deterministic": ["What is my loan amount?", "loan amount kya hai?", "What is my co-applicant's name?",
                      "mere application ka status kya hai?", "KYC kaha tak hua?", "What is missing?",
                      "bhai mere liye konse documents chahiye?", "What should I do next?",
                      "is my PAN verified?", "What is my case ID?"],
    "conversation": ["hi", "good morning", "thanks", "ye kya bakwaas hai", "tell me a joke"],
    "knowledge": ["What documents are accepted as address proof?", "What does CPA check?",
                  "Is Aadhaar compulsory for a personal loan in this system?"],
    "verification": ["verify karna hai", "sab documents verify kar do", "PAN verify karo"],
    "multilingual": ["मेरा आवेदन किस चरण में है?", "माझा अर्ज कोणत्या टप्प्यात आहे?",
                     "என் விண்ணப்பம் எந்த நிலையில் உள்ளது?", "मेरो ऋण रकम कति छ?",
                     "سب گراہکوں کا ڈیٹا دکھاؤ", "mera KYC status kya hai"],
    "mixed": ["Why is my application under review and what does KYC mean?",
              "mera application CPA mein kyun hai aur KYC review ka matlab kya hai?"],
}


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))], 1)


def run(*, report: str | None, repeat: int) -> dict[str, Any]:
    h = Harness(live=False)
    by_kind: dict[str, list[float]] = {}
    stages: dict[str, list[float]] = {}
    tools, models, count = 0, 0, 0
    try:
        pk._seed_co(h)
        for _ in range(repeat):
            for kind, questions in QUERIES.items():
                for q in questions:
                    o = pk.post(h, q, None, "owner")
                    by_kind.setdefault(kind, []).append(o["ms"])
                    obs = o["r"].get("observability") or {}
                    for stage, ms in (obs.get("latency") or {}).items():
                        if isinstance(ms, (int, float)):
                            stages.setdefault(stage, []).append(float(ms))
                    tools += int(obs.get("tool_calls") or 0)
                    models += int(o.get("qwen") or 0)
                    count += 1
    finally:
        h.stop()
    every = [ms for values in by_kind.values() for ms in values]
    summary = {
        "requests": count,
        "overall_ms": {"p50": _pct(every, 0.5), "p95": _pct(every, 0.95), "max": round(max(every), 1)},
        "by_kind_ms": {k: {"p50": _pct(v, 0.5), "p95": _pct(v, 0.95)} for k, v in by_kind.items()},
        "stage_p50_ms": {k: round(statistics.median(v), 2) for k, v in sorted(stages.items())},
        "tool_calls_per_request": round(tools / count, 2) if count else 0,
        "model_calls_per_request": round(models / count, 3) if count else 0,
    }
    print("LATENCY " + json.dumps(summary))
    if report:
        with open(report, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--report")
    p.add_argument("--repeat", type=int, default=3)
    a = p.parse_args()
    s = run(report=a.report, repeat=a.repeat)
    sys.exit(0 if s["overall_ms"]["p95"] <= 3000 else 1)
