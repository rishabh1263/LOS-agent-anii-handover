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
        # THE STAGE THE CASE RECORD ESTABLISHES. Every consumer of this
        # resolution -- checklist, pending items, next action, readiness --
        # becomes stage-aware here, in one place. A FOS case resolves
        # exactly as before.
        stage=_current_stage(application),
    )


def _current_stage(application: Application | None) -> str | None:
    """The case's authoritative stage (stages.resolve), or None."""
    if application is None or not application.case_id:
        return None
    try:
        from app.agents.los import stages

        context = stages.resolve(application.case_id)
    except Exception:
        return None
    return context.stage.value if context.stage is not None else None


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
    # Which LOS stage's rules added this slot; absent for the product
    # policy's own (FOS) slots, so a FOS checklist row is unchanged.
    if getattr(requirement, "stage", None):
        row["stage"] = requirement.stage

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
    *,
    resolution=None,
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

    if coapp_mandatory_enabled():
        # PER PARTY (LOS_COAPP_MANDATORY_DOCS): the applicant's slots from the
        # applicant's own documents, and every co-applicant's from theirs
        for party in party_checklists(application, documents, resolution):
            items.extend(_document_items(party["checklist"], rules, party["party_role"], party["party_id"]))
    else:
        items.extend(_document_items(build_checklist(application, documents, resolution), rules))
    items.extend(signature_items(application, documents))
    return items


def _document_items(checklist: list[dict[str, Any]], rules: dict[str, Any], party_role: str | None = None,
                    party_id: str | None = None) -> list[dict[str, Any]]:
    """The pending document items of one checklist. A co-applicant's are named as theirs."""
    items: list[dict[str, Any]] = []
    co = party_role == "CO_APPLICANT"
    whose = "Co-applicant's " if co else ""
    party = {"party_role": party_role, "party_id": party_id} if co else {}
    for entry in checklist:
        # An optional slot never blocks the handoff. It is still reported in
        # the checklist so a FOS can see what has been collected beyond the
        # minimum, but its absence is not a pending item.
        if not entry.get("mandatory", True):
            continue

        if entry["status"] == "MISSING" and rules["require_all_documents"]:
            items.append({
                "type": "DOCUMENT",
                "code": "DOCUMENT_MISSING",
                "detail": f"{whose}{_readable(entry['slot'])} has not been uploaded.",
                "slot": entry["slot"],
                "accepts": entry["accepts"],
                **party,
            })
        elif entry["status"] == DocumentStatus.REJECTED.value:
            items.append({
                "type": "DOCUMENT",
                "code": "DOCUMENT_REJECTED",
                "detail": f"{whose}{_readable(entry['slot'])} was rejected and must be re-uploaded.",
                "slot": entry["slot"],
                "document_type": entry["document_type"],
                "reason_codes": entry["reason_codes"],
                **party,
            })
        elif entry["status"] == DocumentStatus.REVIEW.value and rules["block_on_review"]:
            items.append({
                "type": "DOCUMENT",
                "code": "DOCUMENT_UNDER_REVIEW",
                "detail": f"{whose}{_readable(entry['slot'])} is under review.",
                "slot": entry["slot"],
                "document_type": entry["document_type"],
                "reason_codes": entry["reason_codes"],
                **party,
            })
        elif entry["status"] in {DocumentStatus.UPLOADED.value,
                                 DocumentStatus.PROCESSING.value}:
            if rules["require_documents_verified"]:
                items.append({
                    "type": "DOCUMENT",
                    "code": "DOCUMENT_NOT_VERIFIED",
                    "detail": f"{whose}{_readable(entry['slot'])} has not completed verification.",
                    "slot": entry["slot"],
                    "document_type": entry["document_type"],
                    **party,
                })

    return items


# ==========================================================================
# CO-APPLICANT MANDATORY DOCUMENTS (Phase 3 step 5b; LOS_COAPP_MANDATORY_DOCS, default off)
# ==========================================================================
#
# A co-applicant's mandatory documents are ONLY the configured slots
# (applicant_agent.yaml readiness.co_applicant_documents): PAN, address proof
# (any one of Aadhaar / passport / licence / voter ID / utility bill) and
# employment proof (salaried or self-employed documents); no bank statement --
# verified by the existing OCR + verification + KYC flow (no Aadhaar API).
# Each party's slots are matched against THAT party's documents only: with the
# flag off, one checklist is matched against every document on the case, so a
# co-applicant's PAN could satisfy the applicant's PAN slot.

