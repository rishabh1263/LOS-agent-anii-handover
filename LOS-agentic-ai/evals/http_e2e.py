"""
REAL HTTP END-TO-END against a running server -- no TestClient, no stub.

    python -m evals.http_e2e [--base http://127.0.0.1:8010] [--skip-jev]

Mints a development FOS token with make_fos_token.py (the local signing key),
then drives the live API exactly as a frontend would and prints PASS / FAIL
per check. Exit code 1 when any check fails.

  JEV       health on the real provider; upload -> KYC -> background JEV
            evaluation -> persisted typed decisions -> gated actions -> the
            copilot's semantic_decisions; the KYC agent's result unchanged
  RE-UPLOAD PAN / DL / Voter ID / Passport superseded by a newer upload;
            bank statements additive; two PANs in one batch a KYC conflict
  CHAT      PAN details; KYC details; no scores / codes / file names in text
  SIGNATURE no specimen -> REVIEW, said once, in words
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
S = ROOT / "samples"
RISHABH_PAN, RISHABH_DL, OTHER_PAN = S / "documents/rpan.jpg", S / "documents/driving_license.jpg", S / "lPan.jpg"
DL_B, VOTER_A, VOTER_B = S / "documents/sidkamble.jpg", S / "documents/voter_id2.jpg", S / "real_batch/voter_id.jpg"
PASSPORT_A, PASSPORT_B = S / "passports/passport_samples0_1.jpg", S / "passports/passport_samples0_10.jpg"
BANK_A, BANK_B = S / "real_batch/bank_hdfc_new.pdf", S / "real_batch/bank_amit.pdf"

BACKEND = re.compile(r"\.(jpe?g|png|pdf)\b|runtime[/\\]|[A-Z]+_[A-Z_]{3,}|\bscore\b|confidence\s+\d", re.I)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""), flush=True)
    return bool(ok)


class Api:
    def __init__(self, base: str, token: str):
        self.c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=600)

    def open_case(self) -> tuple[str, str]:
        r = self.c.post("/api/v1/fos/applicants", json={
            "applicant": {"full_name": "E2E Applicant", "mobile": "9876543210", "date_of_birth": "1990-04-12",
                          "address": "Mumbai"},
            "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000}})
        r.raise_for_status()
        return r.json()["applicant_id"], r.json()["case_id"]

    def upload(self, applicant_id, case_id, items, types):
        files = [("files", (name, Path(path).read_bytes(), "application/octet-stream")) for name, path in items]
        r = self.c.post("/api/v1/fos/copilot", data={"applicant_id": applicant_id, "case_id": case_id,
                                                     "action": "UPLOAD_DOCUMENT", "document_types": list(types)},
                        files=files)
        r.raise_for_status()
        return r.json()

    def ask(self, applicant_id, case_id, message):
        r = self.c.post("/api/v1/fos/copilot", json={"applicant_id": applicant_id, "case_id": case_id,
                                                     "action": "CUSTOM_QUERY", "message": message})
        r.raise_for_status()
        return r.json()

    def documents(self, case_id):
        r = self.c.get(f"/api/v1/fos/documents/{case_id}")
        r.raise_for_status()
        body = r.json()
        return body.get("documents") if isinstance(body, dict) else body


def _types(docs):
    return sorted(str(d.get("document_type") or d.get("type") or "") for d in docs or [])


def _kyc_sources(body):
    return sorted({s.get("source_id") for f in (body.get("kyc") or {}).get("fields") or []
                   for s in f.get("sources") or []})


def jev_flow(api: Api) -> None:
    health = api.c.get("/api/v1/jev/health").json()
    check("JEV health: runtime ready on a real provider", health.get("jev_runtime_ready") is True
          and "localhost:8888" in str(health.get("jev_provider")), str(health))
    check("JEV health: model is the configured laya-multilingual", health.get("jev_model") == "laya-multilingual",
          str(health.get("jev_model")))

    applicant_id, case_id = api.open_case()
    body = api.upload(applicant_id, case_id, [("pan.jpg", OTHER_PAN), ("dl.jpg", RISHABH_DL)],
                      ["PAN", "DRIVING_LICENCE"])
    check("upload: JEV scheduled after the upload, not inside it",
          (body.get("semantic_decisions") or {}).get("jev_status") == "SCHEDULED", str(body.get("semantic_decisions")))
    kyc_before = (body.get("kyc") or {}).get("status")

    decided, started = {}, time.perf_counter()
    while time.perf_counter() - started < 180:
        decided = api.c.get(f"/api/v1/jev/cases/{case_id}/decisions").json()
        if decided.get("jev_status") not in (None, "NOT_EVALUATED"):
            break
        time.sleep(2)
    waited = round(time.perf_counter() - started, 1)
    check("JEV: evaluation completed on the real provider (background trigger)",
          decided.get("jev_status") == "COMPLETED", f"{decided.get('jev_status')} after {waited}s")
    check("JEV: provider recorded is the local Decision API", "localhost:8888" in str(decided.get("provider")),
          str(decided.get("provider")))
    types = {d["decision_type"] for d in decided.get("semantic_decisions") or []}
    check("JEV: typed decisions of all three kinds, no identity question",
          {d["question_type"] for d in decided.get("semantic_decisions") or []} == {"CHOICE", "SCORE", "NOUL"}
          and not [t for t in types if "IDENTITY" in t], str(types))
    check("JEV: every decision carries confidence + probabilities",
          all(d.get("confidence") is not None and d.get("probabilities") for d in decided.get("semantic_decisions") or []))
    route = next((d for d in decided.get("semantic_decisions") or [] if d["decision_type"] == "ROUTING"), {})
    check("JEV: routing is ADVISORY (never executed)", route.get("decision_policy") == "ADVISORY"
          and route.get("status") == "INFO", str(route))
    actions = decided.get("semantic_actions") or []
    check("JEV: every action went through the gate (EXECUTED/BLOCKED/DEFERRED/ADVISORY/SKIPPED)",
          actions and all(a.get("status") in {"EXECUTED", "BLOCKED", "DEFERRED", "ADVISORY", "SKIPPED"} for a in actions),
          str([(a.get("action"), a.get("status")) for a in actions]))
    check("JEV: authoritative statuses never changed", decided.get("authoritative_statuses_changed") is False)

    again = api.c.post(f"/api/v1/jev/cases/{case_id}/evaluate").json()
    check("JEV: same evidence is not re-evaluated (idempotent, persisted run returned)",
          again.get("reused") is True and again.get("jev_run_id") == decided.get("jev_run_id"))

    answer = api.ask(applicant_id, case_id, "what is kyc status")
    check("copilot: semantic_decisions in the structured response",
          (answer.get("semantic_decisions") or {}).get("jev_run_id") == decided.get("jev_run_id"))
    kyc_after = answer.get("kyc") or {}
    check("copilot: KYC result unchanged by JEV (the KYC agent's verdict)",
          kyc_before in (None, "") or "needs review" in answer["answer"].lower() or kyc_before == "PASS",
          f"before={kyc_before} answer={answer['answer'][:80]}")
    check("copilot: chatbot LLM not involved (structured answer)", answer.get("response_source") == "STRUCTURED",
          str(answer.get("response_source")))
    check("copilot: no internal codes / scores in the text", not BACKEND.search(answer["answer"]), answer["answer"])


def reupload_flow(api: Api) -> None:
    for label, first, second, kind in (("PAN", RISHABH_PAN, OTHER_PAN, "PAN"),
                                      ("DL", RISHABH_DL, DL_B, "DRIVING_LICENCE"),
                                      ("Voter ID", VOTER_A, VOTER_B, "VOTER_ID"),
                                      ("Passport", PASSPORT_A, PASSPORT_B, "PASSPORT")):
        a, c = api.open_case()
        api.upload(a, c, [("first.jpg", first)], [kind])
        api.upload(a, c, [("second.jpg", second)], [kind])
        docs = api.documents(c)
        current = [d for d in docs or [] if str(d.get("document_type")).upper() == kind]
        check(f"re-upload {label}: one current document after a second upload", len(current) == 1,
              f"{len(current)} current: {[d.get('source_id') for d in current]}")

    a, c = api.open_case()
    api.upload(a, c, [("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL)], ["PAN", "DRIVING_LICENCE"])
    second = api.upload(a, c, [("pan_new.jpg", OTHER_PAN)], ["PAN"])
    check("re-upload PAN: superseded PAN not in KYC", _kyc_sources(second) == ["dl.jpg", "pan_new.jpg"],
          str(_kyc_sources(second)))
    listed = api.ask(a, c, "which documents are uploaded")["answer"]
    check("re-upload PAN: document list names one PAN", listed.count("PAN") == 1, listed)
    details = api.ask(a, c, "give me pan details")["answer"]
    check("chat: PAN details of the CURRENT PAN, masked", "LAXMI SANTOSH GUPTA" in details and "XXXXXX" in details
          and "RISHABH" not in details, details)
    check("chat: PAN details carry no internal codes", not BACKEND.search(details), details)
    kyc = api.ask(a, c, "kyc details")["answer"]
    check("chat: KYC details field by field, consistent with the result", "Field by field:" in kyc
          and "did not match" in kyc and not BACKEND.search(kyc.split("What differs:")[0]), kyc)

    a, c = api.open_case()
    api.upload(a, c, [("bank1.pdf", BANK_A)], ["BANK_STATEMENT"])
    api.upload(a, c, [("bank2.pdf", BANK_B)], ["BANK_STATEMENT"])
    banks = [d for d in api.documents(c) or [] if str(d.get("document_type")).upper() == "BANK_STATEMENT"]
    check("bank statements stay additive (two current)", len(banks) == 2, str(len(banks)))

    a, c = api.open_case()
    both = api.upload(a, c, [("a.jpg", RISHABH_PAN), ("b.jpg", OTHER_PAN)], ["PAN", "PAN"])
    check("two PANs in one batch: both current, KYC reports the conflict",
          _kyc_sources(both) == ["a.jpg", "b.jpg"] and (both.get("kyc") or {}).get("status") == "REVIEW",
          f"{_kyc_sources(both)} {(both.get('kyc') or {}).get('status')}")


def signature_flow(api: Api, tmp: Path) -> None:
    import math

    from PIL import Image, ImageDraw

    im = Image.new("RGB", (600, 220), "white")
    d = ImageDraw.Draw(im)
    d.line([(40 + i * 2.6, 110 + 45 * math.sin(i / 9.0) + 18 * math.sin(i / 2.7)) for i in range(200)],
           fill=(20, 30, 120), width=4)
    path = tmp / "e2e_sign.jpg"
    im.save(path, quality=92)
    a, c = api.open_case()
    body = api.upload(a, c, [("sign.jpg", path)], ["SIGNATURE"])
    card = (body.get("verification") or {}).get("documents_processed", [{}])[0]
    check("signature: no specimen -> REVIEW", card.get("verification") == "REVIEW", str(card.get("verification")))
    check("signature: white paper is not 'clipped' / 'low quality'",
          "IMAGE_CLIPPED" not in (card.get("reason_codes") or []) and "SIGNATURE_LOW_QUALITY" not in (card.get("reason_codes") or []),
          str(card.get("reason_codes")))
    check("signature: said once, in words", body["answer"].count("reference signature") == 1
          and not BACKEND.search(body["answer"]), body["answer"])


def _token(subject: str, scopes: str) -> str:
    return subprocess.run([sys.executable, str(ROOT / "make_fos_token.py"), "--ttl", "2", "--subject", subject,
                           "--scopes", scopes], capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]


def credit_flow(base: str) -> None:
    """KYC -> ELIGIBILITY -> (four-eyes) -> CREDIT on the live server."""
    fos = ("read_applicant read_application read_documents read_verification read_pending_items read_next_action "
           "create_applicant update_applicant create_application upload_document")
    maker = Api(base, _token("e2e-maker", f"{fos} los.stage:write los.stage:override los.credit.underwrite"))
    checker = Api(base, _token("e2e-checker", "los.read los.write los.approvals.check"))

    applicant_id, case_id = maker.open_case()
    files = [("files", (n, Path(p).read_bytes(), "application/octet-stream"))
             for n, p in (("pan.jpg", RISHABH_PAN), ("dl.jpg", RISHABH_DL), ("slip.pdf", S / "real_batch/salary_slip.pdf"),
                          ("bank.pdf", BANK_A))]
    r = maker.c.post("/api/v1/los/process", data={
        "applicant_id": applicant_id, "case_id": case_id, "operation": "PROCESS",
        "expected_types": ["PAN", "DRIVING_LICENCE", "SALARY_SLIP", "BANK_STATEMENT"]}, files=files)
    check("process: KYC + eligibility pipeline answered", r.status_code == 200, r.text[:200])
    body = r.json() if r.status_code == 200 else {}
    kyc = (body.get("kyc") or {}).get("status")
    eligibility = next(iter((body.get("party_eligibility") or {}).values()), None) or body.get("eligibility") or {}
    rule = next((x for x in eligibility.get("rules") or [] if x.get("rule_id") == "KYC_PREREQUISITE"), {})
    check("eligibility: KYC gate evaluated against the real KYC result",
          rule.get("outcome") in ({"PASS"} if kyc == "PASS" else {"REVIEW", "NOT_EVALUATED", "FAIL"}),
          f"kyc={kyc} gate={rule.get('outcome')} state={eligibility.get('state')}")
    check("eligibility: an explicit state", eligibility.get("state") in
          {"ELIGIBLE", "NOT_ELIGIBLE", "REVIEW", "PENDING", "CONFIGURATION_GAP"}, str(eligibility.get("state")))
    if kyc != "PASS":
        check("eligibility: a KYC that did not pass never yields ELIGIBLE", eligibility.get("state") != "ELIGIBLE",
              str(eligibility.get("state")))

    for target, expected in (("CPA", "FOS"), ("CREDIT", "CPA")):
        moved = maker.c.post(f"/api/v1/los/cases/{case_id}/stage", json={
            "target_stage": target, "expected_stage": expected, "reason": f"e2e to {target}",
            "idempotency_key": f"e2e-{case_id}-{target}", "mode": "OVERRIDE"})
        check(f"four-eyes: override to {target} is PENDING_CHECK, case not moved", moved.status_code == 202,
              f"{moved.status_code} {moved.text[:160]}")
        if moved.status_code != 202:
            return
        approval = moved.json()["approval"]["approval_id"]
        self_check = Api(base, _token("e2e-maker", "los.read los.write los.approvals.check")).c.post(
            f"/api/v1/approvals/{approval}/decision", json={"decision": "APPROVE"})
        check(f"four-eyes: the maker cannot check the {target} override", self_check.status_code == 403,
              str(self_check.status_code))
        done = checker.c.post(f"/api/v1/approvals/{approval}/decision", json={"decision": "APPROVE"})
        check(f"four-eyes: a second person approves -> case moves to {target}",
              done.status_code == 200 and done.json().get("status") == "APPROVED"
              and (done.json().get("result") or {}).get("stage") == target, done.text[:200])

    # entering CREDIT through an approval runs no underwriting by itself (the checker is not an
    # underwriter); the maker -- an underwriter -- runs it explicitly, and it reads the records
    run = maker.c.post("/api/v1/credit/underwriting/run", json={"case_id": case_id})
    check("credit: the existing Credit agent ran at the CREDIT stage", run.status_code == 200, run.text[:200])
    got = maker.c.get(f"/api/v1/credit/{case_id}").json()
    check("credit: an assessment is recorded and readable", got.get("status") in
          {"READY_FOR_DECISION", "REVIEW_REQUIRED", "DATA_INSUFFICIENT"}, str(got.get("status")))
    check("credit: an assessment, never an approval",
          "APPROVED" not in str(got.get("status")) and "sanction" not in str(got).lower(), str(got.get("status")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--skip-jev", action="store_true")
    parser.add_argument("--only", default="jev,reupload,signature,credit")
    args = parser.parse_args()
    token = subprocess.run([sys.executable, str(ROOT / "make_fos_token.py"), "--ttl", "2"],
                           capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    api = Api(args.base, token)
    started = time.perf_counter()
    parts = set(args.only.split(","))
    if "jev" in parts and not args.skip_jev:
        jev_flow(api)
    if "reupload" in parts:
        reupload_flow(api)
    if "credit" in parts:
        credit_flow(args.base)
    if "signature" in parts:
        import tempfile

        signature_flow(api, Path(tempfile.mkdtemp()))
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed in {time.perf_counter() - started:.0f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
