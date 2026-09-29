"""
ANSWER-QUALITY EVAL -- beyond the route: the source, the facts, the cost.

    python -m evals.copilot.quality [--live] [--report out.json]

Every case is graded through the real HTTP API on SIX dimensions, and passes
only when all six hold:

    intent    the capability that answered is the one the question means
    source    the tools called are that capability's authoritative source
              (capabilities.REGISTRY), and nothing else
    facts     the answer carries the value the SEEDED RECORD holds (read from
              the harness store), or the honest missing-field sentence
    context   no clarification where the question was clear; language kept
    security  no other customer's value; no model where none is allowed
    cost      repository reads within the capability's budget, no retrieval,
              latency by component within bounds (from `observability`)

`--live` adds the FIDELITY section: MODEL-route answers phrased by the real
model must keep every fact of the deterministic answer.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from typing import Any

from evals.copilot.harness import CANARIES, MINE, TRACE, Harness

#: Read budget per intent (repository reads observed by the harness).
READ_BUDGET = {"APPLICANT_PROFILE": 14, "APPLICATION_STAGE": 12, "DOCUMENTS_PENDING": 14,
               "DOCUMENTS_MISSING": 14, "DOCUMENT_VERIFICATION": 12, "DOCUMENTS_REQUIRED": 14,
               "APPLICATION_STATUS": 20, "NEXT_ACTION": 16, "READINESS": 16, "PENDING_ITEMS": 14,
               "CASE_HISTORY": 20, "CASE_FINDINGS": 20, "KYC_RESULT": 20, "FOS_KNOWLEDGE": 6,
               "STAGE_PROCESS": 12}
#: Tools each intent may call (the registry's sources, plus the checklist the
#: planner appends to case-scoped reads).
TOOLS = {
    "APPLICANT_PROFILE": {"applicant.get", "application.get", "documents.checklist"},
    "APPLICATION_STAGE": {"applicant.360", "documents.checklist"},
    "APPLICATION_STATUS": {"application.get", "documents.checklist"},
    "DOCUMENTS_PENDING": {"documents.get", "workflow.pending_items", "documents.checklist"},
    "DOCUMENTS_MISSING": {"documents.checklist"},
    "DOCUMENTS_REQUIRED": {"documents.checklist"},
    "DOCUMENT_VERIFICATION": {"documents.verification", "documents.get", "documents.checklist"},
    "NEXT_ACTION": {"workflow.next_action", "documents.checklist"},
    "READINESS": {"workflow.readiness", "documents.checklist"},
    "PENDING_ITEMS": {"workflow.pending_items", "documents.checklist"},
    "CASE_HISTORY": {"application.get", "documents.checklist"},
    "CASE_FINDINGS": {"application.get", "documents.checklist"},
    "KYC_RESULT": {"application.get", "documents.checklist"},
    "FOS_KNOWLEDGE": set(), "STAGE_PROCESS": set(),
}
LATENCY_BOUNDS_MS = {"security_ms": 25, "routing_ms": 60, "semantic_ms": 30}


def _c(q, intents, fact=None, *, language="en", clarify=False, missing=None, model=False):
    return {"q": q, "intents": set(intents), "fact": fact, "language": language,
            "clarify": clarify, "missing": missing, "model": model}


def cases(h: Harness) -> list[dict[str, Any]]:
    """The cases, with the expected FACT read from the seeded store."""
    from app.agents.applicant.copilot.answering import profile

    application = h.repo.get_application(MINE)
    applicant = h.repo.get_applicant(h.ids("mine")["applicant_id"])
    amount = profile._rupees(application.loan_amount) or str(application.loan_amount)
    return [
        _c("What is my loan amount?", {"APPLICANT_PROFILE"}, amount),
        _c("how much loan did i ask for", {"APPLICANT_PROFILE"}, amount),
        _c("mera loan amount kitna hai", {"APPLICANT_PROFILE"}, amount, language="hi-Latn"),
        _c("What is my mobile number?", {"APPLICANT_PROFILE"}, applicant.mobile),
        _c("customer contact number", {"APPLICANT_PROFILE"}, applicant.mobile),
        _c("my email?", {"APPLICANT_PROFILE"}, applicant.email),
        _c("What is my case id?", {"APPLICANT_PROFILE"}, MINE),
        _c("and the case number?", {"APPLICANT_PROFILE"}, MINE),
        _c("What is my applicant id?", {"APPLICANT_PROFILE"}, applicant.applicant_id),
        _c("What is the application number?", {"APPLICANT_PROFILE"}, MINE),
        _c("What is my tenure?", {"APPLICANT_PROFILE"},
           f"{application.tenure_months} months" if application.tenure_months else None,
           missing="tenure" if not application.tenure_months else None),
        _c("What is the property value?", {"APPLICANT_PROFILE"},
           None if not getattr(application, "property_value", None) else str(application.property_value),
           missing="property value" if not getattr(application, "property_value", None) else None),
        _c("Aadhar card number kya hai?", {"APPLICANT_PROFILE"}, None, language="hi-Latn",
           missing="Aadhaar number"),
        _c("What stage am I in?", {"APPLICATION_STAGE"}, "FOS"),
        _c("which stage is this case in?", {"APPLICATION_STAGE"}, "FOS"),
        _c("Which documents are pending?", {"DOCUMENTS_PENDING"}, "pending"),
        _c("What documents are pending and why?", {"DOCUMENTS_PENDING"}, "not been uploaded"),
        _c("Are my documents verified?", {"DOCUMENT_VERIFICATION"}, "PAN"),
        _c("What is my application status?", {"APPLICATION_STATUS"}, "Basic Document Verification"),
        _c("What should I do next?", {"NEXT_ACTION"}, "Address Proof", model=True),
        _c("Is my case ready to hand over?", {"READINESS"}, "Address Proof", model=True),
        _c("what findings were recorded?", {"CASE_FINDINGS"}, "recorded"),
        _c("what is my KYC score?", {"KYC_RESULT"}, "KYC"),
        _c("What is KYC?", {"FOS_KNOWLEDGE"}, "KYC", model=True),
        _c("What happens after this step?", {"STAGE_PROCESS"}, "CPA"),
    ]


def post(h: Harness, message: str) -> dict[str, Any]:
    TRACE.reset()
    headers = {"Authorization": f"Bearer {h.token('owner')}"}
    started = time.perf_counter()
    r = h.http.post("/api/v1/fos/copilot", json={
        "applicant_id": h.ids("mine")["applicant_id"], "case_id": MINE,
        "action": "CUSTOM_QUERY", "message": message}, headers=headers)
    ms = (time.perf_counter() - started) * 1000
    body = r.json() if r.status_code < 500 else {"_raw": r.text}
    return {"status": r.status_code, "r": body, "text": r.text, "ms": ms,
            "reads": list(TRACE.reads), "retrievals": TRACE.retrievals, "qwen": TRACE.qwen_calls}


def _allowed_tools(intent: str) -> set[str]:
    """The intent's planned tools (intents.PLANS) plus the checklist the planner appends."""
    try:
        from app.agents.applicant.copilot.semantics.intents import PLANS, Intent

        planned = set(PLANS.get(Intent(intent), ()))
    except (ValueError, KeyError):
        planned = set()
    if not planned and not TOOLS.get(intent):
        return set()
    return planned | TOOLS.get(intent, set()) | {"documents.checklist"}


