"""
MCP capabilities for the FOS stage.

THE ONLY PATH TO THE CASE STORE. The Applicant Agent, the orchestrator and any
future caller reach applicant, application and document records through these
capabilities and through nothing else. No agent imports the repository, and
none imports sqlite3.

That is not ceremony. Permission checks, timeouts, audit and the
untrusted-data boundary all live at this layer, so a caller that went around
it would be a caller with none of them.

Same envelope, same error codes and the same `_envelope` discipline as
app/mcp/capabilities.py: no exception crosses this boundary as a traceback.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from app.mcp.errors import (
    InvalidInput,
    NotFound,
    ToolError,
    ToolStatus,
    Unavailable,
)
from app.mcp.schemas import ToolEnvelope, ToolErrorInfo

logger = logging.getLogger(__name__)


# ==========================================================================
# BOUNDARY
# ==========================================================================

async def _envelope(
    capability: str,
    run: Callable[[], Awaitable[dict[str, Any]]],
) -> ToolEnvelope:
    """Run a capability; convert every failure into a structured envelope."""
    from app.agents.applicant import config

    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    try:
        result = await asyncio.wait_for(run(), timeout=config.tool_timeout_seconds())
    except asyncio.TimeoutError:
        return ToolEnvelope(
            ok=False, capability=capability, status=ToolStatus.UNAVAILABLE,
            error=ToolErrorInfo(
                code="TOOL_TIMEOUT",
                message="The tool did not answer inside its time budget.",
                context={"timeout_seconds": config.tool_timeout_seconds()},
            ),
            processing_ms=elapsed(),
        )
    except ToolError as exc:
        return ToolEnvelope(
            ok=False, capability=capability, status=exc.status,
            error=ToolErrorInfo(code=exc.code, message=exc.message,
                                context=dict(exc.context)),
            processing_ms=elapsed(),
        )
    except ValidationError as exc:
        return ToolEnvelope(
            ok=False, capability=capability, status=ToolStatus.INVALID_INPUT,
            error=ToolErrorInfo(
                code="INVALID_INPUT",
                message="Request failed schema validation.",
                context={"errors": exc.errors(include_url=False)},
            ),
            processing_ms=elapsed(),
        )
    except Exception as exc:
        # The message, never the traceback. A stack trace crossing this
        # boundary would reach a model, and from there possibly a screen.
        logger.exception("Applicant capability failed: %s", capability)
        return ToolEnvelope(
            ok=False, capability=capability, status=ToolStatus.FAILED,
            error=ToolErrorInfo(
                code="CAPABILITY_FAILED",
                message=f"{capability} could not be completed.",
                context={"error_type": type(exc).__name__},
            ),
            processing_ms=elapsed(),
        )

    return ToolEnvelope(ok=True, capability=capability, status=ToolStatus.OK,
                        result=result, processing_ms=elapsed())


def _repo():
    """The configured repository. Never a concrete backend by name."""
    from app.store import RepositoryError, get_repository

    try:
        return get_repository()
    except RepositoryError as exc:
        raise Unavailable("CASE_STORE_UNAVAILABLE", str(exc)) from exc


def _require(value: str | None, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise InvalidInput(f"{field} is required.")
    if len(text) > 128:
        raise InvalidInput(f"{field} is too long.")
    return text


def _applicant_json(a) -> dict[str, Any]:
    return {
        "applicant_id": a.applicant_id,
        "full_name": a.full_name,
        "mobile": a.mobile,
        "email": a.email,
        "date_of_birth": a.date_of_birth,
        "address": a.address,
        "missing_fields": a.missing_fields(),
        "is_complete": a.is_complete(),
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def _application_json(app) -> dict[str, Any]:
    return {
        "case_id": app.case_id,
        "applicant_id": app.applicant_id,
        "status": app.status.value,
        "product": app.product,
        "loan_amount": app.loan_amount,
        "employment_type": app.employment_type,
        "missing_fields": app.missing_fields(),
        "created_at": app.created_at.isoformat(),
        "updated_at": app.updated_at.isoformat(),
    }


def _pin_policy(record) -> bool:
    """
    Record the policy version this case is being assessed under.

    ONCE. A pin that moved every time the file changed would record nothing
    -- the point of it is that an applicant told on Monday to bring three
    documents can be shown, later, which version of the rules said so.

    Returns whether anything was pinned.
    """
    from app.agents.policy import engine as policy
    from app.store.models import utcnow

    if record.policy_version:
        return False
    try:
        resolution = policy.resolve(
            record.product,
            loan_amount=record.loan_amount,
            attributes=record.policy_attributes(),
        )
    except Exception:  # pragma: no cover - configuration failure
        logger.exception("Could not resolve a policy to pin to %s",
                         record.case_id)
        return False

    record.policy_id = resolution.policy_id
    record.policy_version = resolution.policy_version
    record.policy_pinned_at = utcnow()
    return True


def _policy_json(app, resolution) -> dict[str, Any]:
    """
    Which policy produced this checklist, and whether it is still the one
    the case was opened under.

    THE PIN IS A RECORD, NOT A REPLAY. A case stores the policy version it
    was first assessed under so an audit can see it. This service resolves
    against the CURRENT file -- it does not keep old policy files and cannot
    re-run a superseded version. When the two differ that is stated in
    `version_changed` rather than implied by a version number that would
    otherwise read as the one in force.
    """
    block = dict(resolution.provenance())
    block["pinned_version"] = app.policy_version
    block["pinned_at"] = (app.policy_pinned_at.isoformat()
                          if app.policy_pinned_at else None)
    changed = bool(app.policy_version
                   and app.policy_version != resolution.policy_version)
    block["version_changed"] = changed
    if changed:
        block["note"] = (
            f"This case was opened under policy {app.policy_version}. The "
            f"checklist above was resolved under {resolution.policy_version}, "
            f"which is the version now in force."
        )
    return block


def _document_json(d) -> dict[str, Any]:
    return {
        "document_id": d.document_id,
        "source_id": d.source_id,
        "document_type": d.document_type,
        "status": d.status.value,
        "verification_status": d.verification_status,
        "reason_codes": list(d.reason_codes or []),
        "has_extracted_fields": bool(d.extracted_fields),
        "uploaded_at": d.uploaded_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
    }


# ==========================================================================
# READ CAPABILITIES
# ==========================================================================

async def applicant_get(applicant_id: str) -> ToolEnvelope:
    """One applicant's captured information."""

    async def run() -> dict[str, Any]:
        aid = _require(applicant_id, "applicant_id")
        record = _repo().get_applicant(aid)
        if record is None:
            raise NotFound(f"No applicant record for {aid}.", applicant_id=aid)
        return {"applicant": _applicant_json(record)}

    return await _envelope("applicant.get", run)


