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
import re
import time
import uuid

from app.store import request_cache
from enum import Enum
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.agents.applicant import audit, config, permissions
from app.agents.applicant.copilot.agent import AgentError, answer_question
from app.agents.applicant.grounded import supported_by_case_evidence
from app.agents.applicant.copilot.semantics.intents import Intent
from app.agents.applicant.permissions import Caller, PermissionDenied
from app.agents.applicant.copilot.conversation import followup
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
    # 6-MVP CASE WORKSPACE (COPILOT_CASE_WORKSPACE; refused with 422 while it is off)
    LIST_CASES = "LIST_CASES"
    OPEN_CASE = "OPEN_CASE"
    EXIT_CASE = "EXIT_CASE"
    # 6i CASE ACTIONS (COPILOT_CASE_ACTIONS; refused with 422 while it is off)
    VIEW_DOCUMENT = "VIEW_DOCUMENT"
    RAISE_QUERY = "RAISE_QUERY"
    MARK_QUERY_SENT = "MARK_QUERY_SENT"
    LIST_QUERIES = "LIST_QUERIES"
    NEW_CASE = "NEW_CASE"


#: the workspace actions (6-MVP)
_WORKSPACE_ACTIONS = frozenset({FosAction.LIST_CASES, FosAction.OPEN_CASE, FosAction.EXIT_CASE})
#: the case actions (6i)
_CASE_ACTIONS = frozenset({FosAction.VIEW_DOCUMENT, FosAction.RAISE_QUERY, FosAction.MARK_QUERY_SENT,
                           FosAction.LIST_QUERIES, FosAction.NEW_CASE})


#: Action -> the phrasing the existing intent classifier already understands.
#:
#: Structured actions are mapped rather than re-implemented: a dropdown
#: selection and the same question typed out must reach identical code, or the
#: two paths drift and only one of them stays tested.
#: A named action and the intent it IS.
#:
#: WHY THIS REPLACED A TABLE OF ENGLISH SENTENCES. Every action below
#: used to be turned into a phrase -- "Show me the document checklist."
#: -- and handed to the intent classifier, which matched it against a
#: few hundred regexes to recover the intent the caller had already
#: named. A button that says CHECKLIST does not need to be understood.
#:
#: AND THE ROUND TRIP WAS NOT FREE. Every routing defect found in review
#: was a pattern-ordering bug in that ladder: a status question answered
#: from the handbook, "still pending" missed by one adverb, a mismatch
#: question answered with a dictionary definition. A dropdown reaching
#: its capability directly cannot have any of them.
#:
#: NOTHING ELSE CHANGES. The intent still goes through the capability
#: check, the ownership check, the same tool plan, the same
#: deterministic answer and the same audit record. Only the guessing is
#: gone.
_ACTION_INTENT: dict[FosAction, Intent] = {
    FosAction.GET_APPLICANT: Intent.APPLICANT_DETAILS,
    FosAction.GET_APPLICATION_STATUS: Intent.APPLICATION_STATUS,
    FosAction.GET_DOCUMENTS: Intent.DOCUMENTS_UPLOADED,
    FosAction.GET_DOCUMENT_CHECKLIST: Intent.DOCUMENTS_REQUIRED,
    FosAction.GET_VERIFICATION_STATUS: Intent.DOCUMENT_VERIFICATION,
    FosAction.GET_PENDING_ITEMS: Intent.PENDING_ITEMS,
    FosAction.GET_NEXT_ACTION: Intent.NEXT_ACTION,
    FosAction.GET_CASE_360: Intent.FULL_SUMMARY,
    FosAction.CHECK_CPA_READINESS: Intent.READINESS,
}

#: The same actions as sentences.
#:
#: KEPT, AND NO LONGER ON THE ROUTING PATH. The phrase is what the audit
#: record and the response echo as the question that was asked, so a
#: trail written before this change and one written after it read the
#: same. It no longer decides anything.
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
    FosAction.LIST_CASES: "Show my cases.",
    FosAction.OPEN_CASE: "Open this case.",
    FosAction.EXIT_CASE: "Close this case.",
    FosAction.VIEW_DOCUMENT: "View this document.",
    FosAction.RAISE_QUERY: "Raise a query.",
    FosAction.MARK_QUERY_SENT: "Mark the query as sent.",
    FosAction.LIST_QUERIES: "Show the queries on this case.",
    FosAction.NEW_CASE: "Start a new case.",
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
    FosAction.LIST_CASES: "My cases",
    FosAction.OPEN_CASE: "Open a case",
    FosAction.EXIT_CASE: "Close the case",
    FosAction.VIEW_DOCUMENT: "View document",
    FosAction.RAISE_QUERY: "Raise a query",
    FosAction.MARK_QUERY_SENT: "Mark as sent",
    FosAction.LIST_QUERIES: "Track queries",
    FosAction.NEW_CASE: "New case",
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
    mobile_aadhaar_linked: bool | None = Field(
        None, description="LOS Sourcing screen: is the mobile Aadhaar-linked (yes / no). No Aadhaar number is taken.")


def _text(value: object) -> str | None:
    """A captured figure as the store holds it, or nothing at all."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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

    # -- affordability inputs ------------------------------------------
    #
    # ALL OPTIONAL AND NONE DEFAULTED. Affordability needs an instalment,
    # and an instalment needs all three of amount, tenure and rate. Where
    # one is absent the eligibility check names it and assesses nothing,
    # which is the honest outcome -- a default tenure would produce an
    # EMI that looks calculated and was invented.
    tenure_months: int | str | None = Field(
        None, examples=[36],
        description=(
            "Repayment period in months. Without it no instalment can be "
            "computed and eligibility reports EMI_INPUTS_MISSING."
        ),
    )
    interest_rate_pct: float | str | None = Field(
        None, examples=[12.5],
        description=(
            "Annual interest rate. Without it no instalment can be "
            "computed and eligibility reports EMI_INPUTS_MISSING."
        ),
    )
    declared_monthly_obligations: float | str | None = Field(
        None, examples=[8000],
        description=(
            "What the applicant says they already repay each month. "
            "DECLARED, and recorded as declared: whether a declared "
            "figure may be used in an affordability calculation is a "
            "policy decision, set in eligibility_config.yaml, and it is "
            "not accepted by default."
        ),
    )
    declared_monthly_income: float | str | None = Field(
        None, examples=[65000],
        description=(
            "What the applicant says they earn each month. DECLARED, and "
            "recorded as declared: it is never used as verified income. "
            "Optional; credit underwriting reports its comparison with the "
            "documented income as unavailable when it is absent."
        ),
    )
    property_value: float | str | None = Field(
        None, examples=[8000000],
        description=(
            "The property's value, for a secured product such as HOME_LOAN, "
            "where eligibility computes LTV. Recorded as DECLARED. Ignored "
            "for an unsecured product, where LTV is NOT_APPLICABLE."
        ),
    )
    property_type: str | None = Field(
        None, examples=["RESIDENTIAL"],
        description="RESIDENTIAL / COMMERCIAL / INDUSTRIAL -- selects the LTV limit for a secured product.")
    purchase_price: float | str | None = Field(
        None, examples=[7500000],
        description=("The agreed price of the property being bought (agreement to sell). With a valuation, the "
                     "LOWER of the two is the property value. Never an old sale deed's consideration."))
    valuation_value: float | str | None = Field(
        None, examples=[7800000],
        description="The value on the valuation report. Without one, a declared value gives an indicative LTV.")
    loan_purpose: str | None = Field(None, max_length=100, examples=["Business Expansion"],
                                     description="LOS Sourcing screen. Free text until the business list is configured.")
    applicant_type: str | None = Field(None, max_length=40, examples=["Individual"],
                                       description="LOS Sourcing screen. Free text until the business list is configured.")
    document_received_date: str | None = Field(None, max_length=10, examples=["2026-06-20"],
                                               description="LOS Application Details: YYYY-MM-DD, not in the future.")
    co_origination: bool | None = Field(None, description="LOS Application Details: co-origination yes / no. "
                                                          "Absent = not captured (never read as no).")


class CoApplicantDetails(BaseModel):
    """A co-applicant declared on the form (2026-10-22). Stored through the SAME intake as a document upload
    (co_applicants.record_intake: encrypted, on the case; access goes through the case). The co-applicant table has
    no mobile column, so none is taken."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(..., min_length=1, max_length=200)
    date_of_birth: str | None = Field(None, max_length=32)
    pan: str | None = Field(None, max_length=20)
    relationship: str | None = Field(None, max_length=40, description="To the applicant, e.g. SPOUSE (free text).")


class CreateCaseRequest(BaseModel):
    """One call: the applicant, their application, and the opened case."""

    applicant: ApplicantDetails
    application: ApplicationDetails | None = None
    co_applicants: list[CoApplicantDetails] = Field(
        default_factory=list,
        description="Optional co-applicants (at most product_flow.yaml case_form.co_applicants.max). Needs "
                    "LOS_COAPP_IDENTITY on with migration 0004 applied; otherwise refused, never dropped.")
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

    applicant_id: str | None = Field(
        None, max_length=128, examples=["APP-3D51FFAC6342"],
        description="Required, except in the case workspace (COPILOT_CASE_WORKSPACE) where an opened case "
                    "supplies it.")
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
    response_language: str | None = Field(
        None, max_length=12, examples=["mr"],
        description=(
            "THE LANGUAGE SELECTED IN THE FRONTEND (en, hi, hi-Latn, mr, mr-Latn, ...). AUTHORITATIVE: "
            "the answer is presented in it whatever language the question was typed in -- "
            "detection (lexicon / Lingua) is used only when this is omitted. Facts never change with "
            "the language; where no template covers a fact the English answer stands and "
            "`language_contract.localized` is false."),
    )
    reply_language: str | None = Field(
        None, max_length=16, examples=["en"],
        description=("The same as `response_language` (FOS plan section 2, the frontend's name for it). Either one "
                     "LOCKS every text of the reply -- answer, clarifications, buttons, refusals -- to that language."),
    )
    # 6i CASE ACTIONS (COPILOT_CASE_ACTIONS)
    document_id: str | None = Field(None, max_length=256, description="VIEW_DOCUMENT: the document to open.")
    query: dict[str, Any] | None = Field(None, description="RAISE_QUERY: the (edited) draft to send.")
    query_id: str | None = Field(None, max_length=128, description="MARK_QUERY_SENT: the query.")
    confirm: bool = Field(False, description="RAISE_QUERY: true only from the explicit Send button.")
    co_applicant_id: str | None = Field(
        None, max_length=128, examples=["COAPP-7F2A11C4D9E0"],
        description=(
            "Ask about one CO-APPLICANT by their id (LOS_COAPP_IDENTITY). Resolved through the case: "
            "allowed only when the caller may open the case it is on; otherwise the same 403 as any "
            "case the caller does not own."),
    )
    # MASTER SPEC section 8: the reply is {request_id, markdown, tts}; these drive it
    action_link: str | None = Field(
        None, max_length=600, examples=["action:open_case?id=CASE-852C"],
        description=("A link from the reply's markdown, posted back as is (`action:...` or `ask:...`). Mapped by the "
                     "server's action registry to the request it stands for; scope is re-checked like any request."))
    chat_id: str | None = Field(
        None, max_length=128, description="The chat this message belongs to (the server remembers its context).")
    new_chat: bool = Field(False, description="Start this chat fresh: nothing remembered from before.")
    stream: bool = Field(
        False, description=("Reply as Server-Sent Events (status lines while it works, then the answer) instead of "
                            "one JSON body. The same as sending `Accept: text/event-stream`. COPILOT_STREAMING."))
    with_sources: bool = Field(
        False, description=("Add `sources` [{type, document, section, title}] to a handbook answer: the handbook "
                            "file and section retrieval cited for it. Left out on case answers and refusals."))
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
    understanding: dict[str, Any] | None = Field(
        None,
        description=(
            "How the question was understood: the semantic frame (task, "
            "object, qualifiers, referents, stage, language), what each "
            "referent resolved to, which layer decided (FRAME / RULES / LLM) "
            "and whether the understanding model was called."
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
    # ---- the compact blocks a screen binds to ---------------------------
    #
    # ADDITIVE, ALL FOUR. Everything they contain is already elsewhere in
    # this response; they exist so a client does not have to assemble a
    # header out of six fields and a checklist, and so the one piece of
    # generated prose is clearly separated from the state it describes.
    summary: str | None = Field(
        None,
        description=(
            "One or two sentences describing where the case stands. "
            "Phrasing only: every fact in it is computed before any model "
            "is called, and `response_source` says whether a model wrote "
            "the words."
        ),
        examples=["The application is at basic document verification with "
                  "PAN verified and the bank statement under review."],
    )
    status: dict[str, Any] | None = Field(
        None,
        description="The stage and the application status, together.",
        examples=[{"stage": "BASIC_DOCUMENT_VERIFICATION",
                   "application_status": "BASIC_DOCUMENT_VERIFICATION"}],
    )
    processing_queue: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Documents still being read in the background, with the state "
            "of each. A document here is neither finished nor forgotten."
        ),
        examples=[[{"document_type": "BANK_STATEMENT", "status": "QUEUED"}]],
    )
    grounded: bool = Field(
        False,
        description=(
            "Whether the answer rests on stored records. False for a "
            "clarification or a refusal, which rest on nothing."
        ),
    )

    next_action: dict[str, Any] | None = None
    response_type: str | None = Field(
        None, description=(
            "What kind of answer this is, for the UI to pick a renderer: CONVERSATION, REFUSAL, "
            "CLARIFICATION, KNOWLEDGE_ANSWER, CASE_FACT, CASE_STATUS, DOCUMENT_CHECKLIST, "
            "DOCUMENT_STATUS, DOCUMENT_VERIFICATION_SELECTION, DOCUMENT_VERIFICATION_RESULT, "
            "KYC_RESULT, NEXT_ACTION, CASE_PORTFOLIO, PENDING_WORK, ACTION_RESULT, UPLOAD_RESULT, "
            "UPLOAD_VALIDATION, ROUTED, HANDOFF."))
    subject: dict[str, Any] | None = Field(
        None, description="Whose answer this is: {\"party\": PRIMARY_APPLICANT | CO_APPLICANT | BOTH | null}.")
    language: str | None = Field(None, description="The language the question was asked in (en, hi, hi-Latn, mr).")
    scope: str | None = Field(
        None, description=("What the answer is about: CASE (this application), APPLICANT (the person's "
                           "recorded details), APPLICANT_CASES (every case the caller may see), NONE "
                           "(a chat reply, a handbook answer or a refusal)."))
    language_contract: dict[str, Any] | None = Field(
        None, description=("How the question was read: `language`, `script`, `input_mode` (ENGLISH / "
                           "NATIVE_SCRIPT / ROMANIZED / CODE_MIXED), `confidence`, `response_language` "
                           "(asked for), `reply_language` (what the prose is really in), `localized`, "
                           "`provider`. Never the user's words."))
    portfolio: dict[str, Any] | None = Field(
        None, description=("Every case of the applicant the caller may see: `count`, `blocked`, `focus` "
                           "(LATEST / PREVIOUS / ATTENTION) and one row per case -- status, verification "
                           "counts, KYC, blockers, next step. Unauthorized cases are never counted."))
    gate: dict[str, Any] | None = Field(
        None, description=("The current stage's gate (app/config/stage_gates.yaml, criteria UNCONFIRMED): "
                           "`status` PASS / REVIEW / BLOCKED / NOT_READY / CONFIGURATION_GAP, one `checks` "
                           "entry per criterion with the recorded value, `reason_code`, `evidence` and "
                           "`next_action`, and `moved_to` when an authorized move was made."))
    pending_work: dict[str, Any] | None = Field(
        None, description=("What is left on the case: one item per document / slot / KYC check with its "
                           "`state` and `owner` (USER, SYSTEM, REVIEWER, PROCESSING, COMPLETED), a "
                           "`summary`, and -- after \"do what's pending\" -- what was `executed`, read "
                           "back from the store."))
    processing: dict[str, Any] | None = Field(
        None, description=("A verification run: documents verified now, reused from the record, or still "
                           "PROCESSING with their job ids -- poll GET_VERIFICATION_STATUS for the rest."))
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
    semantic_decisions: dict[str, Any] | None = Field(
        None, description=(
            "JEV typed decisions on this case (the semantic decision layer): "
            "`semantic_decisions[]` with decision_type, answer, confidence, confidence_band, "
            "probabilities, severity, recommended_action, status; `semantic_actions[]` "
            "with what code executed (EXECUTED / BLOCKED / DEFERRED / SKIPPED); `jev_status` "
            "COMPLETED / CONFIGURATION_GAP / EXTERNAL_DEPENDENCY_REQUIRED / NOT_EVALUATED / "
            "SCHEDULED. Never changes a KYC, eligibility, credit or risk status "
            "(`authoritative_statuses_changed` is always false)."))
    presentation: dict[str, Any] | None = Field(
        None,
        description=(
            "THE CANONICAL RESPONSE CONTRACT -- present on every route (typed question, dropdown read, "
            "upload) with the same shape. DERIVED from the fields above; nothing in it is model-written, "
            "and it carries no internal ids, reason codes, scores or provider names.\n\n"
            "- `message` the prose answer minus list lines; `intent`; `status` one of ACTION_REQUIRED / "
            "PROCESSING / REVIEW / OK / INFO; `next_step` (or null); `case_context` {case_id, applicant_id, stage}\n"
            "- `sections[]` {title, items[{icon, label, status}]} -- the lists in the answer, structured\n"
            "- `documents[]` {party, label, document_type, icon, status, state (VERIFIED / REVIEW / REJECTED / "
            "PROCESSING), reason, action, action_required}; SUPERSEDED documents are never listed\n"
            "- `document_groups[]` {party, documents[]} -- grouped by applicant / co-applicant\n"
            "- `actions[]` {label, action} (alias `next_actions`)\n"
            "- `knowledge_sources[]` {title, source, type, version, effective_date} (alias `citations`) -- "
            "provenance is here, never in the prose\n"
            "- `semantic_decisions[]` {type, severity, subject, confidence, recommended_action, source: JEV, "
            "acted_upon} -- ADVISORY; never changes a document, KYC, eligibility or credit result\n"
            "- `metadata` {request_id, language, response_source}"),
    )
    observability: dict[str, Any] | None = Field(
        None, description="Why this answer: the capability and intent selected, the model "
                          "consulted (if any), the tools called and latency by component. "
                          "Codes, counts and milliseconds only -- never a value.")


def _co_applicant_rows(request: "CreateCaseRequest", request_id: str) -> list[dict[str, Any]]:
    """The declared co-applicants, validated with the case form's own rules (case_form.check; PAN by identity
    rules); 422 INVALID_FIELDS naming co_applicants[i].field. More than the configured maximum, or co-applicant
    identity not enabled: refused -- a declared co-applicant is never silently dropped."""
    rows = list(request.co_applicants or [])
    if not rows:
        return []
    from app.agents.applicant.copilot.capabilities import case_form as _cf
    from app.agents.dedupe import identity as _ident
    from app.agents.los import co_applicants as _co

    limit = int(((_cf._cfg().get("co_applicants") or {}).get("max")) or 0)
    if not _co.enabled():
        raise HTTPException(409, detail={"request_id": request_id, "error": "CO_APPLICANTS_NOT_ENABLED",
                                         "message": "Co-applicants cannot be stored here (co-applicant identity is "
                                                    "off or its migration is not applied)."})
    if limit and len(rows) > limit:
        raise HTTPException(422, detail={"request_id": request_id, "error": "INVALID_FIELDS",
                                         "fields": {"co_applicants": f"At most {limit} co-applicant(s)."}})
    errors: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for i, co in enumerate(rows):
        name, why = _cf.check("full_name", co.full_name)
        if why:
            errors[f"co_applicants[{i}].full_name"] = why
        dob = None
        if co.date_of_birth:
            dob, why = _cf.check("date_of_birth", co.date_of_birth)
            if why:
                errors[f"co_applicants[{i}].date_of_birth"] = why
        pan = None
        if co.pan:
            pan = _ident.normalise("PAN", co.pan)
            if not pan:
                errors[f"co_applicants[{i}].pan"] = _cf._why("pan", _cf._lang())
        out.append({"profile": {"name": name, "date_of_birth": dob, "pan_number": pan},
                    "relationship": (co.relationship or "").strip() or None})
    if errors:
        raise HTTPException(422, detail={"request_id": request_id, "error": "INVALID_FIELDS", "fields": errors})
    return out


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
        "understanding": None,
        "observability": None,
        "pending_items": [], "verification": None, "kyc": None,
        "knowledge": None, "category": "CASE_ONLY", "next_action": None,
        "readiness": None, "actions": [], "route_to": None,
        "response_source": "STRUCTURED", "processing_ms": 0.0, "errors": [],
        # The compact blocks. Present on every action, like everything
        # else here, so a client binds one shape.
        "summary": None, "status": None, "processing_queue": [],
        "grounded": False,
        # THE RENDERING CONTRACT (copilot/answering/structured.py): what kind
        # of answer, whose, in which language, and a verification run.
        "response_type": None, "subject": None, "language": None, "processing": None,
        "portfolio": None, "scope": None, "language_contract": None,
        # what is left on the case and who moves each item (capabilities/work.py)
        "pending_work": None,
        # the current stage's gate, evaluated from recorded results (capabilities/gates.py)
        "gate": None,
        # JEV typed decisions (app/jev): attached by the copilot endpoint
        "semantic_decisions": None, "presentation": None,
    }
    base.update(overrides)
    return base


#: Upload refusals as codes a frontend can switch on (the gate's own codes, and
#: the file validation's messages mapped onto the same vocabulary).
_UPLOAD_REFUSAL_WORDS = {
    "EMPTY_FILE": "the file is empty", "FILE_TOO_LARGE": "the file is too large",
    "UNSUPPORTED_FILE_TYPE": "that file type isn't supported",
    "FILE_SIGNATURE_MISMATCH": "the file's content doesn't match its type",
    "MALICIOUS_CONTENT": "it isn't a document file",
}


def _upload_refusal_code(message: str) -> str:
    head = message.split(":", 1)[0].strip()
    if head in _UPLOAD_REFUSAL_WORDS:
        return head
    lowered = message.lower()
    if "unsupported file type" in lowered:
        return "UNSUPPORTED_FILE_TYPE"
    if "exceeds" in lowered or "too large" in lowered:
        return "FILE_TOO_LARGE"
    if "empty" in lowered:
        return "EMPTY_FILE"
    return "INVALID_UPLOAD"


def _required_slots(checklist: list[dict[str, Any]]) -> list[str]:
    return [e["slot"] for e in checklist if e.get("mandatory", True)]


# ==========================================================================
# API 1 -- CREATE APPLICANT + APPLICATION
# ==========================================================================

#: The fields a product's application uses beyond the common ones (property / LTV inputs).
_PROPERTY_FIELDS = ("property_type", "property_value", "purchase_price", "valuation_value")

#: /docs examples, ONE PER PRODUCT, each with only that product's fields.
def _create_texts() -> dict[str, Any]:
    """The create-case messages and example titles (applicant_agent.yaml chatbot.create_case)."""
    from app.agents.applicant import config as _config

    return _config.chatbot("create_case")


_CREATE_CASE_EXAMPLES = {
    "personal_loan": {
        "summary": _create_texts()["examples"]["personal_loan"],
        "value": {"applicant": {"full_name": "Rahul Sharma", "mobile": "9876543210", "date_of_birth": "1990-04-12",
                                "address": "Pune, Maharashtra"},
                  "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000, "tenure_months": 60,
                                  "employment_type": "SALARIED", "declared_monthly_obligations": 10000}}},
    "home_loan": {
        "summary": _create_texts()["examples"]["home_loan"],
        "value": {"applicant": {"full_name": "Rahul Sharma", "mobile": "9876543210", "date_of_birth": "1990-04-12",
                                "address": "Pune, Maharashtra"},
                  "application": {"product": "HOME_LOAN", "loan_amount": 2000000, "tenure_months": 240,
                                  "employment_type": "SALARIED", "declared_monthly_obligations": 0,
                                  "property_type": "RESIDENTIAL", "valuation_value": 3000000,
                                  "purchase_price": 2800000}}},
    "loan_against_property": {
        "summary": _create_texts()["examples"]["loan_against_property"],
        "value": {"applicant": {"full_name": "Rahul Sharma", "mobile": "9876543210", "date_of_birth": "1990-04-12",
                                "address": "Pune, Maharashtra"},
                  "application": {"product": "LOAN_AGAINST_PROPERTY", "loan_amount": 3000000, "tenure_months": 120,
                                  "employment_type": "SELF_EMPLOYED", "declared_monthly_obligations": 5000,
                                  "property_type": "COMMERCIAL", "valuation_value": 8000000}}},
}



def _already_created(case_id: str) -> str:
    """product_flow.yaml case_form.already_created."""
    from app.agents.applicant.copilot.capabilities import case_form, product_flow

    return product_flow.say((case_form._cfg().get("already_created") or {}), case_form._lang(), case_id=case_id)

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
async def create_case_route(
    response: Response,
    request: CreateCaseRequest = Body(..., openapi_examples=_CREATE_CASE_EXAMPLES),
    claims: dict[str, Any] = Depends(require_jwt),
    idempotency_key: str | None = Header(
        None, alias="Idempotency-Key", max_length=128,
        description="Optional. A retry with the same key returns the case the first call created (never a second "
                    "case); the reply then carries `Idempotent-Replayed: true`."),
):
    # WHAT IT IS GIVEN IS VALIDATED (2026-10-13; a real run accepted a loan amount "lots", product "CAR_LOAN",
    # mobile "123" ...): the form / chat rules (case_form.check) for every field sent, and the PRODUCT's own fields
    # (from its eligibility policy: no property fields for an unsecured loan; the property types its LTV grid knows)
    errors = _new_case_errors(request)
    if errors:
        raise HTTPException(422, detail={"request_id": f"fos_{uuid.uuid4().hex}", "error": "INVALID_FIELDS",
                                         "fields": errors})
    return await create_case(request, claims, channel="ui", idempotency_key=idempotency_key, response=response)


def _new_case_errors(request: "CreateCaseRequest") -> dict[str, str]:
    """{field: why} for a new case: the form rules for every field sent, the name always, and the product's own
    fields (its eligibility policy decides: property fields only where LTV applies, the property types its grid
    knows, a property type required where the grid needs one and names no default)."""
    from app.agents.applicant.copilot.capabilities import case_form as _form

    from app.agents.applicant import plausibility

    _texts = _create_texts()
    errors: dict[str, str] = {}
    applicant = request.applicant.model_dump() if request.applicant else {}
    name = str(applicant.get("full_name") or "").strip()
    if len(name) < 2 or not re.search(r"[A-Za-zऀ-ॿ]", name):
        errors["full_name"] = _texts["name_required"]
    for field in ("mobile", "email", "date_of_birth", "address"):
        if applicant.get(field) not in (None, ""):
            _, why = _form.check(field, applicant[field], "en")
            if why:
                errors[field] = why
    if request.application is None:
        return errors
    application = request.application.model_dump()
    # an amount the intake plausibility check refuses ("5", "5 lakh") keeps ITS answer (AMOUNT_IMPLAUSIBLE, with the
    # rupees example) -- create_case raises it; it is not re-reported here as INVALID_FIELDS
    implausible = plausibility.problem(request.application.model_dump(exclude_none=True))
    skip = {implausible[0]} if implausible else set()
    for field in ("product", "loan_amount", "employment_type", "tenure_months", "interest_rate_pct",
                  "declared_monthly_income", "declared_monthly_obligations", "property_value"):
        if field not in skip and application.get(field) not in (None, ""):
            _, why = _form.check(field, application[field], "en")
            if why:
                errors[field] = why
    for field in ("purchase_price", "valuation_value"):
        value = application.get(field)
        if value not in (None, ""):
            try:
                if float(str(value).replace(",", "").strip()) <= 0:
                    raise ValueError
            except ValueError:
                errors[field] = _texts["amount_above_zero"]
    product = str(application.get("product") or "").strip().upper()
    if not product or "product" in errors:
        return errors
    from app.agents.eligibility import config as _eligibility_config

    rules = _eligibility_config.product_terms(product)
    if rules is None:
        return errors                   # no eligibility terms for the product: its checklist decides, not this
    given = [f for f in _PROPERTY_FIELDS if application.get(f) not in (None, "")]
    if not rules.ltv_enabled:
        for field in given:
            errors[field] = _texts["not_used_for"].format(product=product)
        return errors
    known = set(rules.property_types)
    if rules.ltv_default_property_type:
        known.add(rules.ltv_default_property_type)
    kind = str(application.get("property_type") or "").strip().upper()
    if kind and known and kind not in known:
        errors["property_type"] = _texts["property_type_one_of"].format(product=product, types=", ".join(sorted(known)))
    elif not kind and known and not rules.ltv_default_property_type:
        errors["property_type"] = _texts["property_type_required"].format(product=product, types=", ".join(sorted(known)))
    return errors


async def create_case(request: CreateCaseRequest, claims: dict[str, Any], *, channel: str = "ui",
                      idempotency_key: str | None = None, response: Response | None = None):
    """
    THE one create path, CONCURRENCY-SAFE (app/agents/applicant/create_guard.py; Phase A hardening 2026-10-22):
    serialised per supplied id / declared mobile + product / idempotency key (database-wide advisory locks, no schema
    change), so a supplied id is never overwritten by a simultaneous create; an Idempotency-Key retry returns the
    case the first call created; a same-mobile same-product case is recorded as a possible duplicate, not blocked
    (the business duplicate window is not approved).
    """
    from app.agents.applicant import create_guard

    caller = Caller.from_claims(claims)
    idem_ref = create_guard.idempotency_ref(caller.subject, idempotency_key)
    details = request.application or ApplicationDetails()
    # the body's fingerprint: a key reused for a DIFFERENT body is refused (409), never replayed as the wrong case
    body_print = create_guard.fingerprint(request.model_dump(mode="json")) if idem_ref else None
    keys = create_guard.keys_for(applicant_id=request.applicant_id, case_id=request.case_id,
                                 mobile=request.applicant.mobile, product=details.product, idem_ref=idem_ref)
    try:
        async with create_guard.serialised(keys):
            replay = create_guard.find_replay(idem_ref, body_print)
            if replay is not None:
                if response is not None:
                    response.headers["Idempotent-Replayed"] = "true"
                return await _replayed_case(*replay)
            # A SUPPLIED case id that exists is refused BEFORE the applicant is written: the unguarded path created
            # the applicant first and then failed on the case, leaving an orphan applicant behind. Same answer as the
            # tool's own refusal (400 INVALID_INPUT), now checked under the lock, so it holds for simultaneous calls.
            if request.case_id and request.case_id.strip():
                from app.store import get_repository

                if get_repository().get_application(request.case_id.strip()) is not None:
                    raise HTTPException(400, detail={"request_id": f"fos_{uuid.uuid4().hex}", "error": "INVALID_INPUT",
                                                     "message": f"Application {request.case_id.strip()} already "
                                                                "exists."})
            out = await _create_case_unguarded(request, claims, channel=channel)
            case_id = out.get("case_id")
            if case_id:
                create_guard.remember(case_id, idem_ref, body_print)
                create_guard.note_possible_duplicate(case_id, create_guard.possible_duplicates(
                    mobile=request.applicant.mobile, product=details.product, exclude_case=case_id))
            return out
    except create_guard.KeyReused:
        raise HTTPException(409, detail={"request_id": f"fos_{uuid.uuid4().hex}", "error": "IDEMPOTENCY_KEY_REUSED",
                                         "message": "This Idempotency-Key was already used for a different case "
                                                    "request; send a new key for a new case."}) from None
    except create_guard.CreateBusy:
        raise HTTPException(503, detail={"request_id": f"fos_{uuid.uuid4().hex}", "error": "CREATE_BUSY",
                                         "message": "The same case is being created by another request; retry "
                                                    "shortly."}) from None


async def _replayed_case(case_id: str, applicant_id: str) -> dict[str, Any]:
    """The case an earlier call with the same Idempotency-Key created, in the create response's envelope."""
    from app.mcp import applicant as tools

    request_id = f"fos_{uuid.uuid4().hex}"
    view = await tools.applicant_360(case_id)
    if not view.ok:
        _raise_from(request_id, view)
    result = view.result or {}
    checklist = result.get("checklist") or []
    return _blank(
        request_id, applicant_id=applicant_id, case_id=case_id, action="CREATE_CASE", intent="CREATE_CASE",
        answer=_already_created(case_id),
        applicant=result.get("applicant"), application=result.get("application"), stage=result.get("stage"),
        documents=result.get("documents") or [], checklist=checklist, required_documents=_required_slots(checklist),
        policy=result.get("policy"), pending_items=result.get("pending_items") or [],
        next_action=result.get("next_action"), readiness=result.get("readiness"), query_type="CASE_FACT",
        **_frontend_contract({"query_type": "CASE_FACT"}, {
            "applicant": result.get("applicant"), "application": result.get("application"),
            "stage": result.get("stage"), "documents": result.get("documents") or [], "checklist": checklist,
            "readiness": result.get("readiness"), "policy": result.get("policy")}),
    )


