"""
COPILOT AGENT-INSIGHTS EVAL (2026-11-04, owner: "outcome abhi current se bht best aana chahaiye") -- against a RUNNING
API on the agentic_LOS database, as the live development officer (local-developer holds the 500 seed cases).

    python -m evals.copilot.agent_insights_eval --base http://127.0.0.1:8021 [--out agent_insights.json]

What the officer asks about the AGENTS' results: one case across every agent (Case 360), one agent on one case,
credit readiness, portfolio questions over the agents (how many / which), follow-ups, Hinglish, another officer (must
see nothing), and the existing answers that must not regress. THE EXPECTED VALUES ARE READ FROM THE DATABASE AT RUN
TIME (never typed here), so the eval stays right when the agents' data changes. Every answer is also checked for a
PAN / mobile pattern (never allowed). Tokens: the local development signing key (development only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from evals.copilot.golden_aw import token

ROOT = Path(__file__).resolve().parents[2]
OFFICER, OTHER = "local-developer", "agentic-eval-other-officer"
PACE = 25                                   # messages a minute, per officer
PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
MOBILE = re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)")


def _db() -> Any:
    import psycopg
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    return psycopg.connect(os.environ["LOS_AGENTIC_DSN"])


def truth() -> dict[str, Any]:
    """The facts the answers must state, from the agents' own tables (latest run per application)."""
    t: dict[str, Any] = {}
    with _db() as c:
        def case(no: str) -> str:
            return f"CASE-SEED-{no}"
        t["fraud_high"] = sorted({case(r[0]) for r in c.execute(
            "SELECT DISTINCT ON (application_number) application_number, risk_level FROM fraud_detection.fraud_results "
            "ORDER BY application_number, created_at DESC") if str(r[1]).upper() == "HIGH"})
        t["valuation_high"] = sorted({case(r[0]) for r in c.execute(
            "SELECT DISTINCT ON (l.application_number) l.application_number, r.agent_status FROM "
            "property_valuation.property_valuation_agent_results r JOIN public.loan_applications l "
            "ON l.application_id = r.application_id ORDER BY l.application_number, r.created_at DESC")
            if str(r[1]).upper() == "HIGH_RISK"})
        t["collateral"] = {case(r[0]): r[1] for r in c.execute(
            "SELECT DISTINCT ON (application_number) application_number, overall FROM "
            "collateral_verification.collateral_verification_results ORDER BY application_number, created_at DESC")}
        t["office"] = {case(r[0]): r[1] for r in c.execute(
            "SELECT DISTINCT ON (l.application_number) l.application_number, r.overall_recommendation FROM "
            "office_residence_verification.office_rule_results r JOIN public.loan_applications l "
            "ON l.application_id = r.application_id ORDER BY l.application_number, r.created_at DESC")}
        t["residence"] = {case(r[0]): r[1] for r in c.execute(
            "SELECT DISTINCT ON (l.application_number) l.application_number, r.overall_recommendation FROM "
            "office_residence_verification.residence_rule_results r JOIN public.loan_applications l "
            "ON l.application_id = r.application_id ORDER BY l.application_number, r.created_at DESC")}
        t["dedupe"] = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT DISTINCT ON (case_id) case_id, outcome, customer_status FROM dedupe.dedupe_checks "
            "ORDER BY case_id, checked_at DESC")}
        t["dedupe_checks"] = c.execute("SELECT count(*) FROM dedupe.dedupe_checks").fetchone()[0]
        t["fos_cases"] = c.execute(
            "SELECT count(DISTINCT a.case_id) FROM public.applications a JOIN public.access_grants g "
            "ON g.resource_id = a.case_id WHERE g.subject = %s", (OFFICER,)).fetchone()[0]
        t["applicant"] = {r[0]: r[1] for r in c.execute("SELECT case_id, applicant_id FROM public.applications")}
    with_any = set(t["fraud_high"]) | set(t["valuation_high"]) | set(t["collateral"]) | set(t["office"]) | \
        set(t["residence"]) | set(t["dedupe"])
    t["no_agent_case"] = next(f"CASE-SEED-LOS-DUMMY-{n:04d}" for n in range(300, 500)
                              if f"CASE-SEED-LOS-DUMMY-{n:04d}" not in with_any)
    return t


def short(case_id: str) -> str:
    return case_id.replace("CASE-SEED-", "")             # LOS-DUMMY-0001: the application number officers type


