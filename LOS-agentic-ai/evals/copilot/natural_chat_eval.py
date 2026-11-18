"""
NATURAL CONVERSATION EVAL -- 30 unseen messages against a RUNNING API (2026-11-02).

    python -m evals.copilot.natural_chat_eval --base http://127.0.0.1:8765 [--out natural.json]

Six groups of five: social, general knowledge, technical, LOS handbook, case-specific, follow-up / ambiguous. Each
item is judged on its answer text (any / none phrases) and recorded with the answering intent, the source the reply
declares, latency and HTTP. Synthetic identities only; tokens minted with the LOCAL DEVELOPMENT key (as golden_aw).
Works with both reply shapes (the full envelope, or the {markdown, tts} contract).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Any

import httpx

from evals.copilot.golden_aw import token

STATIC = ["how can i help you today", "what would you like to check today"]
MENU = ["](ask:", "](action:"]


def items() -> list[dict[str, Any]]:
    social = dict(none_of=STATIC + MENU)
    # LOS-ONLY SCOPE (owner 2026-11-03): a question outside LOS work gets the out-of-domain line, never an answer
    ood = dict(any_of=["built for los work", "los ke kaam ke liye"], none_of=MENU)
    general = ood
    return [
        # A. social
        {"id": "S1", "group": "social", "turns": ["hows you ?"], **social},
        {"id": "S2", "group": "social", "turns": ["good evening, long day today"], **social},
        {"id": "S3", "group": "social", "turns": ["hello ji kaise ho"], **social},
        {"id": "S4", "group": "social", "turns": ["thank you so much"], "any_of": ["welcome", "glad", "happy"]},
        {"id": "S5", "group": "social", "turns": ["hey there"], **social},
        # B. general knowledge
        {"id": "G1", "group": "general", "turns": ["What is machine learning?"], **general},
        {"id": "G2", "group": "general", "turns": ["Tell me something interesting about the ocean"], **general},
        {"id": "G3", "group": "general", "turns": ["Who wrote the play Romeo and Juliet?"], **general},
        {"id": "G4", "group": "general", "turns": ["Why is the sky blue?"], **general},
        {"id": "G5", "group": "general", "turns": ["What does photosynthesis produce?"], **general},
        # C. technical
        {"id": "T1", "group": "technical", "turns": ["What is a primary key in a database?"], **general},
        {"id": "T2", "group": "technical", "turns": ["Explain what an API is in simple words"], **general},
        {"id": "T3", "group": "technical", "turns": ["What is the difference between RAM and storage?"], **general},
        {"id": "T4", "group": "technical", "turns": ["What does HTTP status 404 mean?"], **general},
        {"id": "T5", "group": "technical", "turns": ["How does a cost-based query planner choose a plan?"], **general},
        # D. LOS handbook
        {"id": "H1", "group": "handbook", "turns": ["What is FOIR?"], "any_of": ["fixed obligation"]},
        {"id": "H2", "group": "handbook", "turns": ["What file types can I upload?"], "any_of": ["pdf"]},
        {"id": "H3", "group": "handbook", "turns": ["What is a co-applicant?"], "any_of": ["co-applicant", "applies"]},
        {"id": "H4", "group": "handbook", "turns": ["What is CIBIL?"], "any_of": ["credit"]},
        {"id": "H5", "group": "handbook", "turns": ["What happens after CPA?"], "any_of": ["credit"]},
        # E. case-specific (the officer's first case: Personal Loan, 5,00,000, 36 months)
        {"id": "C1", "group": "case", "case": True, "turns": ["what is my loan amount?"], "any_of": ["5,00,000"]},
        {"id": "C2", "group": "case", "case": True, "turns": ["what is the tenure?"], "any_of": ["36"]},
        {"id": "C3", "group": "case", "case": True, "turns": ["which documents are pending?"],
         "any_of": ["pan", "address proof", "bank statement", "signature"]},
        {"id": "C4", "group": "case", "case": True, "turns": ["can this case move to CPA?"],
         "any_of": ["not", "waiting", "pending", "missing", "before"]},
        {"id": "C5", "group": "case", "turns": ["how many applications did I get this month?"], "any_of": ["**3**",
                                                                                                         "3 cases"]},
        # F. follow-up / ambiguous
        {"id": "F1", "group": "followup", "turns": ["Explain CPA.", "What happens after that?"], "any_of": ["credit"]},
        {"id": "F2", "group": "followup", "turns": ["What is machine learning?", "give me one example of it"],
         **ood},
        {"id": "F3", "group": "followup", "case": True, "turns": ["what is my loan amount?", "and the tenure?"],
         "any_of": ["36"]},
        {"id": "F4", "group": "followup", "case": True, "turns": ["move it ahead"],
         "any_of": ["?", "which", "pending", "missing", "not met"]},
        {"id": "F5", "group": "followup", "turns": ["How many applications this month?", "compare with last month"],
         "any_of": ["change", "last month"]},
        # LOS-ONLY SCOPE (2026-11-03): LOS technical questions answered, out-of-domain ones redirected
        {"id": "L1", "group": "los_technical", "turns": ["How does the bank statement reader work?"],
         "any_of": ["statement", "pdf", "ocr", "text"], "none_of": ["built for los work"]},
        {"id": "L2", "group": "los_technical", "turns": ["What file types can I upload?"], "any_of": ["pdf"]},
        {"id": "L3", "group": "los_technical", "turns": ["Can I upload several documents at once?"],
         "any_of": ["yes", "multiple", "several"]},
        {"id": "O1", "group": "out_of_domain", "turns": ["Tell me a joke"], **ood},
        {"id": "O2", "group": "out_of_domain", "turns": ["Who won the last cricket world cup?"], **ood},
        {"id": "O3", "group": "out_of_domain", "turns": ["machine learning kya hai"], **ood},
        {"id": "W1", "group": "capabilities", "turns": ["What can you do?"], "any_of": ["case", "kyc", "document"],
         "none_of": ["built for los work"]},
        {"id": "W2", "group": "capabilities", "turns": ["How are you?"], "none_of": STATIC + MENU + ["built for los work"]},
        # CRITICAL ACCEPTANCE (master task section 12, exact inputs)
        {"id": "X1", "group": "acceptance", "turns": ["Hows you bro?"], **social},
        {"id": "X2", "group": "acceptance", "turns": ["What is an API?"], "any_of": ["api", "integration", "los"]},
        {"id": "X3", "group": "acceptance", "turns": ["Explain the difference between FOS and CPA"],
         "all_of": ["fos", "cpa"], **general},
        {"id": "X4", "group": "acceptance", "turns": ["Show my oldest cases"], "any_of": ["mahesh", "neha", "ravi"]},
        {"id": "X5", "group": "acceptance", "turns": ["Explain CPA.", "What happens after that?"], "any_of": ["credit"]},
        {"id": "X6", "group": "acceptance", "turns": ["Kitne cases abhi CPA mein pending hain?"],
         "any_of": ["0", "**0**", "koi", "none", "no case"], "none_of": ["**3**"]},
        {"id": "X7", "group": "acceptance", "turns": ["Which branch has the highest loan approval rate?"],
         "any_of": ["not record", "can't", "cannot", "not available"]},
        {"id": "X8", "group": "acceptance", "case": True, "turns": ["Is this applicant approved?"],
         "none_of": ["is approved", "has been approved", "approved."],
         "any_of": ["not", "pending", "fos", "review", "yet"]},
    ]


def _open(cl, product, amount, tenure, name):
    r = cl.post("/api/v1/fos/applicants", json={
        "applicant": {"full_name": name, "mobile": "9876543210", "date_of_birth": "1990-01-01", "address": "Pune"},
        "application": {"product": product, "loan_amount": amount, "tenure_months": tenure,
                        "employment_type": "SALARIED", "declared_monthly_income": 90000}})
    body = r.json()
    if "case_id" not in body:
        raise SystemExit(f"setup failed: HTTP {r.status_code} {str(body)[:200]}")
    return body["applicant_id"], body["case_id"]


def run(base: str, tag: str, pause: float = 2.2) -> dict[str, Any]:
    me = httpx.Client(base_url=base, timeout=120)
    me.headers["Authorization"] = "Bearer " + token(f"natural-officer-{tag}")
    first = _open(me, "PERSONAL_LOAN", 500000, 36, "Mahesh Verma")
    _open(me, "HOME_LOAN", 2500000, 240, "Neha Jadhav")
    _open(me, "PERSONAL_LOAN", 300000, 24, "Ravi Kulkarni")
    # WARM-UP (not scored): the first model call after a server start loads the model -- a deployment's warm-up / first
    # user pays that once; the items measure the steady state
    me.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "hello", "chat_id": f"{tag}-warmup"})
    time.sleep(pause)
    out = []
    for it in items():
        context, body, status, per_turn = None, {}, 0, []
        for turn in it["turns"]:
            # one chat per item, as a client starting a new conversation (else every item shares the officer's chat)
            payload: dict[str, Any] = {"action": "CUSTOM_QUERY", "message": turn, "chat_id": f"{tag}-{it['id']}"}
            if it.get("case"):
                payload.update(applicant_id=first[0], case_id=first[1])
            if context:
                payload["context"] = context
            time.sleep(pause)                    # the API's rate limit (30 messages a minute)
            started = time.perf_counter()
            r = me.post("/api/v1/fos/copilot", json=payload)
            per_turn.append(round((time.perf_counter() - started) * 1000))
            status = r.status_code
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            context = body.get("context") or context
        answer = str(body.get("answer") or body.get("markdown") or "") + "\n" + str(body.get("faq_block") or "")
        low = answer.lower()
        problems = []
        if status != 200:
            problems.append(f"HTTP {status}")
        if it.get("any_of") and not any(p.lower() in low for p in it["any_of"]):
            problems.append(f"lacks any of {it['any_of']}")
        missing = [p for p in it.get("all_of", []) if p.lower() not in low]
        if missing:
            problems.append(f"lacks {missing}")
        said = [p for p in it.get("none_of", []) if p.lower() in low]
        if said:
            problems.append(f"says {said}")
        out.append({"id": it["id"], "group": it["group"], "turns": it["turns"], "http": status,
                    "intent": body.get("intent"), "source": body.get("response_source"),
                    "ms_last_turn": per_turn[-1], "passed": not problems, "problems": problems,
                    "answer": answer.strip()[:300]})
    lat = sorted(r["ms_last_turn"] for r in out)
    groups: dict[str, list[bool]] = {}
    for r in out:
        groups.setdefault(r["group"], []).append(r["passed"])
    return {"passed": sum(r["passed"] for r in out), "total": len(out),
            "by_group": {k: f"{sum(v)}/{len(v)}" for k, v in groups.items()},
            "latency_ms": {"p50": statistics.median(lat), "p95": lat[max(0, int(0.95 * len(lat)) - 1)],
                           "max": lat[-1]}, "results": out}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8765")
    p.add_argument("--out", default="natural.json")
    p.add_argument("--tag", default=str(int(time.time())))
    p.add_argument("--pause", type=float, default=2.2, help="seconds between messages (rate limit: 30 a minute)")
    a = p.parse_args()
    report = run(a.base, a.tag, a.pause)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    for r in report["results"]:
        mark = "ok  " if r["passed"] else "FAIL"
        print(f"{mark} {r['id']} {r['ms_last_turn']:>6} ms {str(r['intent']):18s} {r['turns'][-1][:40]!r} -> "
              f"{r['answer'][:110]!r} {'' if r['passed'] else r['problems']}")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
