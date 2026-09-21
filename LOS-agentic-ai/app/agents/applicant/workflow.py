"""
The deterministic half of the Applicant Agent.

EVERY BUSINESS ANSWER IS COMPUTED HERE. The document checklist, what is
missing, what the FOS should do next, and whether the case may be handed to
CPA are all decided by this module from stored records and configuration.

The language model never reaches any of it. It is handed the results and asked
to phrase them, exactly as the LOS summary works, and for the same reason: a
readiness verdict a model could influence is a readiness verdict nobody can
audit.

Nothing here writes. It reads records and returns findings.
"""

from __future__ import annotations

from typing import Any

from app.agents.applicant import config
from app.agents.policy import engine as policy
from app.store.models import (
    ACTIONABLE_STATUSES,
    Applicant,
    Application,
    ApplicationStatus,
    Document,
    DocumentStatus,
)

# ==========================================================================
# CHECKLIST
# ==========================================================================


#: How a stored document status reads as a CHECKLIST state.
#:
#: TWO AXES, NOT ONE. A checklist row answers two separate questions -- how
#: strongly the case needs this slot (`requirement`: REQUIRED, CONDITIONAL,
#: OPTIONAL, NOT_APPLICABLE) and how far it has got (`fulfilment`). Folding
#: them into a single field forces a choice between displaying "REQUIRED" and
#: "SATISFIED" for a row that is both, and a frontend then cannot render a
#: required-and-still-missing slot differently from an optional one.
#:
#: `status` stays exactly as it was: the stored document status, or MISSING.
#: Existing callers read it and nothing here moves it.
SATISFIED = "SATISFIED"
MISSING = "MISSING"
UNDER_REVIEW = "UNDER_REVIEW"
FAILED = "FAILED"
IN_PROGRESS = "IN_PROGRESS"

_FULFILMENT = {
    DocumentStatus.VERIFIED: SATISFIED,
    DocumentStatus.REVIEW: UNDER_REVIEW,
    DocumentStatus.REJECTED: FAILED,
    # Collected, not yet concluded. Neither satisfied nor missing, and
    # calling it either would misreport the case: "satisfied" invites a
    # handoff that verification has not cleared, "missing" sends a field
    # officer back for a document the customer already handed over.
    DocumentStatus.PROCESSING: IN_PROGRESS,
    DocumentStatus.UPLOADED: IN_PROGRESS,
}


def resolution_for(application: Application | None):
    """
    The policy resolution behind this case's checklist.

    Separate from `build_checklist` because the resolution carries things a
    checklist row cannot: which rules fired, which could not be evaluated,
    and the policy version the whole answer came from.
    """
    return policy.resolve(
        application.product if application else None,
        loan_amount=application.loan_amount if application else None,
        attributes=(application.policy_attributes() if application else {}),
    )


def build_checklist(
    application: Application | None,
    documents: list[Document],
    resolution=None,
) -> list[dict[str, Any]]:
    """
    The required-document checklist for this case, with what satisfies each.

    One entry per slot. A slot is either a single document type or a group
    where one of several types will do -- address proof being the usual
    case, where a licence and a passport are equally good evidence.

    WHERE THE SLOTS COME FROM. The policy engine, which resolves them from
    the product, the loan amount and the case's own attributes. A product
    with no policy file resolves through the agent checklist exactly as it
    did before the engine existed, so this is a widening, not a change of
    answer.
    """
    resolution = resolution if resolution is not None else resolution_for(application)

    by_type: dict[str, list[Document]] = {}
    for document in documents:
        by_type.setdefault((document.document_type or "").upper(), []).append(document)

    return [_slot(requirement, by_type, resolution)
            for requirement in resolution.requirements]


