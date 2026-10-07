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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

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
            "policy decision, set in eligibility_policy.yaml, and it is "
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
            uploaded = await _copilot_upload(request, claims, request_id)
            if isinstance(uploaded, dict):
                # JEV runs AFTER the upload, on the persisted evidence -- never inside it.
                from app.jev import config as _jev_config

                uploaded["semantic_decisions"] = {
                    "jev_status": ("SCHEDULED" if _jev_config.enabled()
                                   and _jev_config.trigger_enabled("document_processed") else "DISABLED"),
                    "semantic_decisions": [], "semantic_actions": [],
                    "poll": f"/api/v1/jev/cases/{uploaded.get('case_id')}/decisions"}
            return uploaded
        # THE SAME PUBLISHING RULE AS THE UNIVERSAL COPILOT: no full PAN,
        # Aadhaar or account number anywhere in the published response --
        # including a number typed into a free-text field of a record
        # (sensitivity.mask_payload; record ids are left untouched).
        from app.security import sensitivity as _sensitivity

        published = await _copilot_json(request, claims, request_id)
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

            published = _ws_header.in_case_header(published)
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
            return _sensitivity.mask_payload(published)
        return published
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
            return ws_turn.reply
        if ws_turn.case_id:
            case_id = ws_turn.case_id
            payload.applicant_id = ws_turn.applicant_id or payload.applicant_id
        if not case_id and not payload.applicant_id:
            return _ws._base(request_id, "CASE_SELECTION", _ws._label("no_case"))

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
                    return {"request_id": request_id, "intent": "CASE_SELECTION", "case_id": None,
                            "answer": "👉 Open a case first.", "errors": [], "actions": []}
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
        term = _style.defined_term(message)
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
        result["answer"] = f"For application {case_id}: {str(result.get('answer') or '').lstrip()}"
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
    envelope = await _with_summary(envelope, result)
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
        envelope["observability"] = record
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

    return _blank(
        request_id,
        applicant_id=applicant_id,
        case_id=case_id,
        action=FosAction.UPLOAD_DOCUMENT.value,
        intent="UPLOAD_DOCUMENT",
        answer=answer,
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
                        state: dict[str, Any] | None = None) -> dict[str, Any]:
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
        summary, source = await case_summary.summarise(state or envelope)
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
)
async def copilot_stream(request: Request, claims: dict[str, Any] = Depends(require_jwt)):
    """
    The same JSON request as /fos/copilot (COPILOT_STREAMING, default off -> 404). Events: `status`
    (at once, then every tick_seconds), then `answer` (the full /fos/copilot response + `latency`) or
    `error` (an HTTP status + detail). One pipeline: the answer IS the /fos/copilot answer.
    """
    from fastapi.responses import StreamingResponse

    from app.agents.applicant.copilot.answering import streaming as _streaming

    if not _streaming.enabled():
        raise HTTPException(404, detail={"error": "STREAMING_DISABLED", "message": "Streaming is off."})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - the copilot reports an unreadable body itself
        body = {}
    status_text = _streaming.first_status(str((body or {}).get("message") or ""), str((body or {}).get("action") or ""))
    return StreamingResponse(_streaming.events(lambda: copilot(request, claims), status_text),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    store = get_document_store()
    content = store.get(found["document_id"]) if document is not None else None
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
    endpoints = {"copilot": "/api/v1/fos/copilot", "actions": "/api/v1/fos/actions", "config": "/api/v1/fos/config"}
    if features["streaming"]:
        endpoints["stream"] = "/api/v1/fos/copilot/stream"
    if features["case_actions"]:
        endpoints["view_document"] = "/api/v1/fos/documents/view?token={token}"
    out: dict[str, Any] = {"features": features, "endpoints": endpoints}
    if features["case_workspace"]:
        cfg = config.chatbot("case_workspace") or {}
        out["workspace"] = {"page_size": cfg.get("page_size", 10), "quick_questions": cfg.get("quick_questions") or [],
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
