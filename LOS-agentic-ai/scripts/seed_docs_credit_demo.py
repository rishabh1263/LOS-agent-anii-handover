"""
DEMO cases for the Credit Document Policy Agent (POST /api/v1/credit/document-readiness/{case_id}).

ALL DATA IS SYNTHETIC (made-up names, no real customer). Each case is written into the CONFIGURED case store (the one
the server reads) through the LOS ingest path (app.store.ingest.persist_los_result): documents, verdicts and
extracted values land exactly where the document pipeline puts them. Deviations go through the real deviation workflow
(raised by one person, decided by another). Re-running is safe: an existing demo case is brought up to date (stage and deviations are not redone).

    python scripts/seed_docs_credit_demo.py            # seed, then print each case's readiness
    python scripts/seed_docs_credit_demo.py --quiet    # seed only
    python scripts/seed_docs_credit_demo.py --ai       # also write each case's AI note now (local Qwen, ~10 s each),
                                                       # so the API shows it on the very first call

Case ids follow the eligibility demo cases (CASE-E11D...): CASE-D0C5000000NN. Like real files, most have gaps:

    01 Rahul Sharma        everything in place                                   -> COMPLETE, underwriting allowed
    02 Priya Nair          PAN not given; bank statement only 2 months           -> ACTION_REQUIRED
    03 Amit Verma + Neha   co-applicant gave only PAN; applicant's salary slip
                           is 4 months old                                       -> ACTION_REQUIRED
    04 Sunita Rao          utility bill as address proof (exception pending);
                           signature not given                                   -> ACTION_REQUIRED
    05 Mohammed Irfan      utility bill, exception APPROVED; but bank statement
                           was collected at CPA, credit needs a fresh one        -> ACTION_REQUIRED
    06 Kavita Joshi        self-employed: shop photo 2 missing, bank statement
                           80 days old, ITR without its computation              -> ACTION_REQUIRED
    07 Rajesh Kumar        passport expired; salary slip still being verified    -> ACTION_REQUIRED
    08 Anjali Mehta        PAN rejected by verification; address proof sent
                           for manual review                                     -> ACTION_REQUIRED
    09 Vikram Singh        employment type never captured; signature not given   -> ACTION_REQUIRED
    10 Deepa Iyer          everything in place, but the case is still at CPA     -> COMPLETE, NOT allowed yet
    11 Sanjay + Ramesh     co-applicant gave nothing; bank statement dates
       Patel               could not be read                                     -> ACTION_REQUIRED
    12 Fatima Shaikh       only exception left: utility bill, approval pending   -> EXCEPTION_PENDING
    14 Arjun + Pooja       both complete; the co-applicant's salary slip is
       Malhotra            4 months old (not critical)                           -> COMPLETE, moves ahead with notes
    13 Neeraj Gupta        everything in place; ITR without its computation
                           (not critical)                                        -> COMPLETE, moves ahead with notes
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEV_SUBJECT = os.getenv("AUTH_LOCAL_SUBJECT") or "local-developer"
TODAY = date.today()
PREFIX = "CASE-D0C5"


def _add_months(day: date, months: int) -> date:
    from app.agents.docs_verify_credit_agent.services.dates import add_months

    return add_months(day, months)


def _month(months_ago: int) -> str:
    day = TODAY.replace(day=1)
    for _ in range(months_ago):
        day = (day - timedelta(days=1)).replace(day=1)
    return day.strftime("%B %Y").upper()


def doc(party: str, kind: str, source: str, fields: dict | None = None, verification: str | None = "PASS") -> dict:
    return {"source_id": source, "type": kind, "verification": verification, "party_id": party,
            **({"extraction": {"fields": fields}} if fields else {})}


def pan(party="APP", **kw):
    return doc(party, "PAN", "pan.jpg", **kw)


def passport(party="APP", *, expires_in_days=900, **kw):
    return doc(party, "PASSPORT", "passport.jpg",
               {"valid_until": (TODAY + timedelta(days=expires_in_days)).isoformat()}, **kw)


def signature(party="APP", **kw):
    return doc(party, "SIGNATURE", "signature.png", **kw)


def slip(party="APP", *, months_ago=1, **kw):
    return doc(party, "SALARY_SLIP", "salary-slip.pdf", {"pay_period": _month(months_ago)}, **kw)


def bank(party="APP", *, months=3, end_days_ago=5, source="bank-statement.pdf", fields=None, **kw):
    end = TODAY - timedelta(days=end_days_ago)
    start = _add_months(end, -months) + timedelta(days=1)
    return doc(party, "BANK_STATEMENT", source,
               fields or {"period_start": start.isoformat(), "period_end": end.isoformat()}, **kw)


def business(party="APP", *, computation=True) -> list[dict]:
    """A self-employed applicant's income documents: the ITR (+ its computation) and the two business photos."""
    out = [doc(party, "ITR", "itr-ay2026.pdf"), doc(party, "BUSINESS_PROOF_1", "shop-front.jpg"),
           doc(party, "BUSINESS_PROOF_2", "shop-inside.jpg")]
    return out + ([doc(party, "ITR_COMPUTATION", "itr-computation.pdf")] if computation else [])


