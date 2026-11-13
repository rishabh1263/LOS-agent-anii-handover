"""
THE CASE, FROM ITS AUTHORITATIVE RECORDS -- the application, its parties, its stage (and when it was entered), its
documents (as the existing document agents recorded them) and its deviations. Nothing comes from the caller.

    parties     the applicant + the application's co-applicant + every co-applicant recorded on the case
                (app/agents/los/co_applicants.py), each once
    stage       app/agents/los/stages.resolve (the same authority the stage gates use); the moment it was entered
                from the case's stage record, when the stage record is that stage
    deviations  app/agents/los/queries.list_deviations: status as the deviation / maker-checker workflow left it

A store that cannot be read is CASE_STORE_UNAVAILABLE (503) -- never an empty case.
"""

from __future__ import annotations

from typing import Any

from app.agents.docs_verify_credit_agent.config import reasons as T
from app.agents.docs_verify_credit_agent.config.policy_loader import EvaluationError
from app.agents.docs_verify_credit_agent.integrations import existing_document_agents_adapter as documents
from app.agents.docs_verify_credit_agent.models.application import CaseContext, Deviation, Party


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value).strip() or None


def _stage(case_id: str, repository: Any):
    from app.agents.los import stages

    resolved = stages.resolve(case_id)
    stage = getattr(getattr(resolved, "stage", None), "value", None)
    record = repository.get_case_stage(case_id)
    entered = record.stage_started_at if record is not None and str(getattr(record.stage, "value", record.stage)
                                                                         ).upper() == (stage or "").upper() else None
    return (stage.upper() if stage else None), entered


def _parties(case_id: str, application: Any, repository: Any) -> tuple[Party, ...]:
    out = [Party(str(application.applicant_id), "PRIMARY_APPLICANT")]
    seen = {out[0].party_id}
    co_ids = [_text(getattr(application, "co_applicant_id", None))]
    try:
        from app.agents.los import co_applicants

        co_ids += [_text(r.get("co_applicant_id")) for r in co_applicants.list_for_case(case_id, repository)]
    except Exception:  # noqa: BLE001 - no co-applicant register on this store: the application's own field stands
        pass
    for co in co_ids:
        if co and co not in seen:
            seen.add(co)
            out.append(Party(co, "CO_APPLICANT"))
    return tuple(out)


def _deviations(case_id: str, repository: Any) -> tuple[Deviation, ...]:
    from app.agents.los import queries

    return tuple(Deviation(deviation_id=str(d.get("deviation_id")), code=str(d.get("code") or "").upper(),
                           status=str(d.get("status") or "").upper(), party_id=d.get("party_id"),
                           updated_at=d.get("updated_at"))
                 for d in queries.list_deviations(case_id, repository=repository))


def build(case_id: str, repository: Any) -> CaseContext:
    try:
        application = repository.get_application(case_id)
        if application is None:
            raise EvaluationError("CASE_NOT_FOUND", T.error("case_not_found"), 404)
        stage, entered = _stage(case_id, repository)
        return CaseContext(
            case_id=case_id, applicant_id=str(application.applicant_id),
            product=(_text(application.product) or "").upper() or None,
            scheme=(_text(getattr(application, "scheme", None)) or "").upper() or None,
            loan_amount=application.loan_amount, attributes=application.policy_attributes(),
            stage=stage, stage_entered_at=entered, parties=_parties(case_id, application, repository),
            documents=tuple(documents.documents_for(case_id, repository)),
            deviations=_deviations(case_id, repository),
            co_attributes=(application.co_applicant_policy_attributes()
                           if hasattr(application, "co_applicant_policy_attributes") else {}))
    except EvaluationError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable store is a dependency failure, never "nothing on file"
        raise EvaluationError("CASE_STORE_UNAVAILABLE", T.error("store_unavailable", error=type(exc).__name__),
                              503) from None


__all__ = ["build"]