@dataclass
class Item:
    iid: str
    category: str
    turns: list[str]
    all_of: list[str] = field(default_factory=list)
    any_of: list[str] = field(default_factory=list)
    none_of: list[str] = field(default_factory=list)
    open_case: str | None = None                          # sent as the open case
    caller: str = OFFICER
    critical: bool = False


def items(t: dict[str, Any]) -> list[Item]:
    f, v = t["fraud_high"], t["valuation_high"]
    col = next(iter(t["collateral"]))
    office_case = next(iter(t["office"]))
    ded = sorted(t["dedupe"])[-1]
    both = sorted(set(f) & set(v))
    fraud_not_val = sorted(set(f) - set(v))
    attention = sorted(set(f) | set(v) | {k for k, x in t["collateral"].items() if x != "APPROVE"}
                       | {k for k, x in t["office"].items() if x != "PASS"}
                       | {k for k, x in t["residence"].items() if x != "PASS"})
    return [
        # A. ONE CASE, EVERY AGENT
        Item("A1", "A case 360", [f"What do the agents say about case {col}?"],
             all_of=["fraud", "collateral", t["collateral"][col].lower()], any_of=["high"], critical=True),
        Item("A2", "A case 360", ["sab agents ka result batao"], open_case=both[0],
             all_of=["fraud", "valuation"], any_of=["high"], critical=True),
        Item("A3", "A case 360", [f"show all agent results for {short(office_case)}"],
             all_of=["office", "residence"], critical=True),
        Item("A4", "A case 360", [f"agents result for {t['no_agent_case']}"],
             any_of=["no agent", "not run", "no result", "has not", "none of the agents"], none_of=["high risk"],
             critical=True),
        # B. ONE AGENT, ONE CASE
        Item("B1", "B one agent", [f"fraud risk of {short(f[-1])}"], all_of=["high"], critical=True),
        Item("B2", "B one agent", [f"property valuation result for case {v[1]}"], any_of=["high risk", "high_risk"],
             critical=True),
        Item("B3", "B one agent", [f"dedupe result of case {ded}"],
             any_of=[t["dedupe"][ded][1].lower(), t["dedupe"][ded][0].lower().replace("_", " ")], critical=True),
        Item("B4", "B one agent", [f"case {both[-1]} ka fraud aur valuation kya bola?"], all_of=["fraud", "valuation"],
             any_of=["high"]),
        Item("B5", "B one agent", [f"which agents have not run on case {fraud_not_val[0]}?"],
             all_of=["valuation"], any_of=["not run", "no result", "not been run", "pending", "missing"]),
        # C. READINESS
        Item("C1", "C readiness", [f"Is case {both[0]} ready for credit?"],
             all_of=["fraud"], any_of=["not ready", "review", "attention"], none_of=["is ready for credit"],
             critical=True),
        Item("C2", "C readiness", [f"can {t['no_agent_case']} go to credit?"],
             any_of=["not run", "no agent", "pending", "missing", "not ready"], none_of=["is ready for credit"]),
        # D. PORTFOLIO OVER THE AGENTS
        Item("D1", "D portfolio", ["How many cases are high fraud risk?"], all_of=[str(len(f))], critical=True),
        Item("D2", "D portfolio", ["List the potential fraud cases"], all_of=[short(f[0]), short(f[-1])], critical=True),
        Item("D3", "D portfolio", ["Which cases have high risk property valuation?"],
             all_of=[short(v[0]), short(v[-1])], critical=True),
        Item("D4", "D portfolio", ["How many dedupe checks have been done?"], all_of=[str(t["dedupe_checks"])]),
        Item("D5", "D portfolio", ["Which cases need attention from any agent?"], all_of=[str(len(attention))]),
        Item("D6", "D portfolio", ["kitne cases fraud me high risk hai?"], all_of=[str(len(f))]),
        Item("D7", "D portfolio", ["How many cases are in FOS stage?"], all_of=[str(t["fos_cases"])],
             none_of=["field officer stage is"], critical=True),
        # E. FOLLOW-UPS
        Item("E1", "E follow-up", ["How many cases are high fraud risk?", "which ones?"],
             all_of=[short(f[0]), short(f[-1])]),
        Item("E2", "E follow-up", [f"What do the agents say about case {both[0]}?", "and the valuation details?"],
             all_of=["valuation"], any_of=["high"]),
        # F. ANOTHER OFFICER SEES NOTHING
        Item("F1", "F isolation", [f"What do the agents say about case {f[0]}?"], caller=OTHER,
             none_of=["high risk", "fraud risk is high", "risk_level"], critical=True),
        Item("F2", "F isolation", ["How many cases are high fraud risk?"], caller=OTHER,
             none_of=[short(x) for x in f] + [f" {len(f)} cases"], critical=True),
        # H. HELD-OUT PHRASINGS (written before any tuning on them; never tuned for)
        Item("H1", "H held-out", [f"give me the fraud report for {short(f[1])}"], all_of=["high"]),
        Item("H2", "H held-out", [f"what did valuation find on {v[-2]}?"], any_of=["high risk", "high_risk"]),
        Item("H3", "H held-out", [f"is {short(both[-1])} clear for credit?"], any_of=["not ready", "review"],
             none_of=["is ready for credit"]),
        Item("H4", "H held-out", ["show me cases flagged by fraud"], all_of=[short(f[0])]),
        Item("H5", "H held-out", ["total cases with fraud red flag"], all_of=[str(len(f))]),
        Item("H6", "H held-out", [f"residence check result for {short(office_case)}"],
             all_of=[t["residence"][office_case].lower()]),
        Item("H7", "H held-out", [f"kya {short(both[0])} credit ke liye ready hai?"], any_of=["not ready", "review"]),
        Item("H8", "H held-out", ["count of cases where valuation is high risk"], all_of=[str(len(v))]),
        Item("H9", "H held-out", [f"dedupe status of {sorted(t['dedupe'])[0]}"],
             any_of=[t["dedupe"][sorted(t["dedupe"])[0]][1].lower(),
                     t["dedupe"][sorted(t["dedupe"])[0]][0].lower().replace("_", " ")]),
        Item("H10", "H held-out", ["which of my cases have collateral issues?"], all_of=[short(col)]),
        Item("H11", "H held-out", ["what does the fraud agent check?"], none_of=["of your", "agent results for"]),
        Item("H12", "H held-out", ["what documents are pending?"], open_case=both[0],
             none_of=["agent results for", "credit readiness"]),
        # I. A SECOND HELD-OUT SET (written 2026-11-04 after H was used for fixes; never run before the measurement)
        Item("I1", "I held-out 2", [f"{short(f[2])} pe fraud check ka kya result hai"], all_of=["high"]),
        Item("I2", "I held-out 2", [f"summary of agent findings for {both[0]}"], all_of=["fraud", "valuation"]),
        Item("I3", "I held-out 2", ["which cases did the valuation agent mark high risk"], all_of=[short(v[0])]),
        Item("I4", "I held-out 2", ["fraud agent ne kitne cases high bataye?"], all_of=[str(len(f))]),
        Item("I5", "I held-out 2", [f"is {col} good to go for credit"], any_of=["not ready", "review"],
             none_of=["is ready for credit"]),
        Item("I6", "I held-out 2", [f"office visit outcome for {short(office_case)}"],
             all_of=[t["office"][office_case].lower()]),
        Item("I7", "I held-out 2", ["number of cases with potential fraud"], all_of=[str(len(f))]),
        Item("I8", "I held-out 2", [f"any agent issues on {short(t['no_agent_case'])}?"],
             any_of=["no agent", "not run", "no result", "has not"], none_of=["high risk"]),
        Item("I9", "I held-out 2", ["how many cases have office verification done"],
             all_of=[f"{len(t['office'])} of your"]),
        Item("I10", "I held-out 2", ["show the agents report of this case"], open_case=f[0], all_of=["fraud"]),
        Item("I11", "I held-out 2", [f"frad risk of {short(f[1])}"], all_of=["high"]),
        Item("I12", "I held-out 2", ["what is collateral verification?"], none_of=["agent results for", "of your"]),
        # J. A THIRD HELD-OUT SET (written after I was used for fixes; run once, never tuned on)
        Item("J1", "J held-out 3", [f"has the fraud agent flagged {short(f[3])}?"], all_of=["high"]),
        Item("J2", "J held-out 3", [f"overall agent status for {short(v[2])}"], any_of=["high risk", "high_risk"]),
        Item("J3", "J held-out 3", ["fraud mein kaun kaun se cases high hai"], all_of=[short(f[0])]),
        Item("J4", "J held-out 3", [f"what's pending from the agents on {short(f[0])}?"],
             any_of=["not run", "not been run", "pending"]),
        Item("J5", "J held-out 3", ["how many of my cases have a valuation result?"], all_of=[f"{len(v)} of your"]),
        Item("J6", "J held-out 3", [f"can I send {short(both[1])} to credit now?"], any_of=["not ready", "review"],
             none_of=["is ready for credit"]),
        Item("J7", "J held-out 3", [f"collateral outcome {short(col)}"], all_of=[t["collateral"][col].lower()]),
        Item("J8", "J held-out 3", ["cases jinko attention chahiye agents ke hisaab se"], all_of=[str(len(attention))]),
        Item("J9", "J held-out 3", ["dedupe kitne cases pe chala?"], all_of=[f"{len(t['dedupe'])} of your"]),
        Item("J10", "J held-out 3", ["what does the valuation agent do?"], none_of=["agent results for", "of your"]),
        # G. WHAT ALREADY WORKED MUST STILL WORK
        Item("G1", "G regression", ["What is FOIR?"], any_of=["fixed obligation"]),
        Item("G2", "G regression", ["cases by stage"], any_of=["fos"]),
        Item("G3", "G regression", ["hi"], any_of=["hello", "hi", "namaste", "help"]),
        Item("G4", "G regression", ["What file types can I upload?"], all_of=["pdf"]),
    ]