async def _create_case_unguarded(request: CreateCaseRequest, claims: dict[str, Any], *, channel: str = "ui"):
    """THE one create path: the UI form (create_case_route) and the chat (case_form, after Confirm) both use it --
    always through create_case (serialised), never directly."""
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

    # CO-APPLICANTS: checked BEFORE anything is written, with the form's own rules
    co_rows = _co_applicant_rows(request, request_id)

    # ALL AMOUNTS ARE IN RUPEES (FOS plan 1.1): an implausible amount is refused BEFORE the applicant is
    # created, so a bad form never leaves a half-made case behind
    if request.application is not None:
        from app.agents.applicant import plausibility

        found = plausibility.problem(request.application.model_dump(exclude_none=True))
        if found is not None:
            raise HTTPException(422, detail={"request_id": request_id, "error": plausibility.CODE,
                                             "field": found[0], "message": found[1]})

    # ANOTHER CASE FOR A CUSTOMER THIS CALLER ALREADY SERVES. One applicant may
    # hold several cases; naming an applicant the caller may WRITE opens a new
    # case for them, and the recorded person is left exactly as captured (never
    # re-created, never overwritten from this request). Any other applicant id
    # is refused below exactly as before.
    existing_applicant = None
    if request.applicant_id:
        from app.security import access as _existing_access
        from app.store import get_repository as _existing_repo

        try:
            _existing_access.authorize(caller.subject, caller.scopes,
                                       applicant_id=request.applicant_id, write=True)
            existing_applicant = _existing_repo().get_applicant(request.applicant_id)
        except Exception:  # noqa: BLE001 - not the caller's: the create below refuses it
            existing_applicant = None
    if existing_applicant is not None:
        applicant_id = existing_applicant.applicant_id
    else:
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
        tenure_months=_text(application_details.tenure_months),
        interest_rate_pct=_text(application_details.interest_rate_pct),
        declared_monthly_obligations=_text(
            application_details.declared_monthly_obligations),
        property_value=_text(application_details.property_value),
        declared_monthly_income=_text(application_details.declared_monthly_income),
        property_type=(str(application_details.property_type).strip().upper() or None)
        if application_details.property_type else None,
        purchase_price=_text(application_details.purchase_price),
        valuation_value=_text(application_details.valuation_value),
        loan_purpose=((application_details.loan_purpose or "").strip() or None),
        applicant_type=((application_details.applicant_type or "").strip() or None),
        document_received_date=((application_details.document_received_date or "").strip() or None),
        co_origination=application_details.co_origination,
    )
    if not application.ok:
        _raise_from(request_id, application)
    case_id = application.result["application"]["case_id"]

    # WHAT THIS CALLER CREATED, THIS CALLER OWNS -- the grant every later
    # read and write of this applicant and case is checked against.
    from app.security import access as _access
    _access.record_ownership(caller.subject, applicant_id=applicant_id,
                             case_id=case_id)

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
                 write=True, confirmed=True, status="OK", detail=f"channel={channel}")
    # the activity log (MASTER SPEC 15.4): who created it, and where
    from app.agents.applicant.copilot.capabilities import case_form as _activity

    _activity.activity(case_id, "CASE_CREATED", f"case created by {caller.subject} ({channel})")
    if co_rows:
        from app.agents.los import co_applicants as _co

        for row in co_rows:     # the same intake a document upload uses; no grant on the co-applicant id
            _co.record_intake(case_id, applicant_id, _co.generate_id(), row["profile"], row["relationship"])
        _activity.activity(case_id, "CO_APPLICANTS_ADDED", f"{len(co_rows)} co-applicant(s) declared ({channel})")

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
                "ask_streamed": {
                "summary": "CUSTOM_QUERY, streamed (SSE) — the same call with stream: true",
                "value": {
                "applicant_id": "APP-3D51FFAC6342",
                "case_id": "CASE-7DFE2F497522",
                "action": "CUSTOM_QUERY",
                "message": "What documents are pending?",
                "stream": True,
                    },
                },
                "feedback": {
                "summary": "FEEDBACK — 👍 / 👎 on an earlier answer (its request_id)",
                "value": {
                "action": "FEEDBACK",
                "reply_to": "fos_0123456789abcdef0123456789abcdef",
                "rating": "down",
                "reason": "wrong document named",
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


#: The upload half of the facade's body, on its own.
#:
#: DERIVED, NOT COPIED. `POST /api/v1/fos/documents` accepts exactly
#: what `/copilot` accepts for an upload because it IS the same handler;
#: a second hand-written schema would be a second thing to keep in step,
#: and the one that drifted would be the one nobody was testing.
_UPLOAD_BODY = {
    "required": True,
    "content": {
        "multipart/form-data":
            _COPILOT_BODY["content"]["multipart/form-data"],
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
        "**Streaming:** `\"stream\": true` (or `Accept: text/event-stream`) returns the same answer as "
        "Server-Sent Events. **Feedback:** `{\"action\": \"FEEDBACK\", \"reply_to\": <request_id>, "
        "\"rating\": \"up\"|\"down\", \"reason\"?}` records a 👍 / 👎 on an earlier answer and returns "
        "`{request_id, status: RECORDED | NOT_RECORDED, reply_to, rating}`.\n\n"
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
    request_id = getattr(request.state, "request_id", None) or f"fos_{uuid.uuid4().hex}"
    content_type = (request.headers.get("content-type") or "").lower()
    from app.agents.applicant.copilot.capabilities import qwen_agent as _hist_agent

    _hist_agent.HISTORY_READ.set(None)          # the turn log reports only what THIS turn read as history
    from app.llm import turn_budget as _turn_budget

    _turn_budget.begin()                        # one failed model call ends the model for this turn (llm/turn_budget)
    # MASTER SPEC section 11: the same Idempotency-Key from the same caller returns the stored reply, never a rerun
    from app.agents.applicant.copilot.answering import contract as _contract_rt
    from app.agents.applicant.copilot.answering import realtime as _realtime
    from app.security.auth import get_subject as _rt_subject

    _idem = (request.headers.get("idempotency-key") or "").strip()[:128] or None
    if _idem and _contract_rt.enabled():
        _again = _realtime.idempotent(str(_rt_subject(claims) or "anonymous"), _idem)
        if _again is not None:
            return _again
    request.state.idempotency_key = _idem

    # ONE API (2026-10-18): a 👍 / 👎 and a streamed reply come through this same route
    if not content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        try:
            _body = await request.json()
        except Exception:  # noqa: BLE001 - _copilot_json reports an unreadable body itself
            _body = None
        if isinstance(_body, dict) and _body.get("action") == "FEEDBACK":
            return _copilot_feedback(_body, claims, request_id)
        if not getattr(request.state, "streaming", False) and (
                (isinstance(_body, dict) and _body.get("stream") is True)
                or "text/event-stream" in (request.headers.get("accept") or "").lower()):
            return await _stream_reply(request, claims)

    try:
        # Both form encodings reach the upload handler. A caller who posts
        # `action=UPLOAD_DOCUMENT` as a urlencoded form has forgotten the
        # files, not sent a malformed body, and should be told which -- the
        # JSON path could only report that it failed to parse.
        if content_type.startswith(("multipart/form-data",
                                    "application/x-www-form-urlencoded")):
            uploaded = await _copilot_upload(request, claims, request_id)
            if isinstance(uploaded, dict):
                # JEV runs AFTER the upload, on the persisted evidence -- never inside it.
                from app.jev import config as _jev_config

                uploaded["semantic_decisions"] = {
                    "jev_status": ("SCHEDULED" if _jev_config.enabled()
                                   and _jev_config.trigger_enabled("document_processed") else "DISABLED"),
                    "semantic_decisions": [], "semantic_actions": [],
                    "poll": f"/api/v1/jev/cases/{uploaded.get('case_id')}/decisions"}
                # MASTER SPEC sections 8 / 9: the upload's answer (smart upload included) is a chat reply too;
                # the case is watched, so its verification result is pushed by /copilot/updates
                from app.security.auth import get_subject as _up_subject

                request.state.chat_key = (str(_up_subject(claims) or "anonymous"),
                                          request.query_params.get("chat_id"))
                return _published_reply(request, uploaded)
            return uploaded
        # THE SAME PUBLISHING RULE AS THE UNIVERSAL COPILOT: no full PAN,
        # Aadhaar or account number anywhere in the published response --
        # including a number typed into a free-text field of a record
        # (sensitivity.mask_payload; record ids are left untouched).
        from app.security import sensitivity as _sensitivity

        import time as _tl_time

        _tl_started = _tl_time.perf_counter()
        published = await _copilot_json(request, claims, request_id)
        try:
            # THE TURN LOG (observability/turn_log.py): masked, no answer text; resolved questions -> labelled rows
            from app.observability import turn_log as _turn_log

            if _turn_log.enabled() and isinstance(published, dict):
                _tl_typed = str(getattr(request.state, "typed_message", "") or ((await request.json()) or {}).get(
                    "message") or "")
                _turn_log.record(chat_key=_agent_chat_key(request), typed=_tl_typed,
                                 fixed=getattr(request.state, "copilot_message", None), reply=published,
                                 meaning_intent=getattr(request.state, "meaning_intent", None),
                                 layer=getattr(request.state, "answer_layer", None), started=_tl_started,
                                 request_id=request_id, subject=str(_rt_subject(claims) or "anonymous"))
        except Exception:  # noqa: BLE001 - the log is a convenience, never the answer
            logger.debug("turn log: skipped", exc_info=True)
        if isinstance(published, dict) and str(published.get("response_source") or "") != "AGENT":
            try:
                from app.agents.applicant.copilot.capabilities import qwen_agent as _mem_agent

                _asked = str(((await request.json()) or {}).get("message") or "")
                if _asked and published.get("answer"):
                    _mem_agent.remember(_agent_chat_ref(request), _asked, str(published.get("answer")), request_id)
            except Exception:  # noqa: BLE001 - the history is a convenience, never the answer
                logger.debug("qwen agent: history not recorded", exc_info=True)
        if isinstance(published, dict) and published.get("atomic"):
            return _published_reply(request, published)         # a blocked message: the warning, untouched
        if isinstance(published, dict):
            # THE TYPED DECISIONS, as recorded -- read, never re-evaluated here.
            # The case was authorized by the action that produced `published`.
            # Only on an answer ABOUT THIS CASE's state (CASE_FACT / DOCUMENT_STATUS /
            # MIXED): a refusal, a greeting or an off-topic turn reads nothing, and
            # this must not add a read.
            if (published.get("case_id") and not published.get("route_to")
                    and published.get("query_type") in {"CASE_FACT", "DOCUMENT_STATUS", "MIXED"}):
                from app.jev import engine as _jev_engine

                # NULL WHEN JEV NEVER EVALUATED THE CASE -- the same value every
                # other read of this envelope publishes (the facade's GET routes),
                # so one shape never depends on which route served it
                _latest = _jev_engine.latest_decisions(published["case_id"])
                published["semantic_decisions"] = None if _latest.get("jev_status") == "NOT_EVALUATED" else _latest
                # The copilot SAYS what was flagged, in code-written words, on the
                # answers about review and status -- never as a verdict.
                if str(published.get("intent") or "").upper() in {
                        "KYC_RESULT", "APPLICATION_STATUS", "CASE_HISTORY", "NEXT_ACTION", "READINESS"}:
                    said = _jev_engine.sentence(published["semantic_decisions"] or {})
                    if said and said not in str(published.get("answer") or ""):
                        published["answer"] = f"{str(published.get('answer') or '').rstrip()} {said}".strip()
            # AN EMPTY CHECKLIST NEVER HIDES AN OPEN REVIEW (answering/attention.py)
            from app.agents.applicant.copilot.answering import attention as _attention
            from app.agents.applicant.copilot.answering import document_actions as _doc_actions

            _diagnose = bool(getattr(request.state, "verify_diagnose", False))
            if ((_doc_actions.enabled() or _diagnose) and published.get("case_id")
                    and str(published.get("intent") or "") in {"DOCUMENTS_PENDING", "DOCUMENTS_MISSING",
                                                               "PENDING_ITEMS"}):
                # ONLY WHAT NEEDS ACTION, from the store (COPILOT_DOCUMENT_ACTIONS, default off)
                _view = _doc_actions.build(published["case_id"], party=_attention._asked_party(published))
                _lang = (published.get("language_contract") or {}).get("reply_language") \
                    if isinstance(published.get("language_contract"), dict) else None
                _said = _doc_actions.render(_view, _lang)
                if _diagnose and not any(_view.get(k) for k in ("reupload", "pending", "under_review", "kyc_issues")):
                    _said = {**_said, "answer": _doc_actions.all_done_text()}      # 6f: nothing left to do
                published["document_actions"] = {**_view, "emphasis": _said["emphasis"]}
                if _diagnose:
                    published["verify_diagnose"] = True
                _other = [i for i in published.get("pending_items") or []
                          if isinstance(i, dict) and i.get("type") not in (None, "DOCUMENT", "INFO")]
                if not _other:
                    published["answer"] = _said["answer"]
                else:
                    # a pending applicant / application detail keeps the original answer, which names it
                    published = _attention.amend(published)
            else:
                published = _attention.amend(published)
            # RESPONSE STYLE + ABBREVIATION RULE (COPILOT_RESPONSE_STYLE, default off; answering/style.py):
            # status emoji, one 👉 next step, first mention of a glossary term expanded (per session)
            from app.agents.applicant.copilot.answering import style as _style
            from app.agents.applicant.copilot.capabilities import safety as _safety_out

            if _safety_out.enabled():
                published = _safety_out.mask_output(published)     # 6h: an address keeps only its last part
            if _style.enabled():
                published = _styled(published, claims)
            # INSIDE AN OPENED CASE (6-MVP): the "📍 CASE-xxx" line goes first, after every rewrite above
            from app.agents.applicant.copilot.capabilities import workspace as _ws_header

            from app.agents.applicant.copilot.capabilities import general as _hdr_general

            published = _ws_header.in_case_header(
                published, _hdr_general._state(claims, getattr(request.state, "copilot_context", None)))
            # THE 1-2 KEY WORDS, BOLD (COPILOT_EMPHASIS, default off; answering/emphasis.py):
            # emphasis + answer_markdown + clean answer_plain; `answer` itself unchanged
            from app.agents.applicant.copilot.answering import emphasis as _emphasis

            if _emphasis.enabled():
                published = _emphasis.apply(published)
            # WHOSE ANSWER, BY ID (LOS_COAPP_IDENTITY): 👥 **Co-applicant: <name> (<id>)**
            published = _co_applicant_header(request, published)
            # THE STRUCTURED CONTRACT beside the prose, derived from it (presentation.py).
            from app.agents.applicant.copilot.answering import presentation as _presentation

            published["presentation"] = _presentation.build(published)
            # THE KYC TABLE on a KYC answer (FOS plan section 4; shared with /copilot/query)
            from app.agents.applicant.copilot.answering import kyc_table as _kyc_table

            published = _kyc_table.attach(published)
            # THE READINESS REPORT on a readiness answer (FOS plan sections 5 / 7.1; shared with /copilot/query)
            from app.agents.applicant.copilot.answering import readiness_report as _readiness_report

            published = _readiness_report.attach(published)
            # COUNT QUESTIONS, counted from the store (FOS plan 6.6; shared with /copilot/query)
            from app.agents.applicant.copilot.answering import counts as _counts

            published = _counts.attach(published, str(getattr(request.state, "copilot_message", "") or ""))
            # TIME QUESTIONS: the dated timeline, days in stage, "kal wala" (FOS plan 7.3; shared)
            from app.agents.applicant.copilot.capabilities import timeline as _timeline

            # the TYPED words too: a rewrite ("which stage is this case in?") can drop "since when"
            published = _timeline.attach(published, " || ".join(
                str(getattr(request.state, k, "") or "") for k in ("copilot_typed", "copilot_message")))
            # "why is it stuck" / "how long till approval": the readiness items, compact (config phrases)
            published = _readiness_report.attach_asked(published, " || ".join(
                str(getattr(request.state, k, "") or "") for k in ("copilot_typed", "copilot_message")))
            # THE CPA HANDOFF NOTE, only for a ready case (FOS plan 7.1; shared with /copilot/query)
            from app.agents.applicant.copilot.answering import handoff_note as _handoff_note

            published = _handoff_note.attach(published, str(getattr(request.state, "copilot_message", "") or ""),
                                             claims)
            # OFFICER TOOLS: what-if, review on open, status tables, customer message, visit checklist (FOS plan 9b)
            from app.agents.applicant.copilot.answering import officer_tools as _officer_tools

            published = _officer_tools.attach(published, str(getattr(request.state, "copilot_message", "") or ""))
            # PROFESSIONAL FORMAT, LAST (FOS plan section 3; shared with /copilot/query): no emojis anywhere,
            # one "Next step:" line, a plain-text answer
            from app.agents.applicant.copilot.answering import professional as _professional
            from app.agents.applicant.copilot.capabilities import faq as _faq_unknown

            # an FAQ-style question with no answer and no data: "I don't know that yet" (15.2 step 2)
            published = _faq_unknown.apply_unknown(published, str(getattr(request.state, "copilot_message", "") or ""))
            from app.agents.applicant.copilot.answering import case_brief as _why

            published = _why.why_fallback(published)          # "kyu?" with nothing recorded: the real blocker
            # "Do you mean X or Y?" when the router's best two are close; the second miss in a row -> the closest FAQ
            # questions + the supervisor line (capabilities/general.py)
            from app.agents.applicant.copilot.answering import language_lock as _after_lock
            from app.agents.applicant.copilot.capabilities import general as _general_after

            published = _general_after.after(published, str(getattr(request.state, "copilot_message", "") or ""),
                                             claims, getattr(request.state, "copilot_context", None),
                                             _after_lock.current() or "en")
            # golden rule 4, said first: a stage move / approval asked in chat is never done here
            from app.agents.applicant.copilot.capabilities import product_flow as _stage_note

            published = _stage_note.stage_move_note(published,
                                                    str(getattr(request.state, "copilot_message", "") or ""))
            # REPLY POLISH (answering/polish.py): the model may REWORD the prose answer -- checked, else unchanged
            from app.agents.applicant.copilot.answering import polish as _polish

            published = await _polish.apply(_professional.apply(_sensitivity.mask_payload(published)),
                                            question=str(getattr(request.state, "asked_message", "")
                                                         or getattr(request.state, "copilot_message", "") or ""))
            return _published_reply(request, published)
        return _published_reply(request, published)
    except HTTPException:
        raise
    except AgentError as exc:
        raise HTTPException(exc.http_status, detail={
            "request_id": request_id, "error": exc.code, "message": exc.message,
        }) from exc
    except Exception as exc:
        logger.exception("FOS copilot failed request_id=%s", request_id)
        from app.api.errors import classify as _classify

        status_code, code, message = _classify(exc)       # DB down -> 503, a timeout -> 504 (FOS plan 9.4)
        if status_code != 500:
            raise HTTPException(status_code, detail={"request_id": request_id, "error": code,
                                                     "message": message}) from exc
        raise HTTPException(500, detail={
            "request_id": request_id, "error": "COPILOT_FAILED",
            "message": "The request could not be completed.",
        }) from exc


def _switches_case(message: str) -> bool:
    """The workspace's own switch / back phrases (applicant_agent.yaml case_workspace.phrases)."""
    from app.agents.applicant.copilot.capabilities import workspace as _sw

    return _sw.enabled() and any(_sw._says(k, message) for k in ("switch", "previous"))


def _agent_chat_key(request: Request) -> str | None:
    key = getattr(request.state, "chat_key", None)
    return "|".join(str(k) for k in key) if key else None


def _clearly_off_topic(said: str) -> bool:
    """
    A message CLEARLY outside the product (2026-10-18, measured: off-topic turns waited 4 s for Qwen's choice to time
    out, then were refused anyway): the configured off-topic words (product_flow.yaml out_of_scope), or the meaning
    reader's TOP reading being out_of_scope at or above the accept score (intent_catalogue.yaml decide.accept).
    Only Qwen is skipped -- the pipeline still writes the reply. Config copilot_reply.yaml agent.skip_off_topic.
    """
    from app.agents.applicant.copilot.capabilities import product_flow as _ot_flow
    from app.agents.applicant.copilot.capabilities import qwen_agent as _ot_agent
    from app.agents.applicant.copilot.semantics import meaning as _ot_meaning

    if not _ot_agent.cfg().get("skip_off_topic", True):
        return False
    if _ot_flow.out_of_scope(said):
        return True
    ranked = _ot_meaning.rank(said)
    if not ranked:
        return False
    name, score = ranked[0]
    kind = ((_ot_meaning.catalogue().get("intents") or {}).get(name) or {}).get("kind")
    accept = float((_ot_meaning.catalogue().get("decide") or {}).get("accept", 0.78))
    return kind == "out_of_scope" and score >= accept


def _agent_chat_ref(request: Request) -> tuple[str, str | None] | None:
    """(subject, chat_id) of this turn: the Qwen agent's history is read / stored under it, shared by every worker."""
    key = getattr(request.state, "chat_key", None)
    return (str(key[0]), key[1]) if key and key[0] else None


#: a message that CONTINUES the last answer ("and the salary slip?", "aur woh?", "what about the co-applicant?")
_CONTINUES = re.compile(r"(?i)^\s*(and|also|aur|or|but|then|so|what about|how about|uska|iska|unka)\b|\b(too|also|bhi)\s*\??\s*$")
#: a PERSON pronoun or a pick from the last list ("what loan is HE applying for", "which ONE first", "for THE FIRST
#: ONE"): leans on what was said even when the message names something (measured 2026-10-15, en_followup_dev)
_REFERS = re.compile(r"(?i)\b(he|she|him|his|her|hers|unka|uska|which one|(the )?(first|second|third|other|last|"
                     r"next|same|previous) one|one of (them|those|these))\b|\b(first|pehla|pehle wala)\b.*\b(one|wala)\b")


def _rule_reading_clear(said: str, case_open: bool) -> str | None:
    """The intent the rule reading names at HIGH confidence for a message that does not lean on the previous turn,
    else None. Such a message is answered by the engine's own tools, never re-read by Qwen."""
    from app.agents.applicant.copilot.semantics import intents as _rr_intents

    if _leans_on_previous(said):
        return None
    rule = _rr_intents.understand(said, has_case=case_open)
    # only the intents config names (copilot_reply.yaml agent.rule_clear_intents: the case's own data)
    from app.agents.applicant.copilot.capabilities import qwen_agent as _rr_agent

    trusted = {str(i).upper() for i in _rr_agent.cfg().get("rule_clear_intents") or []}
    if rule.confidence == "high" and rule.intent.value in trusted:
        return rule.intent.value
    return None


def _leans_on_previous(said: str) -> bool:
    """Whether a message leans on the previous answer: a continuation opener, or a pronoun ("it", "that one", "woh")
    with no subject of its own named. Such a message is Qwen's to resolve with the history (structure, not phrases:
    the same pronoun / subject lists the follow-up resolver uses)."""
    from app.agents.applicant.copilot.conversation import followup as _fu

    if _CONTINUES.search(said) or _REFERS.search(said):
        return True
    return bool(_fu._PRONOUN.search(said)) and not _fu._NAMES_A_THING.search(said)


async def _context_followup_turn(request: Request, message: str, claims: dict[str, Any], payload: Any,
                                 request_id: str) -> dict[str, Any] | None:
    """
    A FOLLOW-UP IS QWEN'S TO UNDERSTAND (owner, 2026-10-14: "hard code nahi ... qwen hi samjh ke dena chahiye"). On an
    open case, with a conversation already under way, a message that names NOTHING of its own -- no document, no
    party, no case id, not the officer's cases -- leans on what was said before ("is it verified by the bank too?",
    "which one first?", "the first one", "that thing we talked about"). Qwen reads it WITH the chat's last exchanges
    (every answer is remembered, whichever layer wrote it) and calls the tools, or asks one clarifying question
    itself. Facts are still only the tools'. Qwen down / slow / no valid call -> None: the pipeline answers as before.
    """
    from app.agents.applicant.copilot.capabilities import general as _cf_general
    from app.agents.applicant.copilot.capabilities import qwen_agent as _agent
    from app.agents.applicant.copilot.capabilities import vague as _cf_vague
    from app.agents.applicant.copilot.semantics import meaning as _cf_meaning
    from app.agents.applicant.copilot.semantics import task as _cf_task
    from app.security import guardrails as _cf_guard

    if not (_agent.enabled() and _agent.cfg().get("followups_to_agent", True)) or not _agent._ready():
        return None
    said = " ".join(str(message or "").split())
    state = _cf_general._state(claims, payload.context)
    case_id = payload.case_id or getattr(state, "active_case_id", None)
    chat_key = _agent_chat_ref(request)
    # QWEN READS EVERY MESSAGE (owner, 2026-10-14: "qwen ko hi sab karne do, 2-3 sec theek hai"; config
    # agent.all_messages): only security, a reply to a waiting question, a workspace command and the eligibility
    # redirect stay before it; everything else -- questions, follow-ups, small talk -- is Qwen's choice
    import os as _cf_os

    _every_env = (_cf_os.getenv("COPILOT_AGENT_ALL_MESSAGES") or "").strip().lower()
    every = _every_env in {"1", "true", "yes", "on"} if _every_env else bool(_agent.cfg().get("all_messages"))
    if not said:
        return None
    if not (case_id or every):
        # no case open, hybrid: only small talk is Qwen's here (the no-case questions keep their own path)
        import asyncio as _cf_asyncio_n

        if not (_agent.cfg().get("small_talk") and ((_cf_task.enabled() and _cf_task.social_kind(said))
                                                    or await _cf_asyncio_n.to_thread(_cf_meaning.social_reading, said))):
            return None
    if getattr(payload, "co_applicant_id", None) or getattr(payload, "party_id", None):
        return None
    if not _cf_guard.check_input(said, allowed_ids=(payload.case_id, payload.applicant_id)).allowed:
        return None
    import asyncio as _cf_asyncio_o

    if await _cf_asyncio_o.to_thread(_clearly_off_topic, said):
        logger.info("qwen agent: skipped, clearly off-topic")
        return None                 # the weather, cricket, coding, investment tips: the pipeline refuses at once
    if _cf_task.enabled() and _cf_task.asks_role(said):
        # A ROLE IN THE WORKFLOW ("who handles cpa?", "who reviews it after that?"): the stage knowledge answers it
        # (the pipeline's role reading); Qwen here sees only the case's tools and answered with its readiness
        logger.info("qwen agent: skipped, a workflow role question")
        return None
    # ITS OWN ANCHOR: the message says what it is about -- the ordinary (fast) path reads it
    if not every and (_cf_meaning.document_in(said) or _cf_meaning.party_in(said) or _cf_meaning._names_many_cases(said)
            or re.search(r"(?i)\b(case|app|appl)[-_ ]?[0-9a-f]{6,}\b", said)
            # ONE recorded field ("what is the tenure"): the profile answer (the agent gave the glossary, 2026-10-23)
            or (case_id and _ag_profile_field(said))):
        return None
    # a reply to a question the chat is waiting on, an option number, yes / no, a command, small talk: their own paths
    if re.fullmatch(r"(?i)[\d\s.,]+|yes|no|ok|okay|haan|nahi", said) \
            or _cf_vague.waits_for(said, state, claims, payload.context) \
            or (_cf_vague.is_command(said, case_open=bool(case_id)) and not _list_reading_without_cases(said)) \
            or (not every and not _agent.cfg().get("small_talk") and _cf_task.enabled() and _cf_task.social_kind(said)):
        return None
    if not every and _agent.cfg().get("small_talk"):
        import asyncio as _cf_asyncio_t

        if (_cf_task.enabled() and _cf_task.social_kind(said)) \
                or await _cf_asyncio_t.to_thread(_cf_meaning.social_reading, said):
            # SMALL TALK IN QWEN'S WORDS (owner, 2026-10-14), capped: past the cap the config line answers at once
            return await _agent_reply(request, message, claims, payload, request_id, case_id,
                                      timeout=float(_agent.cfg().get("small_talk_timeout_seconds", 3.0)))
    if every:
        if (_cf_general._has(said, "eligibility") and not _cf_general._has(said, "eligibility_not"))                 or _ag_is_eligibility(said):
            return None             # owner policy: eligibility is never answered in chat -- the fixed redirect
        reply = await _agent_reply(request, message, claims, payload, request_id, case_id,
                                   timeout=float(_agent.cfg().get("followup_timeout_seconds", 8.0)))
        if reply is not None:
            reply["agent_followup"] = True
        return reply
    # THE CONVERSATION HANDLERS THAT ALREADY READ THE CONTEXT RIGHT answer first (measured 2026-10-14: the router took
    # them and regressed held-out turns): the eligibility redirect (owner policy), "after that" right after a next-step
    # answer, confusion / "can you handle it" / "is X accepted?", the co-applicant follow-ups
    _last_intent = str(_last_turn(request, payload).get("last_intent") or "").upper()
    if ((_cf_general._has(said, "eligibility") and not _cf_general._has(said, "eligibility_not"))
            or _ag_is_eligibility(said)
            or (_cf_general._has(said, "after_that") and not _cf_general._has(said, "after_that_not")
                and (_last_intent in _NEXT_STEP_INTENTS or _last_intent == "PROCESS_STAGES"
                     # a how-to answer ("how do I send a case from FOS to CPA?" -> FAQ_ANSWER): "who reviews it after
                     # that?" is the stage after it -- the stage handler's, never Qwen's next step (dev_en, 2026-10-15)
                     or _last_intent in _STAGE_ANSWER_INTENTS
                     or _after_knowledge(request, payload)))
            or _co_followup(message) or _cf_general._has(said, "only_applicant")
            or _cf_general._has(said, "coapp_need")
            or (_cf_general._has(said, "coapp_need_followup") and len(said.split()) <= 7
                and ("CO_APPLICANT" in _last_intent
                     or str(_last_turn(request, payload).get("last_subject") or "").upper() in ("CO", "CO_APPLICANT")
                     or str(_last_turn(request, payload).get("last_missing_party") or "").upper()
                     in ("CO", "CO_APPLICANT")))
            # the general layer's own direct answers (confusion, "can you handle it", "is X accepted?", "after cpa?")
            or _cf_general._direct_reply(said, case_id, request_id, "en") is not None):
        return None
    must_ask = False
    if not _agent.history(chat_key):
        # NO CONVERSATION YET: only a message the fast matcher cannot read is Qwen's (it asks back when it must)
        import asyncio as _cf_asyncio

        if await _cf_asyncio.to_thread(_cf_meaning.clear_reading, said, True):
            return None
        if _rule_reading_clear(said, bool(case_id)):
            return None             # "pichle case ka status?": short but CLEAR to the rule reading -- never asked back
        # A VERY SHORT UNCLEAR MESSAGE WITH NOTHING BEFORE IT ("hmm the paper", "that one"): nothing to resolve it
        # from -- ASKED, never guessed (owner, 2026-10-14: "weak spot bhi fix")
        must_ask = len(said.split()) <= int(_agent.cfg().get("ask_first_max_words", 3))
    import asyncio as _cf_asyncio_s

    if await _cf_asyncio_s.to_thread(_cf_meaning.social_reading, said):
        return None                 # "thx a lot", "cool thanks": small talk -- its own instant answer, never Qwen
    if (not must_ask and _agent.cfg().get("clear_followups_skip", True) and not _leans_on_previous(said)
            and await _cf_asyncio_s.to_thread(_cf_meaning.clear_reading, said, True)):
        # A CLEAR QUESTION MID-CHAT (owner, 2026-10-15: "clear follow-ups skip Qwen"): "what is pending", "summary of
        # this case" name what they ask -- the fast path answers them as at the start of a chat (~3 s with the natural
        # line instead of 7-12 s for Qwen + line on this CPU). A message leaning on the last answer stays Qwen's.
        logger.info("qwen agent: skipped, clear follow-up")
        return None
    if not must_ask and _agent.cfg().get("clear_followups_skip", True):
        # the same high-confidence rule reading mid-chat ("all document details", "doc pending hai kya?" after another
        # question went to Qwen, which dropped the fields / the Hinglish -- 2026-10-16)
        rule = _rule_reading_clear(said, bool(case_id))
        if rule:
            logger.info("qwen agent: skipped, clear follow-up by rule reading %s", rule)
            return None
    reply = await _agent_reply(request, message, claims, payload, request_id, case_id,
                               timeout=float(_agent.cfg().get("followup_timeout_seconds", 8.0)), must_ask=must_ask)
    if must_ask and reply is not None and str(reply.get("intent") or "") != "CLARIFICATION":
        # Qwen answered instead of asking: the options question for this case (vague.ask), else the pipeline
        reply = _cf_vague.ask(said, state, request_id, case_open=True)
    if reply is None and case_id and len(said.split()) <= int(_agent.cfg().get("ask_on_nothing_max_words", 3)):
        # (only a SHORT message: "ok and which ones r done" is a follow-up the conversation layer answers -- measured
        # 2026-10-15, the options question took it)
        # QWEN GAVE NOTHING (slow / down / no valid choice) AND NO READING IS CONFIDENT ("hmm not sure": best 0.68):
        # the case's options question -- never "that's outside what I can help with", which is for a CLEAR off-topic
        # message (the weather / cricket read 0.99 out_of_scope and keep their polite refusal)
        ranked = await _cf_asyncio_s.to_thread(_cf_meaning.rank, said)
        accept = float((_cf_meaning.catalogue().get("decide") or {}).get("accept", 0.78))
        if ranked and ranked[0][1] < accept:
            reply = _cf_vague.ask(said, state, request_id, case_open=True)
    if reply is not None:
        reply["agent_followup"] = True
    return reply


async def _case_record_reply(message: str, claims: dict[str, Any], payload: Any,
                             request_id: str) -> dict[str, Any] | None:
    """A specific fact of the OPEN case / one of its documents, from its masked fact sheet, fact-checked
    (capabilities/snapshot_qa.py: every number, date, id, status and document name must be on the sheet). Ownership
    is checked first. None: no case open, not authorised, the model unavailable -> the caller goes on as before."""
    from app.agents.applicant.copilot.capabilities import general as _cr_general
    from app.agents.applicant.copilot.capabilities import snapshot_qa as _cr_snapshot
    from app.agents.applicant.copilot.capabilities import workspace as _cr_ws
    from app.security import access as _cr_access

    case_id = _cr_general._state(claims, payload.context).active_case_id or payload.case_id
    if not case_id or not _cr_snapshot.enabled():
        return None
    try:
        _cr_ws.authorize(claims, case_id)
    except _cr_access.AccessDenied:
        return None
    found = await _cr_snapshot.answer(case_id, message)
    if not found or not found.get("answer"):
        return None
    return _cr_general._reply(request_id, "CASE_SNAPSHOT", str(found["answer"]), case_id=case_id,
                              query_type="CASE_FACT", tools_invoked=["case.snapshot"])


def _ag_is_eligibility(said: str) -> bool:
    """The rule classifier reads it as an eligibility question ("what FOIR is allowed?") -- the redirect's, never Qwen's."""
    try:
        from app.agents.applicant.copilot.semantics import intents as _ag_intents

        return _ag_intents.classify(said).intent is _ag_intents.Intent.ELIGIBILITY
    except Exception:  # noqa: BLE001 - unknown: Qwen may read it (the follow-up path keeps its own check)
        return False


def _ag_social_reading(said: str) -> bool:
    from app.agents.applicant.copilot.semantics import meaning as _sr_meaning

    try:
        return _sr_meaning.social_reading(said)
    except Exception:  # noqa: BLE001 - no embeddings: not small talk by meaning
        return False


def _list_reading_without_cases(said: str) -> bool:
    """The case-list reader took the message ("which one should I do FIRST" -> a sorted list) but it names no case /
    file / customer (case_list config case_nouns): on an open case it is about THAT case, never the portfolio."""
    from app.agents.applicant.copilot.capabilities import case_list as _lr_list

    if _lr_list.understand(said) is None:
        return False
    nouns = (_lr_list.cfg().get("phrases") or {}).get("case_nouns") or {}
    words = {str(w).lower() for k in ("plural", "singular") for w in nouns.get(k) or []}
    return not (set(re.findall(r"[a-z]+", said.lower())) & words)


async def _agent_reply(request: Request, message: str, claims: dict[str, Any], payload: Any, request_id: str,
                       case_id: str | None, *, timeout: float | None = None,
                       must_ask: bool = False) -> dict[str, Any] | None:
    """Qwen's tools for this message on `case_id` (authorised per tool by the lookup), as a reply; None: no answer."""
    from app.agents.applicant.copilot.capabilities import general as _ag_general
    from app.agents.applicant.copilot.capabilities import qwen_agent as _agent

    applicant_id = payload.applicant_id
    product = None
    if case_id:
        from app.store import get_repository

        application = get_repository().get_application(case_id)
        product = getattr(application, "product", None)
        applicant_id = applicant_id or getattr(application, "applicant_id", None)

    async def lookup(canonical: str) -> dict[str, Any]:
        return await answer_question(message=canonical, applicant_id=applicant_id, case_id=case_id, claims=claims,
                                     request_id=request_id, intent_override=None, concise=True,
                                     context=payload.context)

    async def handbook(question: str) -> str | None:
        from app.agents.applicant.copilot.answering import language_lock as _ag_lock

        reply = await _ag_general.answer(question, claims, payload.context, request_id, _ag_lock.current() or "en",
                                         case_in_scope=None, meaning_kind="knowledge", knowledge_hint=None)
        text = str((reply or {}).get("answer") or "").strip()
        return text if text and str((reply or {}).get("intent") or "") not in ("UNKNOWN_TERM", "NOT_IN_SCOPE") else None

    if getattr(request.state, "agent_tried", False):
        return None                 # ONE Qwen choice a turn: it already read this message and had no answer
    request.state.agent_tried = True
    done = await _agent.answer(message, chat_key=_agent_chat_ref(request), case_id=case_id, product=product,
                               claims=claims, lookup=lookup, handbook=handbook, timeout=timeout,
                               must_ask=must_ask, request_id=request_id)
    if done is None:
        return None
    if done["kind"] == "ask_rejected":
        # Qwen wanted to ask but its question named something this case is not: the case's own options question
        from app.agents.applicant.copilot.capabilities import vague as _ar_vague

        return _ar_vague.ask(" ".join(str(message).split()), _ag_general._state(claims, payload.context), request_id,
                             case_open=bool(case_id))
    if done["kind"] == "chat":
        # small talk: a CONVERSATION answer that read nothing (the same contract as the config greeting / thanks)
        reply = _ag_general._reply(request_id, "SMALL_TALK", done["answer"], case_id=case_id, category="CONVERSATION",
                                   query_type="CONVERSATION", response_source="AGENT", tools_invoked=[])
        reply["agent_timings"] = done.get("timings")
        return reply
    reply = _ag_general._reply(request_id, "CLARIFICATION" if done["kind"] == "followup" else "AGENT_ANSWER",
                               done["answer"], case_id=case_id,
                               category="CASE_SPECIFIC" if case_id else "KNOWLEDGE_ONLY",
                               response_source="AGENT", tools_invoked=list(done["tools"]))
    reply["no_case_links"] = not case_id
    reply["agent_timings"] = done.get("timings")
    return reply


async def _simple_turn(request: Request, message: str, claims: dict[str, Any], payload: Any,
                       request_id: str) -> dict[str, Any] | None:
    """THE SIMPLE PIPELINE (copilot/simple/pipeline.py, config simple_chat.yaml): a question it understands is the
    engine's canonical question + the natural line from the same records -- no model; small talk and a bare glossary
    acronym are their fixed lines; else None (the rest answers)."""
    from app.agents.applicant.copilot.answering import language_lock as _sp_lock
    from app.agents.applicant.copilot.capabilities import general as _sp_general
    from app.agents.applicant.copilot.simple import pipeline as _simple

    if not _simple.enabled():
        return None
    if getattr(payload, "co_applicant_id", None) or getattr(payload, "party_id", None):
        return None                 # a named party: its authorization path decides first
    from app.agents.applicant.copilot.semantics import intents as _sp_intents

    if _sp_intents.compound_parts(message):
        # SEVERAL QUESTIONS IN ONE MESSAGE (conversation eval n1 / n3): the simple pipeline reads ONE question and
        # answered only the loan amount of "are my documents verified and what is my loan amount?" -- the engine
        # splits it and answers every part
        return None
    state = _sp_general._state(claims, payload.context)
    if _sp_general._pending(state, claims, payload.context):
        return None                 # the chat waits on an answer: "ok" / "2" / "haan" answer ITS question
    case_id = state.active_case_id or payload.case_id
    lang = _sp_lock.current() or "en"

    async def social(frame: Any) -> dict[str, Any] | None:
        from app.agents.applicant import conversation as _sp_conv
        from app.agents.applicant.copilot.capabilities import workspace as _sp_ws

        if frame.intent == _sp_conv.GREETING:
            # NATURAL CONVERSATION (2026-11-02): Qwen answers the actual message ("hows you ?"), no menu; the plain
            # configured line only when the model cannot
            from app.agents.applicant.copilot.capabilities import natural_chat as _sp_nat

            natural = await _sp_nat.social(message, _agent_chat_ref(request), request_id, lang, case_id)
            return natural or _sp_ws.greeting_reply(claims, request_id, state)
        if frame.intent == _sp_conv.GOODBYE:
            return _farewell_reply(request_id, payload)
        return await _sp_general.answer(message, claims, payload.context, request_id, lang, case_in_scope=case_id)

    async def definition(frame: Any) -> dict[str, Any] | None:
        defined = await _sp_general._definition(message, request_id, lang, case_id)
        return defined if defined is not None and defined.get("intent") == "DEFINITION" else None
    applicant_id = payload.applicant_id
    if case_id and not applicant_id:
        from app.store import get_repository

        applicant_id = getattr(get_repository().get_application(case_id), "applicant_id", None)

    looked_up: dict[str, Any] = {}

    async def lookup(canonical: str) -> dict[str, Any]:
        found = await answer_question(message=canonical, applicant_id=applicant_id, case_id=case_id, claims=claims,
                                      request_id=request_id, intent_override=None, concise=True,
                                      context=payload.context)
        looked_up["reply"] = found
        return found

    reply = await _simple.answer(message, chat=_agent_chat_key(request), lookup=lookup,
                                 question=str(getattr(request.state, "copilot_message", "") or message),
                                 handlers={"social": social, "definition": definition}, case_open=bool(case_id),
                                 previous_intent=(payload.context or {}).get("last_intent")
                                 if isinstance(payload.context, dict) else None)
    if reply is not None:
        request.state.simple_answered = True        # its "why?" / "which one first" memory survives this turn
        engine = looked_up.get("reply") if isinstance(looked_up.get("reply"), dict) else None
        engine_conv = ((engine or {}).get("understanding") or {}).get("conversation") \
            if isinstance((engine or {}).get("understanding"), dict) else None
        if isinstance(engine_conv, dict) and engine_conv.get("conversation_id"):
            # A CASE ANSWER IS THE ENGINE'S ANSWER (lookup -> answer_question, which recorded it in the
            # conversation): its conversation reading goes back with it -- the reply's context (conversation id) is
            # built from it -- and moved_on does not erase it, so "more" / "again" / "why?" next refer to it
            # (conversation eval v2: the simple reply came back with NO context, the next turn opened a new
            # conversation and "more" was UNKNOWN)
            understanding = reply.setdefault("understanding", {}) if isinstance(reply.get("understanding"), dict) \
                or "understanding" not in reply else None
            if isinstance(understanding, dict) and not understanding.get("conversation"):
                understanding["conversation"] = engine_conv
            if not reply.get("context"):
                from app.agents.applicant.copilot.conversation import followup as _sp_followup

                reply["context"] = _sp_followup.context_from_response(reply)
        else:
            _sp_general.moved_on(claims, payload.context)
    return reply


async def _clear_reading(request: Request, message: str, claims: dict[str, Any], payload: Any) -> bool:
    """The meaning reader is sure of this message (simple/pipeline.clear) and the chat waits on nothing: the Qwen
    follow-up reader and the Qwen agent are skipped, the deterministic layers answer it."""
    from app.agents.applicant.copilot.capabilities import general as _cr_general
    from app.agents.applicant.copilot.simple import pipeline as _cr_simple

    if not (_cr_simple.enabled() and _cr_simple.cfg().get("front", False)):
        return False
    if _cr_general._pending(_cr_general._state(claims, payload.context), claims, payload.context):
        return False
    import asyncio as _cr_asyncio

    def _sure() -> bool:
        return _cr_simple.sure(message, _agent_chat_key(request))

    try:
        return bool(await _cr_asyncio.to_thread(_sure))
    except Exception:  # noqa: BLE001 - no reading: the layers run as before
        return False


async def _qwen_agent_turn(request: Request, message: str, claims: dict[str, Any], payload: Any,
                           request_id: str) -> dict[str, Any] | None:
    """The Qwen agent's reply for this message, or None (not its turn / it could not answer: the pipeline answers)."""
    from app.agents.applicant.copilot.capabilities import case_list as _ag_list
    from app.agents.applicant.copilot.capabilities import general as _ag_general
    from app.agents.applicant.copilot.capabilities import qwen_agent as _agent
    from app.agents.applicant.copilot.capabilities import vague as _ag_vague
    from app.agents.applicant.copilot.semantics import task as _ag_task

    if not _agent.enabled() or not _agent._ready():
        return None                 # Qwen cannot run: nothing is read for it (a refusal must read nothing)
    if getattr(payload, "co_applicant_id", None) or getattr(payload, "party_id", None):
        return None                 # a named party: its authorization path decides first
    from app.security import guardrails as _ag_guard

    _ag_verdict = _ag_guard.check_input(" ".join(str(message or "").split()),
                                        allowed_ids=(payload.case_id, payload.applicant_id))
    if not _ag_verdict.allowed:
        return None                 # bulk data / export / any flagged input: the guarded pipeline, never the agent
    state = _ag_general._state(claims, payload.context)
    case_id = payload.case_id or getattr(state, "active_case_id", None)
    said = " ".join(str(message or "").split())
    settled = getattr(request.state, "task", None)
    gates = (("reply", lambda: re.fullmatch(r"(?i)[\d\s.,]+|yes|no|ok|okay|haan|nahi", said)),
             # OWNER POLICY: eligibility (FOIR, limits, "is he eligible") is never answered in chat -- the fixed
             # redirect, before Qwen (2026-10-15: "what FOIR is allowed?" got Qwen's clarifying question here)
             ("eligibility", lambda: (_ag_general._has(said, "eligibility") and not _ag_general._has(said, "eligibility_not"))
              or _ag_is_eligibility(said)),
             ("waits_for", lambda: _ag_vague.waits_for(said, state, claims, payload.context)),
             ("command", lambda: _ag_vague.is_command(said, case_open=bool(case_id))),
             ("social", lambda: _ag_task.enabled() and _ag_task.social_kind(said)),
             ("social_meaning", lambda: _ag_social_reading(said)),
             # the weather, cricket, coding, investment tips: refused by the pipeline, never 4 s of Qwen first
             ("off_topic", lambda: _clearly_off_topic(said)),
             ("over_cases", lambda: _ag_task.enabled() and _ag_task.asks_over_cases(said)),
             # a ROLE in the workflow ("who handles cpa?"): the stage knowledge, never the case's tools
             ("role", lambda: _ag_task.enabled() and _ag_task.asks_role(said)),
             ("case_list", lambda: _ag_list.understand(said) is not None),
             # the general layer's own direct answers: confusion, "can you handle it", "is X accepted?"
             ("parties", lambda: _ag_general._has(said, "co_applicant")),
             # ONE recorded field ("what is the tenure"): the profile answer names that field only, never the whole record
             ("one_field", lambda: bool(case_id) and _ag_profile_field(said)),
             ("general_fast", lambda: _ag_general._has(said, "frustrated") or _ag_general._has(said, "delegate")
              or _ag_general._accepted(said, "en") is not None),
             ("settled", lambda: settled is not None and (settled.operation in ("page", "farewell", "greet")
                                                          or settled.references.get("kind") == "ordinal")))
    for gate, hit in gates:
        if hit():
            logger.info("qwen agent: skipped by %s", gate)
            return None             # a command / a reply / small talk / the case list: their own fast paths
    if _agent.cfg().get("only_when_unclear"):
        # HYBRID (copilot_reply.yaml agent.only_when_unclear): a message the fast matcher reads clearly is answered by
        # the same tools directly (well under a second); Qwen chooses only for the unclear ones
        import asyncio as _ag_asyncio

        from app.agents.applicant.copilot.semantics import meaning as _ag_meaning

        if await _ag_asyncio.to_thread(_ag_meaning.clear_reading, said, bool(case_id)):
            logger.info("qwen agent: skipped by clear_reading")
            return None
        # THE RULE READING IS CLEAR TOO (2026-10-16): "bank statement ki details", "docs verify hue?", "show all
        # documents" -- the embedding margin called them unclear, Qwen answered, and the fields / the Hinglish were
        # lost, while the rule reading had each right at HIGH confidence. A high-confidence rule reading that does not
        # lean on the previous turn is answered by the engine; vague messages and follow-ups still go to Qwen.
        rule = _rule_reading_clear(said, bool(case_id))
        if rule:
            logger.info("qwen agent: skipped by rule reading %s", rule)
            return None
    return await _agent_reply(request, message, claims, payload, request_id, case_id)


#: the previous answer's kind that a bare document name continues (a knowledge / process answer)
_KNOWLEDGE_TYPES = {"PROCESS_KNOWLEDGE", "KNOWLEDGE_ONLY", "KNOWLEDGE"}
_KNOWLEDGE_INTENTS = {"FOS_KNOWLEDGE", "GENERAL_KNOWLEDGE", "DEFINITION", "DOCUMENT_ACCEPTED", "PROCESS_STAGES",
                      "DOCUMENTS_REQUIRED", "STAGE_PROCESS"}


def _coapp_need_reply(message: str, case_id: str | None, request: Request, payload: Any,
                      request_id: str) -> dict[str, Any] | None:
    """IS A CO-APPLICANT REQUIRED? (2026-10-14) -- asked outright, or "do we need one?" right after a co-applicant
    answer. From the product's own checklist: no configured product requires one, so the answer says that, for the
    case's product. None when the message is not this question or no case is open."""
    from app.agents.applicant.copilot.capabilities import general as _cn_general
    from app.security import guardrails as _cn_guard

    if not case_id:
        return None
    asked = _cn_general._has(message, "coapp_need")
    if not asked and _cn_general._has(message, "coapp_need_followup") and len(str(message).split()) <= 7:
        last = _last_turn(request, payload)
        asked = "CO_APPLICANT" in str(last.get("last_intent") or "").upper() or \
            str(last.get("last_subject") or "").upper() in ("CO", "CO_APPLICANT") or \
            str(last.get("last_missing_party") or "").upper() in ("CO", "CO_APPLICANT")
    if not asked or not _cn_guard.check_input(" ".join(str(message).split())).allowed:
        return None
    from app.agents.applicant import config as _cn_config
    from app.agents.applicant.copilot.answering.answer import _readable
    from app.store import get_repository as _cn_repo

    application = _cn_repo().get_application(case_id)
    product = str(getattr(application, "product", "") or "")
    if not product:
        return None
    # the checklist decides: a product whose required documents include a co-applicant slot would say "yes"
    rows = _cn_config.checklist_for(product) or []
    if any("CO_APPLICANT" in str(r.get("slot") or "").upper() and r.get("mandatory", True) for r in rows):
        return None
    text = _cn_general._say_text((_cn_general.cfg().get("texts") or {}).get("coapp_not_required"), "en",
                                 product=_readable(product))
    return _cn_general._reply(request_id, "COAPP_REQUIREMENT", text, case_id=case_id, query_type="PROCESS_KNOWLEDGE")


def _misunderstood(message: str) -> bool:
    """"ye nahi poocha" / "that's not what I asked" (config chatbot.misunderstood): the engine's recovery answers it
    -- never the which-action question (frontend contract 22_misunderstood, 2026-10-14)."""
    from app.agents.applicant import config as _mu_config

    said = " " + " ".join(re.findall(r"[\wऀ-ॿ']+", str(message or "").lower())) + " "
    phrases = (_mu_config.chatbot("misunderstood") or {}).get("phrases") or []
    return any(f" {str(p).lower().strip()} " in said for p in phrases)


def _only_applicant_reply(message: str, case_id: str | None, request: Request, payload: Any,
                          request_id: str, claims: dict[str, Any]) -> dict[str, Any] | None:
    """"so it's ONLY Rahul then?" right after a no-co-applicant answer (2026-10-14): a confirmation with the
    applicant's recorded name. Only as that follow-up (the previous turn found the co-applicant missing)."""
    from app.agents.applicant.copilot.capabilities import general as _oa_general
    from app.security import guardrails as _oa_guard

    if not case_id or len(str(message).split()) > 8 or not _oa_general._has(message, "only_applicant"):
        return None
    after_no_coapp = str(_last_turn(request, payload).get("last_missing_party") or "").upper() in ("CO", "CO_APPLICANT")
    if not _oa_guard.check_input(" ".join(str(message).split())).allowed:
        return None
    from app.agents.applicant.copilot.capabilities import workspace as _oa_ws
    from app.security import access as _oa_access
    from app.store import get_repository as _oa_repo

    try:
        _oa_ws.authorize(claims, case_id)              # the ordinary ownership check before any read
    except _oa_access.AccessDenied:
        return None
    application = _oa_repo().get_application(case_id)
    applicant = _oa_repo().get_applicant(application.applicant_id) if application else None
    name = str(getattr(applicant, "full_name", "") or "").strip()
    first = name.split()[0].lower() if name else ""
    # the previous turn found no co-applicant, OR the message names the applicant ("so it's only RAHUL then?")
    if not name or not (after_no_coapp or (first and re.search(rf"\b{re.escape(first)}\b", message.lower()))):
        return None
    if getattr(application, "co_applicant_id", None):
        return None                     # there IS a co-applicant: the ordinary answer says who
    text = _oa_general._say_text((_oa_general.cfg().get("texts") or {}).get("only_applicant"), "en", name=name)
    return _oa_general._reply(request_id, "ONLY_APPLICANT", text, case_id=case_id, query_type="CASE_FACT")


_STATUS_CUE = re.compile(r"\b(verified|verify|uploaded|upload|submitted|received|status|done|pending|approved|"
                         r"rejected|cleared|complete|completed|ok)\b", re.IGNORECASE)
_LIST_CUE = re.compile(r"\b(all|every|each|documents|docs|which|list)\b", re.IGNORECASE)


def _several_documents_reply(message: str, case_id: str | None, request_id: str,
                             claims: dict[str, Any]) -> dict[str, Any] | None:
    """"is pan verified? and bank statement?" (2026-10-14 English officer set: only the last one was answered): TWO
    OR MORE named documents with a status cue -> one status line each, from the case's own documents (the same line
    as the documents list). Never the applicant + co-applicant mix (the conversation layer splits parties), never
    "all / which documents" (the list answers that)."""
    from app.agents.applicant.copilot.semantics import meaning as _sd_meaning
    from app.agents.applicant.copilot.routing import subjects as _sd_subjects
    from app.security import guardrails as _sd_guard

    text = str(message or "")
    if not case_id or len(text.split()) > 14 or not _STATUS_CUE.search(text) or _LIST_CUE.search(text):
        return None
    said = " " + re.sub(r"[^\w\s]", " ", _sd_meaning._normalised(text).lower()) + " "
    spans: list[tuple[int, str]] = []
    for alias, name in _sd_meaning._documents():               # longest alias first; a matched span is used up
        at = said.find(f" {alias} ") if alias else -1
        if at >= 0:
            said = said[:at] + " " + "#" * len(alias) + " " + said[at + len(alias) + 2:]
            spans.append((at, name))
    named = list(dict.fromkeys(name for _, name in sorted(spans)))      # in the order the officer asked
    if len(named) < 2 or _sd_subjects.mentioned(text) is not None:
        return None
    if not _sd_guard.check_input(" ".join(text.split())).allowed:
        return None
    from app.agents.applicant.copilot.answering.answer import _STATE_ICON, _readable
    from app.agents.applicant.copilot.capabilities import general as _sd_general
    from app.agents.applicant.copilot.capabilities import workspace as _sd_ws
    from app.security import access as _sd_access
    from app.store import get_repository as _sd_repo

    try:
        _sd_ws.authorize(claims, case_id)              # the ordinary ownership check before any read
    except _sd_access.AccessDenied:
        return None
    documents = [d for d in _sd_repo().list_documents(case_id)
                 if str(getattr(d, "party_role", "") or "").upper() != "CO_APPLICANT"]
    lines = []
    for name in named:
        found = [d for d in documents if _readable(getattr(d, "document_type", "")) == name]
        status = getattr(found[-1], "status", None) if found else "MISSING"
        status = str(getattr(status, "value", status) or "").upper()
        lines.append(f"- **{name}** — {_STATE_ICON.get(status, ('', status.replace('_', ' ').capitalize()))[1]}")
    return _sd_general._reply(request_id, "DOCUMENT_STATUS", "\n".join(lines), case_id=case_id,
                              query_type="CASE_FACT")


#: answers after which "and after that?" means the case's NEXT STAGE
_NEXT_STEP_INTENTS = {"NEXT_ACTION", "PENDING_ITEMS", "DOCUMENTS_PENDING", "WHY_STUCK", "CPA_READINESS", "READINESS"}
#: process / how-to answers ABOUT A STAGE: "after that" next means the stage after the one they were about
_STAGE_ANSWER_INTENTS = {"STAGE_PROCESS", "PROCESS_ANSWER", "FAQ_ANSWER", "PROCESS_STAGES"}


async def _planner_turn(request: Request, message: str, claims: dict[str, Any], payload: Any,
                        request_id: str) -> dict[str, Any] | None:
    """ONE planner call, then the plan: a registered tool (validated, permission-checked, time-limited -- its own
    deterministic handler writes the answer), Qwen's own words for social / general, or one short question back.
    None when there is no usable plan: the deterministic layers answer, nothing is guessed."""
    from app.agents.applicant.copilot.answering import language_lock as _pl_lock
    from app.agents.applicant.copilot.capabilities import general as _pl_general
    from app.agents.applicant.copilot.capabilities import natural_chat as _pl_nat
    from app.agents.applicant.copilot.planner import plan as _pl_plan
    from app.agents.applicant.copilot.planner import registry as _pl_registry

    state = _pl_general._state(claims, payload.context)
    if _pl_general._pending(state, claims, payload.context):
        return None                          # the chat waits on an answer: its own handler reads it
    case_id = getattr(state, "active_case_id", None) or payload.case_id
    lang = _pl_lock.current() or payload.response_language or "en"
    p = await _pl_plan.plan(message, _pl_nat.history_for(_agent_chat_ref(request)), case_open=bool(case_id))
    request.state.planner = {"intent": p.intent, "tool": p.tool, "ms": p.ms, "error": p.error}
    if p.error:
        return None
    reply: dict[str, Any] | None = None
    if p.intent == "SOCIAL":
        reply = await _pl_nat.social(message, _agent_chat_ref(request), request_id, lang, case_id)
    elif p.intent == "OUT_OF_DOMAIN" and p.tool == "none":
        reply = _pl_nat.out_of_domain_reply(request_id, lang, case_id)
    elif p.tool != "none":
        applicant_id = payload.applicant_id
        if case_id and not applicant_id:
            from app.store import get_repository

            applicant_id = getattr(get_repository().get_application(case_id), "applicant_id", None)
        ctx = _pl_registry.ToolContext(claims=claims, request_id=request_id, case_id=case_id,
                                       applicant_id=applicant_id, lang=lang)
        result = await _pl_registry.run(p.tool, ctx, p.args)
        request.state.planner.update(tool_ok=result.ok, tool_error=result.error, tool_ms=result.ms)
        reply = result.reply if result.ok else None
    elif p.intent == "CLARIFICATION" and p.question.strip().endswith("?") and len(p.question) <= 200 \
            and not _pl_general._unsafe_general(p.question):
        reply = _pl_general._reply(request_id, "CLARIFICATION", p.question.strip(), case_id=case_id,
                                   query_type="CONVERSATION")
    if reply is not None:
        reply.setdefault("tools_invoked", [])
        _pl_general.moved_on(claims, payload.context)
        _turn_layer(request, f"planner({p.intent.lower()}:{p.tool})")
    return reply


async def _natural_turn(request: Request, message: str, claims: dict[str, Any], payload: Any,
                        request_id: str) -> dict[str, Any] | None:
    """The natural-conversation answer of this turn (social / general / a follow-up of a general answer), or None --
    every LOS message, a pending question, a named own case, or natural chat off goes on to the LOS layers."""
    from app.agents.applicant import conversation as _nt_conv
    from app.agents.applicant.copilot.answering import language_lock as _nt_lock
    from app.agents.applicant.copilot.answering import realtime as _nt_rt
    from app.agents.applicant.copilot.capabilities import general as _nt_general
    from app.agents.applicant.copilot.capabilities import natural_chat as _nt_nat

    if not _nt_nat.enabled():
        return None
    state = _nt_general._state(claims, payload.context)
    if _nt_general._pending(state, claims, payload.context) or _names_own_case(message, claims):
        return None
    # THE LANGUAGE OF THIS MESSAGE ("hey there" after a Hinglish chat is English), else the chat's lock
    try:
        from app.agents.applicant import language as _nt_language

        _detected = _nt_language.detect(message)
        lang = ("hi-Latn" if getattr(_detected, "code_mixed", False) else getattr(_detected, "code", None)) \
            or _nt_lock.current() or payload.response_language or "en"
    except Exception:  # noqa: BLE001
        lang = _nt_lock.current() or payload.response_language or "en"
    case_id = getattr(state, "active_case_id", None) or payload.case_id
    try:
        turn = _nt_conv.classify(message)
    except Exception:  # noqa: BLE001
        turn = None
    social = (_nt_rt.is_greeting(message) or _nt_nat.is_social(message)
              or (turn is not None and turn.kind in (_nt_conv.GREETING, _nt_conv.SMALL_TALK)))
    social = social and not _nt_general.names_los(message)   # "how are you handling KYC?" is an LOS question
    if social:
        reply = await _nt_nat.social(message, _agent_chat_ref(request), request_id, lang, case_id)
        if reply is not None:
            _nt_general.moved_on(claims, payload.context)
            _turn_layer(request, "natural(social)")
        return reply
    if _nt_general.names_los(message):
        return None
    # A CASE IS OPEN AND THE DETERMINISTIC READERS ARE SURE it is about the case ("isko aage kaise bheju" -> the next
    # action): never "general" / out of domain (2026-11-06 live eval refused it as a general question)
    if case_id:
        import asyncio as _nt_asyncio

        from app.agents.applicant.copilot.simple import pipeline as _nt_simple

        try:
            if await _nt_asyncio.to_thread(_nt_simple.structural_sure, message):
                return None
        except Exception:  # noqa: BLE001 - not known to be sure: the natural layer decides, as before
            pass
    if not _nt_general.general_chat_available():
        # LOS-ONLY SCOPE (owner 2026-11-03): an ordinary question is answered from the handbook when the handbook holds
        # it (an LOS technical term), else with the out-of-domain line -- never a general answer, never a stray passage
        if not _nt_general.is_general_question(message):
            return None
        found = await _nt_general._knowledge_candidate(message, request_id, case_id, False)
        reply = found[1] if found is not None else _nt_nat.out_of_domain_reply(request_id, lang, case_id)
        _nt_general.moved_on(claims, payload.context)
        _turn_layer(request, "natural(out_of_domain)" if found is None else "natural(los_technical)")
        return reply
    after_general = str(_last_turn(request, payload).get("last_intent") or "").upper() == "GENERAL_CHAT"
    short = len(str(message).split()) <= int((_nt_nat.cfg().get("followup") or {}).get("max_words", 8))
    general = _nt_general.is_general_question(message)
    if general or (after_general and short):
        reply = await _nt_general.general_chat(message, request_id, lang, case_id=case_id,
                                               chat_ref=_agent_chat_ref(request))
        if reply is None and general and not _nt_general._handbook_confident(message):
            # THE MODEL COULD NOT ANSWER an ordinary question (slow / down): say so -- the LOS layers would answer it
            # with an unrelated handbook passage ("why is the sky blue?" -> match_score vs confidence, 2026-11-02)
            texts = (_nt_general.cfg().get("general_chat") or {}).get("texts") or {}
            reply = _nt_general._reply(request_id, "GENERAL_CHAT_UNAVAILABLE",
                                       _nt_general._pick(texts.get("unavailable"), lang), case_id=case_id,
                                       query_type="CONVERSATION")
        if reply is not None:
            _nt_general.moved_on(claims, payload.context)
            _turn_layer(request, "natural(general)")
        return reply
    return None


def _last_turn(request: Request, payload: Any) -> dict[str, Any]:
    """The previous answer's context: the one the client sent back, else the one the server kept (contract.recall)."""
    from app.agents.applicant.copilot.answering import contract as _ak_contract

    last = dict(payload.context) if isinstance(getattr(payload, "context", None), dict) else {}
    key = getattr(request.state, "chat_key", None)
    if not (last.get("last_intent") or last.get("last_query_type")) and key:
        try:
            last = _ak_contract.recall(key[0], key[1]) or {}
        except Exception:  # noqa: BLE001
            last = {}
    return last


def _after_knowledge(request: Request, payload: Any) -> bool:
    """Whether this chat's previous answer was a knowledge / process answer."""
    last = _last_turn(request, payload)
    return (str(last.get("last_query_type") or "").upper() in _KNOWLEDGE_TYPES
            or str(last.get("last_intent") or "").upper() in _KNOWLEDGE_INTENTS)


def _after_clarification(request: Request, payload: Any) -> bool:
    """Whether the previous answer ASKED something ("which one do you mean?"): the reply is its answer, read by the
    conversation layer -- never re-read as a new question (party_kyc v_other_one, 2026-10-14)."""
    last = _last_turn(request, payload)
    return ("CLARIF" in str(last.get("last_query_type") or "").upper()
            or "CLARIF" in str(last.get("last_intent") or "").upper()
            or str(last.get("last_intent") or "").upper() in {"DID_YOU_MEAN", "WHICH_ACTION", "VAGUE_OPTIONS"})


def _co_followup(message: str) -> bool:
    """A SHORT follow-up naming only the co-applicant ("aur co-applicant ka?", "and the co-applicant?"): on a case
    without one, "there is no co-applicant" -- never the co-applicant canonical's "nothing pending" (2026-10-14)."""
    from app.agents.applicant.copilot.routing import subjects as _co_subjects
    from app.security import guardrails as _co_guard

    try:
        if not (len(str(message or "").split()) <= 4 and _co_subjects.mentioned(message) == _co_subjects.Kind.CO):
            return False
        # ONLY A FOLLOW-UP THAT NAMES THE PARTY ("and the co-applicant?", "aur co-applicant ka?") -- an existence
        # question ("is there a co-applicant?", "co-applicant hai kya?") is the profile's to answer
        rest = re.sub(r"(?i)\bco[\s-]?applicant'?s?\b", " ", str(message))
        if set(re.findall(r"[a-z]+", rest.lower())) - {"and", "aur", "the", "ka", "ki", "ke", "what", "about", "how",
                                                       "for", "of", "so", "ok", "okay", "then", "his", "her"}:
            return False
        # A REFUSED REQUEST READS NOTHING ("List all co-applicants." -- bulk): this shortcut reads the case, so it
        # runs only for a message the input guardrail allows (party_kyc s_all_co, 2026-10-14)
        return bool(_co_guard.check_input(" ".join(str(message).split())).allowed)
    except Exception:  # noqa: BLE001
        return False


def _ag_profile_field(said: str) -> bool:
    """Whether the message asks for ONE recorded application / applicant field (answering/profile.py detect)."""
    from app.agents.applicant.copilot.answering import profile

    try:
        asked = profile.detect(said)
    except Exception:  # noqa: BLE001
        return False
    return asked is not None and str(getattr(asked, "field", "") or "") not in ("", profile.ALL)


async def _understood(message: str, claims: dict[str, Any], payload: Any, request_id: str):
    """The meaning Decision for a typed message, or None (meaning off / a command / a pending reply / not understood)."""
    from app.agents.applicant.copilot.semantics import meaning as _meaning

    if not _meaning.enabled():
        return None
    if getattr(payload, "co_applicant_id", None) or getattr(payload, "party_id", None):
        return None                 # a named party: its authorization decides first, never a rewrite
    from app.agents.applicant.copilot.capabilities import general as _gen
    from app.agents.applicant.copilot.capabilities import workspace as _ws_m

    state = _gen._state(claims, payload.context)
    said = " ".join(str(message or "").split())
    # a reply to a question the bot asked ("1", "yes", "ok", a pick, a pending draft) is never re-understood
    from app.agents.applicant.copilot.capabilities import vague as _vq

    # a pick / yes / no / ok answers the waiting question; a question that could answer it (one of its options'
    # words, a draft, a "which case?" pick) stays with it -- any OTHER message is a new question, understood here
    if re.fullmatch(r"(?i)[\d\s.,]+|yes|no|ok|okay|haan|nahi", said) or _vq.waits_for(said, state, claims,
                                                                                   payload.context):
        return None

    from app.agents.applicant.copilot.semantics import task as _und_task

    if _und_task.enabled() and _und_task.social_kind(said):
        return None                 # "take care" / "namaste ji": the conversation's own answer, never a case intent
    selects = _und_task.enabled() and _und_task.asks_over_cases(said)
    if _vq.is_command(said, case_open=bool(state.active_case_id or payload.case_id)) and not selects:
        # open / close / a case id / a list / a new case: the workspace and the forms -- but "WHICH applications can
        # go to CPA now?" is a QUESTION over the cases (task.selectors), read by meaning + plural scope
        return None
    from app.agents.applicant.copilot.capabilities import case_list as _ord_list

    if _ord_list.ordinal_pick(said, getattr(state, "listed_case_ids", None) or []):
        # "why is the first one pending?" after a list -- and "and the first one?" after a row of it was OPENED
        # (audit 2026-10-11): the workspace picks that row of the list shown, never a canonical rewrite
        return None
    bare = re.sub(r"[^\w\s]", " ", said.lower()).split()
    if bare and " ".join(bare) in {str(w).lower() for w in _meaning.catalogue().get("state_followups") or []}:
        return None                 # "why?" / "kyun?": answered from the previous answer (rules), never re-understood
    if _vq.names_a_case(said, claims):
        return None                 # "Priya ka pending": the NAMED applicant's case -- a canonical would drop the name
    from app.agents.applicant.copilot.semantics import intents as _cmp_intents

    if _cmp_intents.compound_parts(said):
        return None                 # two questions in one: the splitter answers BOTH -- one canonical would drop half
    from app.agents.applicant.copilot.answering import profile as _cmp_profile

    _fields = _cmp_profile.detect(said)
    if _fields is not None and len(_fields.fields) >= 2:
        return None                 # two RECORDED fields ("who is the applicant and what loan ...?"): one profile
        #                             answer carries both -- a single canonical would drop one of them
    decision = await _meaning.understand(said, case_open=bool(state.active_case_id or payload.case_id),
                                         previous=(getattr(state, "flow", None) or {}).get("last_meaning"),
                                         request_id=request_id)
    if decision is not None:
        _meaning.remember(state, decision)
        _ws_m._save(state)
    return decision


def _task_social(message: str) -> bool:
    from app.agents.applicant.copilot.semantics import task as _task

    return _task.plain_social(message)


def _task_args(claims: dict[str, Any], payload: Any) -> tuple[Any, dict[str, Any]]:
    from app.agents.applicant.copilot.capabilities import general as _tg

    state = _tg._state(claims, payload.context)
    return state, {"case_open": bool(state.active_case_id or payload.case_id),
                   "listed": list(getattr(state, "listed_case_ids", None) or []),
                   "previous": (getattr(state, "flow", None) or {}).get("last_task"),
                   "list_query": getattr(state, "list_query", None) or None}


def _task_turn(request: Request, message: str, claims: dict[str, Any], payload: Any) -> str:
    """
    THE STRUCTURED TASK, BEFORE ANY HANDLER (Phase 2; semantics/task.py). Returns the message the handlers answer:
      a PORTFOLIO FOLLOW-UP ("what about the stuck ones?" after a count) -> the deterministic question of the previous
        operation with the new filter ("how many cases stuck?") -- answered by the case insights / list, own rows only;
      a CORRECTION to another row of the list just shown ("no, I meant the second one") -> "open <that row>": the
        workspace opens it after its usual ownership check;
      a bare NEGATION with softeners ("nah not really") -> "no": the conversation contract's NEGATION;
      anything else -> unchanged (the meaning layer reads it later; the task is completed there).
    Off (task.enabled false) or any failure: the message as typed.
    """
    from app.agents.applicant.copilot.capabilities import general as _tg
    from app.agents.applicant.copilot.semantics import task as _task

    if not _task.enabled() or getattr(payload, "co_applicant_id", None) or getattr(payload, "party_id", None):
        return message
    try:
        state, args = _task_args(claims, payload)
        # A TYPO of a word the case list knows ("show my oldst cases"): corrected ONLY when that makes the list /
        # the insights understand what the typed words did not -- never a name, never anything else
        from app.agents.applicant.copilot.capabilities import case_insights as _t_ins
        from app.agents.applicant.copilot.capabilities import case_list as _t_list

        fixed = _task.respelled(message)
        if fixed != message and ((_t_list.understand(message) is None and _t_list.understand(fixed) is not None)
                                 or (not _t_ins.kind_of(message) and _t_ins.kind_of(fixed))):
            message = fixed
            request.state.copilot_message = message
        t = _task.interpret(message, None, **args)
    except Exception:  # noqa: BLE001 - the task is an aid: the message goes on as typed
        logger.warning("task: interpretation failed; the message goes on as typed", exc_info=True)
        return message
    request.state.task = t
    if (getattr(state, "flow", None) or {}).get("write"):
        return message                        # a draft / move waiting for Confirm: its own form reads every message
    if t.scope == "unresolved" and not _tg._pending(state, claims, payload.context) and _task.unnamed_action(message):
        # "sort this out for me" / "ye sambhal lo": an action asked that names none -> "which action?" with the supported
        # options, before anything reads a case (a pending question / pick answers it instead)
        t.scope, t.operation, t.intent, t.decided_by = "none", "clarify", "unnamed_action", "unnamed_action"
        t.ambiguities = ["which_action"]
        request.state.task_unnamed_action = True
        return message
    rewritten = None
    if t.references.get("kind") == "previous_task" and t.filters:
        rewritten = _task.canonical_for(t.operation, t.filters[0])
    elif t.correction and t.references.get("kind") == "ordinal" and t.references.get("case_id"):
        rewritten = f"open {t.references['case_id']}"
    elif t.operation == "negate" and not _tg._only(message, "negation"):
        rewritten = "no"
    elif t.operation == "greet" and t.decided_by == "conversation_classifier":
        rewritten = "hello"                   # "namaste ji" / "ram ram bhai": the same greeting as a typed hello
    elif t.operation == "farewell" and t.decided_by == "conversation_classifier":
        # "bye" / "ok bye" / "take care": answered BEFORE any case-name match or case read -- a case named "Rohan Bye"
        # is opened only by asking for it ("open Rohan Bye"), never by a goodbye
        request.state.task_farewell = True
    if t.scope in ("portfolio", "one_case", "knowledge"):
        _task.remember(state, t)              # the next turn's "what about ...?" refers to THIS task
        from app.agents.applicant.copilot.capabilities import workspace as _tws

        _tws._save(state)
    if rewritten:
        request.state.copilot_message = rewritten
        return rewritten
    return message


def _current_party_turn(message: str, claims: dict[str, Any], payload: Any) -> str | None:
    """
    The message with "this borrower" read as "this case" -- or None. All must hold: the message points at ONE current
    party (this / my / our + the noun, request_policy.current_party_neutral), that neutral text passes the full input
    guardrail on its own, and a case is open that the caller is authorized for NOW (the workspace's open case, else
    the request's case id -- each re-authorized; a case id the caller does not hold is no context).
    """
    from app.agents.applicant.copilot.capabilities import workspace as _pws
    from app.security import access as _paccess
    from app.security import guardrails as _pguard
    from app.security import request_policy as _ppolicy

    neutral = _ppolicy.current_party_neutral(message)
    if neutral is None or not _pguard.check_input(
            neutral, allowed_ids=(payload.case_id, payload.applicant_id)).allowed:
        return None
    workspace_id = (payload.context or {}).get("workspace_id") if isinstance(payload.context, dict) else None
    if _pws.enabled() and _pws.active_case(claims, workspace_id) is not None:
        return neutral
    if payload.case_id:
        try:
            _pws.authorize(claims, payload.case_id)
        except _paccess.AccessDenied:
            return None
        return neutral
    return None


def _names_own_case(message: str, claims: dict[str, Any]) -> bool:
    """A name in the message ("tell me about Rohan Mehta") that is one of the caller's OWN applicants: an LOS question,
    never a general one. Only the caller's own case names are compared (no case data is read); a message with no
    capitalised word after its first is not looked up at all."""
    if not re.search(r"\s[A-Z][a-z]+", str(message or "")):
        return False
    try:
        from app.agents.applicant.copilot.capabilities import workspace as _nws

        picked = _nws._by_name(message, _nws.my_cases(claims))
        return bool(getattr(picked, "application", None) or getattr(picked, "candidates", None))
    except Exception:  # noqa: BLE001 - unsure: not a general question
        return True


def _farewell_reply(request_id: str, payload: Any) -> dict[str, Any]:
    """The goodbye, from nothing: no case lookup, no tool, no record read (conversation.py's own reply)."""
    from app.agents.applicant import conversation as _conv
    from app.agents.applicant.copilot.capabilities import workspace as _fws

    language = payload.response_language or "en"
    text, _ = _conv.reply(_conv.GOODBYE, language if _conv.has_reply(_conv.GOODBYE, language) else "en")
    return _fws._base(request_id, _conv.GOODBYE, text, query_type="CONVERSATION", category="CONVERSATION",
                      response_source="CONVERSATION", tools_invoked=[])


def _task_with_meaning(request: Request, message: str, decision: Any, claims: dict[str, Any], payload: Any) -> None:
    """The task completed with the semantic reading (when the early signals left it unresolved); remembered."""
    from app.agents.applicant.copilot.semantics import task as _task

    if not _task.enabled():
        return
    early = getattr(request.state, "task", None)
    if early is not None and early.scope != "unresolved":
        return
    try:
        state, args = _task_args(claims, payload)
        t = _task.interpret(message, decision, **args)
        request.state.task = t
        _task.remember(state, t)
        from app.agents.applicant.copilot.capabilities import workspace as _tws

        _tws._save(state)
    except Exception:  # noqa: BLE001 - diagnostics / follow-up aid only
        logger.warning("task: completion failed", exc_info=True)


def _no_case_turn(claims: dict[str, Any], request_id: str, payload: Any, message: str) -> Any:
    """
    No case open and the request needs one: (case_id, applicant_id) when the officer has exactly ONE case (answered
    for it), else the reply to send -- "Which case ...?" over the recent cases with the question kept for the pick,
    or, with no case at all, the plain fact + Create New Case.
    """
    from app.agents.applicant.copilot.answering import contract as _nc_contract
    from app.agents.applicant.copilot.answering import language_lock as _nc_lock
    from app.agents.applicant.copilot.capabilities import workspace as _nc_ws

    owned = _nc_ws.my_cases(claims)
    if len(owned) == 1:
        return owned[0].case_id, owned[0].applicant_id
    if owned:
        asked = _nc_ws.ask_which_case(claims, request_id, payload.context, message)
        if asked is not None:
            return asked
    reply = _nc_ws._base(request_id, "NO_CASES", _nc_ws._label("no_case"), query_type="CONVERSATION",
                         tools_invoked=["application.list"])
    reply["faq_block"] = _nc_contract.link("new_case", _nc_lock.current() or "en")
    return reply


def move_case_stage(confirmed: dict[str, Any], request_id: str) -> dict[str, Any]:
    """
    Execute a stage move the officer confirmed in chat (capabilities/stage_flow.py proposed it, checked the
    configured move permission and the case's ownership). The transition re-evaluates the gate itself: a case that
    stopped being ready is never moved. Audited; written to the case's activity log.
    """
    from app.agents.applicant.copilot.answering import language_lock as _mv_lock
    from app.agents.applicant.copilot.capabilities import case_form as _mv_form
    from app.agents.applicant.copilot.capabilities import stage_flow as _mv_flow
    from app.agents.los import stage_lifecycle

    move = confirmed["execute_move"]
    case_id, here, there, actor = move["case_id"], move["from"], move["to"], move["actor"]
    lang = _mv_lock.current() or "en"
    reply = {k: v for k, v in confirmed.items() if k != "execute_move"}
    def stage_now() -> str | None:
        """The case's stage READ BACK from the store (never assumed from the call that wrote it), or None."""
        try:
            from app.agents.los import stages as _mv_stages
            from app.store import request_cache as _mv_cache

            _mv_cache.invalidate()
            found = _mv_stages.resolve(case_id).stage
            return str(getattr(found, "value", found) or "").upper() or None
        except Exception:  # noqa: BLE001 - unreadable: the outcome stays unknown, never guessed
            return None

    # THE GATE AGAIN, AT THE MOVE (Phase 4 audit, 2026-10-13): the transition service re-checks it only while
    # LOS_STAGE_GATE_IN_SERVICE is on; a case that stopped passing between the proposal and the Confirm (up to the
    # draft TTL) is never moved, whatever that flag says. Unreadable gate = no move (fail closed).
    try:
        _gate_now = _mv_flow.gate(case_id)
    except Exception as exc:  # noqa: BLE001
        _gate_now = {"status": "GATE_UNAVAILABLE", "blockers": [], "error": type(exc).__name__}
    if str(_gate_now.get("status") or "") != "PASS":
        _why = ", ".join(str(c.get("label")) for c in _gate_now.get("blockers") or [] if c.get("label")) \
            or str(_gate_now.get("status") or "gate not passed")
        audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id, intent="STAGE_MOVE",
                     tools=["los.stage"], write=True, status="REFUSED", detail=f"gate {_gate_now.get('status')}")
        return {**reply, "intent": "STAGE_MOVE_REFUSED", "answer": _mv_flow.move_failed_text(case_id, _why, lang)}
    try:
        stage_lifecycle.transition(case_id, there, reason=f"{here} gate passed; confirmed in chat by {actor}",
                                   actor=actor, source="OPERATOR", expected_stage=here,
                                   idempotency_key=f"chat-move-{move['ref']}", request_id=request_id)
    except stage_lifecycle.StageTransitionError as exc:
        audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id, intent="STAGE_MOVE",
                     tools=["los.stage"], write=True, status="REFUSED", detail=exc.code)
        return {**reply, "intent": "STAGE_MOVE_REFUSED", "answer": _mv_flow.move_failed_text(case_id, exc.message, lang)}
    except Exception as exc:  # noqa: BLE001 - a timeout / dropped connection: RECONCILED from the stored stage
        logger.warning("chat move %s -> %s raised %s; reconciling from the stored stage", case_id, there,
                       type(exc).__name__)
        now = stage_now()
        if now != str(there).upper():
            status_ = "FAILED" if now == str(here).upper() else "UNKNOWN"
            audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id,
                         intent="STAGE_MOVE", tools=["los.stage"], write=True, status=status_,
                         detail=f"{here}->{there} {type(exc).__name__}; stage now {now or 'unreadable'}")
            text = (_mv_flow.move_failed_text(case_id, type(exc).__name__, lang) if status_ == "FAILED"
                    else _mv_flow.move_unknown_text(case_id, there, lang))
            return {**reply, "intent": "STAGE_MOVE_" + status_, "answer": text}
    # SUCCESS IS SAID ONLY WHEN THE STORE SAYS SO: the stage read back must be the target
    if stage_now() != str(there).upper():
        audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id, intent="STAGE_MOVE",
                     tools=["los.stage"], write=True, status="UNKNOWN", detail=f"{here}->{there} not read back")
        return {**reply, "intent": "STAGE_MOVE_UNKNOWN", "answer": _mv_flow.move_unknown_text(case_id, there, lang)}
    audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id, intent="STAGE_MOVE",
                 tools=["los.stage"], write=True, confirmed=True, status="OK", detail=f"{here}->{there}")
    _mv_form.activity(case_id, "STAGE_MOVED", f"{here} -> {there} by {actor} (chat)")
    return {**reply, "intent": "STAGE_MOVED", "tools_invoked": ["los.stage"],
            "answer": _mv_flow.moved_text(case_id, here, there, lang)}


