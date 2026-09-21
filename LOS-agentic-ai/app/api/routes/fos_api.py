"""
The FOS integration surface: two endpoints, one response shape.

    POST /api/v1/fos/applicants   open a case
    POST /api/v1/fos/copilot      everything else

A field-officer frontend should be able to integrate from these two and the
action list, without reading anything about agents, MCP, repositories or
orchestration. That is the whole point of the consolidation.

THIN ADAPTERS. Nothing here decides anything. Intent classification,
permissions, the document checklist, pending items, the next action, the
readiness gate, output validation and audit all stay where they were; this
module translates one public contract onto them. The existing
/api/v1/applicant-agent/* routes still work and are marked deprecated.

ONE RESPONSE SHAPE for every action. Fields not relevant to an action come
back null or empty rather than absent, so a frontend renders one model
instead of eleven.
"""

from __future__ import annotations

import logging
import uuid
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.agents.applicant import audit, config, permissions
from app.agents.applicant.agent import AgentError, answer_question
from app.agents.applicant.intents import Intent
from app.agents.applicant.permissions import Caller, PermissionDenied
from app.agents.applicant import followup
from app.agents.applicant.query_types import QueryType as _QueryType
from app.security.auth import require_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/fos", tags=["FOS"])


# ==========================================================================
# ACTIONS
# ==========================================================================

class FosAction(str, Enum):
    """
    Everything the copilot endpoint can be asked to do.

    A frontend renders its dropdown from this list -- served by
    GET /api/v1/fos/actions with labels -- rather than hardcoding it, so
    adding an action does not require a frontend release.
    """

    GET_APPLICANT = "GET_APPLICANT"
    GET_APPLICATION_STATUS = "GET_APPLICATION_STATUS"
    GET_DOCUMENTS = "GET_DOCUMENTS"
    GET_DOCUMENT_CHECKLIST = "GET_DOCUMENT_CHECKLIST"
    GET_VERIFICATION_STATUS = "GET_VERIFICATION_STATUS"
    GET_PENDING_ITEMS = "GET_PENDING_ITEMS"
    GET_NEXT_ACTION = "GET_NEXT_ACTION"
    GET_CASE_360 = "GET_CASE_360"
    CHECK_CPA_READINESS = "CHECK_CPA_READINESS"
    UPLOAD_DOCUMENT = "UPLOAD_DOCUMENT"
    CUSTOM_QUERY = "CUSTOM_QUERY"


#: Action -> the phrasing the existing intent classifier already understands.
#:
#: Structured actions are mapped rather than re-implemented: a dropdown
#: selection and the same question typed out must reach identical code, or the
#: two paths drift and only one of them stays tested.
_ACTION_PHRASE: dict[FosAction, str] = {
    FosAction.GET_APPLICANT: "Show me the applicant details.",
    FosAction.GET_APPLICATION_STATUS: "What is the application status?",
    FosAction.GET_DOCUMENTS: "Which documents have been uploaded?",
    FosAction.GET_DOCUMENT_CHECKLIST: "Show me the document checklist.",
    FosAction.GET_VERIFICATION_STATUS: "Show me all document issues.",
    FosAction.GET_PENDING_ITEMS: "What is pending?",
    FosAction.GET_NEXT_ACTION: "What should I do next?",
    FosAction.GET_CASE_360: "Give me a complete summary of this applicant.",
    FosAction.CHECK_CPA_READINESS: "Is this ready for CPA?",
}

#: Human labels for the dropdown, served with the action list.
_ACTION_LABELS: dict[FosAction, str] = {
    FosAction.GET_APPLICANT: "Applicant details",
    FosAction.GET_APPLICATION_STATUS: "Application status",
    FosAction.GET_DOCUMENTS: "Uploaded documents",
    FosAction.GET_DOCUMENT_CHECKLIST: "Document checklist",
    FosAction.GET_VERIFICATION_STATUS: "Verification status",
    FosAction.GET_PENDING_ITEMS: "Pending items",
    FosAction.GET_NEXT_ACTION: "Next action",
    FosAction.GET_CASE_360: "Case 360",
    FosAction.CHECK_CPA_READINESS: "CPA readiness",
    FosAction.UPLOAD_DOCUMENT: "Upload a document",
    FosAction.CUSTOM_QUERY: "Ask a question",
}


# ==========================================================================
# REQUESTS
# ==========================================================================

class ApplicantDetails(BaseModel):
    full_name: str | None = Field(None, max_length=200, examples=["Rahul Sharma"])
    mobile: str | None = Field(None, max_length=20, examples=["9876543210"])
    email: str | None = Field(None, max_length=200,
                              examples=["rahul.sharma@example.com"])
    date_of_birth: str | None = Field(None, max_length=32, examples=["1990-04-12"])
    address: str | None = Field(None, max_length=500,
                                examples=["Mumbai, Maharashtra"])


class ApplicationDetails(BaseModel):
    product: str | None = Field(
        None, max_length=64, examples=["PERSONAL_LOAN"],
        description="Decides the document checklist. See GET /api/v1/fos/config.",
    )
    loan_amount: float | str | None = Field(
        None, examples=[500000],
        description=(
            "Drives the amount-based document rules. Omit it and those "
            "rules are reported as unevaluated in `policy."
            "unevaluated_rules` rather than guessed at, so the checklist "
            "is the base one and is known to be provisional."
        ),
    )
    employment_type: str | None = Field(
        None, max_length=64, examples=["SALARIED", "SELF_EMPLOYED"],
        description=(
            "An applicant attribute the document policy may key on. Not "
            "defaulted: a rule that depends on it is reported as "
            "unevaluated when it is absent."
        ),
    )


class CreateCaseRequest(BaseModel):
    """One call: the applicant, their application, and the opened case."""

    applicant: ApplicantDetails
    application: ApplicationDetails | None = None
    applicant_id: str | None = Field(
        None, max_length=128,
        description="Optional. Generated when omitted.",
    )
    case_id: str | None = Field(
        None, max_length=128,
        description="Optional. Generated when omitted.",
    )