COAPP_FLAG = "LOS_COAPP_MANDATORY_DOCS"


def coapp_mandatory_enabled() -> bool:
    import os

    return (os.getenv(COAPP_FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _party_role(document: Document) -> str:
    return str(getattr(document, "party_role", None) or "PRIMARY_APPLICANT").upper()


def co_applicant_parties(application: Application | None, documents: list[Document]) -> list[str]:
    """Every co-applicant on the case: the one the application names, and any with a document."""
    parties: list[str] = []
    if application is not None and getattr(application, "co_applicant_id", None):
        parties.append(application.co_applicant_id)
    for document in documents:
        if _party_role(document) == "CO_APPLICANT" and document.party_id and document.party_id not in parties:
            parties.append(document.party_id)
    return parties


def co_applicant_requirements(resolution=None) -> list:
    """A co-applicant's mandatory slots, from configuration -- never the applicant's full checklist."""
    from app.agents.policy.engine import REQUIRED, Requirement

    return [Requirement(slot=entry["slot"], accepts=tuple(entry["accepts"]), requirement=REQUIRED,
                        rule_ids=("COAPP_MANDATORY_DOCS",), reason="Mandatory for every co-applicant.")
            for entry in config.co_applicant_documents()]


def party_checklists(application: Application | None, documents: list[Document],
                     resolution=None) -> list[dict[str, Any]]:
    """[{party_id, party_role, checklist}] -- the applicant first, then each co-applicant."""
    resolution = resolution if resolution is not None else resolution_for(application)
    primary = [d for d in documents if _party_role(d) != "CO_APPLICANT"]
    out = [{"party_id": getattr(application, "applicant_id", None), "party_role": "PRIMARY_APPLICANT",
            "checklist": build_checklist(application, primary, resolution)}]
    requirements = co_applicant_requirements(resolution)
    for party_id in co_applicant_parties(application, documents):
        by_type: dict[str, list[Document]] = {}
        for document in documents:
            if document.party_id == party_id and _party_role(document) == "CO_APPLICANT":
                by_type.setdefault((document.document_type or "").upper(), []).append(document)
        out.append({"party_id": party_id, "party_role": "CO_APPLICANT",
                    "checklist": [_slot(r, by_type, resolution) for r in requirements]})
    return out


# --------------------------------------------------------------------------
# MANDATORY SIGNATURE (step 5c, LOS_SIGNATURE_MANDATORY, default off)
# --------------------------------------------------------------------------
# The applicant and every co-applicant need a signature whose PRESENCE check
# (signature/presence.py: blank / handwritten) is VERIFIED. Read from the
# stored reason codes; the authenticity verdict (document status) is not used.
# Only for cases created on or after readiness.signature_mandatory.activation_date.

SIGNATURE_TYPES = frozenset({"SIGNATURE", "STANDALONE_SIGNATURE", "BANK_SIGNATURE"})
_INACTIVE = frozenset({"SUPERSEDED"})


def signature_rule_applies(application: Application | None) -> tuple[bool, str | None]:
    """(applies, misconfiguration) for this case -- False for a case created before activation."""
    from app.agents.signature import presence

    if application is None or not presence.enabled():
        return False, None
    activation, problem = config.signature_activation()
    if activation is None:
        return True, problem
    created = getattr(application, "created_at", None)
    if created is None:
        return True, None                       # no creation date: cannot prove it is older
    if created.tzinfo is None:
        from datetime import timezone

        created = created.replace(tzinfo=timezone.utc)
    return created >= activation, None


def signature_rule_config_error() -> str | None:
    """Why the mandatory signature rule cannot run, while its flag is on (startup and /ready)."""
    from app.agents.signature import presence

    if not presence.enabled():
        return None
    _, problem = config.signature_activation()
    return f"{presence.FLAG} is on but {problem}" if problem else None


def signature_parties(application: Application, documents: list[Document]) -> list[tuple[str | None, str]]:
    """[(party_id, party_role)]: the applicant first, then every co-applicant."""
    return [(application.applicant_id, "PRIMARY_APPLICANT"),
            *[(p, "CO_APPLICANT") for p in co_applicant_parties(application, documents)]]


def _party_signature(documents: list[Document], party_id: str | None, role: str, applicant_id: str):
    """The party's latest active signature document, or None."""
    mine = [d for d in documents
            if (d.document_type or "").upper() in SIGNATURE_TYPES
            and str(getattr(d.status, "value", d.status)).upper() not in _INACTIVE
            and _party_role(d) == role
            and (role != "CO_APPLICANT" or d.party_id == party_id)
            and (role == "CO_APPLICANT" or (d.party_id or applicant_id) == applicant_id)]
    return max(mine, key=lambda d: d.uploaded_at) if mine else None


def signature_items(application: Application | None, documents: list[Document]) -> list[dict[str, Any]]:
    """Blocking items for missing / rejected / unsure signatures; [] when the rule does not apply."""
    from app.agents.signature import presence

    applies, problem = signature_rule_applies(application)
    if not applies:
        return []
    if problem:
        # FAIL CLOSED: the flag is on but nobody can tell which cases it covers
        return [{"type": "CONFIGURATION", "code": "SIGNATURE_RULE_MISCONFIGURED",
                 "detail": f"Mandatory signature rule is on but {problem}."}]
    return party_signature_items(application, documents)


def party_signature_items(application: Application, documents: list[Document]) -> list[dict[str, Any]]:
    """The rule itself, with no flag or date gate (the grandfathered-cases report reads it too)."""
    from app.agents.signature import presence

    items: list[dict[str, Any]] = []
    for party_id, role in signature_parties(application, documents):
        co = role == "CO_APPLICANT"
        whose = "Co-applicant's signature" if co else "Signature"
        base = {"type": "DOCUMENT", "slot": "SIGNATURE", "accepts": ["SIGNATURE"],
                **({"party_role": role, "party_id": party_id} if co else {})}
        document = _party_signature(documents, party_id, role, application.applicant_id)
        if document is None:
            items.append({**base, "code": "DOCUMENT_MISSING", "detail": f"{whose} has not been uploaded."})
            continue
        result = presence.from_codes(document.reason_codes)
        if result is None:
            # uploaded while the check was off: unsure, so never approved
            items.append({**base, "code": "SIGNATURE_NOT_CHECKED", "document_id": document.document_id,
                          "detail": f"{whose} has not been checked yet; please upload it again."})
        elif result["status"] == presence.REJECTED:
            why = "; ".join(result["messages"]) or "Signature was rejected"
            items.append({**base, "code": "SIGNATURE_REJECTED", "document_id": document.document_id,
                          "reasons": result["messages"], "detail": f"{whose} was rejected: {why}."})
        elif result["status"] == presence.REVIEW:
            items.append({**base, "code": "SIGNATURE_UNDER_REVIEW", "document_id": document.document_id,
                          "reasons": result["messages"], "detail": f"{whose} is under review."})
    return items


def _readable(slot: str) -> str:
    # "PAN", not "Pan": the answer module's rendering keeps acronyms.
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(slot)


# ==========================================================================
# READINESS
# ==========================================================================


def readiness(
    applicant: Applicant | None,
    application: Application | None,
    documents: list[Document],
    *,
    resolution=None,
) -> dict[str, Any]:
    """
    Whether this case may be handed to CPA.

    THIS IS NOT A CREDIT DECISION. It answers whether the FOS has collected
    enough for the next desk to start work, and nothing about whether the loan
    should be approved.

    A rule switched off in configuration is REPORTED, not silently skipped, so
    a READY verdict can always be read alongside what was actually checked.
    """
    items = pending_items(applicant, application, documents, resolution=resolution)
    blocking = [i for i in items if i["type"] != "INFO"]
    rules = config.readiness_rules()

    return {
        "status": "READY_FOR_CPA" if not blocking else "NOT_READY",
        "blocking_items": [
            # the slot a document item is about travels with it, so a caller
            # can name it in any language without parsing `detail`
            {"type": i["type"], "code": i["code"], "detail": i["detail"],
             **({"slot": i["slot"]} if i.get("slot") else {})}
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
    *,
    resolution=None,
) -> dict[str, Any]:
    """
    The single next thing the FOS should do.

    One action, not a list: a FOS working a queue needs to know what to do
    now. Everything else is in pending_items.
    """
    items = pending_items(applicant, application, documents, resolution=resolution)

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
