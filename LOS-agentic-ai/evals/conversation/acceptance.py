"""
CONVERSATIONAL ACCEPTANCE against a running server (Phase 19).

    python -m evals.conversation.acceptance [--base http://127.0.0.1:8010] [--show]

One realistic case (verified PAN + licence, a salary slip, a bank statement), the
brief's questions in English / Hinglish / Marathi, and per answer: no technical
leakage, concise, a structured presentation, the right language, and the facts the
backend recorded. Prints every answer with --show. Writes runs/conversation_acceptance.json.
"""

from __future__ import annotations

import argparse
import json
import re

from evals.http_e2e import BANK_A, RISHABH_DL, RISHABH_PAN, ROOT, S, Api, _token
from evals.perf.latency import FOS

LEAK = re.compile(r"\[\{|\{'|item\(s\)|document\(s\)|\b[A-Z]+_[A-Z_]{3,}\b|\bscore\b|confidence|"
                  r"version [0-9a-f]{6,}|Source:|\.(jpe?g|png|pdf)\b|\bNone\b|\bnull\b|case_[0-9a-f]{6}|"
                  r"CASE-[0-9A-F]{6}|APP-[0-9A-F]{6}|jev|qwen|laya|okf|rag\b|mcp", re.I)
APPROVAL = re.compile(r"\b(loan (is )?approved|approved the loan|sanctioned|you are eligible)\b", re.I)

#: (question, language it was asked in, must mention any of these words -- or None)
QUESTIONS = [
    ("documents verify ho gaye?", "hi-Latn", None),
    ("what is pending?", "en", None),
    ("PAN ki details batao", "hi-Latn", ["PAN"]),
    ("KYC kyun fail hua?", "hi-Latn", ["KYC"]),
    ("KYC zala ka?", "mr-Latn", ["KYC"]),
    ("pudhe kay karaycha?", "mr-Latn", None),
    ("eligibility ka status kya hai?", "hi-Latn", ["ligib"]),
    ("credit ka kya hua?", "hi-Latn", ["redit"]),
    ("why is the name not matching?", "en", None),
    ("what do I need to upload?", "en", None),
    ("show all documents", "en", ["PAN"]),
    ("show only pending documents", "en", None),
    ("verify documents", "en", None),
    ("what happened to my bank statement?", "en", ["ank"]),
    ("can I proceed to credit?", "en", None),
    ("what is missing?", "en", None),
    ("thanks", "en", None),
    ("hello", "en", None),
]


#: FOLLOW-UP CHAINS: each turn echoes the previous response's `context`, as a frontend does.
#: (turn, the answer must mention any of these, or None)
CHAINS = [
    [("PAN verified hai?", ["PAN"]), ("why?", None), ("what about address?", ["ddress"]),
     ("what should I do?", None)],
    [("KYC zala ka?", ["KYC"]), ("ka?", None)],
]


def ask_with(api, a, c, q, context):
    r = api.c.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY",
                                                "message": q, "context": context})
    r.raise_for_status()
    return r.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    api = Api(args.base, _token("acceptance-officer", FOS))
    a, c = api.open_case()
    api.upload(a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL), ("slip.pdf", S / "real_batch/salary_slip.pdf"),
                      ("bank.pdf", BANK_A)], ["PAN", "DRIVING_LICENCE", "SALARY_SLIP", "BANK_STATEMENT"])
    rows, failed = [], 0
    for q, lang, must in QUESTIONS:
        body = api.ask(a, c, q)
        answer = body.get("answer") or ""
        problems = []
        if LEAK.search(answer):
            problems.append(f"leak: {LEAK.search(answer).group(0)!r}")
        if APPROVAL.search(answer):
            problems.append("claims an approval")
        if len(answer) > 700:
            problems.append(f"long ({len(answer)} chars)")
        if must and not any(m.lower() in answer.lower() for m in must):
            problems.append(f"does not mention {must}")
        if not (body.get("presentation") or {}).get("message") and not (body.get("presentation") or {}).get("sections"):
            problems.append("no structured presentation")
        failed += bool(problems)
        rows.append({"q": q, "intent": body.get("intent"), "language": (body.get("language_contract") or {})
                     .get("reply_language"), "answer": answer, "problems": problems})
        print(f"{'FAIL' if problems else 'PASS'}  [{body.get('intent')}] {q}" + (f"  -> {problems}" if problems else ""))
        if args.show:
            print("      " + answer.replace("\n", "\n      "))
    for chain in CHAINS:
        context = None
        for q, must in chain:
            body = ask_with(api, a, c, q, context)
            answer, context = body.get("answer") or "", body.get("context")
            problems = []
            if LEAK.search(answer):
                problems.append(f"leak: {LEAK.search(answer).group(0)!r}")
            if must and not any(m.lower() in answer.lower() for m in must):
                problems.append(f"does not mention {must}")
            if body.get("case_id") not in (None, c):
                problems.append("answered about another case")
            failed += bool(problems)
            rows.append({"q": q, "chain": True, "intent": body.get("intent"), "answer": answer,
                         "followed_up": body.get("followed_up"), "problems": problems})
            print(f"{'FAIL' if problems else 'PASS'}  [chain {body.get('intent')}] {q}"
                  + (f"  -> {problems}" if problems else ""))
            if args.show:
                print("      " + answer.replace("\n", "\n      "))
    # A NEW CASE drops the old context: the old case's facts must not appear
    a2, c2 = api.open_case()
    stale = ask_with(api, a2, c2, "why?", context)
    leaked = "RISHABH" in (stale.get("answer") or "") or stale.get("case_id") not in (None, c2)
    failed += leaked
    print(f"{'FAIL' if leaked else 'PASS'}  [case switch] old context does not leak into a new case")
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "conversation_acceptance.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False),
                                                                encoding="utf-8")
    print(f"\n{len(rows) - failed}/{len(rows)} answers acceptable")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