class CopilotRequest(BaseModel):
    """
    One request shape for every copilot action.

    `action` decides what happens. `message` is required only for
    CUSTOM_QUERY. Document upload uses the multipart form of this same
    endpoint -- see the endpoint description.
    """

    applicant_id: str = Field(..., max_length=128, examples=["APP-3D51FFAC6342"])
    case_id: str | None = Field(
        None, max_length=128, examples=["CASE-7DFE2F497522"],
        description="Required for everything case-specific, which is most actions.",
    )
    action: FosAction = Field(
        FosAction.CUSTOM_QUERY,
        description="What to do. CUSTOM_QUERY answers `message` in natural language.",
    )
    message: str | None = Field(
        None, max_length=1000,
        description="The question, for CUSTOM_QUERY. Ignored otherwise.",
        examples=["What documents are pending?"],
    )
    context: dict[str, Any] | None = Field(
        None,
        description=(
            "The `context` block from the PREVIOUS response, echoed back "
            "so a bare follow-up such as \"why?\" can be resolved.\n\n"
            "This service holds no conversation state, so the caller "
            "carries it. The context can only rewrite the message into "
            "another question, which is then classified exactly as a typed "
            "one is -- it selects no intent, names no case and skips no "
            "permission check. When a follow-up is resolved, the response "
            "says so in `followed_up`."
        ),
        examples=[{"last_query_type": "POLICY_REQUIREMENT",
                   "last_intent": "DOCUMENTS_MISSING",
                   "last_slot": "ADDRESS_PROOF"}],
    )


# ==========================================================================
# RESPONSE -- one shape, every action
# ==========================================================================

class FosErrorInfo(BaseModel):
    code: str
    message: str


class FosResponse(BaseModel):
    """
    The single response contract.

    Every field is always present. Ones an action does not populate come back
    null or empty, so a frontend binds one model rather than branching on the
    action it sent.
    """

    request_id: str
    applicant_id: str | None = None
    case_id: str | None = None
    action: str | None = Field(None, examples=["GET_DOCUMENTS"])
    intent: str | None = Field(None, examples=["DOCUMENTS_UPLOADED"])
    answer: str = Field("", description="Prose for the officer to read.")

    applicant: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    stage: str | None = Field(None, examples=["DOCUMENT_COLLECTION"])

    documents: list[dict[str, Any]] = Field(default_factory=list)
    checklist: list[dict[str, Any]] = Field(default_factory=list)
    required_documents: list[str] = Field(
        default_factory=list,
        description="Mandatory slots for this product. Optional slots appear "
                    "in `checklist` with mandatory=false.",
    )
    policy: dict[str, Any] | None = Field(
        None,
        description=(
            "Where the checklist came from. Present whenever `checklist` "
            "is.\n\n"
            "- `policy_id`, `policy_version`, `status` — the configured "
            "policy that produced the requirements. A `status` of "
            "`UNCONFIRMED` means the thresholds in that file are "
            "placeholders that no lender has signed off, and a UI should "
            "say so rather than presenting them as policy.\n"
            "- `applied_rules` — the rule ids that fired. Every checklist "
            "row names its own in `rule_ids`.\n"
            "- `unevaluated_rules` — rules that could NOT be decided "
            "because the case has not captured what they key on (a loan "
            "amount, an employment type). Their documents are **not** "
            "imposed; each entry names the missing attribute and what the "
            "rule would have required, so the checklist is known to be "
            "provisional rather than appearing final.\n"
            "- `pinned_version` / `version_changed` — the policy version "
            "the case was opened under. This service resolves against the "
            "current file; when it differs from the pin, `version_changed` "
            "is true and `note` says so."
        ),
    )
    pending_items: list[dict[str, Any]] = Field(default_factory=list)

    # ---- the frontend contract ------------------------------------------
    #
    # Derived from the fields above and from configuration. Nothing here is
    # written by a language model, and nothing here decides an outcome --
    # these are navigation aids, so a client does not have to re-implement
    # (and drift from) what the service will actually permit.
    query_type: str | None = Field(
        None,
        description=(
            "What was ASKED FOR, as opposed to `category`, which says what "
            "was CONSULTED. One of CASE_FACT, DOCUMENT_STATUS, "
            "POLICY_REQUIREMENT, PROCESS_KNOWLEDGE, MIXED, ACTION_REQUEST, "
            "DOWNSTREAM, CLARIFICATION."
        ),
        examples=["POLICY_REQUIREMENT"],
    )
    case_state: dict[str, Any] | None = Field(
        None,
        description=(
            "Compact header state: counts of required, satisfied, missing, "
            "under-review and failed documents, and `collection_progress` "
            "over REQUIRED slots. Counted from the same checklist this "
            "response carries. Null on an answer that did not read the "
            "case."
        ),
    )
    suggested_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Follow-ups THIS case can answer, most blocking first. Built "
            "from what is outstanding, never from a model, and never a "
            "downstream question the stage would refuse."
        ),
    )
    available_actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "What can be done now. Each entry has `action`, `label` and "
            "`enabled`, plus `disabled_reason` when it is off. Disabled "
            "rather than hidden, so the panel keeps its shape and says why."
        ),
    )
    document_highlights: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "One card per document: `severity`, a one-line `headline` and "
            "`primary_reason_code`. Reason codes are passed through from "
            "verification, never rephrased."
        ),
    )
    followed_up: dict[str, Any] | None = Field(
        None,
        description=(
            "Set when a bare follow-up was expanded into a whole question. "
            "Carries `original_message`, `interpreted_as` and `reason`, so "
            "a misreading is visible instead of producing an answer that "
            "does not match the question."
        ),
    )
    context: dict[str, Any] | None = Field(
        None,
        description=(
            "Echo this back as the request's `context` on the next "
            "question so a follow-up can be resolved. This service holds "
            "no conversation state; the caller carries it."
        ),
    )
    clarification_required: dict[str, Any] | None = Field(
        None,
        description=(
            "Set when the service declined to guess what was meant. "
            "Carries a `question` and `options` the caller can pick from. "
            "A null here is a claim that the request WAS understood."
        ),
    )

    verification: dict[str, Any] | None = Field(
        None,
        description=(
            "Populated by GET_VERIFICATION_STATUS and UPLOAD_DOCUMENT.\n\n"
            "On an upload it carries `documents_processed` — **one entry per "
            "file**, with that file's own class, verdict and reason codes — "
            "plus `total`, `passed` and `not_passed`. A batch where one "
            "document failed reports the failure beside the successes rather "
            "than collapsing to a single verdict."
        ),
    )
    kyc: dict[str, Any] | None = Field(
        None,
        description=(
            "Cross-document consistency, as the KYC agent computed it over "
            "the documents that cleared the verification gate: field-level "
            "`match_score` and `confidence`, source attribution, and an "
            "overall score.\n\n"
            "Consumed here, never re-derived — FOS has no KYC logic of its "
            "own. It establishes that the documents describe the same "
            "person; it does NOT establish that any of them is genuine, and "
            "it is not a credit, fraud or lending decision."
        ),
    )
    knowledge: dict[str, Any] | None = Field(
        None,
        description=(
            "Present when an answer drew on the FOS knowledge base. "
            "`grounded` says whether retrieval was confident enough to "
            "answer at all; `sources` names the passages used.\n\n"
            "Absent on a pure case answer — no knowledge was consulted, and "
            "citing some would imply a source the facts did not have."
        ),
    )
    next_action: dict[str, Any] | None = None
    readiness: dict[str, Any] | None = Field(
        None,
        description="Whether the FOS stage is complete enough to hand to CPA. "
                    "NOT a credit, risk, KYC or lending decision.",
    )

    actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Changes awaiting confirmation. Never already applied.",
    )
    route_to: str | None = Field(
        None,
        description="Set when the question belongs downstream.",
        examples=["CREDIT"],
    )

    category: str = Field(
        "CASE_ONLY",
        description=(
            "What kind of question this was, and therefore what was "
            "consulted. "
            "`CASE_ONLY` the store answered it. `KNOWLEDGE_ONLY` the FOS "
            "knowledge base did, and no record was read. `MIXED` both. "
            "`DOWNSTREAM` neither — it was routed. `UNSUPPORTED` it was not "
            "understood."
        ),
        examples=["CASE_ONLY", "KNOWLEDGE_ONLY", "MIXED", "DOWNSTREAM"],
    )
    response_source: str = Field(
        "STRUCTURED",
        description=(
            "What the answer was actually built from. "
            "`STRUCTURED` computed from stored records. `KNOWLEDGE` "
            "retrieved from the FOS knowledge base. `MIXED` both. `LLM` a "
            "model phrased it — the FACTS still came from one of the others. "
            "`ROUTED` no answer was produced. "
            "Differs from `category` where retrieval was attempted and came "
            "back unconfident: the category says what was consulted, this "
            "says what contributed."
        ),
        examples=["STRUCTURED", "KNOWLEDGE", "MIXED", "LLM", "ROUTED"],
    )
    processing_ms: float = 0.0
    errors: list[FosErrorInfo] = Field(default_factory=list)