def full(party="APP", months=3) -> list[dict]:
    """The standard set; `months` of bank statement (personal loan 3, home loan 6, LAP 12 in the sample policies)."""
    return [pan(party), passport(party), signature(party), slip(party), bank(party, months=months)]


def without(docs: list[dict], *kinds: str) -> list[dict]:
    return [d for d in docs if d["type"] not in kinds]


def _stage(repository, case_id: str, stage: str, version: int) -> None:
    from app.store.models import CaseEvent, CaseStage, StageTransition

    repository.apply_stage_transition(
        version - 1, CaseStage(case_id=case_id, stage=stage, stage_status="IN_PROGRESS", version=version),
        StageTransition(transition_id=f"STG-{case_id}-{stage}-{version}", case_id=case_id, version=version,
                        kind="STAGE_ENTERED", to_stage=stage, to_status="IN_PROGRESS"),
        CaseEvent(event_id=f"EV-{case_id}-{stage}-{version}", case_id=case_id, event_type="STAGE_ENTERED",
                  stage=stage))


def _deviation(case_id: str, *, approve: bool) -> None:
    from app.agents.los import queries

    raised = queries.raise_deviation(case_id, code="DVM-62A2A3",
                                     reason="Customer has only an electricity bill as address proof",
                                     actor="demo-sales-officer", scopes={"los.deviation:write"})
    if approve and raised.get("status") == "PENDING_APPROVAL":
        queries.decide_deviation(case_id, raised["deviation_id"], decision="APPROVED", actor="demo-acm",
                                 scopes={"los.deviation:approve"},
                                 justification="Bill in customer's name, address matches field visit",
                                 authorities={"ACM"})


# THE STORED FILES (RULE-11): small synthetic files whose first bytes are a real format's signature
_PDF_TYPES = {"BANK_STATEMENT", "SALARY_SLIP", "ITR", "UTILITY_BILL", "LOAN_STATEMENT", "LOAN_SANCTION_LETTER",
              "SALE_DEED", "ITR_COMPUTATION"}
_HEAD = {"pdf": b"%PDF-1.7\n", "jpg": b"\xff\xd8\xff\xe0", "png": b"\x89PNG\r\n\x1a\n"}
#: realistic size ranges in KB per document type (a phone photo of a card, a multi-page PDF statement, a scanned deed)
_SIZE_KB = {
    "PAN": (180, 420), "PASSPORT": (650, 1400), "DRIVING_LICENCE": (300, 700), "VOTER_ID": (250, 600),
    "SIGNATURE": (40, 90), "SALARY_SLIP": (140, 320), "BANK_STATEMENT": (900, 2600), "ITR": (380, 950),
    "ITR_COMPUTATION": (120, 300), "BUSINESS_PROOF_1": (1500, 3000), "BUSINESS_PROOF_2": (1400, 2900),
    "SALE_DEED": (3000, 6500), "UTILITY_BILL": (220, 480), "LOAN_STATEMENT": (260, 600),
}
#: the files a demo case deliberately gets wrong
_SPECIAL = {"big": ("jpg", 12 * 1024), "tiny": ("pdf", 2)}          # 12 MB over the 10 MB limit; 2 KB unreadable


def _file_bytes(doc_type: str, kind: str, key: str) -> bytes:
    """A file of a believable size for its type, the same every run for the same upload (seeded by its key)."""
    import hashlib

    if kind in _SPECIAL:
        fmt, kb = _SPECIAL[kind]
    else:
        fmt = kind
        low, high = _SIZE_KB.get(doc_type, (200, 800))
        kb = low + int(hashlib.sha1(key.encode()).hexdigest(), 16) % (high - low)
    head = _HEAD[fmt]
    return head + b"0" * (kb * 1024 - len(head))


def _store_file(case_id: str, party_id: str, source_id: str, kind: str, doc_type: str) -> None:
    from app.agents.los import parties
    from app.store import documents as store_module

    key = store_module.storage_key(parties.document_key(case_id, party_id, source_id))
    store_module.get_document_store().put(key, _file_bytes(doc_type, kind, key))


