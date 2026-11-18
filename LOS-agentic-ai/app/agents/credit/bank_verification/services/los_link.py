"""
THE LOS CASES (config los_cases): the search opens a case of the LOS itself -- the same cases the Dedupe Agent
creates -- by its case ID (CASE-...) or its applicant ID (APP-...; that applicant's latest case).

    find(ref)          the LOS case id the reference names, or None
    sync(repo, id)     the bank verification case for it, created or refreshed from the LOS record: applicant name,
                       loan amount, product, stage. The LOS bank statement document, when one was uploaded, is copied in
                       as the statement on file (once per file; its hash says whether it is already here).

Only what the screen needs is read from the LOS; nothing is written back to it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.credit.bank_verification.config import section, words
from app.agents.credit.bank_verification.repository import Repository
from app.agents.credit.bank_verification.services import statement_service

logger = logging.getLogger(__name__)


def enabled() -> bool:
    import os

    override = (os.getenv("BANK_VERIFICATION_LOS") or "").strip().lower()      # tests: off
    if override:
        return override in ("1", "on", "true", "yes")
    return bool(section("los_cases").get("enabled", False))


def _repo():
    from app.store import get_repository

    return get_repository()


def find(ref: str) -> str | None:
    """The LOS case id for a case ID or an applicant ID (exact, case and spacing ignored), else None."""
    if not enabled():
        return None
    wanted = "".join(str(ref).split()).upper()
    los = _repo()
    try:
        if los.get_application(wanted) is not None:
            return wanted
        cases = los.list_applications(wanted)
    except Exception as exc:  # noqa: BLE001 - the LOS store unreachable: nothing found, said as such
        logger.warning("bank verification: LOS lookup failed (%s)", type(exc).__name__)
        return None
    if not cases:
        return None
    latest = max(cases, key=lambda a: str(a.created_at or ""))
    return latest.case_id


def sync(repo: Repository, case_id: str, actor: str) -> None:
    """Create or refresh the bank verification case from the LOS record, and copy in its bank statement."""
    los = _repo()
    app = los.get_application(case_id)
    if app is None:
        return
    applicant = los.get_applicant(app.applicant_id)
    stage = los.get_case_stage(case_id)
    header = {"application_ref": app.applicant_id,
              "applicant_name": (applicant.full_name if applicant and applicant.full_name else app.applicant_id),
              "loan_amount": str(app.loan_amount or "0"), "product": str(app.product or "PERSONAL_LOAN"),
              "stage": str(stage.stage if stage else section("los_cases").get("default_stage", "Sourcing"))}
    if repo.case(case_id) is None:
        repo.put_case({"case_id": case_id, **header, "mode": "AA_FAILURE",
                       "status": str(words("case_status.seeded"))})
        repo.audit(case_id, actor, "CASE_OPENED", {"source": "LOS"})
    else:
        repo.update_case(case_id, **header)
    _copy_statement(repo, los, case_id, actor)


def _copy_statement(repo: Repository, los: Any, case_id: str, actor: str) -> None:
    spec = section("los_cases")
    types = {str(t).upper() for t in spec.get("statement_document_types") or []}
    statuses = {str(s).upper() for s in spec.get("statement_statuses") or []}
    docs = [d for d in los.list_documents(case_id)
            if str(d.document_type).upper() in types and str(getattr(d.status, "value", d.status)).upper() in statuses]
    if not docs:
        return
    from app.store.documents import get_document_store, storage_key

    # newest first: the latest statement WHOSE FILE IS STORED and is a statement file (a record without its file, or
    # a photo, is skipped -- the officer can still upload one)
    content = kind = None
    for doc in sorted(docs, key=lambda d: str(d.uploaded_at or ""), reverse=True):
        try:
            found = get_document_store().get(storage_key(doc.document_id))
            if found:
                kind = statement_service.sniff(found, None, list(section("uploads").get("statement_types") or ["pdf"]))
                content = found
                break
        except statement_service.UploadError:
            continue
        except Exception as exc:  # noqa: BLE001 - an unreadable LOS file: try the next one
            logger.warning("bank verification: LOS statement unreadable (%s)", type(exc).__name__)
    if not content:
        return
    sha = statement_service.sha256(content)
    if any(d["sha256"] == sha for d in repo.documents(case_id, "STATEMENT")):
        return                                          # already on file
    document_id = repo.add_document(case_id, "STATEMENT", content, file_name=f"bank_statement.{kind}",
                                    kind=kind.upper(), sha256=sha)
    repo.audit(case_id, actor, "STATEMENT_RECEIVED", {"document_id": document_id, "source": "LOS"})