def _answered_before(claims: dict[str, Any], context: dict[str, Any] | None) -> bool:
    """Whether this chat's conversation state holds a previous ANSWER (its intent) a bare word can follow up on."""
    from app.agents.applicant.copilot.conversation import state as _conv
    from app.security.auth import get_subject     # was missing: the NameError was swallowed -> always False

    if isinstance(context, dict) and context.get("last_intent"):
        return True                     # the previous reply's own context names what it answered (echoed back)
    conversation_id = (context or {}).get("conversation_id") if isinstance(context, dict) else None
    if not conversation_id:
        return False
    try:
        found = _conv.STORE.get(str(get_subject(claims) or "anonymous"), conversation_id)
    except Exception:  # noqa: BLE001 - no state: nothing to follow up on
        return False
    return bool(found is not None and (found.last_answer_reference or {}).get("intent"))


def _attach_next_step_upload(reply: dict[str, Any]) -> None:
    """
    "Ab kya karna hai?" -> "Next step: collect and upload the Signature": the step is offered as its action -- the
    Upload link of the document the answer names (the case's own document actions; re-upload rows first), the same
    rule as the case brief. Nothing is added when the answer names no actionable document.
    """
    from app.agents.applicant.copilot.capabilities import product_flow as _pu_flow

    intent = str(reply.get("intent") or "")
    _policy = _pu_flow.cfg().get("link_policy") or {}
    every = {str(i) for i in _policy.get("pending_uploads_on") or []}
    # ANY CASE ANSWER THAT SAYS SOMETHING IS PENDING / TO UPLOAD (owner, 2026-10-14: "kuch bhi pending hai ya docs, toh
    # neeche upload aana" -- the KYC answer said "upload the documents" with no button): the case's own pending /
    # re-upload documents as Upload buttons. Never on a reply that asks back, refuses or is small talk.
    cues = [str(w).lower() for w in _policy.get("upload_cue_words") or []]
    said_low = str(reply.get("answer") or "").lower()
    quiet = {str(i) for i in _policy.get("no_upload_buttons_on") or []}
    if (intent not in every and intent != "NEXT_ACTION" and intent not in quiet and cues
            and any(re.search(rf"\b{re.escape(c)}", said_low) for c in cues)):
        every = every | {intent}
    if intent in every and reply.get("case_id") and not reply.get("document_actions"):
        # after an upload (config link_policy.pending_uploads_on): what is STILL pending / needs a re-upload, each as
        # its Upload button -- the officer goes straight on to the next document
        try:
            from app.agents.applicant.copilot.answering import document_actions as _pu_da

            view = _pu_da.build(str(reply["case_id"]))
        except Exception:  # noqa: BLE001 - the buttons are a convenience; the answer stands without them
            return
        if view.get("reupload") or view.get("pending"):
            reply["document_actions"] = {"reupload": list(view.get("reupload") or []),
                                         "pending": list(view.get("pending") or [])}
        return
    # "pan" / "signature uploaded?" -> "No PAN document has been uploaded": the status AND its Upload button together
    # (owner, 2026-10-14; config link_policy.named_document_upload_on) -- only for a document the case's own document
    # actions list as pending / to re-upload, so a verified document never gets one
    named = {"NEXT_ACTION"} | {str(i) for i in (_pu_flow.cfg().get("link_policy") or {}).get(
        "named_document_upload_on") or []}
    if intent not in named or reply.get("document_actions") or not reply.get("case_id"):
        return
    try:
        from app.agents.applicant.copilot.answering import document_actions as _next_da

        view = _next_da.build(str(reply["case_id"]))
    except Exception:  # noqa: BLE001 - the link is a convenience; the answer stands without it
        return
    said = str(reply.get("answer") or "").lower()
    for key in ("reupload", "pending"):
        for row in view.get(key) or []:
            label = str((row or {}).get("label") or "").lower()
            if label and label in said:
                reply["document_actions"] = {key: [row]}
                return


