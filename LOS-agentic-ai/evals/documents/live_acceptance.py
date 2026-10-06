"""
LIVE DOCUMENT ACCEPTANCE against a running server, on REAL samples (no synthetic data).

    python -m evals.documents.live_acceptance [--base http://127.0.0.1:8010]

For each supported type: upload one real sample to its own case -> the status the
frontend polls (GET /applications/{id}/verification-status) -> any background job
(GET /jobs/{id}) until it settles -> the chatbot's answer about that document. Checks
that the chat and the status API agree and that nothing technical leaks. Writes
runs/live_document_acceptance.json. Aadhaar is reported NOT_TESTABLE: no sample exists.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from evals.http_e2e import ROOT, S, Api, _token
from evals.perf.latency import FOS

SAMPLES = [
    ("PAN", S / "documents/rpan.jpg", "PAN ka kya hua?"),
    ("DRIVING_LICENCE", S / "documents/driving_license.jpg", "driving licence ka kya hua?"),
    ("VOTER_ID", S / "documents/voter_id2.jpg", "voter id ka kya hua?"),
    ("PASSPORT", S / "passports/passport_samples0_4.jpg", "passport ka kya hua?"),
    ("SALARY_SLIP", S / "real_batch/salary_slip.pdf", "salary slip ka kya hua?"),
    ("BANK_STATEMENT", S / "real_batch/bank_hdfc_new.pdf", "bank statement ka kya hua?"),
    ("BANK_STATEMENT", S / "real_batch/bank_sbi_scanned.pdf", "bank statement ka kya hua?"),   # scanned
    ("ITR", S / "documents/ITR.pdf", "ITR ka kya hua?"),
    ("SALE_DEED", S / "real_batch/sale_deed_clean.pdf", "sale deed ka kya hua?"),
]
LEAK = re.compile(r"\b[A-Z]+_[A-Z_]{3,}\b|\bscore\b|confidence|\.(jpe?g|png|pdf)\b|Traceback", re.I)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--job-wait-s", type=int, default=120)
    args = parser.parse_args()
    api = Api(args.base, _token("doc-acceptance", FOS))
    rows = [{"type": "AADHAAR", "result": "NOT_TESTABLE", "why": "no Aadhaar sample in the repository"}]
    for doc_type, path, question in SAMPLES:
        if not Path(path).exists():
            rows.append({"type": doc_type, "file": path.name, "result": "NOT_TESTABLE", "why": "sample missing"})
            continue
        if doc_type == "SALE_DEED":
            # A PROPERTY document: not a FOS-stage upload (the FOS route refuses it, 422
            # UNSUPPORTED_DOCUMENT_TYPE, by design) -- processed by the LOS pipeline.
            t = time.perf_counter()
            r = api.c.post("/api/v1/los/process", data={"applicant_id": "APP-DEED-LIVE", "expected_types": [doc_type]},
                           files=[("files", (path.name, Path(path).read_bytes(), "application/pdf"))])
            d = (r.json().get("documents") or [{}])[0] if r.status_code == 200 else {}
            verdict = d.get("verification")
            row = {"type": doc_type, "file": path.name, "upload_ms": round((time.perf_counter() - t) * 1000),
                   "route": "/api/v1/los/process", "classified": d.get("type"), "status": verdict,
                   "reasons": d.get("reason_codes"), "fields": sorted((d.get("extraction") or {}).keys()),
                   "result": "PASS" if r.status_code == 200 and verdict in ("PASS", "REVIEW") else "CHECK"}
            rows.append(row)
            print(f"{row['result']:5} {doc_type:16} {path.name:28} {verdict or '-':11} upload={row['upload_ms']}ms "
                  f"classified={row['classified']} reasons={row['reasons']}")
            continue
        a, c = api.open_case()
        t = time.perf_counter()
        api.upload(a, c, [(path.name, path)], [doc_type])
        upload_ms = round((time.perf_counter() - t) * 1000)
        status = api.c.get(f"/api/v1/applications/{c}/verification-status").json()
        doc = next((d for d in status["documents"] if d.get("document_id")), None)
        waited = 0
        while doc and doc["status"] == "PROCESSING" and waited < args.job_wait_s:     # background read
            time.sleep(5)
            waited += 5
            status = api.c.get(f"/api/v1/applications/{c}/verification-status").json()
            doc = next((d for d in status["documents"] if d.get("document_id")), None)
        job = status.get("jobs") or []
        body = api.ask(a, c, question)
        answer = body.get("answer") or ""
        card = next((x for x in (body.get("presentation") or {}).get("documents") or []), None)
        agree = bool(card and doc and card.get("state") == doc["status"]) or (doc and doc["status"].lower() in answer.lower())
        problems = []
        if not doc:
            problems.append("no document recorded")
        if LEAK.search(answer):
            problems.append(f"leak: {LEAK.search(answer).group(0)}")
        if doc and doc["status"] == "PROCESSING":
            problems.append(f"still processing after {waited}s")
        rows.append({"type": doc_type, "file": path.name, "upload_ms": upload_ms,
                     "status": doc["status"] if doc else None, "reasons": (doc or {}).get("reasons"),
                     "background_job": job[0].get("status") if job else None, "waited_s": waited,
                     "chat_agrees": agree, "answer": answer[:160], "problems": problems,
                     "result": "PASS" if not problems else "CHECK"})
        r = rows[-1]
        print(f"{r['result']:5} {doc_type:16} {path.name:28} {r['status'] or '-':11} upload={upload_ms}ms "
              f"job={r['background_job']} chat_agrees={agree} {problems or ''}")
        print(f"      chat: {answer[:150]}")
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "live_document_acceptance.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False),
                                                                 encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