def _blank(request_id: str, **overrides: Any) -> dict[str, Any]:
    """The full envelope, so no action can return a differently-shaped one."""
    base: dict[str, Any] = {
        "request_id": request_id,
        "applicant_id": None, "case_id": None, "action": None, "intent": None,
        "answer": "", "applicant": None, "application": None, "stage": None,
        "documents": [], "checklist": [], "required_documents": [],
        "policy": None,
        "query_type": None, "case_state": None, "suggested_questions": [],
        "available_actions": [], "document_highlights": [],
        "clarification_required": None, "followed_up": None, "context": None,
        "pending_items": [], "verification": None, "kyc": None,
        "knowledge": None, "category": "CASE_ONLY", "next_action": None,
        "readiness": None, "actions": [], "route_to": None,
        "response_source": "STRUCTURED", "processing_ms": 0.0, "errors": [],
    }
    base.update(overrides)
    return base


def _required_slots(checklist: list[dict[str, Any]]) -> list[str]:
    return [e["slot"] for e in checklist if e.get("mandatory", True)]


# ==========================================================================
# API 1 -- CREATE APPLICANT + APPLICATION
# ==========================================================================

@router.post(
    "/applicants",
    summary="Open a case: create the applicant and their application",
    status_code=status.HTTP_201_CREATED,
    description=(
        "One call creates the applicant, generates `applicant_id` and "
        "`case_id`, creates the application, initialises the FOS stage and "
        "returns the opened case -- including the document checklist the "
        "chosen product requires.\n\n"
        "**Scopes:** `create_applicant` and, when an application is included, "
        "`create_application`."
    ),
    responses={201: {"model": FosResponse, "description": "The opened case."}},
)
async def create_case(
    request: CreateCaseRequest,
    claims: dict[str, Any] = Depends(require_jwt),
):
    from app.mcp import applicant as tools

    request_id = f"fos_{uuid.uuid4().hex}"
    caller = Caller.from_claims(claims)

    try:
        permissions.check_capability(caller, Intent.CREATE_APPLICANT)
        if request.application is not None:
            permissions.check_capability(caller, Intent.CREATE_APPLICATION)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=None, case_id=None, intent="CREATE_CASE",
                     tools=[], write=True, status="DENIED", detail=exc.code)
        raise HTTPException(403, detail={
            "request_id": request_id, "error": exc.code, "message": exc.message,
        }) from exc

    created = await tools.applicant_create(
        **request.applicant.model_dump(exclude_none=True),
        applicant_id=request.applicant_id,
    )
    if not created.ok:
        _raise_from(request_id, created)
    applicant_id = created.result["applicant"]["applicant_id"]

    application_details = request.application or ApplicationDetails()
    amount = application_details.loan_amount
    application = await tools.application_create(
        applicant_id=applicant_id,
        product=application_details.product,
        loan_amount=(str(amount) if amount is not None else None),
        employment_type=application_details.employment_type,
        case_id=request.case_id,
    )
    if not application.ok:
        _raise_from(request_id, application)
    case_id = application.result["application"]["case_id"]

    # Read the opened case back through the same tool the copilot uses, so the
    # state returned here is the state a subsequent query will report.
    view = await tools.applicant_360(case_id)
    if not view.ok:
        _raise_from(request_id, view)
    result = view.result or {}
    checklist = result.get("checklist") or []

    audit.record(request_id=request_id, subject=caller.subject,
                 applicant_id=applicant_id, case_id=case_id,
                 intent="CREATE_CASE", tools=["applicant.create",
                        "application.create"],
                 write=True, confirmed=True, status="OK")

    logger.info("fos create_case request_id=%s applicant=%s case=%s product=%s",
                request_id, applicant_id, case_id, application_details.product)

    return _blank(
        request_id,
        applicant_id=applicant_id,
        case_id=case_id,
        action="CREATE_CASE",
        intent="CREATE_CASE",
        answer=(
            f"Case {case_id} opened for "
            f"{request.applicant.full_name or applicant_id}. "
            f"{len(_required_slots(checklist))} document(s) required."
        ),
        applicant=result.get("applicant"),
        application=result.get("application"),
        stage=result.get("stage"),
        documents=result.get("documents") or [],
        checklist=checklist,
        required_documents=_required_slots(checklist),
        policy=result.get("policy"),
        pending_items=result.get("pending_items") or [],
        next_action=result.get("next_action"),
        readiness=result.get("readiness"),
        query_type="CASE_FACT",
        **_frontend_contract({"query_type": "CASE_FACT"}, {
                              "applicant": result.get("applicant"),
                              "application": result.get("application"),
                              "stage": result.get("stage"),
                              "documents": result.get("documents") or [],
                              "checklist": checklist,
                              "readiness": result.get("readiness"),
                              "policy": result.get("policy"),
        }),
    )