async def application_get(case_id: str) -> ToolEnvelope:
    """One application."""

    async def run() -> dict[str, Any]:
        cid = _require(case_id, "case_id")
        record = _repo().get_application(cid)
        if record is None:
            raise NotFound(f"No application for case {cid}.", case_id=cid)
        return {"application": _application_json(record)}

    return await _envelope("application.get", run)


async def applications_list(applicant_id: str) -> ToolEnvelope:
    """Every application belonging to one applicant."""

    async def run() -> dict[str, Any]:
        aid = _require(applicant_id, "applicant_id")
        records = _repo().list_applications(aid)
        return {"applications": [_application_json(a) for a in records],
                "count": len(records)}

    return await _envelope("applications.list", run)


async def documents_get(case_id: str) -> ToolEnvelope:
    """Every document attached to one case, with its stored verdict."""

    async def run() -> dict[str, Any]:
        cid = _require(case_id, "case_id")
        records = _repo().list_documents(cid)
        return {"documents": [_document_json(d) for d in records],
                "count": len(records)}

    return await _envelope("documents.get", run)


async def document_checklist(case_id: str) -> ToolEnvelope:
    """
    The required-document checklist, and what satisfies each slot.

    Missing documents are DERIVED here -- the checklist from configuration
    measured against what is stored -- rather than being a status anyone
    writes down.
    """

    async def run() -> dict[str, Any]:
        from app.agents.applicant import workflow

        cid = _require(case_id, "case_id")
        repo = _repo()
        application = repo.get_application(cid)
        if application is None:
            raise NotFound(f"No application for case {cid}.", case_id=cid)
        documents = repo.list_documents(cid)
        resolution = workflow.resolution_for(application)
        entries = workflow.build_checklist(application, documents, resolution)
        return {
            "checklist": entries,
            "missing": [e["slot"] for e in entries if e["status"] == "MISSING"],
            "satisfied": [e["slot"] for e in entries if e["status"] == "VERIFIED"],
            "product": application.product,
            "policy": _policy_json(application, resolution),
        }

    return await _envelope("documents.checklist", run)