def build(repository, n: int, name: str, *, documents: list[dict], amount: str = "300000", product: str = "PERSONAL_LOAN",
          employment: str | None = "SALARIED", co_name: str | None = None, stage: str = "CREDIT",
          before_stage: list[dict] | None = None, deviation: str | None = None, emis: str = "0",
          co_job: str | None = "SALARIED",
          income: str = "65000") -> str:
    """Create the case, or bring an existing demo case up to date (application, documents, files)."""
    from app.store.ingest import persist_los_result
    from app.store.models import Applicant, Application

    case_id, app, co = f"{PREFIX}{n:08d}", f"APP-D0C5{n:08d}", f"APP-D0C5{n:08d}-CO"
    existed = repository.get_application(case_id) is not None
    repository.save_applicant(Applicant(applicant_id=app, full_name=name))
    if co_name:
        repository.save_applicant(Applicant(applicant_id=co, full_name=co_name))
    repository.save_application(Application(case_id=case_id, applicant_id=app, product=product,
                                            loan_amount=amount, employment_type=employment,
                                            declared_monthly_income=income, declared_monthly_obligations=emis,
                                            co_applicant_id=co if co_name else None,
                                            co_applicant_employment_type=co_job if co_name else None))
    repository.grant_access(DEV_SUBJECT, "APPLICANT", app)
    names = {"APP": app, "CO": co}

    def rename(d: dict) -> dict:
        out = {k: v for k, v in d.items() if k != "file"}
        return {**out, "party_id": names[d["party_id"]],
                "source_id": f"{case_id}-{d['party_id'].lower()}-{d['source_id']}"}

    def files(docs: list[dict]) -> None:
        for d in docs:
            kind = d.get("file") or ("pdf" if d["type"] in _PDF_TYPES else "png" if d["type"] == "SIGNATURE" else "jpg")
            r = rename(d)
            _store_file(case_id, r["party_id"], r["source_id"], kind, d["type"])

    version = 1
    if before_stage:                                   # collected at CPA, then the case moves to CREDIT
        if not existed:
            _stage(repository, case_id, "CPA", version)
        version += 1
        persist_los_result({"applicant_id": app, "case_id": case_id, "documents": [rename(d) for d in before_stage]})
        files(before_stage)
    if not existed:
        _stage(repository, case_id, stage, version)
    if documents:
        persist_los_result({"applicant_id": app, "case_id": case_id, "documents": [rename(d) for d in documents]})
        files(documents)
    if deviation and not existed:
        _deviation(case_id, approve=deviation == "APPROVED")
    return case_id