def grade(case: dict[str, Any], o: dict[str, Any], *, live: bool) -> dict[str, bool | str]:
    r = o["r"]
    intent = str(r.get("intent") or "")
    answer = str(r.get("answer") or "")
    obs = r.get("observability") or {}
    latency = obs.get("latency") or {}
    tools = set(obs.get("tools") or [])
    allowed = _allowed_tools(intent)
    fact_ok = True
    if case["missing"]:
        fact_ok = bool(re.search(r"(not (been )?provided|haven't provided|not available|not recorded|"
                                 r"don'?t have your .* recorded|isn'?t (available|recorded)|"
                                 r"nahi (kiya|diya|hai|hua)|नहीं|नाही)", answer, re.I)) and \
            case["missing"].lower() in answer.lower()
    elif case["fact"]:
        fact_ok = str(case["fact"]).lower() in answer.lower()
    knowledge = intent in ("FOS_KNOWLEDGE", "STAGE_PROCESS")
    dims = {
        "intent": intent in case["intents"],
        "source": (tools <= allowed) if allowed else (not tools or knowledge),
        "facts": fact_ok,
        "context": not r.get("clarification_required") and
        (((r.get("understanding") or {}).get("frame") or {}).get("language") == case["language"]),
        "security": not any(c in o["text"] for c in CANARIES) and (
            o["qwen"] <= (1 if (case["model"] and live) else 0)),
        "cost": len(o["reads"]) <= READ_BUDGET.get(intent, 20)
        and (o["retrievals"] == 0 or knowledge)
        and all(float(latency.get(k, 0) or 0) <= v for k, v in LATENCY_BOUNDS_MS.items()),
    }
    return dims


def run(*, live: bool, report: str | None) -> int:
    h = Harness(live=live)
    rows = []
    latencies: dict[str, list[float]] = {}
    try:
        for case in cases(h):
            o = post(h, case["q"])
            dims = grade(case, o, live=live)
            r = o["r"]
            obs = r.get("observability") or {}
            for k, v in (obs.get("latency") or {}).items():
                latencies.setdefault(k, []).append(float(v))
            ok = all(dims.values())
            rows.append({"q": case["q"], "passed": ok, "dims": dims, "intent": r.get("intent"),
                         "answer": str(r.get("answer") or "")[:160], "reads": len(o["reads"]),
                         "tools": obs.get("tools"), "model_route": obs.get("model_route"),
                         "model_calls": obs.get("model_calls"), "fidelity": obs.get("fidelity"),
                         "latency": obs.get("latency"), "ms": round(o["ms"], 1)})
            failed = [d for d, v in dims.items() if not v]
            print(f"{'PASS' if ok else 'FAIL'}  {case['q'][:44]!r:46} {r.get('intent')} reads={len(o['reads'])} "
                  f"route={obs.get('model_route')} qwen={o['qwen']} {o['ms']:5.0f}ms"
                  + (f"  <- {failed} {str(r.get('answer'))[:70]!r}" if failed else ""))
    finally:
        h.stop()
    summary = {
        "passed": sum(1 for x in rows if x["passed"]), "total": len(rows), "live": live,
        "by_dimension": {d: sum(1 for x in rows if x["dims"][d]) for d in
                         ("intent", "source", "facts", "context", "security", "cost")},
        "latency_ms": {k: {"p50": round(statistics.median(v), 2),
                           "p95": round(sorted(v)[int(0.95 * (len(v) - 1))], 2)}
                       for k, v in latencies.items() if v},
        "reads": {"p50": statistics.median([x["reads"] for x in rows]),
                  "max": max(x["reads"] for x in rows)},
        "model_calls": sum(int(x["model_calls"] or 0) for x in rows),
    }
    print("\nQUALITY", json.dumps(summary, ensure_ascii=False))
    if report:
        from pathlib import Path

        Path(report).write_text(json.dumps({"summary": summary, "cases": rows}, indent=2,
                                           ensure_ascii=False), encoding="utf-8")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--report")
    a = p.parse_args()
    sys.exit(run(live=a.live, report=a.report))