async def document_verification_get(
    case_id: str,
    document_type: str,
) -> ToolEnvelope:
    """
    The stored verification verdict for one document type on one case.

    CONSUMES the Document Verification Agent's result. It does not re-run or
    re-derive verification; the verdict here is the one that agent reached.
    """

    async def run() -> dict[str, Any]:
        cid = _require(case_id, "case_id")
        wanted = _require(document_type, "document_type").upper()
        documents = _repo().list_documents(cid)
        matches = [d for d in documents
                   if (d.document_type or "").upper() == wanted]
        if not matches:
            return {
                "document_type": wanted,
                "found": False,
                "status": "MISSING",
                "detail": f"No {wanted} document has been uploaded for this case.",
            }
        latest = sorted(matches, key=lambda d: d.updated_at)[-1]
        return {
            "document_type": wanted,
            "found": True,
            **_document_json(latest),
        }

    return await _envelope("documents.verification", run)


async def pending_items_get(case_id: str) -> ToolEnvelope:
    """Everything outstanding at the FOS stage for this case."""

    async def run() -> dict[str, Any]:
        from app.agents.applicant import workflow

        cid = _require(case_id, "case_id")
        repo = _repo()
        application = repo.get_application(cid)
        if application is None:
            raise NotFound(f"No application for case {cid}.", case_id=cid)
        applicant = repo.get_applicant(application.applicant_id)
        documents = repo.list_documents(cid)
        items = workflow.pending_items(applicant, application, documents)
        return {"pending_items": items, "count": len(items)}

    return await _envelope("workflow.pending_items", run)


async def next_action_get(case_id: str) -> ToolEnvelope:
    """The single next thing the FOS should do on this case."""

    async def run() -> dict[str, Any]:
        from app.agents.applicant import workflow

        cid = _require(case_id, "case_id")
        repo = _repo()
        application = repo.get_application(cid)
        if application is None:
            raise NotFound(f"No application for case {cid}.", case_id=cid)
        applicant = repo.get_applicant(application.applicant_id)
        documents = repo.list_documents(cid)
        return {"next_action": workflow.next_action(applicant, application, documents)}

    return await _envelope("workflow.next_action", run)


async def readiness_get(case_id: str) -> ToolEnvelope:
    """Whether this case may be handed to CPA. Not a credit decision."""

    async def run() -> dict[str, Any]:
        from app.agents.applicant import workflow

        cid = _require(case_id, "case_id")
        repo = _repo()
        application = repo.get_application(cid)
        if application is None:
            raise NotFound(f"No application for case {cid}.", case_id=cid)
        applicant = repo.get_applicant(application.applicant_id)
        documents = repo.list_documents(cid)
        return {"readiness": workflow.readiness(applicant, application, documents)}

    return await _envelope("workflow.readiness", run)