def _slot(
    requirement,
    by_type: dict[str, list[Document]],
    resolution=None,
) -> dict[str, Any]:
    """One checklist row: what satisfies it, and what state it is actually in."""
    accepts = list(requirement.accepts)
    row: dict[str, Any] = {
        "slot": requirement.slot,
        "accepts": accepts,
        "mandatory": requirement.mandatory,
        # -- why the case needs it, straight from the rule that said so
        "requirement": requirement.requirement,
        "rule_ids": list(requirement.rule_ids),
        "reason": requirement.reason,
        "policy_status": requirement.policy_status,
    }
    if requirement.applicable_conditions:
        row["applicable_conditions"] = list(requirement.applicable_conditions)

    # WHAT HAS TO BE READABLE ON THE DOCUMENT, per accepted type.
    #
    # Published, not enforced here. Verification decides whether a
    # document is any good and this module does not second-guess it; what
    # this adds is that a field officer can be told "a bank statement
    # needs to show its period" BEFORE they collect one, rather than
    # finding out from a rejection afterwards.
    #
    # Sourced from the policy file, so a lender changing what it wants to
    # see does not require a release.
    if resolution is not None:
        content = {
            accepted: resolution.evidence_for(accepted)
            for accepted in accepts
            if resolution.evidence_for(accepted)
        }
        if content:
            row["content_requirements"] = content

    matched: list[Document] = []
    for accepted in accepts:
        matched.extend(by_type.get(accepted, []))

    if not matched:
        row.update({
            "status": MISSING,
            "fulfilment": MISSING,
            "document_type": None,
            "document_id": None,
            "reason_codes": [],
        })
        return row

    # The best one wins. A rejected first attempt followed by a verified
    # re-upload is a satisfied slot, not a blocked one.
    ranking = {
        DocumentStatus.VERIFIED: 0,
        DocumentStatus.PROCESSING: 1,
        DocumentStatus.UPLOADED: 2,
        DocumentStatus.REVIEW: 3,
        DocumentStatus.REJECTED: 4,
    }
    best = sorted(matched, key=lambda d: ranking.get(d.status, 9))[0]

    row.update({
        "status": best.status.value,
        "fulfilment": _FULFILMENT.get(best.status, IN_PROGRESS),
        "document_type": best.document_type,
        "document_id": best.document_id,
        "reason_codes": list(best.reason_codes or []),
    })
    return row


# ==========================================================================
# PENDING ITEMS
# ==========================================================================


def pending_items(
    applicant: Applicant | None,
    application: Application | None,
    documents: list[Document],
) -> list[dict[str, Any]]:
    """
    Everything standing between this case and the CPA handoff.

    Ordered the way a FOS would work through them: information first, because
    a document collected against a half-captured applicant often has to be
    collected again.
    """
    items: list[dict[str, Any]] = []
    rules = config.readiness_rules()

    if applicant is None:
        items.append({
            "type": "APPLICANT",
            "code": "APPLICANT_NOT_FOUND",
            "detail": "No applicant record exists.",
        })
        return items

    if rules["require_applicant_fields"]:
        for field_name in applicant.missing_fields():
            items.append({
                "type": "APPLICANT_INFORMATION",
                "code": f"MISSING_{field_name.upper()}",
                "detail": f"Applicant {field_name.replace('_', ' ')} is not captured.",
            })

    if application is None:
        items.append({
            "type": "APPLICATION",
            "code": "APPLICATION_NOT_FOUND",
            "detail": "No application has been created for this applicant.",
        })
        return items

    if rules["require_application_fields"]:
        for field_name in application.missing_fields():
            items.append({
                "type": "APPLICATION_INFORMATION",
                "code": f"MISSING_{field_name.upper()}",
                "detail": f"Application {field_name.replace('_', ' ')} is not set.",
            })

    for entry in build_checklist(application, documents):
        # An optional slot never blocks the handoff. It is still reported in
        # the checklist so a FOS can see what has been collected beyond the
        # minimum, but its absence is not a pending item.
        if not entry.get("mandatory", True):
            continue

        if entry["status"] == "MISSING" and rules["require_all_documents"]:
            items.append({
                "type": "DOCUMENT",
                "code": "DOCUMENT_MISSING",
                "detail": f"{_readable(entry['slot'])} has not been uploaded.",
                "slot": entry["slot"],
                "accepts": entry["accepts"],
            })
        elif entry["status"] == DocumentStatus.REJECTED.value:
            items.append({
                "type": "DOCUMENT",
                "code": "DOCUMENT_REJECTED",
                "detail": f"{_readable(entry['slot'])} was rejected and must be re-uploaded.",
                "slot": entry["slot"],
                "document_type": entry["document_type"],
                "reason_codes": entry["reason_codes"],
            })
        elif entry["status"] == DocumentStatus.REVIEW.value and rules["block_on_review"]:
            items.append({
                "type": "DOCUMENT",
                "code": "DOCUMENT_UNDER_REVIEW",
                "detail": f"{_readable(entry['slot'])} is under review.",
                "slot": entry["slot"],
                "document_type": entry["document_type"],
                "reason_codes": entry["reason_codes"],
            })
        elif entry["status"] in {DocumentStatus.UPLOADED.value,
                                 DocumentStatus.PROCESSING.value}:
            if rules["require_documents_verified"]:
                items.append({
                    "type": "DOCUMENT",
                    "code": "DOCUMENT_NOT_VERIFIED",
                    "detail": f"{_readable(entry['slot'])} has not completed verification.",
                    "slot": entry["slot"],
                    "document_type": entry["document_type"],
                })

    return items