def _turn_layer(request: Request, layer: str) -> None:
    """The layer that answered this turn (observability/turn_diagnostics.py; recorded only, never shown by default)."""
    from app.observability import turn_diagnostics as _td

    _td.mark(request, layer)


def _published_reply(request: Request, reply: Any) -> Any:
    """MASTER SPEC section 8: remember the chat's context server-side, then publish {request_id, markdown, tts}."""
    from app.agents.applicant.copilot.answering import contract as _contract

    if not _contract.enabled() or not isinstance(reply, dict):
        return reply
    if not (reply.get("simple") or getattr(request.state, "simple_answered", False)):
        # another layer answered: the simple pipeline's "why?" / "which one first" no longer follow its last answer
        from app.agents.applicant.copilot.simple import pipeline as _sp_forget

        _sp_forget.forget(_agent_chat_key(request))
    _attach_next_step_upload(reply)
    key = getattr(request.state, "chat_key", None)
    if key and isinstance(reply.get("context"), dict):
        _contract.remember(key[0], key[1], reply["context"])
    reply.setdefault("_question", str(getattr(request.state, "copilot_message", "") or ""))
    # EVERY REPLY CHECKED AGAINST THE CASE'S FACTS (2026-11-06): whichever layer wrote it, a wrong stage / pending count
    # / loan amount / readiness claim about this case is corrected here, once, from facts/case_facts.py
    from app.agents.applicant.copilot.answering import fact_check as _fact_check

    if _fact_check.enabled():
        try:
            _fact_check.check_reply(reply, reply.get("case_id") or getattr(request.state, "fact_case_id", None))
        except Exception:  # noqa: BLE001 - a checker failure never blocks the reply
            logger.info("fact_check failed; reply published unchecked", exc_info=True)
    published = _contract.publish(reply, chat_key=key)
    if getattr(request.state, "with_sources", False) and isinstance(published, dict):
        # OPT-IN (contract 1.1): the handbook sources retrieval cited for this answer; none -> the key is left out
        _cited_sources = _contract.sources(reply)
        if _cited_sources:
            published["sources"] = _cited_sources
    from app.observability import turn_diagnostics as _td

    if _td.enabled() and isinstance(published, dict):
        # OPT-IN, NEVER IN PRODUCTION: which layer answered and why -- codes and names only, no text, no ids
        published["diagnostics"] = _td.summary(reply, request)
    from app.agents.applicant.copilot.answering import realtime as _realtime

    if key:
        # section 11: kept for replay (reconnect) and the Idempotency-Key; the case is watched for pushed updates
        _realtime.keep(key[0], published, getattr(request.state, "idempotency_key", None))
        if reply.get("case_id"):
            try:
                _realtime.watch(key[0], key[1], str(reply["case_id"]))
            except Exception:  # noqa: BLE001 - watching is a convenience; the answer stands
                pass
    return published


def _resolve_co_applicant(payload: "CopilotRequest", claims: dict[str, Any], request_id: str,
                          case_id: str | None, message: str, action: "FosAction"):
    """
    ((case_id, co_applicant_id) | None, case_id, message) for this turn.

    An id (request field, or typed: "COAPP-1023 ke docs kya baaki hai?") is resolved
    through the case and refused with 403 unless the caller owns it; a name ("Priya ke
    docs") is matched only on a case the caller is already authorized for. Either way
    the reference is rewritten to "co-applicant", so the existing party routing answers.
    """
    from app.agents.los import co_applicants as _co
    from app.security import access as _acc
    from app.security.auth import get_scopes, get_subject

    subject, scopes = get_subject(claims), get_scopes(claims)
    requested = [str(payload.co_applicant_id).strip().upper()] if payload.co_applicant_id else []
    typed = _co.ids_in(message) if action is FosAction.CUSTOM_QUERY else []
    resolved = None
    for co_id in dict.fromkeys(requested + typed):
        try:
            found = _acc.authorize_co_applicant(subject, scopes, co_id, case_id=case_id, conversation=True)
        except _acc.AccessDenied as denied:
            audit.record(request_id=request_id, subject=subject, applicant_id=payload.applicant_id,
                         case_id=case_id, intent="CO_APPLICANT_ACCESS", tools=[], write=False,
                         status="DENIED", detail=denied.code)
            raise _acc.http_denied(denied, request_id) from None
        if resolved and resolved[0] != found:            # two ids on two different cases
            raise _acc.http_denied(_acc.AccessDenied(), request_id)
        case_id, resolved = found, (found, co_id)
        if co_id in typed:
            message = _co.replace_reference(message, co_id)
    if case_id and action is FosAction.CUSTOM_QUERY:
        try:
            _acc.authorize(subject, scopes, case_id=case_id)
        except _acc.AccessDenied:
            return resolved, case_id, message           # refused later, exactly as before
        named = _co.names_in(message, _co.list_for_case(case_id))
        if len(named) == 1:                             # two people with that name: never guessed
            full = named[0]["name"].strip()
            message = _co.replace_reference(_co.replace_reference(message, full), full.split()[0])
            resolved = resolved or (case_id, named[0]["co_applicant_id"])
    return resolved, case_id, message