async def applicant_360(case_id: str) -> ToolEnvelope:
    """
    The whole FOS-stage picture for one case, in one call.

    A convenience over the tools above, for the summary intents that would
    otherwise make six round trips. It composes them; it computes nothing of
    its own.
    """

    async def run() -> dict[str, Any]:
        from app.agents.applicant import workflow

        cid = _require(case_id, "case_id")
        repo = _repo()
        application = repo.get_application(cid)
        if application is None:
            raise NotFound(f"No application for case {cid}.", case_id=cid)
        applicant = repo.get_applicant(application.applicant_id)
        documents = repo.list_documents(cid)

        readiness_result = workflow.readiness(applicant, application, documents)
        # ONE RESOLUTION, used for both the checklist and the block that
        # explains it. Resolving twice would let the two disagree if a
        # policy file were edited between the calls.
        resolution = workflow.resolution_for(application)
        return {
            "applicant": _applicant_json(applicant) if applicant else None,
            "application": _application_json(application),
            "stage": workflow.derived_stage(application, documents, readiness_result),
            "documents": [_document_json(d) for d in documents],
            "document_summary": workflow.document_summary(documents),
            "checklist": workflow.build_checklist(application, documents,
                                                  resolution),
            "policy": _policy_json(application, resolution),
            "pending_items": workflow.pending_items(applicant, application, documents),
            "next_action": workflow.next_action(applicant, application, documents),
            "readiness": readiness_result,
        }

    return await _envelope("applicant.360", run)


# ==========================================================================
# WRITE CAPABILITIES
#
# Separated from the reads above, because the permission layer treats them
# differently and a reviewer should be able to see every write in one place.
# ==========================================================================

async def applicant_create(
    full_name: str | None = None,
    mobile: str | None = None,
    email: str | None = None,
    date_of_birth: str | None = None,
    address: str | None = None,
    applicant_id: str | None = None,
) -> ToolEnvelope:
    """Create an applicant. The identifier is generated unless supplied."""

    async def run() -> dict[str, Any]:
        from app.store.models import Applicant

        aid = (applicant_id or "").strip() or f"APP-{uuid.uuid4().hex[:12].upper()}"
        repo = _repo()
        if repo.get_applicant(aid) is not None:
            raise InvalidInput(f"Applicant {aid} already exists.", applicant_id=aid)

        record = Applicant(
            applicant_id=aid,
            full_name=(full_name or None),
            mobile=(mobile or None),
            email=(email or None),
            date_of_birth=(date_of_birth or None),
            address=(address or None),
        )
        repo.save_applicant(record)
        return {"applicant": _applicant_json(record), "created": True}

    return await _envelope("applicant.create", run)


async def applicant_update(
    applicant_id: str,
    full_name: str | None = None,
    mobile: str | None = None,
    email: str | None = None,
    date_of_birth: str | None = None,
    address: str | None = None,
) -> ToolEnvelope:
    """Update basic applicant information. Only supplied fields change."""

    async def run() -> dict[str, Any]:
        aid = _require(applicant_id, "applicant_id")
        repo = _repo()
        record = repo.get_applicant(aid)
        if record is None:
            raise NotFound(f"No applicant record for {aid}.", applicant_id=aid)

        changed: list[str] = []
        for name, value in (("full_name", full_name), ("mobile", mobile),
                            ("email", email), ("date_of_birth", date_of_birth),
                            ("address", address)):
            if value is not None and str(value).strip():
                setattr(record, name, str(value).strip())
                changed.append(name)

        if not changed:
            raise InvalidInput("No fields were supplied to update.")

        repo.save_applicant(record)
        return {"applicant": _applicant_json(record), "updated_fields": changed}

    return await _envelope("applicant.update", run)


async def application_create(
    applicant_id: str,
    product: str | None = None,
    loan_amount: str | None = None,
    case_id: str | None = None,
    employment_type: str | None = None,
) -> ToolEnvelope:
    """Create an application for an existing applicant."""

    async def run() -> dict[str, Any]:
        from app.store.models import Application

        aid = _require(applicant_id, "applicant_id")
        repo = _repo()
        if repo.get_applicant(aid) is None:
            raise NotFound(
                f"No applicant record for {aid}. Create the applicant first.",
                applicant_id=aid,
            )

        cid = (case_id or "").strip() or f"CASE-{uuid.uuid4().hex[:12].upper()}"
        if repo.get_application(cid) is not None:
            raise InvalidInput(f"Application {cid} already exists.", case_id=cid)

        record = Application(
            case_id=cid, applicant_id=aid,
            product=(product or None), loan_amount=(loan_amount or None),
            employment_type=((employment_type or "").strip().upper() or None),
        )
        _pin_policy(record)
        repo.save_application(record)
        return {"application": _application_json(record), "created": True}

    return await _envelope("application.create", run)


