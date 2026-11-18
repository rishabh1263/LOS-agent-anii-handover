"""
COPILOT INTELLIGENCE EVAL -- 30 items, against a RUNNING API (2026-11-02, master prompt section 12).

    python -m evals.copilot.intelligence_eval --base http://127.0.0.1:8765 [--out intelligence.json]

Independent of the golden A-W set: handbook, workflow, case data (tools), aggregation, comparison, multi-turn
follow-ups, ambiguity, missing data, general, session isolation. SYNTHETIC identities only; the expected numbers are
known from the setup (the officer's three cases are created by this run, so "this month" holds exactly them).

Each item is judged on the answer text (any / all / none phrases), the HTTP status and, for the critical items, on
fabrication: an analytics or case-data answer that states a number the setup does not hold fails. Recorded per item:
the turns, intent, HTTP, latency, model calls (server ledger), pass / fail and the reasons.

Tokens are minted with the LOCAL DEVELOPMENT signing key, as golden_aw does. Development only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from evals.copilot.golden_aw import token

OTHER_NAME = "Zara Qureshi"
CASES = [  # (product, amount, tenure, applicant name) -- synthetic people
    ("PERSONAL_LOAN", 500000, 36, "Mahesh Verma"),
    ("HOME_LOAN", 2500000, 240, "Neha Jadhav"),
    ("PERSONAL_LOAN", 300000, 24, "Ravi Kulkarni"),
]


@dataclass
class Item:
    iid: str
    category: str
    turns: list[str]
    any_of: list[str] = field(default_factory=list)       # the last answer has at least one
    all_of: list[str] = field(default_factory=list)       # ... and every one of these
    none_of: list[str] = field(default_factory=list)      # ... and none of these
    case: bool = True                                     # sent with the officer's first case open
    caller: str = "me"                                    # me / other
    target: str = "main"                                  # main / other (the case sent)
    http: tuple[int, ...] = (200,)
    critical: bool = False


def items() -> list[Item]:
    return [
        # A. HANDBOOK
        Item("A1", "A handbook", ["Can I upload several documents at once?"], any_of=["yes", "multiple", "several"],
             none_of=["still missing", "is pending"]),
        Item("A2", "A handbook", ["What file types can I upload?"], all_of=["pdf"], any_of=["jpeg", "png", "image"]),
        Item("A3", "A handbook", ["Do I have to say what each document is?"], any_of=["classif", "no."]),
        Item("A4", "A handbook", ["What is FOIR?"], any_of=["fixed obligation"]),
        # B. WORKFLOW / STATUS (case open)
        Item("B1", "B workflow", ["What stage is my application at?"], any_of=["fos"]),
        Item("B2", "B workflow", ["can I move this case to CPA?"],
             any_of=["not", "waiting", "missing", "pending", "before", "needs"]),
        # C. CASE DATA (tools)
        Item("C1", "C case data", ["what is my loan amount?"], any_of=["5,00,000"], none_of=["25,00,000"],
             critical=True),
        Item("C2", "C case data", ["what is the tenure?"], any_of=["36"], critical=True),
        Item("C3", "C case data", ["which documents are still pending?"],
             any_of=["pan", "address proof", "bank statement", "signature", "salary"]),
        # E. AGGREGATION
        Item("E1", "E aggregation", ["How many applications were received this month?"], any_of=["**3**", "3 cases"],
             critical=True, case=False),
        Item("E2", "E aggregation", ["What is the total loan exposure?"], any_of=["33,00,000"], critical=True,
             case=False),
        Item("E3", "E aggregation", ["Calculate the average loan amount of my cases"], any_of=["11,00,000"],
             critical=True, case=False),
        Item("E4", "E aggregation", ["Count applications by product"], all_of=["personal loan: 2", "home loan: 1"],
             critical=True, case=False),
        Item("E5", "E aggregation", ["Which case has the highest loan amount?"], any_of=["25,00,000", "neha"],
             case=False),
        Item("E6", "E aggregation", ["how many home loan cases this month?"], any_of=["**1**", "1 case"],
             critical=True, case=False),
        # F. COMPARISON
        Item("F1", "F comparison", ["Compare this month against last month applications"],
             all_of=["this month", "last month"], any_of=["change"], critical=True, case=False),
        Item("F2", "F comparison", ["Compare approved and rejected applications"], any_of=["not recorded"],
             none_of=["approved: ", "rejected: "], critical=True, case=False),
        Item("F3", "F comparison", ["Which branch has the highest application count?"], all_of=["branch"],
             any_of=["can't", "cannot", "not record"], critical=True, case=False),
        # G. MULTI-TURN
        Item("G1", "G multi-turn", ["Show me the number of applications this month", "How many were rejected?"],
             any_of=["not recorded"], critical=True, case=False),
        Item("G2", "G multi-turn", ["How many applications this month?", "Compare with last month"],
             all_of=["last month"], any_of=["change"], critical=True, case=False),
        Item("G3", "G multi-turn", ["How many applications this month?", "and the total amount?"],
             any_of=["33,00,000"], critical=True, case=False),
        Item("G4", "G multi-turn", ["is my PAN verified?", "and the driving licence?"], any_of=["licence", "license"]),
        Item("G5", "G multi-turn", ["what is my loan amount?", "what about tenure?"], any_of=["36"]),
        # H. AMBIGUOUS
        Item("H1", "H ambiguous", ["move it ahead"],
             any_of=["?", "which", "pick", "not met", "blocking", "missing", "pending"]),
        # I. MISSING DATA / NO ANSWER
        Item("I1", "I missing data", ["What is the prepayment penalty percentage on a top-up loan at this lender?"],
             none_of=["%"], any_of=["not", "don't", "knowledge base", "policy", "can't", "sanction letter"],
             case=False),
        Item("I2", "I missing data", ["How many overdue accounts are there?"], any_of=["not part", "can't"],
             critical=True, case=False),
        Item("I3", "I missing data", ["Calculate the average sanctioned amount"], any_of=["not recorded"],
             critical=True, case=False),
        # J. GENERAL
        Item("J1", "J general", ["what is the capital of france?"], any_of=["outside", "help with", "loan"],
             none_of=["paris"], case=False),
        # L. SESSION ISOLATION
        Item("L1", "L isolation", ["what is the loan amount on this case?"], caller="other", target="main",
             http=(403, 404, 200), none_of=["5,00,000", "mahesh"], critical=True),
        Item("L2", "L isolation", ["How many applications were received this month?"], caller="other",
             any_of=["**1**", "1 case"], none_of=["**4**", "**3**"], critical=True, case=False),
    ]


def _open(cl: httpx.Client, product: str, amount: int, tenure: int, name: str) -> tuple[str, str]:
    r = cl.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": name, "mobile": "9876543210", "date_of_birth": "1990-01-01", "address": "Pune"},
        "application": {"product": product, "loan_amount": amount, "tenure_months": tenure,
                        "employment_type": "SALARIED", "declared_monthly_income": 90000}})
    body = r.json()
    if "case_id" not in body:
        raise SystemExit(f"setup failed: HTTP {r.status_code} {str(body)[:200]}")
    return body["applicant_id"], body["case_id"]


def run(base: str, tag: str) -> dict[str, Any]:
    me = httpx.Client(base_url=base, timeout=300)
    me.headers["Authorization"] = "Bearer " + token(f"intel-officer-{tag}")
    other = httpx.Client(base_url=base, timeout=300)
    other.headers["Authorization"] = "Bearer " + token(f"intel-other-{tag}")
    mine = [_open(me, *c) for c in CASES]
    theirs = _open(other, "PERSONAL_LOAN", 300000, 24, OTHER_NAME)
    ids = {"main": mine[0], "other": theirs}
    results = []
    for item in items():
        cl = other if item.caller == "other" else me
        aid, cid = ids[item.target]
        context, body, status, model_calls = None, {}, 0, 0
        started = time.perf_counter()
        for turn in item.turns:
            payload: dict[str, Any] = {"action": "CUSTOM_QUERY", "message": turn}
            if item.case:
                payload.update(applicant_id=aid, case_id=cid)
            if context:
                payload["context"] = context
            r = cl.post("/api/v1/fos/copilot", json=payload)
            status = r.status_code
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if status == 403 and isinstance(body.get("detail"), dict):
                body = {"answer": body["detail"].get("message"), "intent": "ACCESS_DENIED"}
            context = body.get("context") or context
            model_calls += int(((body.get("observability") or {}).get("model_ledger") or {}).get("calls", 0) or 0)
        ms = round((time.perf_counter() - started) * 1000, 1)
        answer = str(body.get("answer") or body.get("markdown") or "")
        low = answer.lower()
        problems = []
        if status not in item.http:
            problems.append(f"HTTP {status}")
        if item.any_of and not any(p.lower() in low for p in item.any_of):
            problems.append(f"lacks any of {item.any_of}")
        missing = [p for p in item.all_of if p.lower() not in low]
        if missing:
            problems.append(f"lacks {missing}")
        said = [p for p in item.none_of if p.lower() in low]
        if said:
            problems.append(f"says {said}")
        results.append({"id": item.iid, "category": item.category, "critical": item.critical, "turns": item.turns,
                        "http": status, "intent": body.get("intent"), "ms": ms,
                        "ms_per_turn": round(ms / len(item.turns), 1), "model_calls": model_calls,
                        "passed": not problems, "problems": problems, "answer": answer[:400],
                        # an error response's own code / message (never the request): why a turn failed
                        "error": None if status == 200 else str(body.get("error") or body.get("detail") or "")[:300]})
    lat = sorted(r["ms_per_turn"] for r in results)
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    critical = [r for r in results if r["critical"]]
    return {"passed": sum(r["passed"] for r in results), "total": len(results),
            "critical_passed": sum(r["passed"] for r in critical), "critical_total": len(critical),
            "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in by_cat.items()},
            "latency_ms_per_turn": {"p50": statistics.median(lat), "p95": lat[max(0, int(0.95 * len(lat)) - 1)],
                                    "max": lat[-1]},
            "model_calls": sum(r["model_calls"] for r in results), "results": results}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8765")
    p.add_argument("--out", default="intelligence.json")
    p.add_argument("--tag", default=str(int(time.time())), help="officer subject suffix: a fresh portfolio per run")
    a = p.parse_args()
    report = run(a.base, a.tag)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    for r in report["results"]:
        if not r["passed"]:
            print(f"FAIL {r['id']} {r['category']:16s} {r['turns']} -> {r['problems']}\n     {r['answer'][:200]!r}")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