def seed(repository) -> list[str]:
    """Every case has a co-applicant; personal loan, home loan and loan against property, each on its own checklist
    (a home loan / LAP also needs the property's sale deed, held by the applicant)."""
    bill = doc("APP", "UTILITY_BILL", "electricity-bill.pdf")
    loan = doc("APP", "LOAN_STATEMENT", "car-loan-statement.pdf")
    deed = doc("APP", "SALE_DEED", "sale-deed.pdf")
    PL, HL, LAP = "PERSONAL_LOAN", "HOME_LOAN", "LOAN_AGAINST_PROPERTY"
    co = full("CO")
    return [
        build(repository, 1, "Rahul Sharma", co_name="Kiran Sharma", product=PL, amount="450000",
              documents=[*full(), *co]),
        build(repository, 2, "Priya Nair", co_name="Arun Nair", product=PL, amount="250000",
              documents=[passport(), signature(), slip(), {**bank(months=2), "file": "jpg"}, *co]),
        build(repository, 3, "Amit Verma", co_name="Neha Verma", product=PL, amount="800000", emis="15000",
              documents=[*without(full(), "SALARY_SLIP"), slip(months_ago=4), pan("CO")]),
        build(repository, 4, "Sunita Rao", co_name="Mohan Rao", product=HL, amount="2500000",
              deviation="PENDING_APPROVAL",
              co_job="SELF_EMPLOYED",
              documents=[*without(full(months=6), "PASSPORT", "SIGNATURE"), bill, deed,
                         *without(full("CO", months=6), "SALARY_SLIP"), *business("CO")]),
        build(repository, 5, "Mohammed Irfan", co_name="Ayesha Irfan", product=LAP, amount="1800000",
              employment="SELF_EMPLOYED",
              deviation="APPROVED", emis="8000", before_stage=[bank(months=12, source="bank-statement-cpa.pdf")],
              documents=[*without(full(), "PASSPORT", "BANK_STATEMENT"), bill, loan, deed, *business(),
                         *without(co, "BANK_STATEMENT"), bank("CO", months=12, end_days_ago=70)]),
        build(repository, 6, "Kavita Joshi", co_name="Rohan Joshi", product=LAP, employment="SELF_EMPLOYED",
              amount="3200000",
              documents=[*without(full(), "SALARY_SLIP", "BANK_STATEMENT"), bank(months=12, end_days_ago=80),
                         doc("APP", "ITR", "itr-ay2026.pdf"), doc("APP", "BUSINESS_PROOF_1", "shop-front.jpg"),
                         *without(co, "BANK_STATEMENT"), bank("CO", months=12)]),
        build(repository, 7, "Rajesh Kumar", co_name="Seema Kumar", product=HL, amount="3500000",
              documents=[*without(full(), "PASSPORT", "SALARY_SLIP", "BANK_STATEMENT"), bank(months=6),
                         {**passport(expires_in_days=-40), "file": "big"}, slip(verification=None), deed,
                         *without(co, "SALARY_SLIP", "BANK_STATEMENT"), slip("CO", verification=None),
                         bank("CO", months=6)]),
        build(repository, 8, "Anjali Mehta", co_name="Vivek Mehta", product=PL, amount="275000",
              documents=[*without(full(), "PAN", "PASSPORT"), pan(verification="FAIL"),
                         passport(verification="REVIEW"), *without(co, "PAN"), pan("CO", verification="FAIL")]),
        build(repository, 9, "Vikram Singh", co_name="Neelam Singh", product=HL, employment=None, amount="4000000",
              co_job="SELF_EMPLOYED",
              documents=[*without(full(), "SIGNATURE", "BANK_STATEMENT"), bank(months=6),
                         *without(co, "BANK_STATEMENT", "SALARY_SLIP"), bank("CO", months=6),
                         doc("CO", "ITR", "itr-ay2026.pdf"), doc("CO", "ITR_COMPUTATION", "itr-computation.pdf"),
                         doc("CO", "BUSINESS_PROOF_1", "shop-front.jpg")]),
        build(repository, 10, "Deepa Iyer", co_name="Suresh Iyer", product=PL, amount="320000", stage="CPA",
              documents=[*full(), *co]),
        build(repository, 11, "Sanjay Patel", co_name="Ramesh Patel", product=LAP, amount="2800000",
              co_job="SELF_EMPLOYED",
              documents=[*without(full(), "BANK_STATEMENT", "PAN"), {**pan(), "file": "tiny"},
                         bank(fields={"period_start": "see page 2", "period_end": "unclear"})]),
        build(repository, 12, "Fatima Shaikh", co_name="Imran Shaikh", product=HL, amount="2200000",
              deviation="PENDING_APPROVAL",
              documents=[*without(full(), "PASSPORT", "BANK_STATEMENT"), bank(months=6), bill, deed,
                         *without(co, "BANK_STATEMENT"), bank("CO", months=6)]),
        build(repository, 13, "Neeraj Gupta", co_name="Ritu Gupta", product=PL, amount="400000",
              employment="SELF_EMPLOYED",
              co_job="SELF_EMPLOYED",
              documents=[*full(), *business(computation=False), *without(co, "SALARY_SLIP"), *business("CO")]),
        build(repository, 14, "Arjun Malhotra", co_name="Pooja Malhotra", product=HL, amount="7000000",
              documents=[*without(full(), "BANK_STATEMENT"), bank(months=6), deed,
                         *without(co, "SALARY_SLIP", "BANK_STATEMENT"), slip("CO", months_ago=4), bank("CO", months=6)]),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--quiet", action="store_true", help="seed only; do not print readiness")
    parser.add_argument("--ai", action="store_true", help="write each case's AI note now (waits for the model)")
    args = parser.parse_args()
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")                 # the same case store the server reads
    os.environ.setdefault("ENVIRONMENT", "development")
    if not args.ai:
        os.environ["DOCS_CREDIT_AI_SUMMARY"] = "false"
    else:
        from app.agents.docs_verify_credit_agent.services import summary_service

        summary_service.wording()["ai_summary"]["timeout_seconds"] = 120     # this run waits for each note
    from app.store import get_repository

    repository = get_repository()
    cases = seed(repository)
    if args.quiet:
        print("\n".join(cases))
        return 0
    from app.agents.docs_verify_credit_agent.services import readiness_service

    for case_id in cases:
        r = readiness_service.evaluate(case_id, repository)
        print(f"\n{case_id}  stage={r.stage:<6} {r.overall_status:<18} allowed={r.credit_underwriting_allowed}")
        print(f"  {r.summary.headline}")
        for line in r.summary.missing:
            print(f"    missing:       {line}")
        for line in r.summary.not_satisfied:
            print(f"    not satisfied: {line}")
        print(f"  next: {r.summary.next_step}")
        if args.ai:
            print(f"  AI ({r.summary.ai_summary.source}): {r.summary.ai_summary.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