async def application_update(
    case_id: str,
    product: str | None = None,
    loan_amount: str | None = None,
    employment_type: str | None = None,
) -> ToolEnvelope:
    """Update basic application information."""

    async def run() -> dict[str, Any]:
        cid = _require(case_id, "case_id")
        repo = _repo()
        record = repo.get_application(cid)
        if record is None:
            raise NotFound(f"No application for case {cid}.", case_id=cid)

        changed: list[str] = []
        if product is not None and str(product).strip():
            record.product = str(product).strip().upper()
            changed.append("product")
        if loan_amount is not None and str(loan_amount).strip():
            record.loan_amount = str(loan_amount).strip()
            changed.append("loan_amount")
        if employment_type is not None and str(employment_type).strip():
            record.employment_type = str(employment_type).strip().upper()
            changed.append("employment_type")

        # A case opened before its product was chosen has nothing to pin to.
        # Pin now, on the first update that gives it a product.
        if _pin_policy(record):
            changed.append("policy_version")

        if not changed:
            raise InvalidInput("No fields were supplied to update.")

        repo.save_application(record)
        return {"application": _application_json(record), "updated_fields": changed}

    return await _envelope("application.update", run)


async def document_mark_for_reupload(
    case_id: str,
    document_type: str,
) -> ToolEnvelope:
    """
    Mark a document as needing re-upload.

    Sets the stored status to REJECTED with an explicit reason code, so the
    checklist and the next action both reflect the FOS's decision. It does not
    touch the verification verdict the pipeline reached -- that stays as the
    record of what verification concluded.
    """

    async def run() -> dict[str, Any]:
        from app.store.models import DocumentStatus

        cid = _require(case_id, "case_id")
        wanted = _require(document_type, "document_type").upper()
        repo = _repo()
        matches = [d for d in repo.list_documents(cid)
                   if (d.document_type or "").upper() == wanted]
        if not matches:
            raise NotFound(
                f"No {wanted} document on case {cid}.",
                case_id=cid, document_type=wanted,
            )

        latest = sorted(matches, key=lambda d: d.updated_at)[-1]
        latest.status = DocumentStatus.REJECTED
        codes = list(latest.reason_codes or [])
        if "FOS_REQUESTED_REUPLOAD" not in codes:
            codes.append("FOS_REQUESTED_REUPLOAD")
        latest.reason_codes = codes
        repo.save_document(latest)
        return {"document": _document_json(latest), "marked": True}

    return await _envelope("documents.mark_for_reupload", run)


#: capability name -> coroutine. The registry the planner selects from; a tool
#: not in here cannot be called, whatever a model asks for.
READ_TOOLS = {
    "applicant.get": applicant_get,
    "application.get": application_get,
    "applications.list": applications_list,
    "documents.get": documents_get,
    "documents.checklist": document_checklist,
    "documents.verification": document_verification_get,
    "workflow.pending_items": pending_items_get,
    "workflow.next_action": next_action_get,
    "workflow.readiness": readiness_get,
    "applicant.360": applicant_360,
}

WRITE_TOOLS = {
    "applicant.create": applicant_create,
    "applicant.update": applicant_update,
    "application.create": application_create,
    "application.update": application_update,
    "documents.mark_for_reupload": document_mark_for_reupload,
}

ALL_TOOLS = {**READ_TOOLS, **WRITE_TOOLS}


__all__ = ["ALL_TOOLS", "READ_TOOLS", "WRITE_TOOLS"] + sorted(ALL_TOOLS)