# ==========================================================================
# API 2 -- THE COPILOT
# ==========================================================================

_COPILOT_BODY = {
    "required": True,
    "content": {
    "application/json": {
    "schema": {"$ref": "#/components/schemas/CopilotRequest"},
    "examples": {
    "ask_a_question": {
    "summary": "CUSTOM_QUERY — natural language",
    "value": {
    "applicant_id": "APP-3D51FFAC6342",
    "case_id": "CASE-7DFE2F497522",
    "action": "CUSTOM_QUERY",
    "message": "What documents are pending?",
                    },
                },
                "documents": {
                "summary": "GET_DOCUMENTS — what has been uploaded",
                "value": {
                "applicant_id": "APP-3D51FFAC6342",
                "case_id": "CASE-7DFE2F497522",
                "action": "GET_DOCUMENTS",
                    },
                },
                "checklist": {
                "summary": "GET_DOCUMENT_CHECKLIST — what this product needs",
                "value": {
                "applicant_id": "APP-3D51FFAC6342",
                "case_id": "CASE-7DFE2F497522",
                "action": "GET_DOCUMENT_CHECKLIST",
                    },
                },
                "pending": {
                "summary": "GET_PENDING_ITEMS — everything outstanding",
                "value": {
                "applicant_id": "APP-3D51FFAC6342",
                "case_id": "CASE-7DFE2F497522",
                "action": "GET_PENDING_ITEMS",
                    },
                },
                "next_action": {
                "summary": "GET_NEXT_ACTION — the one thing to do now",
                "value": {
                "applicant_id": "APP-3D51FFAC6342",
                "case_id": "CASE-7DFE2F497522",
                "action": "GET_NEXT_ACTION",
                    },
                },
                "case_360": {
                "summary": "GET_CASE_360 — the whole picture",
                "value": {
                "applicant_id": "APP-3D51FFAC6342",
                "case_id": "CASE-7DFE2F497522",
                "action": "GET_CASE_360",
                    },
                },
                "readiness": {
                "summary": "CHECK_CPA_READINESS — may this go to CPA?",
                "value": {
                "applicant_id": "APP-3D51FFAC6342",
                "case_id": "CASE-7DFE2F497522",
                "action": "CHECK_CPA_READINESS",
                    },
                },
                "out_of_scope": {
                "summary": "A downstream question — routed, not answered",
                "value": {
                "applicant_id": "APP-3D51FFAC6342",
                "case_id": "CASE-7DFE2F497522",
                "action": "CUSTOM_QUERY",
                "message": "Should we approve this loan?",
                    },
                },
            },
        },
        "multipart/form-data": {
        "schema": {
        "type": "object",
        "required": ["applicant_id", "case_id", "action"],
        "properties": {
        "applicant_id": {"type": "string",
        "example": "APP-3D51FFAC6342"},
        "case_id": {"type": "string",
        "example": "CASE-7DFE2F497522"},
        "action": {"type": "string", "enum": ["UPLOAD_DOCUMENT"],
        "example": "UPLOAD_DOCUMENT"},
        "files": {
        "type": "array",
        "items": {"type": "string", "format": "binary"},
        "description": (
        "One or more documents. In Swagger, press **Add "
        "string item** once per document and pick a file "
        "for each.\n\n"
        "Each file is classified and verified "
        "independently and gets its own entry in "
        "`verification.documents_processed`, so one bad "
        "file never stops the rest."
                        ),
                    },
                    "document_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                    "Optional, positional: the Nth value asserts the "
                    "type of the Nth file. Omit the field entirely to "
                    "let the pipeline classify everything, or leave "
                    "an individual entry blank to classify just that "
                    "file.\n\n"
                    "Send it as repeated parts (one per file). A "
                    "single comma-separated value "
                    "(`PAN,DRIVING_LICENCE`) is also accepted, "
                    "because some clients join array fields that "
                    "way.\n\n"
                    "An assertion is CHECKED, never applied: a file "
                    "that is not the type claimed fails with "
                    "DOCUMENT_TYPE_MISMATCH and is not silently "
                    "reclassified. A checklist slot name such as "
                    "ADDRESS_PROOF is accepted and resolves to the "
                    "document types that satisfy it."
                        ),
                        "example": ["PAN", "DRIVING_LICENCE"],
                    },
                    # `file` and `document_type` -- the original single-file
                    # fields -- are STILL ACCEPTED by the handler but are no
                    # longer published here.
                    #
                    # Showing both forms put two file pickers side by side in
                    # Swagger, and the legacy one takes a single document. A
                    # person driving the demo picked it, uploaded one file,
                    # and reasonably concluded multi-upload did not work. One
                    # visible way to do this is worth more than a documented
                    # alternative nobody needed.
                },
            },
            # EXPLODE THE ARRAYS.
            #
            # Without this, Swagger UI serialises an array field in a
            # multipart body by joining it with commas -- three picked types
            # became one part, `document_types=PAN,DRIVING_LICENCE,...`, and
            # the request was refused as an unknown document type. The
            # handler now splits that anyway, but a form whose generated curl
            # is wrong teaches every reader the wrong shape, so it is fixed
            # here too rather than only absorbed downstream.
            #
            # style/explode is the OpenAPI 3 way to say "one part per item".
                                                        "encoding": {
                                                        "files": {"style": "form", "explode": True},
                                                        "document_types": {
                                                        "style": "form",
                                                        "explode": True,
                                                        "contentType": "text/plain",
                },
            },
        },
    },
}


