"""
LIVE CHATBOT EXAMPLES: USER -> RESPONSE -> STRUCTURED UI PAYLOAD (against a running server).

    python -m evals.conversation.examples [--base http://127.0.0.1:8010]

One case (verified PAN + licence, a salary slip and a bank statement belonging to
other people -> a real KYC review), the brief's representative questions, and for
each: the message and the structured payload the frontend renders. Writes
runs/chatbot_examples.json.
"""

from __future__ import annotations

import argparse
import json

from evals.http_e2e import BANK_A, RISHABH_DL, RISHABH_PAN, ROOT, S, Api, _token
from evals.perf.latency import FOS

#: (question, frontend-selected language or None)
EXAMPLES = [
    ("documents verify ho gaye?", None),
    ("what is pending?", None),
    ("KYC kyun fail hua?", None),
    ("KYC kyun fail hua?", "mr"),                 # typed in Hinglish, Marathi selected
    ("why is the name not matching?", "hi"),
    ("eligibility ka status?", None),
    ("credit ka kya hua?", None),
    ("pudhe kay karaycha?", None),
    ("show all documents", "en"),
    ("bank statement ka kya hua?", None),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    api = Api(args.base, _token("examples-officer", FOS))
    a, c = api.open_case()
    api.upload(a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL), ("slip.pdf", S / "real_batch/salary_slip.pdf"),
                      ("bank.pdf", BANK_A)], ["PAN", "DRIVING_LICENCE", "SALARY_SLIP", "BANK_STATEMENT"])
    out = []
    for q, lang in EXAMPLES:
        payload = {"applicant_id": a, "case_id": c, "action": "CUSTOM_QUERY", "message": q}
        if lang:
            payload["response_language"] = lang
        body = api.c.post("/api/v1/fos/copilot", json=payload).json()
        p = body.get("presentation") or {}
        ui = {k: p.get(k) for k in ("response_type", "response_depth", "language", "status", "next_action")}
        ui["documents"] = [{k: d.get(k) for k in ("label", "status", "action")} for d in p.get("documents") or []]
        ui["audio"] = {k: (p.get("audio") or {}).get(k) for k in ("status", "language", "text")}
        out.append({"user": q, "selected_language": lang, "message": body.get("answer"), "ui": ui})
        print(f"\nUSER ({lang or 'auto'}): {q}\nRESPONSE: {body.get('answer')}\nUI: {json.dumps(ui, ensure_ascii=False)}")
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "chatbot_examples.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