def _readable(slot: str) -> str:
    return slot.replace("_", " ").title()


# ==========================================================================
# READINESS
# ==========================================================================


def readiness(
    applicant: Applicant | None,
    application: Application | None,
    documents: list[Document],
) -> dict[str, Any]:
    """
    Whether this case may be handed to CPA.

    THIS IS NOT A CREDIT DECISION. It answers whether the FOS has collected
    enough for the next desk to start work, and nothing about whether the loan
    should be approved.

    A rule switched off in configuration is REPORTED, not silently skipped, so
    a READY verdict can always be read alongside what was actually checked.
    """
    items = pending_items(applicant, application, documents)
    blocking = [i for i in items if i["type"] != "INFO"]
    rules = config.readiness_rules()

    return {
        "status": "READY_FOR_CPA" if not blocking else "NOT_READY",
        "blocking_items": [
            {"type": i["type"], "code": i["code"], "detail": i["detail"]}
            for i in blocking
        ],
        "blocking_count": len(blocking),
        "rules_applied": rules,
    }


# ==========================================================================
# NEXT ACTION
# ==========================================================================

#: Pending-item code -> what the FOS should do about it. Ordered by priority:
#: the first match in this list is the next action.
_ACTIONS: list[tuple[str, str, str]] = [
    ("APPLICANT_NOT_FOUND", "CREATE_APPLICANT",
     "Create the applicant record."),
    ("APPLICATION_NOT_FOUND", "CREATE_APPLICATION",
     "Create an application for this applicant."),
    ("MISSING_FULL_NAME", "CAPTURE_APPLICANT_INFORMATION",
     "Capture the applicant's full name."),
    ("MISSING_MOBILE", "CAPTURE_APPLICANT_INFORMATION",
     "Capture the applicant's mobile number."),
    ("MISSING_DATE_OF_BIRTH", "CAPTURE_APPLICANT_INFORMATION",
     "Capture the applicant's date of birth."),
    ("MISSING_ADDRESS", "CAPTURE_APPLICANT_INFORMATION",
     "Capture the applicant's address."),
    ("MISSING_PRODUCT", "CAPTURE_APPLICATION_INFORMATION",
     "Select the loan product for this application."),
    ("DOCUMENT_REJECTED", "REQUEST_CORRECT_DOCUMENT",
     "Collect a replacement for the rejected document."),
    ("DOCUMENT_MISSING", "COLLECT_DOCUMENT",
     "Collect and upload the missing document."),
    ("DOCUMENT_UNDER_REVIEW", "RESOLVE_DOCUMENT_REVIEW",
     "Resolve the document currently under review."),
    ("DOCUMENT_NOT_VERIFIED", "AWAIT_VERIFICATION",
     "Wait for document verification to complete."),
]