@router.post(
    "/copilot",
    summary="The FOS copilot: questions, dropdown actions and document upload",
    description=(
        "**The single endpoint a FOS frontend needs.**\n\n"
        "`action` selects what happens. Two request forms:\n\n"
        "- **`application/json`** for every action except upload. Use "
        "`CUSTOM_QUERY` with `message` for natural language; use any other "
        "action for a dropdown selection.\n"
        "- **`multipart/form-data`** for `UPLOAD_DOCUMENT`, with `file` and an "
        "optional `document_type`.\n\n"
        "An upload runs the existing LOS pipeline — classification, then the "
        "verification gate, then extraction only behind a PASS — and stores "
        "the verdict, which every later query then reports.\n\n"
        "Credit, risk, KYC, RCU and lending questions are **routed**, never "
        "answered: the response carries `route_to` and no verdict.\n\n"
        "Every action returns the same response shape; fields an action does "
        "not populate come back null or empty.\n\n"
        "**Actions:** `GET_APPLICANT`, `GET_APPLICATION_STATUS`, "
        "`GET_DOCUMENTS`, `GET_DOCUMENT_CHECKLIST`, `GET_VERIFICATION_STATUS`, "
        "`GET_PENDING_ITEMS`, `GET_NEXT_ACTION`, `GET_CASE_360`, "
        "`CHECK_CPA_READINESS`, `UPLOAD_DOCUMENT`, `CUSTOM_QUERY`. "
        "Render the dropdown from `GET /api/v1/fos/actions`."
    ),
    responses={200: {"model": FosResponse, "description": "The answer."}},
    openapi_extra={"requestBody": _COPILOT_BODY},
)
async def copilot(
    request: Request,
    claims: dict[str, Any] = Depends(require_jwt),
):
    request_id = f"fos_{uuid.uuid4().hex}"
    content_type = (request.headers.get("content-type") or "").lower()

    try:
        # Both form encodings reach the upload handler. A caller who posts
        # `action=UPLOAD_DOCUMENT` as a urlencoded form has forgotten the
        # files, not sent a malformed body, and should be told which -- the
        # JSON path could only report that it failed to parse.
        if content_type.startswith(("multipart/form-data",
                                    "application/x-www-form-urlencoded")):
            return await _copilot_upload(request, claims, request_id)
        return await _copilot_json(request, claims, request_id)
    except HTTPException:
        raise
    except AgentError as exc:
        raise HTTPException(exc.http_status, detail={
            "request_id": request_id, "error": exc.code, "message": exc.message,
        }) from exc
    except Exception as exc:
        logger.exception("FOS copilot failed request_id=%s", request_id)
        raise HTTPException(500, detail={
            "request_id": request_id, "error": "COPILOT_FAILED",
            "message": "The request could not be completed.",
        }) from exc