def _party_recognition_on() -> bool:
    import os

    return (os.getenv("COPILOT_PARTY_RECOGNITION", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _same_name_question(case_id: str | None, message: str, request_id: str, claims: dict[str, Any]) -> dict | None:
    """
    6g (COPILOT_PARTY_RECOGNITION): a NAME that fits more than one person on the case -- two
    co-applicants, or the applicant and a co-applicant -- is asked back once, never guessed.
    Only on a case the caller may open; the names come from the case record.
    """
    from app.agents.los import co_applicants as _co
    from app.security import access as _acc
    from app.store import get_repository

    if not (case_id and _party_recognition_on() and _co.enabled()):
        return None
    try:
        _acc.authorize_claims(claims, case_id=case_id)
    except _acc.AccessDenied:
        return None
    people = [{"label": f"Co-applicant {c['name']} ({c['co_applicant_id']})", "name": c["name"]}
              for c in _co.list_for_case(case_id) if c.get("name")]
    application = get_repository().get_application(case_id)
    applicant = get_repository().get_applicant(application.applicant_id) if application else None
    if applicant is not None and applicant.full_name:
        people.append({"label": f"Applicant {applicant.full_name}", "name": applicant.full_name})
    words = set(re.findall(r"[a-z]+", str(message or "").lower()))
    hits = [p for p in people if any(part.lower() in words for part in str(p["name"]).split() if len(part) >= 3)]
    if len(hits) < 2:
        return None
    options = [h["label"] for h in hits[:3]]
    question = "Which person do you mean?"
    return {"request_id": request_id, "case_id": case_id, "intent": "UNKNOWN", "category": "UNSUPPORTED",
            "query_type": "CLARIFICATION", "answer": question + "\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1)),
            "response_source": "CONVERSATION", "documents": [], "actions": [], "errors": [], "tools_invoked": [],
            "suggested_questions": options,
            "clarification_required": {"reason": "PARTY_AMBIGUOUS", "question": question, "options": options}}


def _co_applicant_header(request: Request, published: dict[str, Any]) -> dict[str, Any]:
    """👥 **Co-applicant: <name> (<id>)** on an answer about a co-applicant (LOS_COAPP_IDENTITY)."""
    from app.agents.applicant.copilot.answering import attention as _attention
    from app.agents.los import co_applicants as _co

    if not _co.enabled() or not published.get("case_id"):
        return published
    asked = getattr(request.state, "co_applicant", None)
    if not asked and _attention._asked_party(published) != "CO_APPLICANT":
        return published
    records = _co.list_for_case(published["case_id"])
    if asked:
        record = next((r for r in records if r["co_applicant_id"] == asked["co_applicant_id"]), None)
        co_id = asked["co_applicant_id"]
    elif len(records) == 1:
        record, co_id = records[0], records[0]["co_applicant_id"]
    else:
        return published
    said = _co.header(record, co_id)
    published["co_applicant"] = {"co_applicant_id": co_id, "name": (record or {}).get("name")}
    for key, line in (("answer", said["plain"]), ("answer_plain", said["plain"]),
                      ("answer_markdown", said["markdown"])):
        if isinstance(published.get(key), str) and not published[key].startswith("👥"):
            published[key] = f"{line}\n{published[key]}"
    return published


async def _copilot_json(
    request: Request,
    claims: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """Every action except upload."""
    try:
        payload = CopilotRequest.model_validate(await request.json())
    except Exception as exc:
        # WHICH field, so the frontend can fix it from the browser (names and pydantic's reason only -- never values)
        from pydantic import ValidationError

        fields = [{"field": ".".join(str(p) for p in e.get("loc") or ()), "problem": str(e.get("msg") or "")}
                  for e in exc.errors()] if isinstance(exc, ValidationError) else []
        raise HTTPException(422, detail={
            "request_id": request_id, "error": "INVALID_REQUEST",
            "message": f"The request body could not be read: {type(exc).__name__}"
                       + (" (" + "; ".join(f"{f['field']}: {f['problem']}" for f in fields) + ")." if fields else "."),
            "fields": fields,
        }) from exc

    # MASTER SPEC section 8: an action link posted back becomes the request it stands for (one registry,
    # app/config/copilot_reply.yaml); every check below runs on it exactly as on a typed request
    from app.agents.applicant.copilot.answering import contract as _contract
    from app.security.auth import get_subject as _chat_subject

    if payload.action_link:
        mapped = _contract.request_for(payload.action_link)
        if mapped is None:
            raise HTTPException(422, detail={"request_id": request_id, "error": "INVALID_ACTION_LINK",
                                             "message": "This link is not a server action."})
        payload = CopilotRequest.model_validate({**payload.model_dump(exclude={"action_link"}), **mapped})
    _subject_key = str(_chat_subject(claims) or "anonymous")
    request.state.chat_key = (_subject_key, payload.chat_id)
    request.state.fact_case_id = payload.case_id
    request.state.with_sources = bool(payload.with_sources)
    if _contract.enabled():
        from app.agents.applicant.copilot.answering import realtime as _realtime

        _realtime.start(_subject_key, payload.chat_id, request_id)    # a newer message cancels an older stream
    if payload.new_chat:
        _contract.forget(_subject_key, payload.chat_id)
    elif _contract.enabled() and not (isinstance(payload.context, dict) and payload.context.get("conversation_id")):
        # the context the reply no longer carries. An EMPTY / partial context counts as none (Sprint 3A, real UI: the
        # React client sends `context: {}` on every turn, so every turn started a new conversation and "why?" had
        # nothing to refer to); the keys the client did send still win
        recalled = _contract.recall(_subject_key, payload.chat_id)
        if recalled:
            payload.context = {**recalled, **(payload.context or {})}
    if payload.chat_id:
        # ONE WORKSPACE PER CHAT (MASTER SPEC 4: a new chat starts fresh): the open case, the last list, a pending
        # question or draft never leak from one chat into another. An explicit workspace_id still wins.
        payload.context = {**(payload.context if isinstance(payload.context, dict) else {})}
        payload.context.setdefault("workspace_id", str(payload.chat_id)[:128])

    action = payload.action

    # THE LANGUAGE LOCK (FOS plan section 2): the selected language, for every text this turn writes
    from app.agents.applicant.copilot.answering import language_lock as _lock

    locked = _lock.set_for_request(payload.reply_language, payload.response_language)
    if locked:
        payload.response_language = locked

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
    request.state.copilot_message = message if action is FosAction.CUSTOM_QUERY else ""   # post-steps read it
    request.state.copilot_typed = request.state.copilot_message      # as TYPED, before any meaning rewrite
    if action is FosAction.CUSTOM_QUERY and (payload.case_id or payload.applicant_id):
        # "what documents can he give for THE FIRST ONE" right after a list of documents: that document, by its place
        # in the list the previous reply showed (followup.ordinal_document) -- every layer below reads the document's
        # name; a model rewrite had made it "for it" (2026-10-23)
        from app.agents.applicant.copilot.conversation import followup as _ord_followup

        _ord_doc = _ord_followup.ordinal_document(message, _last_turn(request, payload).get("last_documents"))
        if _ord_doc:
            message = _ord_doc
            request.state.copilot_message = message

    # THE ABUSE RULE, FIRST (MASTER SPEC sections 16 / 19; capabilities/abuse_guard.py): a message with foul
    # language is never answered in any part -- no case read, no tool, no model, no pending intent stored
    from app.agents.applicant.copilot.capabilities import abuse_guard as _abuse_guard

    if action is FosAction.CUSTOM_QUERY:
        screened = _abuse_guard.screen(message, _subject_key, request_id, payload.case_id,
                                       lang=payload.response_language)
        if screened is not None:
            request.state.chat_key = None                  # nothing of this turn is remembered or replayed
            return screened

    # 6h GUARDRAIL HARDENING (COPILOT_GUARDRAIL_HARDENING): rate limit, self-harm care, threats,
    # social engineering, abuse cooldown -- BEFORE anything is read
    from app.agents.applicant.copilot.capabilities import safety as _safety

    if _safety.enabled() and action is FosAction.CUSTOM_QUERY:
        from app.security.auth import get_subject as _safety_subject

        screened = _safety.screen(message, str(_safety_subject(claims) or "anonymous"), request_id, payload.case_id)
        if screened is not None:
            return screened

    # NO CASE ID (COPILOT_SINGLE_CASE_RESOLVE, default off; capabilities/case_pick.py):
    # one case of the applicant's is answered for, several are listed and asked about.
    case_id, picked = payload.case_id, None

    # THE INPUT GUARDRAIL, BEFORE ANYTHING IS READ (MASTER SPEC 17.1 / 19): injection, encoded / other-language
    # attacks, prompt and tool leakage, SQL / code, role-play -- refused here exactly as /copilot/query refuses
    # them, with nothing read. An id typed in chat is NOT refused here: the workspace resolves it within the
    # caller's own cases and answers not-yours / unknown with the same neutral line (section 2).
    if action is FosAction.CUSTOM_QUERY:
        from app.agents.applicant.copilot.agent import GUARDRAIL_BLOCKED
        from app.security import guardrails as _early_guard

        from app.agents.applicant.copilot.capabilities import case_list as _scope_list

        verdict = _early_guard.check_input(message, allowed_ids=(payload.case_id, payload.applicant_id))
        others = _scope_list.names_other_people(message)
        if (not verdict.allowed and verdict.category is _early_guard.Category.BULK_DATA and not others
                and not _early_guard.names_protected_field(message)
                # only a request for the CASE LIST is the caller's own list (config guardrails.yaml own_list_nouns /
                # bulk_export_words); an encoded one, an export or "all customers" stays refused before any read
                and _early_guard.own_list_request(message, verdict.rule)):
            # "saare cases" / "list all case": the caller's OWN list (section 2) -- scoped by the grants
            verdict = _early_guard.Verdict(True)
        elif (not verdict.allowed and verdict.category is _early_guard.Category.CROSS_CUSTOMER_DATA and not others
              # a switch names no identity field: "the mobile number of the other applicant in my branch" is
              # another person's data, not a case switch (2026-11-06 live eval: it listed the officer's cases)
              and not _early_guard.names_protected_field(message)
              and _switches_case(message)):
            # an OFFICER's "doosre applicant ka status" / "pehle wale applicant par chalo" is ANOTHER OF THEIR OWN
            # cases: the workspace's switch / back (the "which case?" pick lists only the caller's own cases, and
            # every case read is authorised per case). A customer never reaches this route.
            verdict = _early_guard.Verdict(True)
        elif (not verdict.allowed and verdict.category is _early_guard.Category.CROSS_CUSTOMER_DATA
              and verdict.rule == "cross_0" and not others):
            # "what should I do NEXT with THIS borrower" (rule cross_0 reads "next ... borrower" as another person):
            # the open case's own party -- ONLY with a case the caller is authorized for open now, and only when the
            # message read with "this case" in place of "this borrower" passes the WHOLE input check again. That
            # neutral text is what every later layer sees, so the agent's own guardrail and every per-case
            # authorization still run on it; "next customer's PAN" / "the last borrower" stay blocked
            _neutral = _current_party_turn(message, claims, payload)
            if _neutral is not None:
                verdict = _early_guard.Verdict(True)
                message = _neutral
                request.state.copilot_message = message
        elif verdict.allowed and others:
            # "show cases of other officers": never answered with anyone's list (section 17.2)
            verdict = _early_guard.Verdict(False, _early_guard.Category.BULK_DATA, "other_people")
        # a named id / person is NOT decided here: it depends on the ids the caller holds in this case, which the
        # agent knows (and it remembers the refusal, so "that loan" next is refused too)
        if not verdict.allowed and verdict.category is not _early_guard.Category.UNAUTHORIZED_SUBJECT:
            audit.record(request_id=request_id, subject=_subject_key, applicant_id=None, case_id=None,
                         intent=GUARDRAIL_BLOCKED, tools=[], status="BLOCKED", detail=verdict.category.value)
            # the refusal is remembered like the agent's own, so a follow-up ("and that?") points at it
            from app.agents.applicant.copilot.conversation import state as _refusal_state

            _conv_id = (payload.context or {}).get("conversation_id") if isinstance(payload.context, dict) else None
            _found = _refusal_state.STORE.get(_subject_key, _conv_id) if _conv_id else None
            if _found is not None:
                _found.last_refusal = verdict.category.value
                _refusal_state.STORE.put(_found)
            return {"request_id": request_id, "intent": GUARDRAIL_BLOCKED, "case_id": None, "applicant_id": None,
                    "answer": _early_guard.refusal(verdict.category, verdict.rule), "category": "UNSUPPORTED",
                    "query_type": "CLARIFICATION", "response_source": "GUARDRAIL", "documents": [], "actions": [],
                    "tools_invoked": [], "suggested_questions": [],
                    "guardrail": {"stage": "input", "action": "BLOCKED", "category": verdict.category.value},
                    "errors": [{"code": "REQUEST_NOT_ALLOWED",
                                "message": _early_guard.refusal(verdict.category, verdict.rule)}]}

    if action is FosAction.CUSTOM_QUERY and message:
        # TYPOS / SMS SPELLING (owner, 2026-10-14; normalize.correct_typos): "pndng docs" -> "pending docs",
        # "signatre uploded" -> "signature uploaded". AFTER the input guardrail (it judged the words typed); only
        # misspelt words change, never the language. The typed message is kept for the history and the turn log.
        from app.agents.applicant import normalize as _entry_normalize

        request.state.typed_message = message
        _fixed = _entry_normalize.correct_typos(message)
        if _fixed.changed:
            message = _fixed.text
            request.state.copilot_message = message
            request.state.typo_changes = list(_fixed.changes)
        # what the officer ASKED, typo-fixed but never rewritten: the answer's shape (yes / no, "once done, can it",
        # why, how many) is read from these words -- a follow-up rewrite to "Is this case ready for CPA?" turned
        # "ye sab upload karne ke baad cpa ja sakta hai?" into a plain yes / no (2026-11-06 live eval)
        request.state.asked_message = message
        # STRONG ROUTING (owner 2026-10-25; simple_chat.yaml front / clear): a message the simple pipeline understands
        # or the meaning reader is sure of never waits on the Qwen follow-up reader / agent -- the deterministic layers
        # below answer it (the edit / case switch / list paging layers keep their turn first, then the simple pipeline)
        # DETERMINISTIC FIRST (2026-11-02): the handbook's own question and portfolio analytics are answered from the
        # approved source / the store BEFORE the Qwen follow-up reader -- with the model up it answered "What file
        # types can I upload?" with the product checklist (AGENT_ANSWER), and the polish model reworded an analytics
        # reply into a claim ("No cases were rejected")
        from app.agents.applicant.copilot.capabilities import analytics as _analytics
        from app.agents.applicant.copilot.capabilities import general as _det_general

        # THE HANDBOOK'S OWN QUESTION (golden U2): "Can I upload several documents at once?" is a faq.md heading
        _heading = await _det_general.heading_answer(message, request_id, payload.case_id)
        if _heading is not None:
            _det_general.moved_on(claims, payload.context)
            _turn_layer(request, "handbook_heading")
            return _heading
        # ANALYTICS (capabilities/analytics.py): a period / grouping / total / average / comparison over the officer's
        # own cases, or a follow-up of the last one ("compare with September") -- computed in code from the store,
        # read from the officer's own words (the task layer below may rewrite them)
        # AGENT INSIGHTS (2026-11-04; capabilities/agent_insights.py): what the AGENTS found -- one case across every
        # agent, one agent on a case, credit readiness, or a count / list over the officer's own cases ("how many cases
        # are high fraud risk?"). Read in code, each agent's latest result from its own table, a template reply whose
        # every number is checked against those facts: no model call, so as accurate on a CPU laptop as anywhere.
        import asyncio as _ai_asyncio

        from app.agents.applicant.copilot.capabilities import agent_insights as _agent_insights

        _ai_state = _det_general._state(claims, payload.context)
        _ai_conv = (payload.context or {}).get("conversation_id") if isinstance(payload.context, dict) else None
        _insight = await _ai_asyncio.to_thread(_agent_insights.answer, message, claims, request_id, state=_ai_state,
                                           open_case=payload.case_id, conversation_id=_ai_conv)
        if _insight is not None:
            _analytics.forget(_ai_state)
            _det_general.moved_on(claims, payload.context)
            _turn_layer(request, "agent_insights")
            return _insight
        _analytic = _analytics.answer(message, claims, request_id, state=_det_general._state(claims, payload.context),
                                      strong_only=True,
                                      conversation_id=(payload.context or {}).get("conversation_id")
                                      if isinstance(payload.context, dict) else None)
        if _analytic is not None:
            _det_general.moved_on(claims, payload.context)
            _turn_layer(request, "case_analytics")
            return _analytic
        # NATURAL CONVERSATION FIRST (owner 2026-11-02; capabilities/natural_chat.py): a social turn ("hows you ?") and
        # an ordinary question naming nothing of the LOS ("why is the sky blue?") -- or a short follow-up right after
        # such an answer ("give me one example of it") -- go to Qwen HERE, with the turn's whole model time. Later,
        # the follow-up reader / agent spent it first and the reply fell back to the fixed greeting + menu.
        _nat = await _natural_turn(request, message, claims, payload, request_id)
        if _nat is not None:
            return _nat
        request.state.clear_reading = await _clear_reading(request, message, claims, payload)
        # THE PLANNER (2026-11-03; copilot/planner): a message the deterministic readers are NOT sure of gets ONE
        # model call (intent + a registered read-only tool + validated arguments) in place of the follow-up reader /
        # agent / chooser chain (up to 6 calls). Clear messages never wait on it. Off -> the follow-up reader as before.
        from app.agents.applicant.copilot.planner import plan as _pl_plan

        if not request.state.clear_reading and _pl_plan.enabled():
            _planned = await _planner_turn(request, message, claims, payload, request_id)
            if _planned is not None:
                return _planned
            _followup = None
        else:
            _followup = None if request.state.clear_reading else \
                await _context_followup_turn(request, message, claims, payload, request_id)
        if _followup is not None:
            _turn_layer(request, "qwen_followup")
            return _followup

    # CASE INSIGHTS (2026-10-11, audit P2-C): "how many of my cases have a KYC problem?", "which case is closest to
    # CPA?", "compare CASE-A and CASE-B" -- counted / ranked / compared deterministically over the caller's OWN cases
    # (capabilities/case_insights.py), before a fragment layer can read them as "which case is this about?"
    if action is FosAction.CUSTOM_QUERY:
        from app.agents.applicant.copilot.capabilities import case_insights as _insights

        from app.agents.applicant.copilot.capabilities import general as _ins_general

        # THE STRUCTURED TASK (Phase 2; semantics/task.py): scope / operation / filter / reference of this turn in its
        # conversation. It re-asks only what the words alone mis-route; the deterministic handlers still answer
        message = _task_turn(request, message, claims, payload)
        if getattr(request.state, "task_farewell", False):
            audit.record(request_id=request_id, subject=_subject_key, applicant_id=None, case_id=None,
                         intent="GOODBYE", tools=[], status="CONVERSATION")
            _turn_layer(request, "conversation")
            return _farewell_reply(request_id, payload)
        if getattr(request.state, "task_unnamed_action", False) and not _misunderstood(message):
            from app.agents.applicant.copilot.capabilities import control as _ua_control

            from app.agents.applicant.copilot.answering import language_lock as _ua_lock

            # A CASE OPEN: the question back is about THAT case ("sort this out" got "top 5 cases / create a new
            # case", 2026-10-13); no case open: the general which-action choice
            _ua_direct = _ins_general.direct(message, claims, payload.context, request_id,
                                             _ua_lock.current() or "en", case_in_scope=payload.case_id)
            _turn_layer(request, "clarify_action(task)")
            if _ua_direct is not None:
                return _ua_direct
            return _ua_control.ask_which_action(_ins_general._state(claims, payload.context), request_id)
        # A GENERAL QUESTION (Phase 4 directive 2C): it names nothing of the LOS -> answered as an ordinary question
        # (handbook first, then the local model with ONLY the question), before any case is read; the open case is
        # kept and re-authorized when the officer comes back to it. A pending question / pick is answered first.
        # (OFF by default, owner 2026-10-13: only LOS content -- with it off the LOS pipeline answers as before; the
        # 2026-10-13 "not_los" refusal hook was REMOVED: it refused LOS questions its vocabulary missed, "What does
        # REVIEW mean?")
        if (_ins_general.general_chat_available() and _ins_general.is_general_question(message)
                and not _ins_general._pending(_ins_general._state(claims, payload.context), claims, payload.context)
                and not _names_own_case(message, claims)):
            _gc_reply = await _ins_general.general_chat(message, request_id, payload.response_language or "en",
                                                        chat_ref=_agent_chat_ref(request))
            if _gc_reply is not None:
                audit.record(request_id=request_id, subject=_subject_key, applicant_id=None, case_id=None,
                             intent=str(_gc_reply.get("intent") or "GENERAL_CHAT"), tools=[], status="CONVERSATION")
                _gc_task = getattr(request.state, "task", None)
                if _gc_task is not None:
                    _gc_task.scope, _gc_task.operation, _gc_task.decided_by = "general", "explain", "general_chat"
                _turn_layer(request, "general_chat")
                return _gc_reply
        # the ids of the list this chat was last shown: "compare the first two" compares THOSE rows
        _ins_listed = list(getattr(_ins_general._state(claims, payload.context), "listed_case_ids", None) or [])
        _insight = _insights.answer(message, claims, request_id, payload.response_language or "en",
                                    listed=_ins_listed)
        if _insight is not None:
            _ins_general.moved_on(claims, payload.context)       # a new topic: an older "which one?" is stale
            _turn_layer(request, "case_insights")
            return _insight

    # VAGUE / ONE-WORD MESSAGES (owner decision A; capabilities/vague.py): a pending option list is answered by the
    # pick (or the most likely option on a second vague message) BEFORE chat case creation, so "Create a new case"
    # picked by its number starts the form like the typed words would; the Upload option answers with its link
    if action is FosAction.CUSTOM_QUERY:
        from app.agents.applicant.copilot.capabilities import general as _vg_early
        from app.agents.applicant.copilot.capabilities import vague as _vague_early

        if _vague_early.enabled():
            _early_state = _vg_early._state(claims, payload.context)
            from app.agents.applicant.copilot.capabilities import product_flow as _pf_early
            from app.agents.applicant.copilot.capabilities import workspace as _ws_early

            if _pf_early.drop_if_not_answered(message, _early_state):
                _ws_early._save(_early_state)
            # "ruko, abhi action mat lena" WHILE a draft / move waits for Confirm: dropped before the form sees it
            if (_early_state.flow or {}).get("write") and not re.fullmatch(r"(?i)[\d\s.,]+", message.strip()):
                from app.agents.applicant.copilot.semantics import meaning as _hold_meaning

                if _hold_meaning.enabled():
                    _held = await _hold_meaning.understand(" ".join(message.split()), case_open=True, previous=None,
                                                           request_id=request_id)
                    if _held is not None and _held.kind == "control":
                        from app.agents.applicant.copilot.capabilities import control as _control_early

                        return _control_early.hold(_early_state, request_id)
            picked = _vague_early.resolve(message, _early_state,
                                          case_open=bool(_early_state.active_case_id or payload.case_id))
            if picked and _vague_early.is_upload(picked):
                return _vague_early.upload_reply(picked, request_id, payload.context, claims)
            if picked:
                message = picked
                request.state.copilot_message = message

    # CASE CREATION AND FIELD EDITS FROM CHAT (MASTER SPEC 15.5; capabilities/case_form.py, COPILOT_CHAT_CASE_CREATE):
    # the same fields and rules as the UI form, a summary, "Confirm?", then the UI form's own create route. Runs
    # BEFORE the workspace: while a draft is open, "Rahul Sharma" is the applicant's name, not a case to open.
    from app.agents.applicant.copilot.capabilities import case_actions as _form_ca
    from app.agents.applicant.copilot.capabilities import case_form as _case_form

    if _case_form.enabled() and action in (FosAction.CUSTOM_QUERY, FosAction.NEW_CASE):
        async def _create_from_chat(body: dict[str, Any]) -> Any:
            body = dict(body)
            key = body.pop("_idempotency_key", None)          # the draft's / sheet row's key (case_form)
            return await create_case(CreateCaseRequest.model_validate(body), claims, channel="chat",
                                     idempotency_key=key)

        from app.agents.applicant.copilot.answering import language_lock as _form_lock
        from app.agents.applicant.copilot.capabilities import faq as _form_faq

        # "naya case kaise banaye" asks HOW (the FAQ's steps), never starts a draft
        wants_new = action is FosAction.NEW_CASE or (
            _form_ca.enabled() and _form_ca.asks("new_case", message)
            and not (_form_faq.enabled() and _form_faq.answer_for(message, _form_lock.current() or "en")))
        from app.security import access as _form_access

        try:
            formed = await _case_form.chat_turn(action.value, message, claims, request_id, payload.context,
                                                _create_from_chat, wants_new)
        except _form_access.AccessDenied as exc:
            raise _form_access.http_denied(exc, request_id) from None
        if formed is not None and formed.get("execute_move"):
            # the officer confirmed a FOS -> CPA move in chat: made HERE, in the route layer (no copilot module
            # reaches the transition service), through the gated transition under the user's own identity
            return move_case_stage(formed, request_id)
        if formed is not None:
            return formed

    # THE GENERAL LAYER (owner 2026-10-08; capabilities/general.py): help / ok / frustration, role and stage from
    # the login, definitions and full forms, process steps from the live config, the product checklist, "whose
    # name?", unknown terms -- answered BEFORE any case logic, so they never meet "which case?"
    request.state.copilot_context = payload.context
    meaning_kind = None
    _knowledge_hint = None
    if action is FosAction.CUSTOM_QUERY and message:
        # QWEN CALLS THE TOOLS (owner, 2026-10-13; capabilities/qwen_agent.py): every question about documents, the
        # open case or the process -- Qwen chooses the tools (or asks one follow-up), the tools answer, Qwen words one
        # opening line. Commands, replies to a waiting question, greetings / thanks and case-list questions keep their
        # own fast paths; anything the agent cannot answer goes on to the pipeline below.
        # ONLY A DOCUMENT NAME (owner, 2026-10-13; capabilities/bare_document.py): a case open -> that document's
        # status question; no case open -> one follow-up question, never a guessed case
        from app.agents.applicant.copilot.answering import language_lock as _bare_lock
        from app.agents.applicant.copilot.capabilities import bare_document as _bare
        from app.agents.applicant.copilot.capabilities import general as _bare_general

        # THE GENERAL LAYER'S DIRECT ANSWERS (confusion / "can you handle it" on an open case, "is X accepted?"):
        # no model, no wait -- before the agent and the meaning step (both may ask Qwen)
        _open_case = _bare_general._state(claims, payload.context).active_case_id or payload.case_id
        if _open_case and _co_followup(message) and not re.search(r"(?i)^\s*(and|what about|how about)\b", message):
            # "aur co-applicant ka?" / "co-applicant ka?" -> the English follow-up the conversation layer already
            # reads correctly (it clarifies BEFORE any read, and says "no co-applicant" on a case without one); this
            # step itself reads nothing (conversation eval o1, variants: "read before clarifying", 2026-10-14)
            message = "and the co-applicant?"
            request.state.copilot_message = message
        _need = _coapp_need_reply(message, _open_case, request, payload, request_id) \
            or _only_applicant_reply(message, _open_case, request, payload, request_id, claims) \
            or _several_documents_reply(message, _open_case, request_id, claims)
        if _need is not None:
            _turn_layer(request, "general")
            return _need
        if _open_case and _bare_general._has(message, "after_that") and not _bare_general._has(message, "after_that_not") \
                and str(_last_turn(request, payload).get("last_intent") or "").upper() in _NEXT_STEP_INTENTS:
            # "ok after that?" RIGHT AFTER A NEXT-STEP / PENDING ANSWER (2026-10-14): once that is done, the case moves
            # on -- the stage after the case's current one (config stage_sequence), never the same step again
            from app.agents.applicant.copilot.capabilities import workspace as _at_workspace

            _stage = str(_at_workspace._stage(_open_case) or "").upper()
            _codes = {str(s.get("code")).upper() for s in _bare_general.cfg().get("stage_sequence") or []
                      if isinstance(s, dict)}
            if _stage in _codes:
                message = f"what comes after {_stage.lower()}?"
                request.state.copilot_message = message
        elif _bare_general._has(message, "after_that") and not _bare_general._has(message, "after_that_not") \
                and str(_last_turn(request, payload).get("last_intent") or "").upper() in _STAGE_ANSWER_INTENTS \
                and str(_last_turn(request, payload).get("last_stage") or "").upper():
            # "who reviews it after that?" RIGHT AFTER A PROCESS / HOW-TO ANSWER ABOUT A STAGE ("how do I send a case
            # from FOS to CPA?" -> STAGE_PROCESS, last_stage FOS; 2026-10-15): the stage after the one it was about
            _prev = str(_last_turn(request, payload).get("last_stage") or "").upper()
            _codes = {str(s.get("code")).upper() for s in _bare_general.cfg().get("stage_sequence") or []
                      if isinstance(s, dict)}
            if _prev in _codes:
                message = f"what comes after {_prev.lower()}?"
                request.state.copilot_message = message
        _direct = _bare_general.direct(message, claims, payload.context, request_id, _bare_lock.current() or "en",
                                       case_in_scope=payload.case_id)
        if _direct is not None:
            _bare_general.moved_on(claims, payload.context)
            _turn_layer(request, "general")
            return _direct
        if not (_bare_general._state(claims, payload.context).active_case_id or payload.case_id):
            # NO CASE OPEN, "what is X" (2026-10-13 live: "what is kyc check" was read as one case's KYC status and
            # asked "which case?" over 52 cases): the glossary definition first -- a known term or an acronym only
            _defined = await _bare_general._definition(message, request_id, _bare_lock.current() or "en", None)
            if _defined is not None:
                _bare_general.moved_on(claims, payload.context)
                _turn_layer(request, "general")
                return _defined
        elif _bare_general.glossary_acronym_asked(message):
            # A CASE OPEN, "what is cpa" / "what is foir" (2026-10-24 live: Qwen asked back "Certificate of Payment
            # Approval?"): a GLOSSARY ACRONYM asked bare is its definition -- no model; "what is the tenure" and other
            # recorded fields never come here (not an acronym the glossary defines)
            _defined = await _bare_general._definition(message, request_id, _bare_lock.current() or "en",
                                                       _bare_general._state(claims, payload.context).active_case_id
                                                       or payload.case_id)
            if _defined is not None and _defined.get("intent") == "DEFINITION":
                _bare_general.moved_on(claims, payload.context)   # "tell me more" next is about the case again
                _turn_layer(request, "general")
                return _defined
        _elig = _bare_general.eligibility_question(message) if (
            _bare_general._state(claims, payload.context).active_case_id or payload.case_id) else None
        if _elig:
            # (no case open: the general layer's own eligibility-policy redirect answers it, as before)
            # ELIGIBILITY IS NOT THE CHAT'S (owner, 2026-10-13): any eligibility wording is the standard question, so
            # the engine's redirect line answers it (in the officer's language) -- never the credit redirect
            message = _elig
            request.state.copilot_message = message
        _contents = _bare.contents(message) if (_bare_general._state(claims, payload.context).active_case_id
                                                 or payload.case_id) else None
        if not _contents and (_bare_general._state(claims, payload.context).active_case_id or payload.case_id):
            # ONE RECORDED FIELD whose name is also a glossary term ("what is the tenure", config
            # texts.field_question_fields): asked as the field's own question, which the profile answers -- the
            # glossary paths otherwise read "what is <term>" as a definition (2026-10-13)
            from app.agents.applicant.copilot.answering import profile as _fq_profile

            _texts = _bare_general.cfg().get("texts") or {}
            _field = str(getattr(_fq_profile.detect(message), "field", "") or "")
            _fq = str((_texts.get("field_question") or {}).get("en") or "")
            from app.agents.applicant.copilot.routing import subjects as _fq_subjects

            if _field and _fq and _field in set(_texts.get("field_question_fields") or []) \
                    and re.match(r"(?i)^\s*(what|whats|what's)\b", message) \
                    and _fq_subjects.mentioned(message) is None:
                # never for a NAMED party ("the co-applicant's employment type"): the rewrite would drop whose
                # only the plain "what is the tenure" form (the glossary clash); "no I mean the tenure" keeps its
                # correction for the conversation state
                message = _fq.format(field=_field.replace("_months", "").replace("_", " "))
                request.state.copilot_message = message
        if _contents:
            message = _contents                      # "what does the salary slip say" -> its recorded fields
            request.state.copilot_message = message
            request.state.skip_meaning = True        # the classic reader quotes DOCUMENT_DETAILS (meaning read "upload")
        _bare_code = _bare.document(message)
        if _bare_code and _after_clarification(request, payload):
            _bare_code = None                        # "the address proof" ANSWERS "which one do you mean?"
        if _bare_code and _after_knowledge(request, payload):
            # "and what about passport?" right after a KNOWLEDGE answer (which documents are accepted): that topic
            # continued -- the follow-up logic reads it -- never the passport's status on the case
            _bare_code = None
        if _bare_code:
            if _bare_general._state(claims, payload.context).active_case_id or payload.case_id:
                message = _bare.question(_bare_code)
                request.state.copilot_message = message
            else:
                _turn_layer(request, "general")
                return _bare.no_case_reply(_bare_code, request_id, _bare_lock.current() or "en")
        _simple_reply = await _simple_turn(request, message, claims, payload, request_id)
        if _simple_reply is not None:
            _turn_layer(request, "simple")
            return _simple_reply
        _agent_reply = None if getattr(request.state, "clear_reading", False) else             await _qwen_agent_turn(request, message, claims, payload, request_id)
        if _agent_reply is not None:
            _turn_layer(request, "qwen_agent")
            return _agent_reply
    if action is FosAction.CUSTOM_QUERY:
        # UNDERSTANDING BY MEANING (Smart Bot plan sections 2-5; semantics/meaning.py): commands and replies to a
        # pending question stay with the rules above / the workspace; every other message is understood by MEANING --
        # a case / process intent becomes its CANONICAL question (answered by the same deterministic engine), the other
        # kinds go to the general layer. Not understood with confidence -> the message goes on as typed (the rules).
        # VAGUE / ONE-WORD MESSAGES (owner decision A; capabilities/vague.py): a pending option list is answered by the
        # pick (or the most likely option on a second vague message); a new fragment gets ONE question with options
        from app.agents.applicant.copilot.capabilities import general as _vg
        from app.agents.applicant.copilot.capabilities import vague as _vague

        if _vague.enabled():
            _vstate = _vg._state(claims, payload.context)
            names_party = bool(getattr(payload, "co_applicant_id", None) or getattr(payload, "party_id", None))
            # a request that NAMES a party (co_applicant_id / party_id) goes through the party authorization first:
            # never answered by a follow-up question before that check can refuse it
            # A SETTLED TASK is never asked back as vague (Phase 2): "next two" pages the list on screen, "the third
            # one" is that row -- a deterministic reading of THIS conversation, not a fragment
            _t_early = getattr(request.state, "task", None)
            # only a PAGE of the list on screen or a ROW of it -- a bare "top 2" still gets its "which order?" question
            _settled = _t_early is not None and (_t_early.operation in ("page", "farewell", "greet")
                                                 or _t_early.references.get("kind") == "ordinal")
            if (not names_party and not _settled and _vague.is_vague(message)
                    and not _vague.waits_for(message, _vstate, claims, payload.context)
                    and not _vague.names_a_case(message, claims)):
                _open = bool(_vstate.active_case_id or payload.case_id)
                read = _vague.contextual(message, _vstate, _open)
                if read and _vague.is_upload(read):
                    return _vague.upload_reply(read, request_id, payload.context, claims)
                if read:
                    message = read                     # the one reading the conversation supports: answered now
                    request.state.copilot_message = message
                elif not _answered_before(claims, payload.context):
                    # a SHORT GREETING / THANKS the phrase lists do not hold ("namaste ji", "helo there"): the
                    # semantic reading answers it as small talk -- never "what do you mean?" options (Phase 2)
                    from app.agents.applicant.copilot.semantics import task as _vq_task

                    _early = None
                    if _vq_task.enabled():
                        _early = await _understood(message, claims, payload, request_id)
                        request.state.early_decision = (message, _early)
                    _social = _early is not None and _early.kind in ("small_talk", "ack")                         and _vq_task.plain_social(message)
                    asked = None if _social else _vague.ask(message, _vstate, request_id, _open)
                    if asked is not None:
                        _turn_layer(request, "vague")
                        return asked
                # else: A BARE WORD AFTER AN ANSWER ("What stage am I in?" -> "documents") is settled by that answer
                # -- the conversation-state short-query resolver reads it in context ("documents" = which documents
                # are required) -- never asked back with options (owner 2026-10-09: one word is answered, not asked)
        # "TELL ME MORE" ON AN OPEN CASE (owner 2026-10-12): the more detailed reading of the topic just answered
        # (conversation_general.yaml more_detail) -- a canonical question the same engine answers for the open case
        _more = _vg._more_detail(message, _vg._state(claims, payload.context), payload.case_id, claims, payload.context)
        if _more:
            message = _more
            request.state.copilot_message = message
        _early_seen = getattr(request.state, "early_decision", None)
        _case_now = bool(payload.case_id or _vg._state(claims, payload.context).active_case_id)
        if getattr(request.state, "skip_meaning", False) or (_case_now and _ag_profile_field(message)):
            # ONE RECORDED FIELD ("what is the tenure") or a document's CONTENTS on an open case: the classic reader
            # answers it exactly (APPLICANT_PROFILE / DOCUMENT_DETAILS) -- the meaning read them as a glossary term /
            # an upload (2026-10-13)
            decision = None
        elif _early_seen and _early_seen[0] == message:
            decision = _early_seen[1]                  # already read above (the vague check): never embedded twice
        else:
            decision = await _understood(message, claims, payload, request_id)
        request.state.meaning_intent = getattr(decision, "intent", None)     # the turn log's label for this turn
        if action is FosAction.CUSTOM_QUERY:
            # PLURAL SCOPE: "which files are still waiting on something" is about the CASES, never one case's pending
            from app.agents.applicant.copilot.capabilities import case_list as _ps_list
            from app.agents.applicant.copilot.semantics import meaning as _ps_meaning

            # a list the case list already reads ("show top 3 pending cases": size / filter / order) keeps its own words
            if _ps_list.understand(message) is None:
                decision = _ps_meaning.plural_scope(message, decision) or decision
            elif decision is not None and getattr(decision, "kind", None) == "case":
                # the CASE LIST reads it ("which of my cases are ready for cpa", 2026-10-13: rewritten to "is THIS case
                # ready for CPA?" and asked "which case?"): one case's reading never replaces a question over the cases
                from app.agents.applicant.copilot.semantics import task as _ps_task

                if _ps_task.asks_over_cases(message):
                    decision = None
            # A ROLE IN THE WORKFLOW ("WHO reviews the file once it reaches CPA?"): the approved stage knowledge --
            # never the open case's own readiness, whatever a case reading says (task.asks_role)
            from app.agents.applicant.copilot.semantics import task as _role_task

            from app.agents.applicant.copilot.semantics import intents as _role_intents

            if _role_task.enabled() and _role_task.asks_role(message) \
                    and _role_intents.looks_like_stage_process(message):
                # A NAMED STAGE ("who handles cpa?"): that stage's guide answers (STAGE_PROCESS), not the stage list
                decision = None
            elif _role_task.enabled() and _role_task.asks_role(message) \
                    and (decision is None or decision.kind == "case"):
                # the question keeps ITS words ("who approves the loan" -> the handbook's credit-team passage); only
                # the reading changes from the case to the workflow knowledge
                decision = _ps_meaning.Decision(kind="process", intent="process_stages", canonical=None,
                                                decided_by="role_question",
                                                score=decision.score if decision is not None else 0.0)
                _early_task = getattr(request.state, "task", None)
                if _early_task is not None and _early_task.scope == "one_case":
                    request.state.task = None          # re-read below with the process reading (task.interpret)
        request.state.meaning_decision = decision                 # the diagnostics' semantic reading
        _task_with_meaning(request, message, decision, claims, payload)
        if decision is not None and decision.kind in ("control", "clarify_action"):
            # "ruko, abhi action mat lena" -> nothing pending is written; "usko process kar do" -> which action?
            from app.agents.applicant.copilot.capabilities import control as _control

            _ctl_state = _vg._state(claims, payload.context)
            if decision.kind == "control":
                return _control.hold(_ctl_state, request_id)
            return _control.ask_which_action(_ctl_state, request_id)
        if decision is not None and decision.kind == "upload":
            # "upload new bank statement" with a case open: that document's upload link (the party the message names)
            _up_state = _vg._state(claims, payload.context)
            _code = _vague._document_code(message)
            if _code and (_up_state.active_case_id or payload.case_id):
                return _vague.upload_reply(_vague.upload_option(message, _code), request_id, payload.context, claims)
            decision = None                          # no case / no document named: the message goes on as typed
        if decision is not None and decision.party == "CO_APPLICANT":
            # "co-applicant pan" on a case WITHOUT one: the message as typed (the engine says there is none) -- the
            # co-applicant canonical would answer "nothing pending" for a party that does not exist
            _cp_case = _vg._state(claims, payload.context).active_case_id or payload.case_id
            from app.store import get_repository as _cp_repo

            _cp_app = _cp_repo().get_application(_cp_case) if _cp_case else None
            if _cp_app is not None and not getattr(_cp_app, "co_applicant_id", None):
                from app.agents.applicant.copilot.capabilities import general as _cp_general

                from app.agents.applicant.copilot.answering import language_lock as _cp_lock

                return _cp_general._reply(request_id, "NO_CO_APPLICANT", _cp_general._say_text(
                    (_cp_general.cfg().get("texts") or {}).get("no_co_applicant"), _cp_lock.current() or "en",
                    case_id=_cp_case),
                    case_id=_cp_case, query_type="CONVERSATION")
        if decision is not None and decision.kind == "ack" and not _task_social(message):
            # "wats pendng here" read as "ok": a question -- as typed; "take care of this" read as "ok": a request
            # without its action -- the next reading among the candidates when it is "which action?"
            from app.agents.applicant.copilot.semantics import meaning as _alt_meaning

            _intents = _alt_meaning.catalogue().get("intents") or {}
            _alt = next((n for n, _ in (decision.candidates or [])[:3]
                         if (_intents.get(n) or {}).get("kind") == "clarify_action"), None)
            if _alt and (_vg._state(claims, payload.context).active_case_id or payload.case_id):
                from app.agents.applicant.copilot.capabilities import control as _alt_control

                _turn_layer(request, "clarify_action(meaning)")
                _t_act = getattr(request.state, "task", None)
                if _t_act is not None:              # the task the turn acted on: a request whose action is unsaid
                    _t_act.scope, _t_act.operation, _t_act.intent = "none", "clarify", _alt
                    _t_act.ambiguities = ["which_action"]
                return _alt_control.ask_which_action(_vg._state(claims, payload.context), request_id)
            decision = None
        if decision is not None and decision.kind == "ack" and decision.canonical:
            # AN ACKNOWLEDGEMENT / THANKS read by meaning ("great, thanks a lot", "theek hai samajh gaya"): the general
            # layer's own THANKS / ACKNOWLEDGEMENT answer (its canonical "thanks" / "ok") -- never a hello, no case read
            message = decision.canonical
            request.state.copilot_message = message
            decision = None
        if decision is not None and decision.kind == "small_talk":
            # MEANING READ A GREETING the phrase list does not hold ("hey there, good evening"; audit root cause #2):
            # the same greeting reply as a typed "hi" -- no case data. Thanks / ok / no / help keep the general layer's
            # own answers (acknowledgement, NEGATION, the help line); a pending question is never answered by a hello.
            _st_state = _vg._state(claims, payload.context)
            if not (_vg._only(message, "ack") or _vg._has(message, "help") or _vg._only(message, "help")
                    or _vg._pending(_st_state, claims, payload.context)):
                from app.agents.applicant.copilot.capabilities import workspace as _st_ws

                from app.agents.applicant.copilot.answering import language_lock as _st_lock
                from app.agents.applicant.copilot.capabilities import natural_chat as _st_nat

                _vg.moved_on(claims, payload.context)
                _turn_layer(request, "greeting(meaning)")
                _st_natural = await _st_nat.social(message, _agent_chat_ref(request), request_id,
                                                   _st_lock.current() or "en", payload.case_id)
                return _st_natural or _st_ws.greeting_reply(claims, request_id, _st_state)
        if decision is not None and decision.kind == "list" and decision.canonical:
            # "kis applicant par pehle kaam karu": the case list's own priority order -- the workspace answers it
            message = decision.canonical
            request.state.copilot_message = message
            # a PORTFOLIO COUNT ("kitne cases mein documents baaki hain" -> "how many cases have documents pending?"):
            # the case insights count it from the caller's own rows -- the same deterministic answer as typed
            from app.agents.applicant.copilot.capabilities import case_insights as _pf_insights
            from app.agents.applicant.copilot.capabilities import general as _pf_general

            _pf_state = _pf_general._state(claims, payload.context)
            _portfolio = _pf_insights.answer(message, claims, request_id, payload.response_language or "en",
                                             listed=list(getattr(_pf_state, "listed_case_ids", None) or []))
            if _portfolio is not None:
                _pf_general.moved_on(claims, payload.context)
                _turn_layer(request, "case_insights(meaning)")
                return _portfolio
            decision = None
        if decision is not None:
            meaning_kind = decision.kind
            if decision.kind == "knowledge":
                from app.agents.applicant.copilot.semantics import meaning as _kn_meaning

                import asyncio as _kn_asyncio

                _knowledge_hint = await _kn_asyncio.to_thread(_kn_meaning.nearest_example, message, decision.intent)
            if decision.kind == "case" and decision.intent == "case_record":
                # ASK ANYTHING ABOUT THE CASE: the fact-checked case record answers it (snapshot_qa); not answered /
                # no case open -> the message goes on as typed (the engine reads it as before)
                _cr_reply = await _case_record_reply(message, claims, payload, request_id)
                if _cr_reply is not None:
                    _turn_layer(request, "case_record")
                    return _cr_reply
            elif decision.kind in ("case", "process") and decision.canonical:
                message = decision.canonical
                request.state.copilot_message = message
        from app.agents.applicant.copilot.answering import language_lock as _gen_lock
        from app.agents.applicant.copilot.capabilities import general as _general

        from app.agents.applicant.copilot.capabilities import case_list as _cl_table

        # "show me the table" is the workspace's (the list just shown, as a table): never a general answer
        _ord_state = _general._state(claims, payload.context)
        _ord_pick = (not getattr(_ord_state, "active_case_id", None)
                     and _cl_table.ordinal_pick(message, getattr(_ord_state, "listed_case_ids", None) or []))
        general_reply = None if (_cl_table.only_asks_for_the_table(message) or _ord_pick) else await _general.answer(
            message, claims, payload.context, request_id, _gen_lock.current() or "en", case_in_scope=payload.case_id,
            meaning_kind=meaning_kind, knowledge_hint=_knowledge_hint)
        if general_reply is None and getattr(decision, "decided_by", None) == "role_question":
            # a ROLE question the handbook does not match as worded: the approved stage sequence (process_stages),
            # never the open case's own data
            _role_canonical = ((_ps_meaning.catalogue().get("intents") or {}).get("process_stages") or {}).get(
                "canonical")
            if _role_canonical:
                general_reply = await _general.answer(
                    str(_role_canonical), claims, payload.context, request_id, _gen_lock.current() or "en",
                    case_in_scope=payload.case_id, meaning_kind="process", knowledge_hint=None)
                if general_reply is None:
                    # a case is open: the engine answers the stage question from the approved stage guide
                    # (FOS_KNOWLEDGE) -- the same path as a typed "what happens after CPA"
                    message = str(_role_canonical)
                    request.state.copilot_message = message
        if general_reply is not None:
            if str(general_reply.get("intent") or "") not in ("THANKS", "ACKNOWLEDGEMENT"):
                _general.moved_on(claims, payload.context)       # a new topic: an older "which one?" is stale
            _turn_layer(request, "general")
            return general_reply

    # "verify karna hai" / "kya upload karu" (6f, COPILOT_VERIFY_DIAGNOSE): never "which document?" --
    # the case is diagnosed (step 4's document action view, every party)
    from app.agents.applicant.copilot.answering import document_actions as _diag

    if _diag.diagnose_enabled() and action is FosAction.CUSTOM_QUERY and _diag.asks_to_verify(message):
        action = FosAction.GET_PENDING_ITEMS
        request.state.verify_diagnose = True

    # 6-MVP CASE WORKSPACE (COPILOT_CASE_WORKSPACE, default off; capabilities/workspace.py):
    # list / open / exit the caller's OWN cases; inside an opened case every question is
    # answered for it. Off: the workspace actions are refused and applicant_id stays required.
    from app.agents.applicant.copilot.capabilities import workspace as _ws

    ws_turn = None
    if not _ws.enabled():
        if action in _WORKSPACE_ACTIONS:
            raise HTTPException(422, detail={"request_id": request_id, "error": "WORKSPACE_DISABLED",
                                             "message": f"{action.value} needs the case workspace (off)."})
        if not payload.applicant_id:
            raise HTTPException(422, detail={"request_id": request_id, "error": "APPLICANT_ID_REQUIRED",
                                             "message": "applicant_id is required."})
    else:
        from app.security import access as _ws_access

        try:
            ws_turn = _ws.handle(action.value, message, case_id, claims, request_id, payload.context)
        except _ws_access.AccessDenied as exc:
            raise _ws_access.http_denied(exc, request_id) from None
        if ws_turn.reply is not None:
            _turn_layer(request, "workspace")
            return ws_turn.reply
        if ws_turn.case_id:
            case_id = ws_turn.case_id
            payload.applicant_id = ws_turn.applicant_id or payload.applicant_id
        if ws_turn.message:
            # the question asked before "which case?" -- answered now for the case just picked
            message = ws_turn.message
            request.state.copilot_message = message
            if _case_form.enabled() and action is FosAction.CUSTOM_QUERY:
                # a WORKFLOW request picked a case ("query raise karo ki ..." -> "which case?" -> "1"): the same chat
                # step as when the case was already open -- the draft, then Confirm (nothing written here)
                try:
                    formed = await _case_form.chat_turn(action.value, message, claims, request_id, payload.context,
                                                        _create_from_chat, False)
                except _form_access.AccessDenied as exc:
                    raise _form_access.http_denied(exc, request_id) from None
                if formed is not None and formed.get("execute_move"):
                    return move_case_stage(formed, request_id)
                if formed is not None:
                    _turn_layer(request, "case_form")
                    return formed
        if not case_id and not payload.applicant_id:
            from app.agents.applicant.copilot.answering import language_lock as _nc_lock
            from app.agents.applicant.copilot.capabilities import faq as _faq_none
            from app.agents.applicant.copilot.capabilities import product_flow as _oos

            _nc_lang = _nc_lock.current() or "en"
            if action is FosAction.CUSTOM_QUERY and _oos.out_of_scope(message):
                # MASTER SPEC 5: out of scope -- one polite line and the next option, nothing read
                line, option = _oos.out_of_scope_reply(_nc_lang, len(message))
                reply = _ws._base(request_id, "NOT_IN_SCOPE", line, query_type="CONVERSATION",
                                  category="UNSUPPORTED", tools_invoked=[])
                reply["faq_block"] = option
                return reply
            about_a_case = _oos.names_case_words(message)
            if _faq_none.enabled() and action is FosAction.CUSTOM_QUERY and not about_a_case \
                    and _faq_none.looks_like_faq(message):
                # 15.2 step 2: a general "how / what is" question, no case data and no FAQ answer -- never a guess
                return _ws._base(request_id, "FAQ_UNKNOWN", _faq_none.unknown(_nc_lang, len(message)),
                                 query_type="CONVERSATION", category="KNOWLEDGE_ONLY", tools_invoked=[])
            if action is FosAction.CUSTOM_QUERY and not about_a_case:
                # NOTHING A CASE HOLDS IS NAMED ("write me a poem", 2026-10-12): not a case question -- the polite
                # out-of-scope line and the next option, never "which case is this about?" over the case list
                line, option = _oos.out_of_scope_reply(_nc_lang, len(message))
                reply = _ws._base(request_id, "NOT_IN_SCOPE", line, query_type="CONVERSATION",
                                  category="UNSUPPORTED", tools_invoked=[])
                reply["faq_block"] = option
                return reply
            # A CASE QUESTION (typed or from the dropdown) WITH NO CASE OPEN -- golden rule 2, never a dead end:
            # 0 cases -> say so + Create New Case; 1 case -> answered for it; several -> "Which case ...?" + the
            # recent cases, and the pick answers THIS question
            picked = _no_case_turn(claims, request_id, payload, message)
            if isinstance(picked, dict):
                return picked
            case_id, payload.applicant_id = picked

    # 6i CASE ACTIONS (COPILOT_CASE_ACTIONS): view a document, raise / track queries, new case.
    # Nothing is created or sent without the explicit Send (confirm=true).
    from app.agents.applicant.copilot.capabilities import case_actions as _ca

    if action in _CASE_ACTIONS and not _ca.enabled():
        raise HTTPException(422, detail={"request_id": request_id, "error": "CASE_ACTIONS_DISABLED",
                                         "message": f"{action.value} needs case actions (off)."})
    if _ca.enabled():
        wanted = action
        if action is FosAction.CUSTOM_QUERY:
            wanted = (FosAction.RAISE_QUERY if _ca.asks("raise_query", message) else
                      FosAction.LIST_QUERIES if _ca.asks("list_queries", message) else
                      FosAction.NEW_CASE if _ca.asks("new_case", message) else action)
        if wanted in _CASE_ACTIONS:
            from app.agents.los.queries import QueryError
            from app.security import access as _ca_access

            try:
                if wanted is FosAction.NEW_CASE:
                    return _ca.new_case(request_id)
                if not case_id:
                    # "query raise karo" with no case open: which case? (the pick answers it), never a dead end
                    picked = _no_case_turn(claims, request_id, payload, message)
                    if isinstance(picked, dict):
                        return picked
                    case_id, payload.applicant_id = picked
                if wanted is FosAction.VIEW_DOCUMENT:
                    return _ca.view_document(case_id, str(payload.document_id or ""), claims, request_id)
                if wanted is FosAction.RAISE_QUERY:
                    if payload.confirm and isinstance(payload.query, dict):
                        return _ca.raise_confirmed(case_id, payload.query, claims, request_id)
                    return _ca.draft(case_id, claims, request_id)
                if wanted is FosAction.MARK_QUERY_SENT:
                    return _ca.mark_sent(case_id, str(payload.query_id or ""), claims, request_id)
                return _ca.list_view(case_id, claims, request_id)
            except _ca_access.AccessDenied as exc:
                raise _ca_access.http_denied(exc, request_id) from None
            except QueryError as exc:
                raise HTTPException(exc.http_status, detail={"request_id": request_id, "error": exc.code,
                                                             "message": exc.message}) from None

    # "X kya hai?" / "X ka full form?" (6e, COPILOT_RESPONSE_STYLE): the glossary's full form +
    # one line, in the user's language -- a PENDING term shows no full form (app/config/glossary.yaml)
    from app.agents.applicant.copilot.answering import style as _style

    if _style.enabled() and action is FosAction.CUSTOM_QUERY:
        # a case open and ONE recorded field asked ("what is the tenure"): that case's value, never the glossary
        term = None if (case_id and _ag_profile_field(message)) else _style.defined_term(message)
        if term:
            from app.agents.applicant import language as _language

            spoken = payload.response_language or _language.detect(message).code
            context = dict(payload.context or {}) if isinstance(payload.context, dict) else {}
            reply = {"request_id": request_id, "applicant_id": payload.applicant_id, "case_id": case_id,
                     "intent": "FOS_KNOWLEDGE", "answer": _style.definition(term, spoken), "category": "KNOWLEDGE_ONLY",
                     "query_type": "PROCESS_KNOWLEDGE", "response_source": "GLOSSARY", "documents": [], "actions": [],
                     "errors": [], "tools_invoked": [], "suggested_questions": [],
                     "glossary_term": term, "context": {k: context[k] for k in ("conversation_id", "workspace_id")
                                                        if context.get(k)}}
            state = _session_state(claims, reply)
            if state is not None and term not in (state.explained_terms or []):
                from app.agents.applicant.copilot.conversation import state as _conv

                state.explained_terms = sorted(set(state.explained_terms or []) | {term})
                _conv.STORE.put(state)
            return reply

    # A CO-APPLICANT BY ID OR NAME (LOS_COAPP_IDENTITY, step 5d), BEFORE anything is
    # read: an id that is not on a case the caller owns is refused here -- zero
    # downstream calls -- with the same 403 as any case the caller does not own.
    from app.agents.los import co_applicants as _co

    if _co.enabled():
        same_name = _same_name_question(case_id, message, request_id, claims) \
            if action is FosAction.CUSTOM_QUERY else None
        if same_name is not None:
            return same_name                             # 6g: two people with that name -> one question
        resolved, case_id, message = _resolve_co_applicant(payload, claims, request_id, case_id, message, action)
        if resolved:
            request.state.co_applicant = {"case_id": resolved[0], "co_applicant_id": resolved[1]}
    if not case_id and action is FosAction.CUSTOM_QUERY and payload.applicant_id:
        from app.agents.applicant.copilot.capabilities import case_pick

        if case_pick.enabled():
            from app.security import access as _pick_access

            try:
                picked = case_pick.resolve(payload.applicant_id, claims)
            except _pick_access.AccessDenied as exc:
                raise _pick_access.http_denied(exc, request_id) from None
            if picked.kind == case_pick.MANY:
                return case_pick.ask_which(picked, payload.applicant_id, request_id, message)
            if picked.kind == case_pick.NONE:
                return case_pick.no_case(payload.applicant_id, request_id)
            case_id = picked.case_id

    result = await _run_action(
        action,
        applicant_id=payload.applicant_id,
        case_id=case_id,
        claims=claims,
        request_id=request_id,
        message=message,
        context=payload.context,
        response_language=payload.response_language,
    )
    if picked is not None and isinstance(result, dict):
        # THE CASE ANSWERED FOR IS NAMED, since the user did not name it
        result["case_resolved_from_applicant"] = True
        from app.agents.applicant.copilot.answering import contract as _named_contract

        prefix = str((_named_contract.cfg().get("style") or {}).get("resolved_case_prefix") or "{case_id}: ")
        result["answer"] = prefix.format(case_id=case_id) + str(result.get("answer") or "").lstrip()
    if ws_turn is not None:
        result = _ws.decorate(result, ws_turn)
    return result


def _session_state(claims: dict[str, Any], published: dict[str, Any]):
    """The conversation state this response belongs to (labels only), or None."""
    from app.agents.applicant.copilot.conversation import state as _conv
    from app.security.auth import get_subject

    context = published.get("context") if isinstance(published.get("context"), dict) else {}
    key = context.get("conversation_id") or (("ws:" + context["workspace_id"]) if context.get("workspace_id") else None)
    if not key:
        return None
    return _conv.STORE.get(str(get_subject(claims) or "anonymous"), key)


def _styled(published: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    """6e: style the answer; the glossary terms explained are remembered for the session."""
    from app.agents.applicant.copilot.answering import style as _style
    from app.agents.applicant.copilot.conversation import state as _conv

    state = _session_state(claims, published)
    explained = set(getattr(state, "explained_terms", None) or [])
    language = ((published.get("language_contract") or {}).get("response_language")
                if isinstance(published.get("language_contract"), dict) else None)
    published, newly = _style.apply(published, explained=explained, language=language)
    if state is not None and newly:
        state.explained_terms = sorted(explained | newly)
        _conv.STORE.put(state)
    return published


def _select_language(result: dict[str, Any], selected: str | None) -> None:
    """Make an explicit frontend language the response language (an unsupported code is ignored)."""
    if not selected or not str(selected).strip():
        return
    from app.agents.applicant import language as _language

    code = str(selected).strip()
    if code not in _language.supported():
        return
    contract = result.get("language_contract")
    if not isinstance(contract, dict):
        contract = result["language_contract"] = {}
    contract["response_language"] = _language.reply_as(code)
    contract["selected_by"] = "FRONTEND"


async def _run_action(
    action: FosAction,
    *,
    applicant_id: str | None,
    case_id: str | None,
    claims: dict[str, Any],
    request_id: str,
    message: str | None = None,
    context: dict[str, Any] | None = None,
    response_language: str | None = None,
) -> dict[str, Any]:
    """
    One FOS action, however the caller asked for it.

    THE ONE IMPLEMENTATION BEHIND TWO DOORS. The REST reads below and
    the `/copilot` facade both land here, so a business endpoint and the
    compatibility endpoint cannot drift into answering the same question
    two ways -- which is the defect that made this refactor worth doing
    in the first place.

    A REFUSAL IS AN HTTP STATUS HERE TOO. The REST reads call this
    directly, and an AgentError (403 for a case the caller may not access)
    escaped them as an unhandled error -- a 500 instead of a refusal.
    """
    # THE APPLICANT FROM THE CASE, when the caller named only the case (the
    # contract marks applicant_id optional). Taken ONLY after the caller is
    # authorized on the case; an unauthorized or unknown case leaves it unset
    # and is refused below exactly as before -- the same 403 either way.
    if case_id and not applicant_id:
        from app.security import access as _read_access
        from app.security.auth import get_scopes, get_subject
        from app.store import get_repository as _read_repo

        try:
            _read_access.authorize(get_subject(claims), get_scopes(claims), case_id=case_id)
            _record = _read_repo().get_application(case_id)
            applicant_id = getattr(_record, "applicant_id", None) or applicant_id
        except Exception:  # noqa: BLE001 - not the caller's: refused below, unchanged
            pass
    try:
        result = await _answer_action(
            action, applicant_id=applicant_id, case_id=case_id, claims=claims,
            request_id=request_id, message=message, context=context,
            response_language=response_language)
    except AgentError as exc:
        raise HTTPException(exc.http_status, detail={
            "request_id": request_id, "error": exc.code,
            "code": "CASE_ACCESS_DENIED" if exc.http_status == 403
            and exc.code in {"CASE_NOT_ACCESSIBLE", "CASE_STORE_UNAVAILABLE"}
            else exc.code,
            "message": exc.message,
        }) from exc
    return result


async def _answer_action(
    action: FosAction,
    *,
    applicant_id: str | None,
    case_id: str | None,
    claims: dict[str, Any],
    request_id: str,
    message: str | None = None,
    context: dict[str, Any] | None = None,
    response_language: str | None = None,
) -> dict[str, Any]:
    """The body of `_run_action`, before refusals become HTTP statuses."""
    with request_cache.scoped():
        return await _answer_action_scoped(
            action, applicant_id=applicant_id, case_id=case_id, claims=claims,
            request_id=request_id, message=message, context=context,
            response_language=response_language)


async def _answer_action_scoped(
    action: FosAction,
    *,
    applicant_id: str | None,
    case_id: str | None,
    claims: dict[str, Any],
    request_id: str,
    message: str | None = None,
    context: dict[str, Any] | None = None,
    response_language: str | None = None,
) -> dict[str, Any]:
    """`_answer_action` inside ONE request-scoped read memo (request_cache)."""
    # A COMPOUND QUESTION -- two case questions in one sentence ("are my
    # documents verified and what is my loan amount?") -- is answered as
    # its halves, each exactly as if asked alone, then joined. The same
    # rule the Universal Copilot route applies (intents.compound_parts).
    from app.agents.applicant.copilot.semantics import intents as _intents

    if action is FosAction.CUSTOM_QUERY and message:
        # PEOPLE BY NAME before the split: "my mobile and Priya's mobile" is
        # two questions about two people. After the input guardrail only, and
        # the names are matched to this case's parties only after ownership.
        from app.agents.applicant.copilot import agent as _copilot_agent
        from app.security import guardrails as _guard

        if _guard.check_input(message, allowed_ids=(case_id, applicant_id)).allowed:
            named = await _copilot_agent._named_people(
                message, {"case_id": case_id, "applicant_id": applicant_id}, claims)
            if named.get("message"):
                message = named["message"]
    # A REQUEST TO ACT ON EVERYTHING PENDING is one request: "check kar aur
    # jo pending hai kar de" is the work capability's, never two questions.
    from app.agents.applicant.copilot.capabilities import work as _work_cap

    parts = (_intents.compound_parts(message)
             if action is FosAction.CUSTOM_QUERY and message
             and _work_cap.request(message) != _work_cap.DO else None)
    result = await answer_question(
        message=(parts[0] if parts else message if message is not None
                 else _ACTION_PHRASE.get(action, action.value)),
        applicant_id=applicant_id,
        case_id=case_id,
        claims=claims,
        request_id=request_id,
        # A NAMED ACTION IS NOT INFERRED. CUSTOM_QUERY is the only
        # request here that is genuinely a question, and it is the only
        # one that reaches the classifier.
        intent_override=(None if action is FosAction.CUSTOM_QUERY
                         else _ACTION_INTENT.get(action)),
        # Only a typed question is pruned. A dropdown action is a screen and
        # keeps the fields that screen renders.
        concise=action is FosAction.CUSTOM_QUERY,
        # A follow-up only makes sense for a typed question. A dropdown
        # action is unambiguous by construction, and resolving one against
        # a stale context would change what the button does.
        context=(context if action is FosAction.CUSTOM_QUERY else None),
    )
    if parts and str(result.get("intent") or "") not in ("GUARDRAIL_BLOCKED",):
        answers = [str(result.get("answer") or "").strip()]
        intents_seen = [result.get("intent")]
        frames = [(result.get("understanding") or {}).get("frame")]
        for part in parts[1:]:
            second = await answer_question(
                message=part, applicant_id=applicant_id, case_id=case_id, claims=claims,
                request_id=request_id, intent_override=None, concise=True, context=context)
            answers.append(str(second.get("answer") or "").strip())
            intents_seen.append(second.get("intent"))
            frames.append((second.get("understanding") or {}).get("frame"))
            for key in ("tools_invoked", "tool_trace", "sources"):
                result[key] = list(result.get(key) or []) + list(second.get(key) or [])
        result["answer"] = " ".join(a for a in answers if a)
        result["_compound"] = intents_seen
        if isinstance(result.get("understanding"), dict):
            result["understanding"]["compound"] = {
                "parts": list(parts), "intents": intents_seen, "frames": frames}
    if (action is FosAction.CUSTOM_QUERY and message and not parts and case_id
            and str(result.get("intent") or "") not in ("GUARDRAIL_BLOCKED", "OUT_OF_SCOPE")):
        # NOTHING DETERMINISTIC PLACED IT (audit 2026-10-11): the bounded planner may propose up to 3 read-only
        # lookups on this case, each validated and answered by the engine; otherwise this reply stands
        from app.agents.applicant.copilot.capabilities import fallback_planner as _planner

        if _planner.enabled() and _planner.needed(message):
            async def _lookup(call: dict[str, Any]) -> dict[str, Any]:
                return await answer_question(
                    message=call["canonical"], applicant_id=applicant_id, case_id=call["case_id"], claims=claims,
                    request_id=request_id, intent_override=None, concise=True, context=context)

            planned, plan_record = await _planner.plan_and_answer(message, case_id=case_id, claims=claims,
                                                                  lookup=_lookup)
            if planned is not None:
                result = planned
            if isinstance(result.get("understanding"), dict):
                result["understanding"]["fallback_planner"] = plan_record
            result["_fallback_planner"] = plan_record
    if str(result.get("intent") or "") == "STAGE_PROCESS" and not str(result.get("answer") or "").strip():
        # HOW A NAMED STAGE WORKS: the agent leaves the text to a caller with a
        # stage-guide source (copilot/agent.py). This route has one -- the
        # configured stage guide -- and a blank reply helps nobody.
        from app.agents.applicant import config as _agent_config
        from app.agents.applicant.copilot.semantics import intents as _stage_intents
        from app.knowledge import process_knowledge
        from app.security import guardrails as _guardrails

        frame = (result.get("understanding") or {}).get("frame") or {}
        stage = (frame.get("stage") if isinstance(frame, dict) else None) \
            or _stage_intents.stage_in(message or "")
        described = process_knowledge.describe(stage, _agent_config.stage_label(stage)) if stage else None
        result["answer"] = _guardrails.published(
            described or ("I don't have a description of that stage in the configured stage "
                          "guide. I can tell you where your own application is and what is "
                          "pending on it."))[0]
    # THE ANSWER IN THE QUESTION'S LANGUAGE where a template covers the fact;
    # the language contract then says which language the prose is in.
    from app.agents.applicant.copilot.answering import localize as _localize

    # THE FRONTEND'S LANGUAGE SELECTION WINS over the detected input language
    # (detection -- lexicon / Lingua -- only decides when nothing was selected).
    _select_language(result, response_language)
    _localize.apply(result, message or "")
    envelope = _from_agent(result, action.value, request_id,
                           concise=action is FosAction.CUSTOM_QUERY)
    # THE SUMMARY DESCRIBES THE CASE, NOT THE REPLY. A typed question
    # is pruned to what it asked about, and summarising that view had
    # the summary announce a case was ready while the answer beside it
    # listed three things blocking it -- the pruned envelope simply had
    # no readiness or pending items in it to see.
    summary_started = time.perf_counter()
    from app.agents.applicant.copilot.answering import contract as _sum_contract

    # A TYPED CHAT QUESTION UNDER THE REPLY CONTRACT never shows `summary` ({request_id, markdown, tts} is built from
    # the answer): the computed sentence, not a model call every turn waits on (2026-10-14: up to ~4 s per turn)
    envelope = await _with_summary(envelope, result, model=not (action is FosAction.CUSTOM_QUERY
                                                                 and _sum_contract.enabled()))
    # ONE STRUCTURED RECORD PER TURN (app/observability/turn.py): why this
    # answer, in codes and milliseconds -- attached, and logged as JSON.
    from app.observability import turn as _turn

    timings = dict(result.get("_timings") or {})
    timings["summary_ms"] = round((time.perf_counter() - summary_started) * 1000, 2)
    record = _turn.build(request_id=request_id, surface="fos", claims=claims, result=result,
                         timings=timings)
    if action is FosAction.CUSTOM_QUERY:
        # A typed question carries its record; a dropdown action is a screen
        # read and stays byte-identical to the facade endpoint's answer.
        envelope["observability"] = _turn.public(record)      # the full record goes to the log only
    _turn.emit(record)
    # THE STRUCTURED CONTRACT on EVERY door (a dropdown read and a typed question
    # alike): one shape, whichever route served it (answering/presentation.py).
    from app.agents.applicant.copilot.answering import presentation as _presentation

    envelope["presentation"] = _presentation.build(envelope)
    return envelope


def _upload_kyc(los: dict[str, Any]) -> dict[str, Any] | None:
    """The KYC this upload ran, identifiers masked; None when nothing reached KYC."""
    kyc = los.get("kyc")
    if not isinstance(kyc, dict) or not kyc:
        return None
    # NO DOCUMENT PASSED VERIFICATION: nothing was released, so KYC had nothing
    # to compare -- that is "not run", not a SKIPPED verdict to show
    if not any(str(d.get("verification") or "").upper() in _PASSED for d in los.get("documents") or []):
        return None
    from app.security import sensitivity

    block = dict(kyc)
    # CROSS-APPLICATION: each party's documents against what the application
    # form recorded for that party (profile_match) -- beside the cross-document
    # result, never merged into it
    matches = los.get("profile_match")
    if matches:
        block["application_match"] = matches
    return sensitivity.mask_payload(block)


#: Document types that never carry identity fields for KYC.
_NOT_KYC_INPUTS = frozenset({"SIGNATURE", "BUSINESS_PHOTO", "PHOTOGRAPH", "SALE_DEED"})


def _pipeline_sentence(outcomes: list[dict[str, Any]], kyc: dict[str, Any] | None) -> str:
    """One plain sentence on what happened after verification -- extraction and KYC."""
    passed = [o for o in outcomes if str(o.get("verification") or "").upper() in _PASSED]
    stopped = [o for o in outcomes if o.get("verification") and str(o.get("verification")).upper() not in _PASSED]
    parts: list[str] = []
    if passed:
        extracted = [o for o in passed if o.get("extracted_fields")]
        if extracted:
            parts.append(f"Details were read from {len(extracted)} verified document"
                         f"{'s' if len(extracted) != 1 else ''}.")
    # A SIGNATURE IS NEVER A KYC INPUT: "KYC was not run" beside one reads as a
    # problem that does not exist.
    kyc_relevant = [o for o in stopped if str(o.get("document_type") or "").upper() not in _NOT_KYC_INPUTS]
    if stopped and not passed and not kyc_relevant:
        pass
    elif stopped and not passed:
        parts.append("It did not pass verification, so no details were read and KYC was not run.")
    elif stopped:
        parts.append(f"{len(stopped)} document{'s' if len(stopped) != 1 else ''} that did not pass verification "
                     f"{'were' if len(stopped) != 1 else 'was'} not processed further.")
    codes_now = [str(c) for c in (kyc or {}).get("reason_codes") or []] if isinstance(kyc, dict) else []
    if passed and codes_now == ["INSUFFICIENT_SOURCES"]:
        # ONE DOCUMENT IS NOT A KYC PROBLEM: there is simply nothing to compare it with yet
        parts.append("KYC will compare the details once another verified identity or bank document is on the case.")
    elif passed and isinstance(kyc, dict) and kyc.get("status"):
        status = str(kyc["status"]).upper()
        said = {"PASS": "KYC passed", "REVIEW": "KYC needs review", "FAIL": "KYC did not pass",
                "PARTIAL": "KYC partly matched", "SKIPPED": "KYC could not compare anything yet"}.get(status,
                                                                                                    f"KYC: {status}")
        codes = [str(c) for c in kyc.get("reason_codes") or []]
        if codes and status != "PASS":
            from app.agents.applicant import case_memory_facts

            readable = [case_memory_facts._readable(c) for c in codes[:2]]
            said += " (" + "; ".join(r for r in readable if r) + ")"
        parts.append(said + ".")
    return " ".join(parts)


def _application_sentence(matches: list[dict[str, Any]] | None) -> str:
    """Fields a document contradicts on the APPLICATION FORM, by party -- named, never valued."""
    said = []
    for party in matches or []:
        bad = [str(f.get("field") or "").replace("_", " ").lower() for f in party.get("fields") or []
               if str(f.get("status") or "").upper() in ("FAIL", "MISMATCH", "REVIEW")]
        if bad:
            who = "the co-applicant's" if party.get("party_role") == "CO_APPLICANT" else "the applicant's"
            said.append(f"{', '.join(bad)} on the document differ{'s' if len(bad) == 1 else ''} from "
                        f"{who} application form")
    return ("Also, " + "; ".join(said) + ".") if said else ""


def _fos_kyc_on_upload() -> bool:
    """Whether the FOS chat upload runs KYC after verification (FOS_KYC_ON_UPLOAD, default on)."""
    import os

    return (os.getenv("FOS_KYC_ON_UPLOAD", "true") or "true").strip().lower() == "true"


_PASSED = {"PASS", "VERIFIED", "SUCCESS"}


def _document_pipeline(document: dict[str, Any], kyc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    One file's steps, as the frontend renders them. THE GATE IS THE VERDICT:
    extraction runs only behind a PASS, and KYC only reads released fields --
    a document that did not pass verification shows EXTRACT and KYC as SKIPPED,
    with the reason, never as pending work.
    """
    verdict = str(document.get("verification") or "").upper()
    processing = str(document.get("status") or "").upper() in ("PROCESSING", "QUEUED") \
        or "DOCUMENT_QUEUED_FOR_PROCESSING" in (document.get("reason_codes") or [])
    steps: list[dict[str, Any]] = []
    if processing and not verdict:
        steps.append({"step": "VERIFY", "status": "PROCESSING"})
        steps.append({"step": "EXTRACT", "status": "WAITING", "reason": "AWAITING_VERIFICATION"})
        steps.append({"step": "KYC", "status": "WAITING", "reason": "AWAITING_VERIFICATION"})
        return steps
    steps.append({"step": "VERIFY", "status": verdict or "NOT_RUN",
                  "reason_codes": list(document.get("reason_codes") or [])})
    if verdict not in _PASSED:
        why = "VERIFICATION_FAILED" if verdict in ("FAIL", "FAILED", "REJECTED") else "VERIFICATION_NOT_PASSED"
        steps.append({"step": "EXTRACT", "status": "SKIPPED", "reason": why})
        steps.append({"step": "KYC", "status": "SKIPPED", "reason": why})
        return steps
    released = document.get("extraction") is not None
    steps.append({"step": "EXTRACT", "status": "DONE" if released else "NOT_RELEASED",
                  **({} if released else {"reason": "NO_FIELDS_RELEASED"})})
    if not kyc:
        steps.append({"step": "KYC", "status": "NOT_RUN",
                      "reason": "KYC_OFF_AT_THIS_STAGE" if not _fos_kyc_on_upload() else "NO_KYC_RESULT"})
    else:
        steps.append({"step": "KYC", "status": str(kyc.get("status") or "").upper() or "NOT_RUN",
                      "reason_codes": list(kyc.get("reason_codes") or [])})
    return steps


def _extracted_fields(document: dict[str, Any]) -> dict[str, Any] | None:
    """The released OCR fields as {field: value}, identifiers masked per the disclosure policy."""
    extraction = document.get("extraction")
    if not isinstance(extraction, dict):
        return None
    fields = extraction.get("fields") if isinstance(extraction.get("fields"), dict) else extraction
    flat: dict[str, Any] = {}
    for name, value in (fields or {}).items():
        if isinstance(value, dict):
            value = value.get("value")
        if value not in (None, "", [], {}):
            flat[str(name)] = value
    from app.security import sensitivity

    # IDENTIFIERS BY FIELD NAME -- the text patterns cover PAN / Aadhaar /
    # accounts only, and a DL or EPIC number is an identifier too
    for key in list(flat):
        if key in _IDENTIFIER_FIELDS and isinstance(flat[key], str) and "X" not in flat[key][:4]:
            flat[key] = sensitivity.mask(flat[key])
    return sensitivity.mask_payload(flat) if flat else None


#: Extracted fields that are identifiers, always masked in a response.
_IDENTIFIER_FIELDS = frozenset({"pan_number", "pan", "dl_number", "epic_number", "passport_number",
                                "aadhaar_number", "account_number", "personal_number"})


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
    from app.agents.applicant.copilot.semantics.intents import Intent as _Intent
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

    # AN EXCEL OF NEW CASES SENT IN CHAT (MASTER SPEC 15.3): the same validation as the UI import -- row-by-row
    # errors, nothing written; the reply's Confirm creates the valid rows (case_form.chat_turn)
    from app.agents.applicant.copilot.capabilities import case_form as _import_form

    if _import_form.enabled() and len(uploads) == 1 \
            and str(getattr(uploads[0], "filename", "") or "").lower().endswith(".xlsx"):
        from app.agents.applicant.copilot.capabilities import importer as _importer
        from app.security.auth import get_subject as _import_subject

        try:
            permissions.check_capability(Caller.from_claims(claims), Intent.CREATE_APPLICANT)
        except PermissionDenied as exc:
            raise HTTPException(403, detail={"request_id": request_id, "error": exc.code,
                                             "message": exc.message}) from exc
        return _importer.chat_reply(await uploads[0].read(), str(_import_subject(claims) or ""), request_id)

    if declared_action != FosAction.UPLOAD_DOCUMENT.value:
        raise HTTPException(422, detail={
            "request_id": request_id, "error": "UNSUPPORTED_ACTION",
            "message": (f"{declared_action} is not valid for a multipart "
            "request. Only UPLOAD_DOCUMENT is."),
        })
    if (not applicant_id or not case_id):
        # INSIDE AN OPENED CASE (6-MVP workspace): an upload without ids goes to the case the officer
        # opened -- ownership is still checked below (write), exactly as for explicit ids
        from app.agents.applicant.copilot.capabilities import workspace as _ws

        # the same workspace as the chat's own turns (one per chat_id, unless a workspace_id is named)
        workspace_key = (str(form.get("workspace_id") or "").strip() or str(form.get("chat_id") or "").strip()
                         or str(request.query_params.get("chat_id") or "").strip() or None)
        opened = _ws.active_case(claims, workspace_key) if _ws.enabled() else None
        if opened is not None and (not case_id or case_id == opened.case_id):
            case_id, applicant_id = opened.case_id, applicant_id or str(opened.applicant_id)
    if not applicant_id or not case_id:
        raise HTTPException(422, detail={
            "request_id": request_id, "error": "INVALID_REQUEST",
            "message": "applicant_id and case_id are required for an upload (or open a case first).",
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
        permissions.check_ownership(applicant_id, case_id, caller=caller,
                                    write=True)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent="UPLOAD_DOCUMENT", tools=[], write=True,
                     status="DENIED", detail=exc.code)
        raise HTTPException(403, detail={
            "request_id": request_id, "error": exc.code, "message": exc.message,
        }) from exc

    # A CO-APPLICANT'S UPLOAD (LOS_COAPP_IDENTITY, step 5d): `co_applicant_id` must be ON
    # this case, which the caller was just authorized to write -- else the same 403.
    upload_co_id = str(form.get("co_applicant_id") or "").strip().upper() or None
    if upload_co_id:
        from app.agents.los import co_applicants as _co
        from app.security import access as _acc

        if not _co.enabled():
            raise HTTPException(422, detail={
                "request_id": request_id, "error": "CO_APPLICANT_ID_NOT_SUPPORTED",
                "message": "co_applicant_id on an upload needs LOS_COAPP_IDENTITY."})
        try:
            _acc.authorize_co_applicant(caller.subject, caller.scopes, upload_co_id, case_id=case_id, write=True)
        except _acc.AccessDenied as denied:
            raise _acc.http_denied(denied, request_id) from None

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
    # THE SAME LIMITS AS POST /los/process, enforced here too: this route read
    # every part whole with no cap (a 30 MB file was accepted, 2026-10-05).
    from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES as _MAX_BYTES
    from app.api.routes.los_api import MAX_DOCUMENTS as _MAX_DOCS

    if len(uploads) > _MAX_DOCS:
        raise HTTPException(413, detail={"request_id": request_id, "error": "TOO_MANY_FILES",
                                         "message": f"At most {_MAX_DOCS} files per upload."})

    for index, upload in enumerate(uploads):
        content = await upload.read(_MAX_BYTES + 1)
        if len(content) > _MAX_BYTES:
            raise HTTPException(413, detail={
                "request_id": request_id, "error": "FILE_TOO_LARGE",
                "message": f"{getattr(upload, 'filename', None) or 'A file'} exceeds the "
                           f"{_MAX_BYTES // (1024 * 1024)}MB limit."})
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
        [] if upload_co_id else documents,
        operation=PROCESS,
        applicant_id=applicant_id,
        case_id=case_id,
        request_id=request_id,
        **({"co_applicant_id": upload_co_id, "co_applicant_uploads": documents} if upload_co_id else {}),
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
        # KYC NOW RUNS ON THE FOS UPLOAD (2026-10-04): verification first; only a
        # PASSED document releases its OCR fields, and only released fields
        # reach KYC -- a document that failed verification goes no further.
        # Income comparison and eligibility stay off at FOS.
        kyc_checks=_fos_kyc_on_upload(),
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
    # THE BYTES A DEFERRED DOCUMENT WILL BE READ FROM -- kept BEFORE
    # persistence queues its job, exactly as /api/v1/los/process does.
    # Without this a FOS upload deferred to the background read queued a
    # job pointing at bytes nobody kept: "the uploaded file is no longer
    # available", on its first attempt, every time.
    from app.api.routes.los_api import _retain_for_ocr

    _retain_for_ocr(los, documents)
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
            # THE NAME THE DOCUMENT CARRIES (released fields only) -- whose document it looks like (FOS plan 7.2)
            "name_on_document": next((((document.get("extraction") or {}).get("fields") or {}).get(k)
                                      for k in ("name", "employee_name", "account_holder", "holder_name")
                                      if ((document.get("extraction") or {}).get("fields") or {}).get(k)), None),
            "status": document.get("status"),
            "reason_codes": document.get("reason_codes") or [],
            # The gate's decision, restated. Extraction is released only
            # behind a PASS, so this says plainly whether fields came back.
            "extraction_released": document.get("extraction") is not None,
            "authenticity": document.get("authenticity"),
            # PASS means these checks passed -- never that the issuer
            # confirmed the document.
            "verification_scope": document.get("verification_scope"),
            "issuer_verified": bool(document.get("issuer_verified")),
            "issuer_verification": document.get("issuer_verification"),
            "fraud_signals": document.get("fraud_signals") or [],
            # THE SCORE AND CONFIDENCE THE VERIFIER COMPUTED (0-100), as it
            # computed them -- None when nothing was scored (a signature with no
            # reference, a file refused at the gate), never a filled-in number.
            "score": document.get("verification_score"),
            "confidence": document.get("verification_confidence"),
            # THE DOCUMENT'S JOURNEY, renderable without reading prose:
            # VERIFY -> EXTRACT -> KYC, each DONE / SKIPPED (with why) / ...
            "pipeline": _document_pipeline(document, los.get("kyc")),
            # THE OCR FIELDS, released only behind a PASS, identifiers masked
            "extracted_fields": _extracted_fields(document),
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

    # WRONG DOCUMENT FOR ITS SLOT, OR A FILE THE UPLOAD GATE REFUSED: said per
    # file, with what the slot expects and the upload action -- rendered by
    # the frontend without reading the prose. Never persisted as a valid
    # document of the slot's type (the verdict is FAIL, extraction withheld).
    errors_by_source = {str(d.get("source_id")): d.get("errors") or []
                        for d in (los.get("documents") or [])}
    for outcome in outcomes:
        codes = outcome.get("reason_codes") or []
        gate = next((_upload_refusal_code(str(e.get("message") or ""))
                     for e in errors_by_source.get(str(outcome.get("source_id")), [])
                     if isinstance(e, dict) and e.get("code") == "INVALID_DOCUMENT"), None)
        if "DOCUMENT_TYPE_MISMATCH" in codes and outcome.get("expected_type"):
            outcome["upload_validation"] = {
                "status": "REJECTED", "reason": "WRONG_DOCUMENT",
                "expected_document": outcome["expected_type"],
                "detected_document": outcome.get("document_type"),
                "action": {"action": "UPLOAD_DOCUMENT", "document_type": outcome["expected_type"]}}
        elif gate or "EMPTY_FILE" in codes:
            outcome["upload_validation"] = {
                "status": "REJECTED", "reason": "INVALID_UPLOAD", "code": gate or "EMPTY_FILE",
                "expected_document": outcome.get("expected_type"),
                "action": {"action": "UPLOAD_DOCUMENT", "document_type": outcome.get("expected_type")}}
    rejected = [o for o in outcomes if o.get("upload_validation")]
    # A VALID DOCUMENT AIMED AT A SLOT IT CANNOT FILL ("a bank statement as
    # address proof"): it is kept as what it is -- it may fill its own slot --
    # but the slot it was aimed at is still empty, and the user is told so.
    # Before this it read "Bank Statement uploaded. Verification: PASS." and
    # the address-proof slot silently stayed MISSING.
    accepts_by_slot = {str(r.get("slot") or "").upper(): {str(a).upper() for a in r.get("accepts") or []}
                       for r in (checklist or []) if isinstance(r, dict)}
    filed_elsewhere = []
    for outcome in outcomes:
        aimed = str(outcome.get("expected_type") or "").upper()
        found = str(outcome.get("document_type") or "").upper()
        if outcome.get("upload_validation") or not aimed or not found or found == "UNKNOWN":
            continue
        fits = accepts_by_slot.get(aimed) or {aimed}
        if found not in fits:
            outcome["upload_validation"] = {
                "status": "FILED_ELSEWHERE", "reason": "SLOT_MISMATCH", "expected_document": aimed,
                "detected_document": found, "filed_under": found,
                "action": {"action": "UPLOAD_DOCUMENT", "document_type": aimed}}
            filed_elsewhere.append(outcome)

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
            k: outcomes[0].get(k) for k in
            ("document_type", "verification", "status", "reason_codes", "score", "confidence",
             "extraction_released", "expected_type",
             "authenticity", "verification_scope", "issuer_verified",
             "issuer_verification", "fraud_signals")
        })

    audit.record(request_id=request_id, subject=caller.subject,
                 applicant_id=applicant_id, case_id=case_id,
                 intent="UPLOAD_DOCUMENT", tools=["los.process",
                        "applicant.360"],
                 write=True, confirmed=True, status="OK")

    stage_sentence = " ".join(x for x in (_pipeline_sentence(outcomes, los.get("kyc")),
                                          _application_sentence(los.get("profile_match"))) if x)

    if len(outcomes) == 1 and (outcomes[0].get("upload_validation") or {}).get("reason") == "INVALID_UPLOAD":
        one = outcomes[0]
        answer = (f"{one['source_id']} couldn't be accepted: "
                  f"{_UPLOAD_REFUSAL_WORDS.get(one['upload_validation']['code'], 'it is not a valid document file')}.")
        rejected = []                       # said above, once
    elif len(outcomes) == 1:
        one = outcomes[0]
        from app.agents.applicant.copilot.answering import structured as _ustructured

        doc_name = _ustructured._readable_type(one.get("document_type"))
        doc_name = doc_name[:1].upper() + doc_name[1:]
        # THE VERDICT IN WORDS, its reasons as sentences: "Verification: REVIEW"
        # printed an enum, and the reasons came back as seven codes (2026-10-05).
        from app.agents.applicant import config as _config
        from app.agents.verification.reasons import spoken

        verdict = str(one.get("verification") or "").upper()
        answer = f"{doc_name} uploaded" + {
            "PASS": " and verified", "VERIFIED": " and verified",
            "REVIEW": "; it needs a review", "FAIL": "; it did not pass verification",
            "REJECTED": "; it did not pass verification",
        }.get(verdict, f"; verification: {verdict.lower() or 'pending'}")
        if one.get("score") is not None and _config.show_scores():
            answer += f" (score {one['score']}" + (
                f", confidence {one['confidence']}" if one.get("confidence") is not None else "") + ")"
        answer += "."
        if verdict not in _PASSED:
            reasons = spoken(one.get("reason_codes") or [], limit=2)
            if reasons:
                answer += " " + " ".join(reasons)
    else:
        # THE SAME FILE TWICE IN ONE REQUEST is stored once (keyed on case,
        # party and file): counted once here too, and said
        seen: dict[str, int] = {}
        for o in outcomes:
            seen[str(o.get("source_id"))] = seen.get(str(o.get("source_id")), 0) + 1
        repeated = [name for name, n in seen.items() if n > 1]
        distinct = {str(o.get("source_id")): o for o in outcomes}.values()
        passed_distinct = sum(1 for o in distinct if o["verification"] == "PASS")
        answer = (
            f"{len(seen)} document{'s' if len(seen) != 1 else ''} uploaded. {passed_distinct} passed "
            f"verification, {len(seen) - passed_distinct} did not."
        )
        if repeated:
            answer += f" {', '.join(repeated)} was sent more than once and kept once."
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

    from app.agents.applicant.copilot.answering import structured as _slot_names

    for o in filed_elsewhere:
        v = o["upload_validation"]
        found_name = _slot_names._readable_type(v["detected_document"])
        aimed_name = _slot_names._readable_type(v["expected_document"])
        answer += (f" {o['source_id']} is a {found_name}, so it was filed as {found_name} -- "
                   f"{aimed_name} still needs its own document.")
    for o in rejected:
        v = o["upload_validation"]
        if v["reason"] == "WRONG_DOCUMENT":
            slot = str(v["expected_document"]).replace("_", " ").title()
            slot = {"Pan": "PAN", "Itr": "ITR"}.get(slot, slot)
            answer += (f" {o['source_id']} doesn't match the {slot} slot -- please upload a {slot} "
                       "document there.")
        else:
            answer += f" {o['source_id']} could not be accepted as a document file -- please upload it again."
    if rejected or filed_elsewhere:
        verification["upload_validation"] = [dict(o["upload_validation"], source_id=o["source_id"])
                                              for o in [*rejected, *filed_elsewhere]]

    if stage_sentence:
        answer = f"{answer} {stage_sentence}"

    # SMART UPLOAD (FOS plan 7.2): what each file looks like, whose, where it was filed -- or ONE question
    from app.agents.applicant.copilot.capabilities import smart_upload as _smart

    smart_question, smart_actions = None, []
    if _smart.enabled():
        smart_lines, smart_question, smart_actions = _smart.describe(case_id, outcomes,
                                                                     filed_as_co=bool(upload_co_id))
        lead = [smart_question["question"]] if smart_question else []
        if lead or smart_lines:
            answer = "\n".join(lead + smart_lines + [answer])

    return _blank(
        request_id,
        applicant_id=applicant_id,
        case_id=case_id,
        action=FosAction.UPLOAD_DOCUMENT.value,
        intent="UPLOAD_DOCUMENT",
        answer=answer,
        clarification_required=smart_question,
        actions=smart_actions,
        response_type="UPLOAD_VALIDATION" if rejected else "UPLOAD_RESULT",
        applicant=result.get("applicant"),
        application=result.get("application"),
        stage=result.get("stage"),
        documents=result.get("documents") or [],
        checklist=checklist,
        required_documents=_required_slots(checklist),
        policy=result.get("policy"),
        pending_items=result.get("pending_items") or [],
        verification=verification,
        # THE KYC THE UPLOAD RAN (2026-10-04): over the fields released by the
        # documents that PASSED verification -- identifiers masked. Null when
        # nothing reached KYC (no passed document, or KYC off at FOS), never a
        # SKIPPED verdict minted for a check that did not run.
        kyc=_upload_kyc(los),
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


def _reads_nothing(result: dict) -> bool:
    """
    A refusal or a clarification READS NOTHING. The question was refused or
    not understood; a case summary, a frontend header or a readiness figure
    beside it would be store reads made for a question that was never
    answered -- and, for a refusal, reads a security boundary said must not
    happen.
    """
    return (str(result.get("intent") or "") == "GUARDRAIL_BLOCKED"
            or bool(result.get("clarification_required"))
            or bool(result.get("guardrail"))
            # A greeting, thanks or goodbye is answered from nothing.
            or str(result.get("category") or "") == "CONVERSATION")


async def _with_summary(envelope: dict[str, Any],
                        state: dict[str, Any] | None = None, *, model: bool = True) -> dict[str, Any]:
    """
    Attach the short summary, and say honestly who wrote it.

    THE FACTS ARE ALREADY DECIDED. `case_summary` is handed the stage,
    whether the applicant record is complete, each document's status,
    what is still being read and what is outstanding -- all computed
    before this runs -- and asked only to phrase them. It cannot
    decide a status, a document, readiness or a next action, because
    it is never asked to and never sees anything it would need to.

    `response_source` BECOMES structured+llm ONLY IF A MODEL WROTE IT.
    A model that is off, slow or unusable leaves the deterministic
    sentence and the plain `structured`, which is true rather than
    flattering.
    """
    if _reads_nothing(state or envelope):
        return envelope
    from app.agents.applicant import case_summary

    try:
        summary, source = await case_summary.summarise(state or envelope, model=model)
    except Exception as exc:
        logger.warning("Case summary failed: %r", exc)
        return envelope

    envelope["summary"] = summary
    if source == case_summary.STRUCTURED_AND_LLM:
        envelope["response_source"] = source
    return envelope


def _from_agent(
    result: dict[str, Any],
    action: str,
    request_id: str,
    *,
    concise: bool = False,
) -> dict[str, Any]:
    """Translate the Applicant Agent's envelope onto the FOS contract."""
    checklist = result.get("checklist") or []

    verification = result.get("verification")
    if verification is None and action == FosAction.GET_VERIFICATION_STATUS.value:
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
    envelope["understanding"] = result.get("understanding") if concise else None
    # THE FRONTEND CONTRACT (copilot/answering/structured.py): what kind of
    # answer, whose, in which language -- and a verification job, if any.
    for key in ("response_type", "language", "processing", "portfolio", "scope", "language_contract",
                "pending_work", "gate", "queries", "deviations", "deviation_rules", "raise_query_action",
                "eligibility"):
        if result.get(key) is not None:
            envelope[key] = result.get(key)
    subject = dict(result.get("subject") or {}) if isinstance(result.get("subject"), dict) else {}
    subject.setdefault("party", result.get("subject_party"))
    envelope["subject"] = subject
    envelope.update(_compact(result, envelope))
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
        from app.agents.applicant.copilot.semantics.intents import Intent as _Intent

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


def _compact(result: dict[str, Any],
             envelope: dict[str, Any]) -> dict[str, Any]:
    """
    The compact blocks, derived from what the agent already computed.

    NOTHING IS RECOMPUTED HERE. `is_complete` and `missing_fields` come
    from the stored applicant record by way of the agent, not from
    document extraction -- a document says nothing about whether
    somebody typed a date of birth into the master record, and
    inferring one from the other is how a chatbot ends up telling an
    officer to collect details that are already captured.
    """
    applicant = dict(result.get("applicant") or {})
    application = result.get("application") or {}

    missing = list(applicant.get("missing_fields") or [])
    if applicant:
        applicant["is_complete"] = not missing
        applicant["missing_fields"] = missing

    queued = [job for job in (result.get("processing_queue") or [])]

    # PUBLISHED ONLY WHEN SOMETHING IS KNOWN. A question about
    # documents carries no stage, and `{"stage": null,
    # "application_status": null}` tells a reader less than no field
    # at all while looking like an answer.
    stage = result.get("stage") or application.get("status")
    status = {"stage": stage, "application_status": application.get("status")}

    compact: dict[str, Any] = {
        "status": status if any(status.values()) else None,
        "processing_queue": queued,
        # GROUNDED MEANS AUTHORITATIVE EVIDENCE SUPPORTS THE ANSWER, by
        # the same rule the Universal Copilot publishes. This used to
        # count any `case_memory` block -- including an empty one -- so
        # "eligibility has not been evaluated" was reported as grounded.
        "grounded": supported_by_case_evidence(result),
    }
    if applicant:
        compact["applicant"] = applicant
    return compact


def _with_case_state(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    The stage and readiness a case HAS, not the ones this answer fetched.

    WHY THEY WERE NULL. The header is built from the envelope, and the
    envelope holds what the question planned tools for. "What documents
    are pending" plans the pending-items tool, so no application record
    and no readiness were read -- and the header published
    `stage: null, readiness: null` for a case whose stage and readiness
    the store knew perfectly well. A client keying on the header saw a
    case with no state at all.

    READ FROM THE RECORD, COMPUTED BY THE EXISTING RULES. The stage is
    the application's own status; readiness is `workflow.readiness`,
    the same deterministic function the readiness intent answers with.
    No model is involved in either, and nothing here overrides a value
    the answer did fetch.

    NEVER FATAL, AND NEVER INVENTED. A store that cannot be reached
    leaves the header exactly as it was.
    """
    case_id = str(envelope.get("case_id") or "").strip()
    if not case_id:
        return envelope
    if envelope.get("stage") and envelope.get("readiness"):
        return envelope
    # A CLARIFICATION READS NOTHING. The question was not understood; a case
    # summary beside "which did you mean?" would be work done for a question
    # that was not asked, and a store read the question never needed.
    if envelope.get("clarification_required"):
        return envelope

    try:
        from app.agents.applicant import workflow
        from app.store import get_repository

        repository = get_repository()
        application = request_cache.read(repository, "get_application", case_id)
        if application is None:
            return envelope

        filled = dict(envelope)
        if not filled.get("stage"):
            filled["stage"] = application.status.value

        if not filled.get("readiness"):
            applicant = request_cache.read(repository, "get_applicant", application.applicant_id)
            documents = request_cache.read(repository, "list_documents", case_id)
            filled["readiness"] = workflow.readiness(
                applicant, application, documents)
        return filled
    except Exception as exc:
        logger.warning("Could not read case state for %s: %r", case_id, exc)
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
    if _reads_nothing(result):
        # A clarification's suggestions are its own options: no read.
        clarification = result.get("clarification_required")
        if isinstance(clarification, dict) and clarification.get("options"):
            return {"suggested_questions": list(clarification["options"])}
        return {}
    from app.agents.applicant import frontend
    from app.agents.applicant.query_types import READS_CASE, QueryType

    envelope = _with_case_state(envelope)

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
    else:
        # the case-derived chips, turned to follow THIS turn (FOS plan 1.7)
        block["suggested_questions"] = frontend.contextual_suggestions(block.get("suggested_questions"),
                                                                       result.get("intent"))
    return block


def _raise_from(request_id: str, envelope) -> None:
    error = envelope.error
    code = error.code if error else "FAILED"
    raise HTTPException(
        404 if code == "NOT_FOUND" else 422 if code == "AMOUNT_IMPLAUSIBLE" else 400,
        detail={"request_id": request_id, "error": code,
                "message": error.message if error else "The call failed."},
    )


# ==========================================================================
# SUPPORTING -- not part of the two-endpoint integration surface
# ==========================================================================

# ==========================================================================
# FOS BUSINESS READS
# ==========================================================================
#
# WHY THESE EXIST BESIDE `/copilot`. Intake is not a conversation. A
# frontend rendering a checklist screen wants the checklist, and posting
# an action name to a chat endpoint to get it made a read look like a
# question -- which is how nine deterministic lookups ended up being
# recovered from English sentences by a regex classifier.
#
# ONE IMPLEMENTATION, NOT TWO. Each of these calls `_run_action`, which
# is what `/copilot` calls. They are a different door, not a second
# answer, so the two can never disagree about what a case contains.
#
# THE SAME RESPONSE SHAPE, DELIBERATELY. A client moving off `/copilot`
# should not have to learn a second envelope to read the same fields,
# and a client using both should not have to hold two shapes in mind.
# Fields an action does not populate come back null or empty, exactly as
# they always have.


@router.post(
    "/copilot/stream",
    summary="The FOS copilot as Server-Sent Events: status lines while it works, then the answer (step 7)",
    responses={200: {"content": {"text/event-stream": {}}}, 404: {"description": "Streaming is off."}},
    # the SAME JSON body as /fos/copilot; declared so Swagger "Try it out" shows a body box (it sent none -> JSONDecodeError)
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/CopilotRequest"},
        "examples": {
            "in_case_question": {"summary": "CUSTOM_QUERY on a case",
                                 "value": {"applicant_id": "APP-3D51FFAC6342", "case_id": "CASE-7DFE2F497522",
                                           "action": "CUSTOM_QUERY", "message": "kaunse documents pending hain?"}},
            "case_list": {"summary": "CUSTOM_QUERY -- my cases (case workspace on)",
                          "value": {"action": "CUSTOM_QUERY", "message": "mere cases dikhao"}},
        }}}}},
)
async def copilot_stream(request: Request, claims: dict[str, Any] = Depends(require_jwt)):
    """
    KEPT FOR EXISTING CLIENTS: the same as POST /fos/copilot with `"stream": true` (or
    `Accept: text/event-stream`). COPILOT_STREAMING, default off -> 404.
    """
    return await _stream_reply(request, claims)


class _Feedback(BaseModel):
    """A 👍 / 👎 on an earlier answer, sent to POST /fos/copilot."""

    action: str
    reply_to: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_\-]+$",
                          description="The `request_id` of the answer being rated.")
    rating: str = Field(..., max_length=16, description="up | down (config turn_log.feedback_ratings)")
    reason: str | None = Field(None, max_length=1000, description="Optional; masked and cut before it is kept.")
    chat_id: str | None = Field(None, max_length=128)


def _copilot_feedback(body: dict[str, Any], claims: dict[str, Any], request_id: str) -> dict[str, Any]:
    from pydantic import ValidationError

    from app.observability import turn_log as _turn_log
    from app.security.auth import get_subject

    try:
        given = _Feedback.model_validate(body)
    except ValidationError as exc:
        fields = [{"field": ".".join(str(p) for p in e.get("loc") or ()), "problem": str(e.get("msg") or "")}
                  for e in exc.errors()]
        raise HTTPException(422, detail={"request_id": request_id, "error": "INVALID_REQUEST",
                                         "message": "The feedback could not be read.", "fields": fields}) from None
    rating = given.rating.strip().lower()
    if rating not in _turn_log.ratings():
        raise HTTPException(422, detail={"request_id": request_id, "error": "INVALID_RATING",
                                         "message": "rating must be one of: " + ", ".join(_turn_log.ratings()) + "."})
    recorded = _turn_log.feedback(subject=str(get_subject(claims) or "anonymous"), reply_to=given.reply_to,
                                  rating=rating, reason=given.reason)
    return {"request_id": request_id, "status": "RECORDED" if recorded else "NOT_RECORDED",
            "reply_to": given.reply_to, "rating": rating}


async def _stream_reply(request: Request, claims: dict[str, Any]):
    """
    The copilot answer as Server-Sent Events. Events: `status` (at once, then every tick_seconds), then `answer`
    (the full /fos/copilot response + `latency`) or `error` (an HTTP status + detail). One pipeline: the answer
    IS the /fos/copilot answer.
    """
    from fastapi.responses import StreamingResponse

    request.state.streaming = True            # the copilot() call inside answers as JSON, never streams again

    from app.agents.applicant.copilot.answering import streaming as _streaming

    if not _streaming.enabled():
        raise HTTPException(404, detail={"error": "STREAMING_DISABLED", "message": "Streaming is off."})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - the copilot reports an unreadable body itself
        body = {}
    from app.agents.applicant.copilot.answering import contract as _contract
    from app.agents.applicant.copilot.answering import language_lock as _lock
    from app.agents.applicant.copilot.answering import realtime as _realtime
    from app.agents.applicant.copilot.capabilities import abuse_guard as _abuse_guard
    from app.security.auth import get_subject as _stream_subject

    body = body if isinstance(body, dict) else {}
    # a blocked message (section 19): no status event, the warning sent whole -- copilot() itself decides the reply
    blocked = bool(_abuse_guard.blocked(str(body.get("message") or "")))
    if _contract.enabled():
        # MASTER SPEC section 11: typing at once, one generic status if slow, the markdown in deltas, `final`
        request.state.request_id = f"fos_{uuid.uuid4().hex}"
        lang = _lock.normalise(body.get("reply_language") or body.get("response_language")) or "en"
        return StreamingResponse(
            _realtime.events(lambda: copilot(request, claims), subject=str(_stream_subject(claims) or "anonymous"),
                             chat_id=body.get("chat_id"), request_id=request.state.request_id, lang=lang,
                             atomic=blocked),
            media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    status_text = "" if blocked else _streaming.first_status(str(body.get("message") or ""),
                                                             str(body.get("action") or ""))
    return StreamingResponse(_streaming.events(lambda: copilot(request, claims), status_text),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class _ChatRef(BaseModel):
    chat_id: str | None = Field(None, max_length=128)


@router.post("/copilot/stop", summary="Stop the reply streaming in this chat (MASTER SPEC section 11)")
async def copilot_stop(body: _ChatRef, claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.answering import realtime as _realtime
    from app.security.auth import get_subject

    _realtime.stop(str(get_subject(claims) or "anonymous"), body.chat_id)
    return {"stopped": True}


@router.get("/copilot/replay/{request_id}",
            summary="The final reply of a request, for a reconnecting client (MASTER SPEC section 11)",
            responses={404: {"description": "Unknown, expired, or not the caller's."}})
async def copilot_replay(request_id: str, claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.answering import realtime as _realtime
    from app.security.auth import get_subject

    found = _realtime.replay(str(get_subject(claims) or "anonymous"), request_id)
    if found is None:
        raise HTTPException(404, detail={"error": "NOT_FOUND", "message": "No reply to replay."})
    return found


@router.get("/copilot/updates",
            summary="Pushed follow-ups: what changed on the cases this chat looked at (MASTER SPEC section 11)")
async def copilot_updates(chat_id: str | None = None, wait: float = 0, reply_language: str | None = None,
                          claims: dict[str, Any] = Depends(require_jwt)):
    """
    Long-poll: returns at once when something changed (a document verified / rejected since the chat last saw the
    case), else waits up to `wait` seconds (capped by config). Only the caller's own watched cases; every message is
    {request_id, markdown, tts}, replayable.
    """
    import asyncio

    from app.agents.applicant.copilot.answering import contract as _contract
    from app.agents.applicant.copilot.answering import language_lock as _lock
    from app.agents.applicant.copilot.answering import realtime as _realtime
    from app.security.auth import get_subject

    subject = str(get_subject(claims) or "anonymous")
    lang = _lock.normalise(reply_language) or "en"
    rt = _contract.cfg().get("realtime") or {}
    deadline = time.monotonic() + max(0.0, min(float(wait or 0), float(rt.get("updates_wait_seconds", 20))))
    while True:
        messages = await asyncio.to_thread(_realtime.updates, subject, chat_id, lang)
        if messages or time.monotonic() >= deadline:
            return {"messages": messages}
        await asyncio.sleep(float(rt.get("updates_poll_seconds", 1.0)))


# ==========================================================================
# MASTER SPEC SECTION 15 -- THE PRODUCT FLOW FOR THE UI (config app/config/product_flow.yaml)
# Home table, form schema / saved form / edit, downloads, Excel import, chat history, activity log,
# notifications, quick buttons. EVERY route: the caller's own cases only (the ordinary ownership check).
# ==========================================================================

def _lang_of(lang: str | None) -> str:
    from app.agents.applicant.copilot.answering import language_lock as _lock

    return _lock.normalise(lang) or "en"


def _flow_text(path: tuple[str, ...], lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow as _pf

    node: Any = _pf.cfg()
    for key in path:
        node = (node or {}).get(key) if isinstance(node, dict) else None
    return _pf.say(node, lang, **values) if node is not None else ""


def _own_case(claims: dict[str, Any], case_id: str, request_id: str) -> None:
    """The ordinary ownership check; not yours and not found are the same 404 (never confirming it exists)."""
    from app.agents.applicant.copilot.capabilities import workspace as _ws
    from app.security import access as _acc
    from app.store import get_repository

    try:
        _ws.authorize(claims, case_id)
    except _acc.AccessDenied:
        raise HTTPException(404, detail={"request_id": request_id, "error": "CASE_NOT_FOUND",
                                         "message": _flow_text(("errors", "not_found"), "en")}) from None
    if get_repository().get_application(case_id) is None:
        raise HTTPException(404, detail={"request_id": request_id, "error": "CASE_NOT_FOUND",
                                         "message": _flow_text(("errors", "not_found"), "en")})


@router.get(
    "/cases",
    summary="The home table: the caller's own cases, searched / filtered / sorted / paged on the server",
)
async def list_cases(filter: str | None = None, search: str | None = None, sort: str | None = None,
                     page: int = 0, size: int | None = None, stage: str | None = None, product: str | None = None,
                     created_from: str | None = None, created_to: str | None = None, status: str | None = None,
                     group: str | None = None, lang: str | None = None,
                     claims: dict[str, Any] = Depends(require_jwt)):
    """
    MASTER SPEC 3 + 15.1: the SAME query as the chat list -- only the caller's cases (live grants, each row
    re-checked by the ownership rule). Columns: Case ID, App ID, Applicant name, Stage, Status (Created / Review /
    Disbursal, mapped from the stage in config), Created date, Action. `group` = pending / done. Clicking a row:
    GET /cases/{case_id}/form (the filled form) -- or post {"action": "OPEN_CASE"} to /fos/copilot for the chat.
    """
    from app.agents.applicant.copilot.answering import contract as _contract
    from app.agents.applicant.copilot.answering import language_lock as _lock
    from app.agents.applicant.copilot.capabilities import case_list as _case_list
    from app.agents.applicant.copilot.capabilities import product_flow as _pf

    lang = _lang_of(lang)
    filters = _case_list.cfg().get("filters") or {}
    home = _pf.cfg().get("home_table") or {}
    sorts = {**{k: k for k in (_case_list.cfg().get("sorts") or {})}, **(home.get("sorts") or {})}
    groups = (_pf.cfg().get("case_status") or {}).get("groups") or {}
    statuses = sorted({s for members in groups.values() for s in members or []})
    for name, value, allowed in (("filter", filter, filters), ("sort", sort, sorts), ("group", group, groups),
                                 ("status", status, statuses)):
        if value and value not in allowed:
            raise HTTPException(422, detail={"error": f"UNKNOWN_{name.upper()}",
                                             "message": f"{name} must be one of {sorted(allowed)}"})
    result = _case_list.run(claims, _case_list.ListQuery(
        filter=filter, search=(search or "").strip()[:80] or None, sort=sorts.get(sort) if sort else None,
        page=max(0, page), size=size, stage=stage, product=product, created_from=created_from,
        created_to=created_to, group=group, status=status))     # a plain order stays ONE SQL page (section 3)
    # every earlier field keeps its meaning (status_label = what needs action, applicant_id, link ...); the home
    # table's fields are ADDED: app_id, status (Created / Review / Disbursal) + status_text, group + group_label,
    # created_date, action. applicant_name is the full name (the table shows it; applicant_short has "Rahul S.").
    rows = [{**{k: v for k, v in r.items() if k not in ("created_at", "updated_at", "applicant_full_name")},
             "applicant_name": r.get("applicant_full_name") or r["applicant_name"], "applicant_short": r["applicant_name"],
             "app_id": r["applicant_id"], "status_text": _pf.status_label(r["status"], lang),
             "group_label": _pf.group_label(r["group"], lang), "created_date": str(r.get("created_at") or "")[:10],
             "link": _contract.link("open_case", lang, id=r["case_id"]),
             "action": {"label": _pf.say(home.get("action_label"), lang),
                        "open_form": f"/api/v1/fos/cases/{r['case_id']}/form",
                        "open_chat": _contract.link("open_case", lang, id=r["case_id"])}}
            for r in result.rows]
    columns = [{"key": c["key"], "label": str(_lock.pick(c.get("label"), lang))} for c in home.get("columns") or []]
    empty = _pf.say(home.get("empty"), lang) if result.all_total == 0 else (
        _pf.say(home.get("no_match"), lang) if result.total == 0 else None)
    return {"rows": rows, "columns": columns, "total": result.total, "all_total": result.all_total,
            "counts": result.counts, "page": result.page, "size": result.size, "has_more": result.has_more,
            "empty_text": empty, "texts": {k: _pf.say(home.get(k), lang) for k in ("loading", "error", "new_case_label")},
            "filters": sorted(filters), "sorts": sorted(sorts), "groups": {g: _pf.group_label(g, lang) for g in groups},
            "statuses": {s: _pf.status_label(s, lang) for s in statuses}}


@router.get("/quick-actions", summary="The always-available chat buttons (MASTER SPEC 15.2 step 5)")
async def quick_actions(lang: str | None = None, claims: dict[str, Any] = Depends(require_jwt)):
    """Each button: {id, label, send} -- the frontend posts `send` as a CUSTOM_QUERY message."""
    from app.agents.applicant.copilot.capabilities import product_flow as _pf

    return {"buttons": _pf.quick_buttons(_lang_of(lang))}


@router.get("/form-schema", summary="The case form: fields, order, required, rules (UI and chat share it)")
async def form_schema(lang: str | None = None, claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.capabilities import case_form as _form

    return _form.schema(_lang_of(lang))


@router.get("/cases/{case_id}/form", summary="The saved form of one case (row click: continue where you stopped)")
async def case_form_values(case_id: str, lang: str | None = None, claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.capabilities import case_form as _form
    from app.agents.applicant.copilot.capabilities import product_flow as _pf
    from app.agents.applicant.copilot.capabilities import workspace as _ws
    from app.store import get_repository

    request_id = f"fos_{uuid.uuid4().hex}"
    _own_case(claims, case_id, request_id)
    lang = _lang_of(lang)
    stage = _ws._stage(case_id) or "--"
    status_ = _pf.status_of(stage)
    return {"request_id": request_id, "case_id": case_id,
            "app_id": get_repository().get_application(case_id).applicant_id, "stage": stage,
            "status": status_, "status_label": _pf.status_label(status_, lang), "values": _form.values_of(case_id),
            "missing": _form.missing(case_id), "editable": _form.editable(case_id)}


class FormUpdate(BaseModel):
    """The changed fields only. The frontend shows the summary and asks "Confirm?" before sending this."""

    changes: dict[str, Any] = Field(..., examples=[{"loan_amount": 600000, "email": "rahul@example.com"}])
    # OPTIONAL, BACKWARD COMPATIBLE (2026-10-10): the values the form SHOWED for the fields being changed. When one
    # no longer matches the stored value (another officer saved in between), nothing is written: 409 STALE_FORM
    # with the current values, so a stale screen never silently overwrites a newer correction.
    expected: dict[str, Any] | None = Field(None, examples=[{"full_name": "Rahul Shrma"}])


@router.put("/cases/{case_id}/form", summary="Save changed fields of a case's form (same rules as create and chat)")
async def case_form_update(case_id: str, body: FormUpdate, lang: str | None = None,
                           claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.capabilities import case_form as _form

    request_id = f"fos_{uuid.uuid4().hex}"
    _own_case(claims, case_id, request_id)
    lang = _lang_of(lang)
    known = {*_form.APPLICANT_FIELDS, *_form.APPLICATION_FIELDS}
    unknown = sorted(set(body.changes) - known)
    if unknown:
        raise HTTPException(422, detail={"request_id": request_id, "error": "UNKNOWN_FIELD", "fields": unknown})
    if not _form.editable(case_id):
        raise HTTPException(409, detail={"request_id": request_id, "error": "FORM_LOCKED",
                                         "message": _flow_text(("case_form", "edit_locked"), lang)})
    clean, errors = {}, {}
    for field, raw in body.changes.items():
        value, why = _form.check(field, raw, lang)
        if why:
            errors[field] = why
        else:
            clean[field] = value
    if errors:
        raise HTTPException(422, detail={"request_id": request_id, "error": "INVALID_FIELDS",
                                         "fields": {f: {"label": _form.label(f, lang), "message": w}
                                                    for f, w in errors.items()}})
    unknown_expected = sorted(set(body.expected or {}) - known)
    if unknown_expected:
        raise HTTPException(422, detail={"request_id": request_id, "error": "UNKNOWN_FIELD",
                                         "fields": unknown_expected})
    result = _form.save(case_id, clean, claims, request_id, "ui", expected=body.expected)
    if result["outcome"] == "STALE":
        raise HTTPException(409, detail={"request_id": request_id, "error": "STALE_FORM",
                                         "current": result["current"]})
    if result["outcome"] == "NOT_FOUND":
        raise HTTPException(404, detail={"request_id": request_id, "error": "CASE_NOT_FOUND"})
    # `changed`: the fields actually written (empty for a duplicate / no-op request) -- additive to the contract
    reply = {"request_id": request_id, "case_id": case_id, "values": _form.values_of(case_id),
             "missing": _form.missing(case_id), "changed": sorted(result["changed"])}
    # THE LOAN TERMS CHANGED: the recorded eligibility verdict is re-assessed for them (the same agent, the recorded
    # evidence) -- never left describing a loan that no longer exists. Additive: `eligibility` only when re-assessed.
    from app.agents.los import eligibility_refresh as _refresh

    if _refresh.affects(result["changed"]):
        from app.security.auth import get_subject as _refresh_subject

        refreshed = await _refresh.refresh(case_id, actor=str(_refresh_subject(claims) or ""), request_id=request_id,
                                           reason="form edit: " + ", ".join(sorted(result["changed"])))
        if refreshed is not None:
            reply["eligibility"] = refreshed
    return reply


@router.get("/exports", summary="A short-lived download link: one case or a filtered list, as xlsx / docx / pdf")
async def export_link(format: str = "xlsx", case_id: str | None = None, q: str | None = None,
                      lang: str | None = None, claims: dict[str, Any] = Depends(require_jwt)):
    """
    `case_id` = one case's summary; else `q` = the list's filters ("group:pending,search:rahul" / "all") over the
    caller's own cases. Returns {url, expires_in}; the url is bound to the caller and opened with the same JWT.
    """
    from app.agents.applicant.copilot.capabilities import exports as _exports
    from app.agents.applicant.copilot.capabilities import product_flow as _pf
    from app.security.auth import get_subject

    request_id = f"fos_{uuid.uuid4().hex}"
    fmt = str(format or "").lower()
    if fmt not in _exports.FORMATS:
        raise HTTPException(422, detail={"request_id": request_id, "error": "UNKNOWN_FORMAT",
                                         "message": f"format must be one of {sorted(_exports.FORMATS)}"})
    subject = str(get_subject(claims) or "")
    lang = _lang_of(lang)
    if not _exports.allowed(subject):
        raise HTTPException(429, detail={"request_id": request_id, "error": "RATE_LIMITED",
                                         "message": _flow_text(("exports", "rate_limited"), lang)})
    if case_id:
        _own_case(claims, case_id, request_id)
        spec = {"k": "case", "c": case_id, "f": fmt, "l": lang}
    else:
        spec = {"k": "list", "q": _pf.parse_list_q(q), "f": fmt, "l": lang}
    token, ttl = _exports.sign(spec, subject)
    audit.record(request_id=request_id, subject=subject, applicant_id=None, case_id=case_id, intent="EXPORT_LINK",
                 tools=[], status="LINK_ISSUED", detail=f"{spec['k']} {fmt}")
    return {"request_id": request_id, "url": f"/api/v1/fos/exports/file?token={token}", "expires_in": ttl}


@router.get("/exports/file", summary="Download the file of a signed export link (same caller, unexpired)")
async def export_file(token: str, claims: dict[str, Any] = Depends(require_jwt)):
    from fastapi.responses import Response

    from app.agents.applicant.copilot.capabilities import exports as _exports
    from app.security.auth import get_subject

    request_id = f"fos_{uuid.uuid4().hex}"
    subject = str(get_subject(claims) or "")
    spec = _exports.verify(token, subject)
    if spec is None:
        raise HTTPException(403, detail={"request_id": request_id, "error": "LINK_INVALID",
                                         "message": "This link is invalid or has expired."})
    return _export_file_response(spec, claims, subject, request_id)


def _export_file_response(spec: dict[str, Any], claims: dict[str, Any], subject: str, request_id: str):
    """The file of one export spec: scope re-checked, built in memory (never written to disk), audited."""
    from fastapi.responses import Response

    from app.agents.applicant.copilot.capabilities import exports as _exports

    if spec["k"] == "case":
        _own_case(claims, spec["c"], request_id)                  # scope re-checked when opened
        content = _exports.case_content(spec["c"], claims, spec["l"])
    else:
        content = _exports.list_content(claims, spec.get("q") or {}, spec["l"])
    data = _exports.render(content, spec["f"])
    audit.record(request_id=request_id, subject=subject, applicant_id=None, case_id=spec.get("c"),
                 intent="EXPORT_DOWNLOAD", tools=["export." + spec["f"]], status="OK", detail=spec["k"])
    return Response(data, media_type=_exports.FORMATS[spec["f"]],
                    headers={"Content-Disposition": f'attachment; filename="{content["file"]}.{spec["f"]}"'})


class ActionRequest(BaseModel):
    """One `action:` link from a reply, exactly as it appeared in the markdown."""

    href: str = Field(..., max_length=600, examples=["action:download?format=xlsx&case=CASE-852C1A2B3C4D"])
    chat_id: str | None = Field(None, max_length=128)
    reply_language: str | None = Field(None, max_length=12)


@router.post(
    "/action",
    summary="Run any action: link from a chat reply (FINAL FIX D) -- a file, a UI route, or the chat reply",
)
async def run_action(body: ActionRequest, request: Request, claims: dict[str, Any] = Depends(require_jwt)):
    """
    ONE endpoint for every `[Label](action:...)` the bot emits. The answer depends on the action:

      download / download_list / handoff_note / view_document  -> the FILE (Content-Disposition: attachment;
                                                                   filename=...), scope-checked and audited
      show_in_ui / show_list_in_ui / new_case                  -> {"type": "open_ui", "route": "..."}
      upload                                                   -> {"type": "upload", "document_type", "party"}
      copy                                                     -> {"type": "copy", "ref"}
      anything else (open_case, list_more, confirm_write ...)  -> {"type": "reply", request_id, markdown, tts}

    401 no / bad token, 403 a link for someone else, 404 not your case (or does not exist), 409 not ready,
    422 not an action link, 429 too many downloads.
    """
    from fastapi.responses import Response

    from app.agents.applicant.copilot.answering import contract as _contract
    from app.agents.applicant.copilot.capabilities import exports as _exports
    from app.agents.applicant.copilot.capabilities import product_flow as _pf
    from app.security.auth import get_subject

    request_id = f"fos_{uuid.uuid4().hex}"
    scheme, name, params = _contract.parse(body.href)
    if scheme != "action" or name not in _contract.registry():
        raise HTTPException(422, detail={"request_id": request_id, "error": "INVALID_ACTION_LINK",
                                         "message": "This is not an action link."})
    lang = _lang_of(body.reply_language)
    subject = str(get_subject(claims) or "")
    routes = _pf.cfg().get("ui_routes") or {}
    if name in ("download", "download_list"):
        fmt = str(params.get("format") or "").lower()
        if fmt not in _exports.FORMATS:
            raise HTTPException(422, detail={"request_id": request_id, "error": "UNKNOWN_FORMAT",
                                             "message": f"format must be one of {sorted(_exports.FORMATS)}"})
        if not _exports.allowed(subject):
            raise HTTPException(429, detail={"request_id": request_id, "error": "RATE_LIMITED",
                                             "message": _flow_text(("exports", "rate_limited"), lang)})
        spec = ({"k": "case", "c": str(params.get("case") or ""), "f": fmt, "l": lang} if name == "download"
                else {"k": "list", "q": _pf.parse_list_q(params.get("q")), "f": fmt, "l": lang})
        return _export_file_response(spec, claims, subject, request_id)
    if name == "handoff_note":
        return await handoff_note_file(str(params.get("case") or ""), "pdf", claims)
    if name == "view_document":
        from app.agents.applicant.copilot.capabilities import case_actions as _ca
        from app.store import get_repository
        from app.store.documents import get_document_store

        document = get_repository().get_document(str(params.get("id") or ""))
        if document is None:
            raise HTTPException(404, detail={"request_id": request_id, "error": "DOCUMENT_NOT_AVAILABLE",
                                             "message": "The document is not available."})
        _own_case(claims, document.case_id, request_id)
        from app.store.documents import storage_key as _storage_key

        store = get_document_store()
        # the bytes are kept under the id itself or, for the OCR queue, under storage_key(id)
        content = store.get(document.document_id) or store.get(_storage_key(document.document_id))
        if content is None:
            raise HTTPException(404, detail={"request_id": request_id, "error": "DOCUMENT_NOT_AVAILABLE",
                                             "message": "The document is not available."})
        _ca._audit(request_id, claims, document.case_id, "VIEW_DOCUMENT", "OPENED", document.document_type)
        described = store.describe(document.document_id)
        name_part = re.sub(r"[^A-Za-z0-9._-]", "_", str(document.document_type or "document"))[:40]
        return Response(content=content, media_type=(described.content_type if described else None)
                        or "application/octet-stream",
                        headers={"Content-Disposition": f'inline; filename="{name_part}"'})
    if name == "show_in_ui":
        _own_case(claims, str(params.get("case") or ""), request_id)
        return {"type": "open_ui", "route": str(routes.get("case") or "/cases/{case_id}").format(
            case_id=params.get("case"))}
    if name == "show_list_in_ui":
        from urllib.parse import urlencode

        query = urlencode(_pf.parse_list_q(params.get("q")))
        return {"type": "open_ui", "route": str(routes.get("list") or "/cases?{query}").format(query=query).rstrip("?")}
    if name == "new_case":
        return {"type": "open_ui", "route": str(routes.get("new_case") or "/cases/new")}
    if name == "upload":
        return {"type": "upload", "document_type": params.get("doc"), "party": params.get("party"),
                "post_to": "/api/v1/fos/copilot" + (f"?chat_id={body.chat_id}" if body.chat_id else "")}
    if name == "copy":
        return {"type": "copy", "ref": params.get("ref") or "draft-1"}
    # every other action is a chat turn: the SAME route, the same checks, as posting {"action_link": ...}
    request._json = {"action_link": body.href, "chat_id": body.chat_id, "reply_language": body.reply_language}
    reply = await copilot(request, claims)
    return {"type": "reply", **reply} if isinstance(reply, dict) else reply


@router.post("/imports", summary="Validate an Excel (.xlsx) of new cases: row-by-row errors, nothing written")
async def import_validate(file: UploadFile = File(...), lang: str | None = None,
                          claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.capabilities import importer as _importer
    from app.security.auth import get_subject

    request_id = f"fos_{uuid.uuid4().hex}"
    try:
        permissions.check_capability(Caller.from_claims(claims), Intent.CREATE_APPLICANT)
    except PermissionDenied as exc:
        raise HTTPException(403, detail={"request_id": request_id, "error": exc.code, "message": exc.message}) from exc
    lang = _lang_of(lang)
    try:
        result = _importer.validate(await file.read(), str(get_subject(claims) or ""), lang)
    except _importer.ImportProblem as exc:
        raise HTTPException(422, detail={"request_id": request_id, "error": "IMPORT_INVALID",
                                         "message": str(exc)}) from None
    return {"request_id": request_id, **result}


@router.post("/imports/{import_id}/confirm", summary="Create the valid rows of a validated import (after Confirm)")
async def import_confirm(import_id: str, lang: str | None = None, claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.capabilities import importer as _importer
    from app.security.auth import get_subject

    request_id = f"fos_{uuid.uuid4().hex}"
    lang = _lang_of(lang)
    rows = _importer.take(import_id, str(get_subject(claims) or ""))
    if rows is None:
        raise HTTPException(404, detail={"request_id": request_id, "error": "IMPORT_NOT_FOUND",
                                         "message": _flow_text(("import", "texts", "expired"), lang)})
    created, failed = [], []
    for number, values in enumerate(rows, start=1):
        try:
            made = await create_case(CreateCaseRequest.model_validate(_importer.body_for(values)), claims,
                                     channel="import", idempotency_key=f"import:{import_id}:{number}")
            created.append(made.get("case_id") if isinstance(made, dict) else getattr(made, "case_id", None))
        except HTTPException as exc:
            failed.append({"row": number, "message": (exc.detail or {}).get("message")
                           if isinstance(exc.detail, dict) else str(exc.detail)})
    return {"request_id": request_id, "created": created, "failed": failed,
            "message": _flow_text(("import", "texts", "done"), lang, n=len(created))}


@router.get("/chats", summary="The caller's own past chats (masked; retention from config)")
async def chat_list(claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.capabilities import product_flow as _pf
    from app.security.auth import get_subject
    from app.store import chat_history

    limit = int((_pf.cfg().get("history") or {}).get("max_chats", 50))
    return {"chats": chat_history.conversations(str(get_subject(claims) or ""), limit),
            "retention_days": chat_history.retention_days()}


@router.get("/chats/{chat_id}", summary="One of the caller's own past chats, as stored (masked)")
async def chat_transcript(chat_id: str, claims: dict[str, Any] = Depends(require_jwt)):
    from app.security.auth import get_subject
    from app.store import chat_history

    turns = chat_history.transcript(str(get_subject(claims) or ""), chat_id)
    if not turns:
        raise HTTPException(404, detail={"error": "CHAT_NOT_FOUND", "message": "This chat was not found."})
    return {"chat_id": chat_id, "turns": turns}


@router.get("/cases/{case_id}/activity", summary="Who created or changed what on a case, and when (UI and chat)")
async def case_activity(case_id: str, claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.capabilities import abuse_guard as _abuse
    from app.security import sensitivity as _sens
    from app.store import get_repository

    request_id = f"fos_{uuid.uuid4().hex}"
    _own_case(claims, case_id, request_id)
    events = get_repository().get_case_timeline(case_id) or []
    return {"case_id": case_id, "events": [
        {"at": str(getattr(e, "created_at", "") or ""), "type": getattr(e, "event_type", None),
         "stage": getattr(e, "stage", None),
         "summary": _abuse.mask_text(_sens.mask_identifiers(str(getattr(e, "summary", "") or "")))}
        for e in sorted(events, key=lambda e: (str(getattr(e, "created_at", "")), getattr(e, "sequence", 0)),
                        reverse=True)]}


@router.get("/notifications", summary="Cases pending longer than the configured days (home screen and greeting)")
async def notifications(lang: str | None = None, claims: dict[str, Any] = Depends(require_jwt)):
    from app.agents.applicant.copilot.capabilities import product_flow as _pf

    lang = _lang_of(lang)
    return {"heading": _flow_text(("notifications", "heading"), lang), "items": _pf.notifications(claims, lang)}


@router.get(
    "/handoff-note",
    summary="The CPA handoff note of a ready case, as md / html / pdf (FOS plan 7.1)",
    responses={200: {"description": "The note."}, 403: {"description": "Not the caller's case."},
               409: {"description": "The case is not ready for CPA."}},
)
async def handoff_note_file(case_id: str, format: str = "pdf", claims: dict[str, Any] = Depends(require_jwt)):
    """Only the caller's own case (the ordinary ownership check), only when ready, every download audited."""
    from fastapi.responses import Response

    from app.agents.applicant import audit
    from app.agents.applicant.copilot.answering import handoff_note
    from app.agents.applicant.copilot.capabilities import workspace as _ws
    from app.security import access as _acc
    from app.security.auth import get_subject

    request_id = f"fos_{uuid.uuid4().hex}"
    if not handoff_note.enabled():
        raise HTTPException(404, detail={"request_id": request_id, "error": "HANDOFF_NOTE_DISABLED",
                                         "message": "The handoff note is off."})
    try:
        _ws.authorize(claims, case_id)
    except _acc.AccessDenied as exc:
        raise _acc.http_denied(exc, request_id) from None
    officer = str(get_subject(claims) or "")
    note = handoff_note.build(case_id, officer)
    if not note["ready"]:
        audit.record(request_id=request_id, subject=officer, applicant_id=None, case_id=case_id,
                     intent="HANDOFF_NOTE_DOWNLOAD", tools=[], status="REFUSED_NOT_READY")
        raise HTTPException(409, detail={"request_id": request_id, "error": "NOT_READY_FOR_CPA",
                                         "message": "A handoff note is made once the case is ready for CPA."})
    kind = str(format or "pdf").lower()
    audit.record(request_id=request_id, subject=officer, applicant_id=None, case_id=case_id,
                 intent="HANDOFF_NOTE_DOWNLOAD", tools=["handoff.note"], status="OK", detail=kind)
    name = f"handoff-note-{case_id}"
    if kind == "md":
        return Response(note["markdown"], media_type="text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="{name}.md"'})
    if kind == "html":
        return Response(handoff_note.to_html(note["markdown"]), media_type="text/html")
    return Response(handoff_note.to_pdf(note["markdown"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})


@router.get(
    "/documents/view",
    summary="Open a document through a short-lived signed link (6i VIEW_DOCUMENT)",
    responses={200: {"description": "The document bytes, inline."}, 403: {"description": "Invalid / expired link."}},
)
async def view_document(token: str, claims: dict[str, Any] = Depends(require_jwt)):
    """
    The link from VIEW_DOCUMENT: valid only for the SAME subject, for `view_ttl_seconds` (5 min), and
    re-checked against the case's ownership now. The bytes come from the document store by id -- no
    path is ever part of a request or a response. Every open is audited.
    """
    from fastapi.responses import Response

    from app.agents.applicant.copilot.capabilities import case_actions as _ca
    from app.security import access as _acc
    from app.security.auth import get_subject
    from app.store import get_repository
    from app.store.documents import get_document_store

    request_id = f"fos_{uuid.uuid4().hex}"
    found = _ca.verify(token, str(get_subject(claims) or "")) if _ca.enabled() else None
    if found is None:
        raise HTTPException(403, detail={"request_id": request_id, "error": "LINK_INVALID",
                                         "message": "This link is invalid or has expired."})
    try:
        _ca._authorize(claims, found["case_id"])
    except _acc.AccessDenied as exc:
        raise _acc.http_denied(exc, request_id) from None
    document = get_repository().get_document(found["document_id"])
    from app.store.documents import storage_key as _storage_key

    store = get_document_store()
    # the bytes are kept under the id itself or, for the OCR queue, under storage_key(id)
    content = (store.get(found["document_id"]) or store.get(_storage_key(found["document_id"]))) \
        if document is not None else None
    if document is None or document.case_id != found["case_id"] or content is None:
        raise HTTPException(404, detail={"request_id": request_id, "error": "DOCUMENT_NOT_AVAILABLE",
                                         "message": "The document is not available."})
    _ca._audit(request_id, claims, found["case_id"], "VIEW_DOCUMENT", "OPENED", document.document_type)
    described = store.describe(found["document_id"])
    return Response(content=content, media_type=(described.content_type if described else None)
                    or "application/octet-stream",
                    headers={"Content-Disposition": "inline", "Cache-Control": "no-store"})


@router.get(
    "/applicants/{applicant_id}",
    summary="The applicant record",
    responses={200: {"model": FosResponse, "description": "The applicant."}},
)
async def get_applicant(
    applicant_id: str,
    case_id: str | None = None,
    claims: dict[str, Any] = Depends(require_jwt),
):
    """Who this applicant is, as the store holds them."""
    return await _run_action(
        FosAction.GET_APPLICANT, applicant_id=applicant_id, case_id=case_id,
        claims=claims, request_id=f"fos_{uuid.uuid4().hex}",
    )


@router.get(
    "/applications/{case_id}",
    summary="The application and where it stands",
    responses={200: {"model": FosResponse, "description": "The application."}},
)
async def get_application(
    case_id: str,
    applicant_id: str | None = None,
    claims: dict[str, Any] = Depends(require_jwt),
):
    """
    The application record and its FOS stage.

    A STAGE, NOT A DECISION. `status` here is how far intake has got --
    documents collected, basic verification done -- and says nothing
    about whether the loan will be approved.
    """
    return await _run_action(
        FosAction.GET_APPLICATION_STATUS, applicant_id=applicant_id,
        case_id=case_id, claims=claims, request_id=f"fos_{uuid.uuid4().hex}",
    )


@router.get(
    "/documents/{case_id}",
    summary="The documents on this case and their verification status",
    responses={200: {"model": FosResponse, "description": "The documents."}},
)
async def get_documents(
    case_id: str,
    applicant_id: str | None = None,
    claims: dict[str, Any] = Depends(require_jwt),
):
    """
    What has been uploaded, and what basic verification made of it.

    BASIC VERIFICATION ONLY: whether each file is readable, is the type
    it was declared as, and is structurally coherent. Identity
    consistency across documents, income and affordability are later
    stages and are not reported here.
    """
    return await _run_action(
        FosAction.GET_DOCUMENTS, applicant_id=applicant_id, case_id=case_id,
        claims=claims, request_id=f"fos_{uuid.uuid4().hex}",
    )


@router.get(
    "/checklist/{case_id}",
    summary="What this product requires, and what is still missing",
    responses={200: {"model": FosResponse, "description": "The checklist."}},
)
async def get_checklist(
    case_id: str,
    applicant_id: str | None = None,
    claims: dict[str, Any] = Depends(require_jwt),
):
    """
    The required documents for this case, with the policy that produced
    them and which rules could not be evaluated.
    """
    return await _run_action(
        FosAction.GET_DOCUMENT_CHECKLIST, applicant_id=applicant_id,
        case_id=case_id, claims=claims, request_id=f"fos_{uuid.uuid4().hex}",
    )


@router.post(
    "/documents",
    summary="Upload one or more documents for a case",
    description=(
        "Runs the existing intake pipeline: classification, then basic "
        "document verification, then extraction only behind a PASS.\n\n"
        "**Intake only.** Cross-document identity checks, income analysis "
        "and affordability do not run here -- they belong to later "
        "stages, and this endpoint has no authority over them.\n\n"
        "The same handler as `POST /api/v1/fos/copilot` with "
        "`action=UPLOAD_DOCUMENT`; both accept `files` and positional "
        "`document_types`."
    ),
    responses={
        200: {"model": FosResponse,
              "description": "What was made of the upload."},
    },
    # The multipart body the facade already documents, so Swagger offers
    # a file picker here too rather than an empty form.
    openapi_extra={"requestBody": _UPLOAD_BODY},
)
async def post_documents(
    request: Request,
    claims: dict[str, Any] = Depends(require_jwt),
):
    """
    THE SAME CODE AS THE FACADE'S UPLOAD, not a copy of it. A second
    upload implementation would be a second place for the stage boundary
    to be got wrong.
    """
    request_id = f"fos_{uuid.uuid4().hex}"
    try:
        return await _copilot_upload(request, claims, request_id)
    except HTTPException:
        raise
    except AgentError as exc:
        raise HTTPException(exc.http_status, detail={
            "request_id": request_id, "error": exc.code, "message": exc.message,
        }) from exc
    except Exception as exc:
        logger.exception("FOS upload failed request_id=%s", request_id)
        raise HTTPException(500, detail={
            "request_id": request_id, "error": "UPLOAD_FAILED",
            "message": "The request could not be completed.",
        }) from exc


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
    # ONLY WHAT THIS DEPLOYMENT ACCEPTS (Phase 3): the workspace / case actions are listed only while
    # their flag is on -- a frontend that builds its buttons from this never shows one that returns 422.
    from app.agents.applicant.copilot.capabilities import case_actions as _ca
    from app.agents.applicant.copilot.capabilities import workspace as _ws

    def offered(action: FosAction) -> bool:
        if action in _WORKSPACE_ACTIONS:
            return _ws.enabled()
        if action in _CASE_ACTIONS:
            return _ca.enabled()
        return True

    group = {**{a: "workspace" for a in _WORKSPACE_ACTIONS}, **{a: "case_action" for a in _CASE_ACTIONS}}
    extra = {FosAction.OPEN_CASE: ["case_id"], FosAction.VIEW_DOCUMENT: ["document_id"],
             FosAction.RAISE_QUERY: ["confirm", "query"], FosAction.MARK_QUERY_SENT: ["query_id"]}
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
                "group": group.get(action, "case"),
                "extra_fields": extra.get(action, []),
            }
            for action in FosAction if offered(action)
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
        # WHAT THE CHAT UI SHOULD SHOW (Phase 3): the features this deployment has on, where to call, and
        # the workspace settings -- a frontend reads this instead of hard-coding, so turning a feature on or
        # off needs no frontend release. Contract: docs/frontend/FRONTEND_API.md.
        **_frontend_features(),
    }


def _frontend_features() -> dict[str, Any]:
    import os

    from app.agents.applicant.copilot.answering import document_actions as _da
    from app.agents.applicant.copilot.answering import streaming as _st
    from app.agents.applicant.copilot.answering import style as _sy
    from app.agents.applicant.copilot.capabilities import case_actions as _ca
    from app.agents.applicant.copilot.capabilities import safety as _sa
    from app.agents.applicant.copilot.capabilities import workspace as _ws
    from app.agents.applicant.copilot.conversation.state import memory_enabled
    from app.agents.applicant.copilot.semantics import llm_router as _lr
    from app.agents.los import co_applicants as _co

    features = {
        "case_workspace": _ws.enabled(), "case_actions": _ca.enabled(), "streaming": _st.enabled(),
        "response_style": _sy.enabled(), "verify_diagnose": _da.diagnose_enabled(),
        "document_actions": _da.enabled(), "guardrail_hardening": _sa.enabled(),
        "session_memory": memory_enabled(), "party_recognition": _party_recognition_on(),
        "co_applicant_identity": _co.enabled(), "llm_router": _lr.enabled(),
        "emphasis": (os.getenv("COPILOT_EMPHASIS", "false") or "false").strip().lower() in {"1", "true", "yes", "on"},
    }
    # FOS E2E plan sections 2-7: each feature's own switch, as the running server sees it
    from app.agents.applicant.copilot.answering import (counts as _cnt, handoff_note as _hn, kyc_table as _kt,
                                                        language_lock as _ll, professional as _pf,
                                                        readiness_report as _rr)
    from app.agents.applicant.copilot.capabilities import (smart_upload as _su, snapshot_qa as _sq,
                                                           timeline as _tl)

    features.update({"language_lock": _ll.enabled(), "professional_format": _pf.enabled(), "kyc_table": _kt.enabled(),
                     "readiness_report": _rr.enabled(), "snapshot_qa": _sq.enabled(), "count_answers": _cnt.enabled(),
                     "handoff_note": _hn.enabled(), "smart_upload": _su.enabled(), "case_timeline": _tl.enabled()})
    endpoints = {"copilot": "/api/v1/fos/copilot", "actions": "/api/v1/fos/actions", "config": "/api/v1/fos/config"}
    if features["handoff_note"]:
        endpoints["handoff_note"] = "/api/v1/fos/handoff-note?case_id={case_id}&format=pdf"
    if features["streaming"]:
        endpoints["stream"] = "/api/v1/fos/copilot/stream"
    if features["case_actions"]:
        endpoints["view_document"] = "/api/v1/fos/documents/view?token={token}"
    out: dict[str, Any] = {"features": features, "endpoints": endpoints}
    if features["case_workspace"]:
        cfg = config.chatbot("case_workspace") or {}
        out["workspace"] = {"page_size": cfg.get("page_size", 10), "quick_questions": _ws.quick_questions(),
                            "list_message": "mere cases dikhao"}
    return out


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