def run(base: str) -> dict[str, Any]:
    t = truth()
    clients = {}
    for who in (OFFICER, OTHER):
        cl = httpx.Client(base_url=base, timeout=300)
        cl.headers["Authorization"] = "Bearer " + token(who)
        clients[who] = cl
    results = []
    last_sent = 0.0
    # PACED like a person (the copilot allows 30 messages a minute per officer, safety.py): at most PACE per minute,
    # so no answer is the rate limit's. The pause is never counted in the latency.
    gap = 60.0 / PACE
    for item in items(t):
        cl, context, body, status, calls = clients[item.caller], None, {}, 0, 0
        started = time.perf_counter()
        for turn in item.turns:
            wait = gap - (time.perf_counter() - last_sent)
            if wait > 0:
                time.sleep(wait)
                started += wait
            last_sent = time.perf_counter()
            payload: dict[str, Any] = {"action": "CUSTOM_QUERY", "message": turn}
            if item.open_case:
                payload.update(case_id=item.open_case, applicant_id=t["applicant"].get(item.open_case))
            if context:
                payload["context"] = context
            r = cl.post("/api/v1/fos/copilot", json=payload)
            status = r.status_code
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if isinstance(body.get("detail"), dict):
                body = {"answer": body["detail"].get("message"), "intent": "ERROR"}
            context = body.get("context") or context
            calls += int(((body.get("observability") or {}).get("model_ledger") or {}).get("calls", 0) or 0)
        ms = round((time.perf_counter() - started) * 1000, 1)
        answer = str(body.get("answer") or body.get("markdown") or "")
        low = answer.lower()
        problems = []
        if status != 200:
            problems.append(f"HTTP {status}")
        if item.any_of and not any(p.lower() in low for p in item.any_of):
            problems.append(f"lacks any of {item.any_of}")
        missing = [p for p in item.all_of if p.lower() not in low]
        if missing:
            problems.append(f"lacks {missing}")
        said = [p for p in item.none_of if p.lower() in low]
        if said:
            problems.append(f"says {said}")
        if PAN.search(answer) or MOBILE.search(answer):
            problems.append("PAN / mobile pattern in the answer")
        results.append({"id": item.iid, "category": item.category, "critical": item.critical, "turns": item.turns,
                        "http": status, "intent": body.get("intent"), "ms_per_turn": round(ms / len(item.turns), 1),
                        "model_calls": calls, "passed": not problems, "problems": problems,
                        "answer": answer[:500]})
    lat = sorted(r["ms_per_turn"] for r in results)
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    crit = [r for r in results if r["critical"]]
    return {"passed": sum(r["passed"] for r in results), "total": len(results),
            "critical_passed": sum(r["passed"] for r in crit), "critical_total": len(crit),
            "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in by_cat.items()},
            "latency_ms_per_turn": {"p50": statistics.median(lat), "p95": lat[max(0, int(0.95 * len(lat)) - 1)],
                                    "max": lat[-1]},
            "model_calls": sum(r["model_calls"] for r in results), "results": results}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8021")
    p.add_argument("--out", default="agent_insights.json")
    a = p.parse_args()
    report = run(a.base)
    Path(a.out).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"passed {report['passed']}/{report['total']}  critical {report['critical_passed']}/"
          f"{report['critical_total']}  by category {report['by_category']}")
    print(f"latency per turn ms: {report['latency_ms_per_turn']}  model calls: {report['model_calls']}")
    for r in report["results"]:
        if not r["passed"]:
            print(f"  FAIL {r['id']} [{r['intent']}] {r['problems']}")


if __name__ == "__main__":
    main()