async def _copilot_json(
    request: Request,
    claims: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """Every action except upload."""
    try:
        payload = CopilotRequest.model_validate(await request.json())
    except Exception as exc:
        raise HTTPException(422, detail={
            "request_id": request_id, "error": "INVALID_REQUEST",
            "message": f"The request body could not be read: {type(exc).__name__}.",
        }) from exc

    action = payload.action

    if action is FosAction.UPLOAD_DOCUMENT:
        raise HTTPException(415, detail={
            "request_id": request_id, "error": "UPLOAD_REQUIRES_MULTIPART",
            "message": ("UPLOAD_DOCUMENT requires multipart/form-data with a "
            "`file` part."),
        })

    if action is FosAction.CUSTOM_QUERY:
        message = (payload.message or "").strip()
        if not message:
            raise HTTPException(422, detail={
                "request_id": request_id, "error": "MESSAGE_REQUIRED",
                "message": "CUSTOM_QUERY requires `message`.",
            })
    else:
        message = _ACTION_PHRASE[action]

    result = await answer_question(
        message=message,
        applicant_id=payload.applicant_id,
        case_id=payload.case_id,
        claims=claims,
        request_id=request_id,
        # Only a typed question is pruned. A dropdown action is a screen and
        # keeps the fields that screen renders.
        concise=action is FosAction.CUSTOM_QUERY,
        # A follow-up only makes sense for a typed question. A dropdown
        # action is unambiguous by construction, and resolving one against
        # a stale context would change what the button does.
        context=(payload.context if action is FosAction.CUSTOM_QUERY else None),
    )
    return _from_agent(result, action.value, request_id,
                       concise=action is FosAction.CUSTOM_QUERY)


def _declared_types(form) -> list[str | None]:
    """
    The asserted document types, however the client chose to send them.

    THE CASE THIS EXISTS FOR. Swagger UI serialises an array field in a
    multipart body by JOINING IT WITH COMMAS, so picking three types in the
    UI produces one part:

        -F 'document_types= PAN,DRIVING_LICENCE,BANK_STATEMENT'

    which arrives as a single value. Read literally that is a request to
    assert one document is of type "PAN,DRIVING_LICENCE,BANK_STATEMENT",
    and the endpoint correctly refused it with UNSUPPORTED_DOCUMENT_TYPE --
    correctly, and uselessly, because the person had done exactly what the
    form asked of them.

    So both spellings are accepted:

        document_types=PAN & document_types=DL     (repeated parts)
        document_types=PAN,DL                      (one joined part)

    SPLITTING IS SAFE HERE BECAUSE OF WHAT THESE VALUES ARE. A document type
    is an uppercase identifier from a closed set -- PAN, DRIVING_LICENCE,
    ADDRESS_PROOF -- and none contains a comma. This would not be safe for a
    free-text field, and it is not applied to one.

    BLANKS ARE PRESERVED, because a blank is meaningful: it means "classify
    this one". `PAN,,BANK_STATEMENT` keeps its middle gap, so the second file
    is still classified rather than the third assertion sliding onto it.
    """
    raw = list(form.getlist("document_types"))

    # The single-file field, still accepted for callers written against the
    # original shape.
    if not raw:
        single = form.get("document_type")
        if single is not None:
            raw = [single]

    declared: list[str | None] = []
    for value in raw:
        text = str(value)
        # A comma means the client joined the array; otherwise this is one
        # value and split() returns it unchanged.
        for part in text.split(","):
            cleaned = part.strip().upper()
            declared.append(cleaned or None)

    # A lone empty part carries no assertion and no position -- it is what an
    # untouched Swagger field sends. Dropping it stops an empty form field
    # from claiming the first file's slot.
    if len(declared) == 1 and declared[0] is None:
        return []

    return declared


async def _copilot_upload(
    request: Request,
    claims: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """
    UPLOAD_DOCUMENT: run the existing LOS pipeline, store what it concluded.

    Verification stays the source of truth. Nothing here re-derives a verdict,
    and nothing here releases extracted fields the gate withheld.
    """
    from app.agents.applicant.intents import Intent as _Intent
    from app.agents.los.flow import PROCESS, UploadedDocument, process_application
    from app.mcp import applicant as tools
    from app.store.ingest import persist_los_result

    form = await request.form()
    applicant_id = str(form.get("applicant_id") or "").strip()
    case_id = str(form.get("case_id") or "").strip()
    declared_action = str(form.get("action") or FosAction.UPLOAD_DOCUMENT.value).strip().upper()

    # `files` is the contract; `file` is the single-file form kept working for
    # callers written against the earlier shape. Both are read so a client
    # that sends one, the other, or both is never silently ignored.
    uploads = [u for u in form.getlist("files") if hasattr(u, "read")]
    single = form.get("file")
    if single is not None and hasattr(single, "read"):
        uploads.append(single)

    # POSITIONAL assertions: the Nth type belongs to the Nth file. A blank
    # entry means "classify this one", so a caller who knows two of three
    # types does not have to guess at the third.
    declared_types = _declared_types(form)

    if declared_action != FosAction.UPLOAD_DOCUMENT.value:
        raise HTTPException(422, detail={
            "request_id": request_id, "error": "UNSUPPORTED_ACTION",
            "message": (f"{declared_action} is not valid for a multipart "
            "request. Only UPLOAD_DOCUMENT is."),
        })
    if not applicant_id or not case_id:
        raise HTTPException(422, detail={
            "request_id": request_id, "error": "INVALID_REQUEST",
            "message": "applicant_id and case_id are required for an upload.",
        })
    if not uploads:
        raise HTTPException(422, detail={
            "request_id": request_id, "error": "FILE_REQUIRED",
            "message": ("At least one file is required for UPLOAD_DOCUMENT. "
            "Send them as `files`."),
        })
    if len(declared_types) > len(uploads):
        raise HTTPException(422, detail={
            "request_id": request_id, "error": "DOCUMENT_TYPES_MISALIGNED",
            "message": (
                f"{len(declared_types)} document_types were supplied for "
                f"{len(uploads)} file(s). They are matched by position, so "
                 "there cannot be more types than files."
            ),
        })

    caller = Caller.from_claims(claims)
    try:
        permissions.check_capability(caller, _Intent.MARK_FOR_REUPLOAD)  # upload_document
        permissions.check_ownership(applicant_id, case_id)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent="UPLOAD_DOCUMENT", tools=[], write=True,
                     status="DENIED", detail=exc.code)
        raise HTTPException(403, detail={
            "request_id": request_id, "error": exc.code, "message": exc.message,
        }) from exc

    # Checklist SLOT names are accepted here as well as document classes.
    # The checklist the FOS is looking at says "ADDRESS_PROOF"; refusing the
    # value it just showed them would be the API arguing with its own screen.
    # Verification resolves the slot to the classes that satisfy it.
    from app.agents.verification import slots as _slots

    known = set(config.document_types()) | set(_slots.known_slots())
    for declared in declared_types:
        if declared and declared not in known:
            raise HTTPException(422, detail={
                "request_id": request_id,
                "error": "UNSUPPORTED_DOCUMENT_TYPE",
                "message": f"{declared} is not a known document type.",
                "supported": sorted(known),
            })

    # Read every part before anything is processed, so a request that is
    # malformed is refused whole rather than half-applied.
    documents: list[UploadedDocument] = []
    empty: list[str] = []

    for index, upload in enumerate(uploads):
        content = await upload.read()
        filename = getattr(upload, "filename", None) or f"upload-{index + 1}"
        if not content:
            empty.append(filename)
            continue
        expected = (declared_types[index]
                    if index < len(declared_types) else None)
        documents.append(UploadedDocument(
            source_id=filename, filename=filename,
            content=content, expected_type=expected,
        ))

    if not documents:
        raise HTTPException(400, detail={
            "request_id": request_id, "error": "EMPTY_FILE",
            "message": (f"Every uploaded file was empty: {', '.join(empty)}."
                        if empty else "The uploaded file is empty."),
        })

    # ONE CALL FOR THE WHOLE BATCH. process_application already owns the OCR
    # worker pool and the per-document concurrency; calling it once per file
    # would serialise work it is built to overlap, and would also produce a
    # separate KYC assessment per document -- each seeing one source and
    # finding nothing to cross-check.
    los = await process_application(
        documents,
        operation=PROCESS,
        applicant_id=applicant_id,
        case_id=case_id,
        request_id=request_id,
        # THE FOS STAGE BOUNDARY.
        #
        # FOS owns classification and basic document verification: is this
        # upload usable as the type it claims to be? It does not own KYC,
        # which asks whether the identity fields across several documents
        # describe one person, and which belongs after the CPA handoff.
        #
        # Without this, adding a second document to a case ran a full
        # cross-document identity comparison -- name, date of birth, father's
        # name -- and returned it from an upload endpoint at a stage with no
        # authority to act on it.
        cross_document_checks=False,
        # And no income analysis. A bank statement is verified here as a
        # DOCUMENT; `signals` carries average monthly credit and net salary,
        # which is the credit stage's output and has no business in a FOS
        # response. /api/v1/los/process leaves this on.
        financial_analysis=False,
        # And no application summary: FOS writes its own answer from these
        # results and never reads `summary`, so generating one is a model
        # call per upload that nobody sees.
        summarise=False,
    )
    persist_los_result(los)

    view = await tools.applicant_360(case_id)
    result = view.result or {}
    checklist = result.get("checklist") or []

    # ONE OUTCOME PER FILE. A batch where the PAN passed and a dummy image
    # failed has to report both: collapsing it to a single verdict either
    # hides a rejection or condemns the whole upload for one bad file, and
    # the officer cannot tell which document to collect again.
    outcomes = [
        {
            "source_id": document.get("source_id"),
            "document_type": document.get("type"),
            "expected_type": document.get("expected_type"),
            "verification": document.get("verification"),
            "status": document.get("status"),
            "reason_codes": document.get("reason_codes") or [],
            # The gate's decision, restated. Extraction is released only
            # behind a PASS, so this says plainly whether fields came back.
            "extraction_released": document.get("extraction") is not None,
            "authenticity": document.get("authenticity"),
        }
        for document in (los.get("documents") or [])
    ]

    for name in empty:
        outcomes.append({
            "source_id": name, "document_type": None, "expected_type": None,
            "verification": "FAIL", "status": "FAILED",
            "reason_codes": ["EMPTY_FILE"],
            "extraction_released": False, "authenticity": None,
        })

    passed = sum(1 for o in outcomes if o["verification"] == "PASS")

    verification = {
        "documents_processed": outcomes,
        "total": len(outcomes),
        "passed": passed,
        "not_passed": len(outcomes) - passed,
    }
    if len(outcomes) == 1:
        # The single-document shape the earlier contract published, kept
        # alongside the list so a caller written against it still works.
        verification.update({
            k: outcomes[0][k] for k in
            ("document_type", "verification", "status", "reason_codes",
             "extraction_released", "expected_type")
        })

    audit.record(request_id=request_id, subject=caller.subject,
                 applicant_id=applicant_id, case_id=case_id,
                 intent="UPLOAD_DOCUMENT", tools=["los.process",
                        "applicant.360"],
                 write=True, confirmed=True, status="OK")

    if len(outcomes) == 1:
        one = outcomes[0]
        doc_name = str(one.get("document_type") or "Document").replace(
                               "_", " ").title()
        answer = f"{doc_name} uploaded. Verification: {one['verification']}."
    else:
        answer = (
            f"{len(outcomes)} documents uploaded. {passed} passed "
            f"verification, {len(outcomes) - passed} did not."
        )
        refused = [o for o in outcomes if o["verification"] != "PASS"]
        if refused:
            # Named, not counted. "One did not pass" leaves the officer
            # opening every file to find out which.
            answer += " Not passed: " + "; ".join(
                f"{o['source_id']} ({o['verification']}"
                + (f", {', '.join(o['reason_codes'])}" if o["reason_codes"]
                   else "")
                + ")"
                for o in refused
            )

    return _blank(
        request_id,
        applicant_id=applicant_id,
        case_id=case_id,
        action=FosAction.UPLOAD_DOCUMENT.value,
        intent="UPLOAD_DOCUMENT",
        answer=answer,
        applicant=result.get("applicant"),
        application=result.get("application"),
        stage=result.get("stage"),
        documents=result.get("documents") or [],
        checklist=checklist,
        required_documents=_required_slots(checklist),
        policy=result.get("policy"),
        pending_items=result.get("pending_items") or [],
        verification=verification,
        # Null on an upload, and deliberately.
        #
        # KYC is a downstream stage. A FOS upload does not compute one, so
        # there is nothing to report -- and reporting SKIPPED would be a KYC
        # verdict where none was reached. A persisted downstream result, once
        # one exists, is surfaced by the read actions rather than minted here.
        kyc=None,
        next_action=result.get("next_action"),
        readiness=result.get("readiness"),
        # DOCUMENT_STATUS, not ACTION_REQUEST. The write has already
        # happened by the time this is built; what the response describes
        # is the state the documents are now in.
        query_type="DOCUMENT_STATUS",
        **_frontend_contract({"query_type": "DOCUMENT_STATUS"}, {
                              "applicant": result.get("applicant"),
                              "application": result.get("application"),
                              "stage": result.get("stage"),
                              "documents": result.get("documents") or [],
                              "checklist": checklist,
                              "readiness": result.get("readiness"),
                              "policy": result.get("policy"),
        }),
        processing_ms=round(float(los.get("processing_ms") or 0.0), 2),
    )


def _from_agent(
    result: dict[str, Any],
    action: str,
    request_id: str,
    *,
    concise: bool = False,
) -> dict[str, Any]:
    """Translate the Applicant Agent's envelope onto the FOS contract."""
    checklist = result.get("checklist") or []

    verification = None
    if action == FosAction.GET_VERIFICATION_STATUS.value:
        documents = result.get("documents") or []
        verification = {
            "documents": [
                {"document_type": d.get("document_type"),
                 "status": d.get("status"),
                 "verification": d.get("verification_status"),
                 "reason_codes": d.get("reason_codes") or []}
                for d in documents
            ],
            "needs_attention": [
                d.get("document_type") for d in documents
                if d.get("status") in {"REVIEW", "REJECTED"}
            ],
        }

    envelope = _blank(
        request_id,
        applicant_id=result.get("applicant_id"),
        case_id=result.get("case_id"),
        action=action,
        intent=result.get("intent"),
        answer=result.get("answer", ""),
        applicant=result.get("applicant"),
        application=result.get("application"),
        stage=result.get("stage"),
        documents=result.get("documents") or [],
        checklist=checklist,
        required_documents=_required_slots(checklist),
        policy=result.get("policy"),
        pending_items=result.get("pending_items") or [],
        verification=verification,
        kyc=result.get("kyc"),
        knowledge=result.get("knowledge"),
        category=result.get("category"),
        next_action=result.get("next_action"),
        readiness=result.get("readiness"),
        actions=result.get("actions") or [],
        route_to=result.get("route_to"),
        response_source=result.get("response_source", "STRUCTURED"),
        query_type=result.get("query_type"),
        clarification_required=result.get("clarification_required"),
        followed_up=result.get("followed_up"),
        processing_ms=result.get("processing_ms", 0.0),
        errors=result.get("errors") or [],
    )
    envelope.update(_frontend_contract(result, envelope))
    # Built from the COMPLETE envelope, before pruning: the slot a
    # follow-up is most likely about comes from the checklist, which a
    # typed question may not carry in its response.
    envelope["context"] = followup.context_from_response(envelope)

    # A TYPED QUESTION GETS ONLY WHAT IT ASKED ABOUT.
    #
    # `_blank` fills the whole envelope so a screen-rendering action always
    # has every field. For a chat answer that is noise: eleven fields the
    # client discards, some of them carrying applicant details the question
    # never touched. Dropdown actions keep the full shape.
    if concise and config.concise_responses():
        from app.agents.applicant import routing as _routing
        from app.agents.applicant.intents import Intent as _Intent

        try:
            intent = _Intent(result.get("intent") or "")
        except ValueError:
            return envelope

        base = result.get("base_intent")
        try:
            base_intent = _Intent(base) if base else None
        except ValueError:
            base_intent = None

        return _routing.prune(envelope, intent, base_intent=base_intent,
                              enabled=True)

    return envelope


def _frontend_contract(result: dict[str, Any],
                       envelope: dict[str, Any]) -> dict[str, Any]:
    """
    The UI fields, computed BEFORE the envelope is pruned.

    ORDER MATTERS HERE. Pruning drops the case fields a typed question did
    not ask about, and `case_state` counts documents out of the checklist.
    Computed after pruning, a question about applicant details would report
    a case with zero required documents -- a header that contradicts the
    screen it sits above. So it is computed from the complete data and
    survives the prune as its own field.

    A KNOWLEDGE OR ROUTED ANSWER GETS NO STATE. Neither read the case, and
    a header stating counts for a case the answer never looked at would be
    the same overreach the routing boundary exists to prevent.
    """
    from app.agents.applicant import frontend
    from app.agents.applicant.query_types import READS_CASE, QueryType

    raw = result.get("query_type")
    try:
        query_type = QueryType(raw) if raw else None
    except ValueError:
        query_type = None

    reads_case = query_type in READS_CASE if query_type else False
    block = frontend.contract(envelope, include_state=reads_case)

    # A clarification carries its own options as the suggestions -- the
    # case-derived ones would be answers to a question nobody asked.
    suggested = result.get("suggested_questions")
    if suggested:
        block["suggested_questions"] = list(suggested)
    return block


def _raise_from(request_id: str, envelope) -> None:
    error = envelope.error
    code = error.code if error else "FAILED"
    raise HTTPException(
        404 if code == "NOT_FOUND" else 400,
        detail={"request_id": request_id, "error": code,
                "message": error.message if error else "The call failed."},
    )


# ==========================================================================
# SUPPORTING -- not part of the two-endpoint integration surface
# ==========================================================================

@router.get(
    "/actions",
    summary="The action list, for rendering the dropdown",
    description=(
        "Values and labels for the copilot's `action` field. A frontend "
        "renders its dropdown from this rather than hardcoding it, so adding "
        "an action does not require a frontend release.\n\n"
        "*Supporting endpoint — not part of the two-endpoint integration "
        "surface.*"
    ),
)
async def actions(claims: dict[str, Any] = Depends(require_jwt)):
    return {
        "actions": [
            {
                "value": action.value,
                "label": _ACTION_LABELS[action],
                "requires_message": action is FosAction.CUSTOM_QUERY,
                "requires_file": action is FosAction.UPLOAD_DOCUMENT,
                "content_type": ("multipart/form-data"
                                 if action is FosAction.UPLOAD_DOCUMENT
                                 else "application/json"),
            }
            for action in FosAction
        ],
    }


@router.get(
    "/config",
    summary="Products, document types and checklists",
    description=(
        "What a frontend needs to build its forms: the products with a "
        "configured checklist, the document taxonomy for the upload selector, "
        "and the workflow states.\n\n"
        "*Supporting endpoint — not part of the two-endpoint integration "
        "surface.*"
    ),
)
async def fos_config(claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.policy import loader as policy_loader

    # Products from BOTH sources. A product described by a policy file and
    # not by the agent config was missing from this list, so a frontend
    # built its product picker without it while the copilot answered
    # questions about it perfectly well.
    products = [p for p in config.products() if p != "default"]

    policies = {}
    for product in policy_loader.known_products():
        document = policy_loader.policy_for(product) or {}
        policies[product] = {
            "policy_id": document.get("policy_id"),
            "policy_version": document.get("policy_version"),
            "status": document.get("status"),
            # What a form needs to know it should capture, because leaving
            # it out makes the checklist provisional.
            "keys_on": sorted({
                str(key).lower()
                for rule in (document.get("conditional_rules") or [])
                for key in (rule.get("when") or {})
            } | ({"loan_amount"} if document.get("amount_rules") else set())),
        }

    return {
        "products": products,
        "document_types": config.document_types(),
        "workflow_states": config.workflow_states(),
        "checklists": {
            product: config.checklist_for(product) for product in products
        },
        "default_checklist": config.checklist_for(None),
        "downstream_routes": sorted(config.routing_table()),
        # The product-level checklist above is what EVERY application for
        # the product needs. A case's own list depends on its amount and
        # attributes; these say which ones matter.
        "policies": policies,
        "query_types": [t.value for t in _QueryType],
    }


@router.get(
    "/tools",
    summary="The MCP tool catalogue",
    description=(
        "Every tool the copilot can call, with its JSON Schema, the scope "
        "required to call it, and whether it writes. Published so the "
        "boundary is reviewable rather than inferred from source.\n\n"
        "The scope shown is the one the permission layer enforces; a test "
        "checks the two agree.\n\n"
        "*Supporting endpoint — not part of the two-endpoint integration "
        "surface.*"
    ),
)
async def fos_tools(claims: dict[str, Any] = Depends(require_jwt)):
    from app.mcp import contracts

    return {
        "tools": [
            {**entry,
             "required_scope": contracts.required_scope(entry["name"]),
             "writes": contracts.CONTRACTS[entry["name"]].writes}
            for entry in contracts.catalogue()
        ],
        "never_answered_here": list(contracts.DOWNSTREAM_CONCERNS),
    }


__all__ = ["FosAction", "router"]
