"""
THE CASE'S FACTS, FROM ONE PLACE (2026-11-06, owner: "yh sab me aise ek ek krke q fix krta rhunga?").

Every chatbot layer used to read a case its own way, so two answers could disagree: the summary said "Stage:
Application Created" (the internal workflow step) while every other answer said FOS (the LOS stage). This module
reads the facts once, from the same sources the engine answers from --

    stage         app.agents.los.stages.resolve      (the authoritative LOS stage) + its configured label
    workflow step workflow.derived_stage              (the FOS-internal step, "Application Created")
    readiness     workflow.readiness                  (READY_FOR_CPA / NOT_READY + the blocking items)
    documents     the case store                       (type -> status)
    application   the case store                       (product, loan amount, tenure)

-- and answering/fact_check.py checks every reply against them before it is published. Labels and values only; no
model, nothing written. Unreadable case -> None (the reply is published unchecked, as before).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaseFacts:
    case_id: str
    stage: str | None                      # LOS stage code ("FOS")
    stage_label: str | None                # its configured label ("FOS")
    workflow_step: str | None              # internal step code ("APPLICATION_CREATED")
    ready: bool                            # READY_FOR_CPA
    pending_documents: tuple[str, ...]     # document slots blocking readiness ("PAN", "ADDRESS_PROOF", ...)
    blocking_count: int                    # every blocking item (documents + others)
    documents: dict[str, str] = field(default_factory=dict)   # document_type -> status
    loan_amount: float | None = None
    tenure_months: int | None = None
    product: str | None = None
    has_co_applicant: bool | None = None   # None: not known


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def read(case_id: str | None) -> CaseFacts | None:
    """The case's facts, or None when it cannot be read (never raises)."""
    if not case_id:
        return None
    try:
        from app.agents.applicant import config, workflow
        from app.agents.los import stages
        from app.store import get_repository

        repo = get_repository()
        application = repo.get_application(case_id)
        if application is None:
            return None
        applicant = repo.get_applicant(application.applicant_id)
        documents = list(repo.list_documents(case_id) or [])
        resolution = workflow.resolution_for(application)
        readiness = workflow.readiness(applicant, application, documents, resolution=resolution)
        blocking = list(readiness.get("blocking_items") or [])
        context = stages.resolve(case_id)
        stage = getattr(getattr(context, "stage", None), "value", None) or None
        try:
            step = workflow.derived_stage(application, documents, readiness)
        except Exception:  # noqa: BLE001 - the step is a detail; the stage stands without it
            step = None
        tenure = _number(getattr(application, "tenure_months", None))
        return CaseFacts(
            case_id=case_id, stage=stage, stage_label=config.stage_label(stage) if stage else None,
            workflow_step=str(step).upper() if step else None,
            ready=readiness.get("status") == "READY_FOR_CPA",
            pending_documents=tuple(dict.fromkeys(str(i.get("slot")) for i in blocking
                                                  if str(i.get("type") or "").upper() == "DOCUMENT" and i.get("slot"))),
            blocking_count=len(blocking),
            documents={str(d.document_type): str(getattr(d.status, "value", d.status)) for d in documents},
            loan_amount=_number(getattr(application, "loan_amount", None)),
            tenure_months=int(tenure) if tenure is not None else None,
            product=str(application.product) if getattr(application, "product", None) else None,
            has_co_applicant=bool(getattr(application, "co_applicant_id", None)))
    except Exception:  # noqa: BLE001 - no facts: the reply goes out unchecked, as it did before this module
        logger.info("case_facts: case could not be read", exc_info=True)
        return None


__all__ = ["CaseFacts", "read"]