def next_action(
    applicant: Applicant | None,
    application: Application | None,
    documents: list[Document],
) -> dict[str, Any]:
    """
    The single next thing the FOS should do.

    One action, not a list: a FOS working a queue needs to know what to do
    now. Everything else is in pending_items.
    """
    items = pending_items(applicant, application, documents)

    if not items:
        return {
            "action": "SUBMIT_TO_CPA",
            "detail": "Everything required at the FOS stage is complete. "
                      "Hand the case to CPA.",
            "target": None,
            "reason_codes": [],
        }

    # FIRST match per code, not last.
    #
    # This was `{i["code"]: i for i in items}`, which collapses every item
    # sharing a code and keeps whichever came last. With PAN and BANK_STATEMENT
    # and ADDRESS_PROOF all missing -- three DOCUMENT_MISSING items -- the FOS
    # was told to collect the address proof, because it happened to be last in
    # the checklist. Pending items are already in the order the FOS should work
    # through them, so the first one is the answer.
    by_code: dict[str, dict[str, Any]] = {}
    for item in items:
        by_code.setdefault(item["code"], item)

    for code, action, detail in _ACTIONS:
        item = by_code.get(code)
        if item is None:
            continue
        target = item.get("slot") or item.get("document_type")
        if target and code in {"DOCUMENT_MISSING", "DOCUMENT_REJECTED",
                               "DOCUMENT_UNDER_REVIEW", "DOCUMENT_NOT_VERIFIED"}:
            detail = f"{detail.rstrip('.')}: {_readable(str(target))}."
        return {
            "action": action,
            "detail": detail,
            "target": target,
            "reason_codes": list(item.get("reason_codes") or []),
        }

    # A pending item with no mapped action. Reported rather than swallowed,
    # because the mapping above is the thing that needs updating.
    first = items[0]
    return {
        "action": "MANUAL_REVIEW",
        "detail": first.get("detail") or "This case needs manual attention.",
        "target": None,
        "reason_codes": [first.get("code")],
    }


# ==========================================================================
# STAGE
# ==========================================================================


def derived_stage(
    application: Application | None,
    documents: list[Document],
    readiness_result: dict[str, Any],
) -> str:
    """
    Where the case actually is, computed from the records.

    Derived rather than trusted from the stored status, so a case cannot sit
    in DOCUMENT_COLLECTION after every document has been verified simply
    because nothing wrote the transition.
    """
    if application is None:
        return ApplicationStatus.APPLICATION_CREATED.value

    if readiness_result.get("status") == "READY_FOR_CPA":
        return ApplicationStatus.READY_FOR_CPA.value

    if documents and any(
        d.status in {DocumentStatus.VERIFIED, DocumentStatus.REVIEW,
                     DocumentStatus.REJECTED}
        for d in documents
    ):
        return ApplicationStatus.BASIC_DOCUMENT_VERIFICATION.value

    if documents:
        return ApplicationStatus.DOCUMENT_COLLECTION.value

    return ApplicationStatus.APPLICATION_CREATED.value


def document_summary(documents: list[Document]) -> dict[str, Any]:
    """Counts by status, for the 360 view and the copilot's opening line."""
    counts: dict[str, int] = {}
    for document in documents:
        counts[document.status.value] = counts.get(document.status.value, 0) + 1
    return {
        "total": len(documents),
        "by_status": counts,
        "needs_attention": [
            d.document_type for d in documents if d.status in ACTIONABLE_STATUSES
        ],
    }


__all__ = [
    "build_checklist", "derived_stage", "document_summary", "next_action",
    "pending_items", "readiness",
]
