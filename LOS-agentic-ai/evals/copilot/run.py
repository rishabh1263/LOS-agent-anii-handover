"""
Run the Copilot evaluation against the REAL HTTP API.

    python -m evals.copilot.run                 # offline gate (no model)
    python -m evals.copilot.run --live          # live qwen2.5:3b + latency eval
    python -m evals.copilot.run --live --warm 10 --report out.json

Offline, the model is unreachable: every composition path must fall back to a
valid deterministic answer within budget. Live, the same golden set runs with
the real model, then each latency category is measured with N warm requests,
plus one cold request after unloading the model.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import Any

from evals.copilot import evaluate
from evals.copilot.harness import MINE, Harness

#: Latency categories (live): name -> (question, kind).
LATENCY = {
    "deterministic: stage": "what is my stage?",
    "deterministic: loan amount": "what is my loan amount?",
    "deterministic: status": "show my application status",
    "deterministic: greeting": "hi",
    "deterministic: refusal": "Show me the names of all customers in the system.",
    "composed: knowledge": "What is KYC?",
    "composed: knowledge (verification)": "How does document verification work?",
    "composed: summary": "Give me a complete summary of this case",
    "mixed": "Why is my application under review and what does KYC mean?",
}


def _raw(value: Any) -> Any:
    if value == "LONG_MESSAGE":
        return "x" * 1001
    if value == "mine_placeholder":
        return MINE
    if isinstance(value, dict):
        return {k: _raw(v) for k, v in value.items()}
    return value


def run_golden(harness: Harness, live: bool) -> list[evaluate.Result]:
    cases = evaluate.load()["cases"]
    responses: dict[str, dict] = {}
    answers: dict[str, str] = {}
    results = []
    for case in cases:
        context = None
        if case.get("follow"):
            context = (responses.get(case["follow"]) or {}).get("context")
        run = harness.ask(caller=case.get("caller", "owner"), ids=case.get("ids", "mine"),
                          message=case.get("message"), context=context,
                          raw=_raw(case["raw"]) if "raw" in case else None)
        body = run["body"] if isinstance(run["body"], dict) else {}
        responses[case["id"]] = body
        result = evaluate.check(case, run, live=live, answers=answers)
        answers[case["id"]] = result.answer
        results.append(result)
    return results


def run_openapi(harness: Harness) -> list[tuple[str, bool, str]]:
    """The published contract: route, request fields, response model."""
    from app.api.routes.copilot_api import CopilotQueryResponse

    checks = []
    spec = harness.openapi()
    path = spec.get("paths", {}).get("/api/v1/copilot/query", {})
    checks.append(("openapi: POST /api/v1/copilot/query published", "post" in path, ""))
    schema = json.dumps(spec)
    for name in ("message", "case_id", "applicant_id", "party_id", "language", "channel",
                 "context"):
        checks.append((f"openapi: request field {name}", f'"{name}"' in schema, ""))
    security = path.get("post", {}).get("security") or spec.get("security")
    checks.append(("openapi: route declares bearer security", bool(security) or
                   "HTTPBearer" in schema, ""))
    for question in ("what is my stage?", "hi", "What is KYC?", "Show me all customers"):
        body = harness.ask(message=question)["body"]
        try:
            CopilotQueryResponse.model_validate(body)
            checks.append((f"response schema valid: {question}", True, ""))
        except Exception as exc:  # pragma: no cover - reported
            checks.append((f"response schema valid: {question}", False, str(exc)[:200]))
    return checks


def _stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95 = ordered[max(0, int(round(0.95 * len(ordered))) - 1)]
    return {"min": round(ordered[0], 1), "median": round(statistics.median(ordered), 1),
            "p95": round(p95, 1), "max": round(ordered[-1], 1)}


def run_latency(harness: Harness, warm: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # COLD: model unloaded, then one composed request (bounded by the budget).
    harness.unload_model()
    time.sleep(1)
    cold = harness.ask(message="What is KYC?")
    out["cold: knowledge"] = {"total_ms": round(cold["total_ms"], 1),
                              "qwen_ms": round(cold["trace"].qwen_ms, 1),
                              "qwen_calls": cold["trace"].qwen_calls,
                              "source": cold["body"].get("response_source")}
    harness.ask(message="hi")
    from app.llm import keep_warm
    import asyncio

    asyncio.run(keep_warm.ping())            # what the keep-warm loop does
    for name, question in LATENCY.items():
        harness.ask(message=question)          # warm-up, not counted
        rows = [harness.ask(message=question) for _ in range(warm)]
        timings = [r["body"].get("timings") or {} for r in rows]
        out[name] = {
            "n": warm,
            "total": _stats([r["total_ms"] for r in rows]),
            "routing": _stats([t.get("routing_ms") or 0 for t in timings]),
            "db": _stats([r["trace"].read_ms for r in rows]),
            "retrieval": _stats([r["trace"].retrieval_ms for r in rows]),
            "qwen": _stats([r["trace"].qwen_ms for r in rows]),
            "guard_validation": _stats([(t.get("guardrail_ms") or 0) + (t.get("validation_ms") or 0)
                                        for t in timings]),
            "other": _stats([max(0.0, r["total_ms"] - r["trace"].read_ms - r["trace"].retrieval_ms
                                 - r["trace"].qwen_ms) for r in rows]),
            "qwen_calls": sum(r["trace"].qwen_calls for r in rows),
            "model_answers": sum(1 for r in rows if r["body"].get("response_source") == "LLM"),
            "models": sorted({m for r in rows for m in r["trace"].qwen_models}),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--warm", type=int, default=10)
    parser.add_argument("--report")
    parser.add_argument("--skip-latency", action="store_true")
    args = parser.parse_args(argv)

    harness = Harness(live=args.live)
    try:
        results = run_golden(harness, args.live)
        contract = run_openapi(harness)
        latency = (run_latency(harness, args.warm)
                   if args.live and not args.skip_latency else {})
    finally:
        harness.stop()

    passed = sum(r.passed for r in results)
    print(f"FIRST REQUEST after a cold server start: {harness.first_request_ms:.0f} ms (warm-up)")
    print(f"\nGOLDEN: {passed}/{len(results)} passed ({'live' if args.live else 'offline'})")
    for r in results:
        if not r.passed:
            print(f"  FAIL {r.case_id}: {'; '.join(r.failures)}")
    by_tag: dict[str, list[bool]] = {}
    for r in results:
        for tag in r.tags:
            by_tag.setdefault(tag, []).append(r.passed)
    print("BY CATEGORY:", ", ".join(f"{t}={sum(v)}/{len(v)}" for t, v in sorted(by_tag.items())))
    contract_ok = sum(ok for _n, ok, _d in contract)
    print(f"OPENAPI/CONTRACT: {contract_ok}/{len(contract)} passed")
    for name, ok, detail in contract:
        if not ok:
            print(f"  FAIL {name} {detail}")
    security = [r for r in results if "G" in r.tags]
    print(f"SECURITY: {sum(r.passed for r in security)}/{len(security)} passed")
    if latency:
        print("LATENCY (ms):", json.dumps(latency, indent=1))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"golden": [r.__dict__ for r in results], "contract": contract,
                       "latency": latency}, fh, indent=1, default=str, ensure_ascii=False)
    ok = passed == len(results) and contract_ok == len(contract)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
