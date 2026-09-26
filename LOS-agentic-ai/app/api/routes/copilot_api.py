"""
The Universal LOS Copilot.

ONE BRAIN, A SECOND DOOR. Every question here is answered by the same
agent that answers `/api/v1/applicant-agent/query` and
`/api/v1/fos/copilot` -- the same classifier, the same router, the same
MCP tools, the same ownership checks. This module adds a surface, not a
second copilot, because two brains answering the same question about the
same case is how they come to disagree.

WHY A SEPARATE SURFACE AT ALL. The FOS route is stage-bounded on
purpose: a field officer must not reach KYC or risk detail through it,
and `tests/integration/test_fos_stage_boundary.py` holds that line. A
caller with broader scope needs a door that is not the FOS one, so that
widening this can never widen that. The boundary lives at the adapter,
which is exactly why there is more than one adapter.

WHAT IT ADDS TODAY. Case history: "why is this case in review?" --
answered from findings the pipeline recorded, never from a model's
guess. Everything else routes exactly as it already did.
"""

from __future__ import annotations

import json
import asyncio
import logging
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.applicant import followup, intents, language, normalize, routing, sentiment
from app.agents.applicant import handoff as handoffs
from app.observability import analytics, cloudwatch
from app.agents.applicant.validate import check_composed
from app.agents.applicant import config as _agent_config
from app.observability.tracing import span, timed
from app.agents.applicant.agent import AgentError, answer_question
from app.agents.applicant.grounded import supported_by_case_evidence
from app.agents.los import stage_registry, stages
from app.knowledge import grounding, retrieval
from app.knowledge.retrieval import NotOwned
from app.knowledge.vector_store import Scope, UnscopedSearch
from app.security.auth import require_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["Copilot"])


class CopilotQueryRequest(BaseModel):
    """One question about one case."""

    message: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="The question, in natural language.",
        examples=["Why is this case in review?"],
    )
    case_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "The case the question is about.\n\n"
            "**Optional.** With a case, the answer is scoped to it and "
            "the caller must own it. Without one, the question is about "
            "the APPLICANT and is scoped to their own applications -- "
            "'how many cases do I have', 'what happened across my "
            "cases'. There is no unscoped read either way: every tool "
            "requires a case_id or an applicant_id."
        ),
        examples=["CASE-9B2E7F1A4C60"],
    )
    applicant_id: str | None = Field(
        None,
        max_length=128,
        description="The applicant. Resolved from the case when omitted.",
        examples=["APP-4C1D9E2B7A03"],
    )
    party_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Narrow the answer to one party on a joint case. Omit to "
            "answer across the case."
        ),
        examples=["COAPP-7F2A11C4D9E0"],
    )
    stage: str | None = Field(
        None,
        description=(
            "The LOS stage this question is about: FOS, CPA, CREDIT, "
            "RCU, BOPS, HOPS or DISBURSEMENT.\n\n"
            "**Advisory only.** It is used ONLY when the case record "
            "establishes no stage of its own. A caller that could "
            "override the case's stage could choose which stage's "
            "answers it receives, which would make stage scoping a "
            "preference rather than a boundary. The response's "
            "`stage_resolution` says which source was used."
        ),
        examples=["FOS"],
    )
    conversation_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Echoed back so a client can correlate turns. **Not stored** -- "
            "there is no server-side conversation memory yet, and pretending "
            "otherwise would invite a client to rely on one."
        ),
    )
    language: str | None = Field(
        None, max_length=16,
        description=("The language to answer in (e.g. `en`, `hi`, `mr`, "
                     "`hi-Latn`). Omitted: the language the question was "
                     "written in. A recorded fact is localized only where a "
                     "deterministic template exists for it; otherwise the "
                     "English answer is returned and `language.localized` is "
                     "false. The business truth never depends on it."),
        examples=["en"])
    channel: str | None = Field(
        None, max_length=32,
        description=("The channel the question came from: web, mobile, "
                     "whatsapp, fos_app, agent_desktop or api. Echoed back "
                     "and counted; it NEVER changes identity, authorization, "
                     "tools, evidence or the answer."),
        examples=["web"])
    context: dict[str, Any] | None = Field(
        None,
        description=(
            "The `context` block from the previous response, sent back "
            "unchanged, so a follow-up such as *\"why?\"* or *\"what about "
            "the document?\"* is understood. **Untrusted and advisory**: it "
            "can only rewrite the message into another question, which is "
            "then classified and authorised like any other. It never "
            "selects a case, a stage or a tool."
        ),
        examples=[{"last_query_type": "CASE_FACT",
                   "last_intent": "CASE_HISTORY", "last_slot": "PAN"}],
    )


class CopilotSource(BaseModel):
    """What one part of the answer rests on."""

    type: str | None = Field(
        None,
        description=(
            "The kind of evidence, in the vocabulary the rest of the "
            "response uses. `kind` is the same thing in lower case and "
            "is kept for callers that already read it."
        ),
        examples=["CASE_FINDING", "CASE_DOCUMENT", "PROCESS_KNOWLEDGE"],
    )
    kind: str = Field(..., examples=["case_finding", "case_decision"])
    finding_kind: str | None = Field(None, examples=["VERIFICATION", "KYC"])
    reason_code: str | None = Field(None, examples=["DOCUMENT_TYPE_MISMATCH"])
    document_id: str | None = None
    source_id: str | None = Field(None, examples=["pan.jpg"])
    party_id: str | None = None
    decision: str | None = Field(None, examples=["REVIEW"])
    status: str | None = Field(None, examples=["PARTIAL"])

    # -- retrieval provenance, added beside the case-memory fields ----
    source_type: str | None = Field(
        None,
        description=(
            "What the evidence was derived from. `PROCESS_KNOWLEDGE` is "
            "stage guidance, not a fact about this case."
        ),
        examples=["CASE_EVENT", "CASE_FINDING", "PROCESS_KNOWLEDGE"],
    )
    case_id: str | None = Field(
        None,
        description="Null on process knowledge, which belongs to no case.",
    )
    stage: str | None = Field(None, examples=["RCU"])
    document_type: str | None = Field(None, examples=["PAN"])


class CopilotQueryResponse(BaseModel):
    """One answered question."""

    request_id: str
    case_id: str | None = None
    applicant_id: str | None = None
    party_id: str | None = None
    conversation_id: str | None = None
    context: dict[str, Any] | None = Field(
        None,
        description="Send this back as `context` with the next question.",
    )
    followed_up: dict[str, Any] | None = Field(
        None,
        description=(
            "Present when a follow-up was rewritten using `context`: what "
            "was typed and what it was taken to mean."
        ),
    )

    stage: str | None = Field(
        None,
        description=(
            "The LOS stage this answer was scoped to. **Absent when the "
            "stage could not be established** -- defaulting to FOS would "
            "answer every unresolvable case out of the FOS corpus."
        ),
        examples=["FOS"],
    )
    stage_resolution: str = Field(
        "UNRESOLVED",
        description=(
            "Where the stage came from: `CASE_TIMELINE` (the pipeline "
            "recorded it), `APPLICATION_STATUS` (derived from the case), "
            "`CALLER_SUPPLIED` (the record was silent and the caller "
            "offered one), or `UNRESOLVED`."
        ),
        examples=["CASE_TIMELINE"],
    )
    stage_source: str | None = Field(
        None,
        description=(
            "`CASE_STATE` when the case record decided the stage, `CALLER` "
            "when only the caller did, `NONE` when nothing did. Never the "
            "question's wording and never a model."
        ),
        examples=["CASE_STATE"],
    )
    stage_status: str | None = Field(
        None,
        description=(
            "Where the case is WITHIN its stage -- `IN_PROGRESS`, or "
            "`READY_FOR_HANDOFF` for a FOS case ready for CPA. A workflow "
            "status, kept apart from the application status (`status` on a "
            "status question), document statuses and decisions."
        ),
        examples=["IN_PROGRESS"],
    )

    category: str = Field(
        ...,
        description="Which kind of question this was.",
        examples=["CASE_ONLY", "KNOWLEDGE_ONLY", "MIXED", "DOWNSTREAM"],
    )
    intent: str = Field(..., examples=["CASE_HISTORY"])
    answer: str = Field(..., description="Prose for the officer to read.")
    response_source: str | None = Field(
        None,
        description="Who produced the wording — deterministic or a model.",
        examples=["deterministic"],
    )
    grounded: bool = Field(
        False,
        description=(
            "Whether retrieved evidence backed this answer. **False "
            "means the answer says so** -- an answer with no evidence "
            "states that the available evidence is insufficient rather "
            "than offering a plausible one."
        ),
    )
    sources: list[CopilotSource] = Field(
        default_factory=list,
        description=(
            "The recorded findings behind the answer. Present on a "
            "case-history answer; empty when nothing was cited."
        ),
    )
    tool_invoked: list[str] = Field(
        default_factory=list,
        description="MCP tools this answer used.",
    )
    status: str | None = Field(
        None,
        description=(
            "On a status or stage question, the application status the "
            "case record holds. Otherwise present only when the request "
            "could not be served as asked: `CAPABILITY_UNAVAILABLE` means "
            "this stage has no registered capability for this kind of "
            "question -- which is neither an authorisation failure, nor "
            "missing case data, nor an unrecognised question."
        ),
        examples=["UNDER_REVIEW", "CAPABILITY_UNAVAILABLE"],
    )
    errors: list[dict[str, Any]] = Field(default_factory=list)

    # -- the frontend-ready case view (additive; derived from records) ------
    stage_label: str | None = Field(
        None, description="The current stage, as a person reads it.")
    delay: dict[str, Any] | None = Field(
        None,
        description="On a delay question: the current stage and since when, "
                    "whether the records ESTABLISH a cause, what holds the "
                    "case (finding, impact, subject), the recorded events in "
                    "this stage and the next action -- codes only. No cause "
                    "is inferred where the records establish none.")
    subject: dict[str, Any] | None = Field(
        None,
        description="Who the answer is about, when the question named a "
                    "party: `kind` (PRIMARY_APPLICANT, CO_APPLICANT or "
                    "BOTH) and the case's `parties` it covers, each with "
                    "`party_id` and `party_role`. Absent on a case-level "
                    "answer.")
    history: dict[str, Any] | None = Field(
        None,
        description="On a \"what changed\" answer: the `window` it used "
                    "(label, kind, start), the recorded `changes` in order "
                    "(event type, when, subject, source, previous -> "
                    "current), how many recorded changes carried no time, "
                    "and which kinds of change the store does not record.")
    next_actions: dict[str, Any] | None = Field(
        None,
        description="The Next Best Action result (deterministic): `primary` "
                    "and `additional` actions -- each with a language-neutral "
                    "`action_code`, `owner`, `subject` (scope, party_role), "
                    "`document`, `blocking`, `priority` and English `text` -- "
                    "and the `handoff` signal. Read-only guidance: nothing "
                    "is performed.")
    handoff: dict[str, Any] | None = Field(
        None,
        description="Whether a person should take this case: `required`, "
                    "`reason` (an action code, or USER_REQUEST), `priority` "
                    "(null -- none is configured). Never set from sentiment.")
    sentiment: dict[str, Any] | None = Field(
        None,
        description="The tone signal: `level` (neutral / confused / "
                    "frustrated / high_frustration), `intensity`, the marker "
                    "families that matched, and `affects: TONE_ONLY`. It may "
                    "add an empathetic opening line and recommend a handoff; "
                    "it never changes a business fact.")
    language: dict[str, Any] | None = Field(
        None,
        description="Language handling: `detected`, `script`, `romanized`, "
                    "`code_mixed`, `review_status` of the lexicon, "
                    "`response_language`, and `localized` (whether the "
                    "answer text is in that language).")
    channel: str | None = Field(
        None, description="The channel echoed back (channel-independent "
                          "answer; see the request field).")
    response_contract_version: str = Field(
        "3.0", description="Version of this structured response contract.")
    problems: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Recorded problems holding the case: type, message, the "
                    "document and field each rests on. Never values.")
    pending_items: list[str] = Field(
        default_factory=list, description="What is still outstanding.")
    next_action: str | None = Field(
        None, description="The next action the workflow recorded.")
    timeline: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The recorded stage history: stage, when entered and "
                    "left, from where, and the recorded reason.")
    answer_basis: dict[str, Any] | None = Field(
        None,
        description="Why this answer: the governed tools (and MCP transport) "
                    "the case facts came from, knowledge retrieved, whether a "
                    "semantic layer contributed, and whether the published "
                    "sentence was validated.")
    timings: dict[str, float] = Field(
        default_factory=dict,
        description="Milliseconds per step: stage, agent (tools, model), "
                    "rag, compose, total.")
    processing_ms: float | None = None
    correlation_id: str | None = Field(
        None, description="Correlates this answer with its logs and spans.")


#: What this surface publishes. AN ALLOWLIST, not a filter: the internal
#: envelope carries the whole case for its other consumers, and a filter
#: would let tomorrow's key through. Nothing here is a raw payload, a
#: prompt, agent state or a tool response.
_PUBLIC = ("request_id", "case_id", "applicant_id", "category", "intent",
           "answer", "response_source", "errors")


#: The kinds of question that need a stage capability behind them.
#:
#: THESE ARE `QueryCategory` VALUES, not `QueryType` ones. The envelope
#: publishes what the service had to CONSULT (the store, the handbook,
#: both, or nobody), and that is the question here -- a stage with no
#: handbook cannot answer anything that needs one.
#:
#: CASE_ONLY IS DELIBERATELY ABSENT. Case facts come from the case's own
#: stored records, which exist whatever desk the case sits on; gating
#: them on stage would make the Copilot useless the moment a case left
#: FOS.
#:
#: MIXED IS PRESENT. It needs the handbook as well as the case, and half
#: an answer to a question that asked for both is a partial answer
#: presented as a whole one.
_NEEDS_CAPABILITY = {"KNOWLEDGE_ONLY", "MIXED", "DOWNSTREAM"}


def _unavailable_for(
    context: stages.StageContext, envelope: dict[str, Any],
) -> CopilotQueryResponse | None:
    """
    The CAPABILITY_UNAVAILABLE answer, when this stage cannot serve this
    kind of question. None when it can.
    """
    category = str(envelope.get("category") or "").upper()
    if category not in _NEEDS_CAPABILITY:
        return None

    registered = stage_registry.capabilities_for(context.stage)
    if category in {"KNOWLEDGE_ONLY", "MIXED"} and registered.answers_knowledge():
        return None
    if category == "DOWNSTREAM" and registered.answers_downstream():
        return None

    unavailable = stage_registry.unavailable(context.stage)

    return CopilotQueryResponse(
        request_id=str(envelope.get("request_id") or ""),
        case_id=envelope.get("case_id"),
        applicant_id=envelope.get("applicant_id"),
        category=category or "UNSUPPORTED",
        intent=str(envelope.get("intent") or "UNKNOWN"),
        answer=unavailable["message"],
        status=unavailable["status"],
        response_source="deterministic",
        **context.public(),
    )


#: Categories whose answer can be improved by retrieved evidence.
#: `DOWNSTREAM` is absent deliberately: it is a routing refusal and
#: existing behaviour, not a question to answer from a corpus.
_RETRIEVES = {"CASE_ONLY", "KNOWLEDGE_ONLY", "MIXED", "PROCESS_KNOWLEDGE"}

#: Agent refusals that mean "not your case". Published in the same
#: shape as the retrieval layer's, so a client sees one outcome.
_ACCESS_DENIED = {"CASE_NOT_ACCESSIBLE", "CASE_STORE_UNAVAILABLE"}


#: A stage capability, as an answer names what is not available.
_CAPABILITY_WORDS = {
    "readiness": "Readiness for the CPA handoff",
    "eligibility": "Eligibility information",
    "document_requirements": "Document requirement information",
    "pending_items": "Pending-item information",
    "next_action": "Next-action information",
    "case_history": "Case history",
}


def _not_served_at_stage(context: stages.StageContext,
                         envelope: dict[str, Any]) -> str | None:
    """
    The answer for a case question whose capability this stage lacks, or
    None when the stage can serve it. The capability each intent needs is
    configuration (`chatbot.stages.intent_capabilities`); what each stage
    can serve is the stage registry. Neither is inferred from the question.
    """
    if context.stage is None:
        return None
    from app.agents.applicant import config as agent_config

    needed_by = agent_config.chatbot("stages").get("intent_capabilities") or {}
    intent = str(envelope.get("base_intent") or envelope.get("intent") or "")
    needed = needed_by.get(intent.upper())
    if not needed:
        return None
    registered = stage_registry.capabilities_for(context.stage)
    if str(needed).lower() in registered.capabilities:
        return None
    label = agent_config.stage_label(context.stage.value)
    what = _CAPABILITY_WORDS.get(str(needed).lower(), "This information")
    return (f"Your application is currently at the {label} stage. {what} "
            f"is not available for the {label} stage.")


#: A sentence saying the service could not answer from evidence.
_NO_EVIDENCE_RE = re.compile(
    r"\b(do\s+not|don't|does\s+not|doesn't)\s+have\s+enough\b"
    r"|\bnot\s+enough\s+(verified\s+)?information\b"
    r"|\bno\s+(verified\s+)?(information|evidence)\s+(is\s+)?available\b",
    re.IGNORECASE)


def _says_no_evidence(answer: object) -> bool:
    return bool(_NO_EVIDENCE_RE.search(str(answer or "")))


def _answered_without_the_case(request: "CopilotQueryRequest") -> bool:
    """A refused request or small talk: answered before any case read."""
    from app.agents.applicant import conversation as conversations
    from app.agents.applicant import handoff as _handoffs
    from app.security import guardrails, request_policy

    message = request.message
    if not guardrails.check_input(
            message, allowed_ids=(request.case_id, request.applicant_id,
                                  request.party_id)).allowed:
        return True
    if _handoffs.asks_for_person(message):
        return False
    return bool(conversations.classify(message)
                or request_policy.asks_capability(message)
                or request_policy.asks_own_history(message))


#: Channels a request may name. Anything else is published as "api".
_CHANNELS = frozenset({"web", "mobile", "whatsapp", "fos_app",
                       "agent_desktop", "api"})


def _converse(request: "CopilotQueryRequest", envelope: dict[str, Any],
              context: stages.StageContext, *, gated: Any) -> dict[str, Any]:
    """
    Language, tone and channel -- around the answer, never inside it.

    Runs AFTER the agent, the stage gate, retrieval and composition have
    settled every fact. It may replace the answer with a deterministic
    localized sentence for an ESTABLISHED fact (the current stage, from
    the stage resolver -- never from memory, retrieval or a model), and
    may prepend an empathetic opening line. It never edits the facts.
    """
    detected = language.detect(request.message)
    target = language.response_language(
        request.language, detected,
        preferred=(request.context or {}).get("language")
        if isinstance(request.context, dict) else None,
        text=request.message)
    canonical = normalize.normalise(request.message).text
    signal = sentiment.detect(request.message, canonical)
    answer = str(envelope.get("answer") or "")
    localized = target == "en" or envelope.get("_presented_language") == target
    template_used = "conversation" if envelope.get("_presented_language") not in (None, "en") else None

    if (target != "en" and gated is None and context.stage is not None
            and str(envelope.get("intent") or "") == "APPLICATION_STAGE"
            and _understood(request).matched_on == "current_stage"
            and not envelope.get("guardrail")):
        sentence = language.localized(
            "current_stage", target,
            stage=_agent_config.stage_label(context.stage.value))
        if sentence:
            envelope["answer_en"] = answer
            answer = sentence
            localized = True
            template_used = "current_stage"
    elif (target != "en" and gated is None
            and str(envelope.get("intent") or "") == "DOCUMENTS_PENDING"
            and str(envelope.get("category") or "") == "CASE_ONLY"
            and not envelope.get("subject") and not envelope.get("guardrail")
            and str(envelope.get("response_source") or "") == "STRUCTURED"):
        # THE SAME TWO LISTS the English answer is built from (answer.py):
        # documents not yet collected, and documents awaiting verification.
        from app.agents.applicant import answer as answers

        missing = [answers._readable(i.get("slot"))
                   for i in envelope.get("pending_items") or []
                   if isinstance(i, dict) and i.get("code") == "DOCUMENT_MISSING"]
        awaiting = [answers._doc_line(d) for d in envelope.get("documents") or []
                    if isinstance(d, dict)
                    and d.get("status") in {"UPLOADED", "PROCESSING", "REVIEW"}]
        sentence = language.localized_pending(target, missing, awaiting)
        if sentence:
            envelope["answer_en"] = answer
            answer = sentence
            localized = True
            template_used = "pending_documents"
    elif (target != "en"
            and str(envelope.get("intent") or "") == "HUMAN_HANDOFF_REQUESTED"):
        sentence = language.localized("handoff_acknowledged", target)
        if sentence:
            envelope["answer_en"] = answer
            answer = sentence
            localized = True
            template_used = "handoff_acknowledged"

    toned = sentiment.apply_tone(answer, signal, target if localized else "en")
    tone_added = toned != answer
    answer = toned
    envelope["answer"] = answer
    channel = str(request.channel or "api").strip().lower()
    return {
        "sentiment": signal,
        "canonical": canonical,
        "channel": channel if channel in _CHANNELS else "api",
        "language": {**detected.public(), "response_language": target,
                     "localized": localized},
        # FOR THE AUDIT TRAIL (answer_basis.presentation): which deterministic
        # template worded the answer, and whether a tone line was added.
        "presentation": {"localized_template": template_used,
                         "tone_opener_added": tone_added,
                         "facts_changed": False},
    }


def _understood(request: CopilotQueryRequest) -> intents.Classification:
    """The question as the agent understood it -- normalised, same rules."""
    return intents.understand(request.message,
                              has_case=bool(request.case_id))


#: Intents whose response carries the application's recorded status.
_STATUS_INTENTS = {"APPLICATION_STATUS", "APPLICATION_STAGE"}


#: Below this much of the request budget left, no model call is started.
_MIN_COMPOSE_SECONDS = 1.0


def _budget_left(envelope: dict[str, Any]) -> float:
    """Seconds left of the request budget (`chatbot.compose.request_budget_seconds`)."""
    from app.agents.applicant import config as agent_config

    started = envelope.get("_request_started")
    budget = agent_config.compose_request_budget_seconds()
    if started is None:
        return budget
    return max(0.0, budget - (time.perf_counter() - started))


def _composition_skip(envelope: dict[str, Any], category: str) -> str | None:
    """
    WHY NO MODEL IS CALLED -- or None when one should be.

    ONE RULE FOR EVERY SURFACE: an intent the agent answers deterministically
    (`SIMPLE_INTENTS`, unless `llm_for_simple_intents`) is answered
    deterministically here too; so is anything that quotes recorded values,
    names a party, states next actions or reports history. Knowledge answers
    ARE phrased (once): condensing a handbook passage is what a composer adds.
    """
    from app.agents.applicant import config as agent_config
    from app.agents.applicant.intents import SIMPLE_INTENTS, Intent

    answered_by = str(envelope.get("base_intent")
                      or envelope.get("intent") or "").upper()
    if envelope.get("answer_is_quoted") or answered_by in _QUOTED:
        return "RECORDED_VALUES"
    if category == "MIXED" and str(envelope.get("response_source") or "")             == routing.ResponseSource.MIXED.value:
        return "BOTH_HALVES_AS_BUILT"
    if envelope.get("subject") or envelope.get("next_actions")             or envelope.get("history") or envelope.get("delay"):
        return "DETERMINISTIC_ANSWER"
    if category in ("KNOWLEDGE_ONLY", "PROCESS_KNOWLEDGE"):
        return None
    try:
        intent = Intent(str(envelope.get("base_intent")
                            or envelope.get("intent") or ""))
    except ValueError:
        intent = None
    if intent in SIMPLE_INTENTS and not agent_config.llm_for_simple_intents():
        return "SIMPLE_INTENT"
    # CASE COMPOSITION IS A SWITCH OF ITS OWN (compose.case_answers, which
    # also requires the model to be enabled); knowledge phrasing, as before,
    # depends only on the model being reachable.
    if not agent_config.compose_case_answers():
        return "CASE_COMPOSITION_OFF"
    return None


def _whole_sentences(text: str, limit: int) -> str:
    """At most `limit` whole sentences -- never a cut mid-sentence."""
    said = " ".join(str(text or "").split())
    parts = [p for p in re.split(r"(?<=[.!?])\s+", said) if p.strip()]
    return " ".join(parts[:limit]) if len(parts) > limit else said


#: Supplying a document -- supported whenever one is recorded as outstanding.
_PROVIDE_VERBS = frozenset({"upload", "reupload", "re-upload", "provide",
                            "submit", "resubmit", "collect", "bring"})

#: Actions a composed answer might tell someone to take.
_ACTION_VERBS = re.compile(
    r"\b(re-?upload|upload|submit|provide|resubmit|collect|bring|sign|"
    r"visit|call|pay|deposit)\b")


def _accept_composed(text: str, *, structured: str, facts: dict[str, Any],
                     identifiers: tuple[str | None, ...], evidence: str,
                     stage: str | None) -> tuple[bool, str]:
    """
    Whether a composed sentence may be published: BOTH validators.

    `check_composed` holds it to the structured answer (nothing dropped,
    leaked, invented or too long). `validate_answer` holds it to what the
    model was shown -- no downstream decision language ("approved",
    "sanctioned", "disbursed") and no number the facts do not carry. Case
    answers were phrased inside the agent, behind `validate_answer`, until
    the Copilot took the phrasing over; the second check came with them.
    """
    from app.agents.applicant.validate import validate_answer

    accepted, checked = check_composed(
        text, structured=structured, identifiers=identifiers,
        evidence=evidence, stage=stage)
    if not accepted:
        return accepted, checked
    # AN ACTION OR A PARTY THE RECORDS DO NOT STATE. The model is told what
    # is established; "please upload your PAN" where the recorded next step
    # is a reviewer's, or "the primary applicant" where the finding is the
    # co-applicant's, is a decision it was not given.
    known = f"{structured} {json.dumps(facts, default=str)} {evidence}".lower()
    # Providing a document the records say is pending or missing is the
    # recorded gap, phrased as a step -- not a new action.
    outstanding = bool(re.search(r"\b(pending|missing|outstanding|not\s+yet\s+"
                                 r"(uploaded|collected)|re-?upload)\b", known))
    for verb in _ACTION_VERBS.findall(checked.lower()):
        if verb in _PROVIDE_VERBS and outstanding:
            continue
        if verb not in known:
            return False, f"answer introduced an action not on record: {verb}"
    for party in ("primary applicant", "co-applicant", "co applicant"):
        if party in checked.lower() and party not in known:
            return False, f"answer named a party not on record: {party}"
    grounded_ok, reason = validate_answer(
        checked, {"facts": facts, "structured": structured,
                  "evidence": evidence}, surface="copilot_composer")
    return (True, checked) if grounded_ok else (False, reason)


#: Intents whose answer quotes recorded values and is never rephrased.
_QUOTED = {"CASE_HISTORY", "ELIGIBILITY", "INCOME_EVIDENCE",
           "DOCUMENT_DETAILS", "APPLICANT_PROFILE"}


async def _grounded(
    request: CopilotQueryRequest, envelope: dict[str, Any],
    context: stages.StageContext, request_id: str,
) -> tuple[grounding.GroundedContext, bool]:
    """
    Retrieve evidence for this question and ground the answer on it.

    NEVER RAISES EXCEPT FOR OWNERSHIP. A vector store that is down, a
    model that is off, an embedding provider that cannot be reached --
    all of them cost the retrieved half and leave the structured
    answer standing. `NotOwned` is the exception, and is deliberately
    allowed to propagate: an access refusal must not be downgraded
    into "nothing found".
    """
    category = str(envelope.get("category") or "").upper()
    if category not in _RETRIEVES:
        return grounding.GroundedContext(), False
    # ONE RECORDED DETAIL IS ANSWERED FROM ITS RECORD ALONE: no retrieval,
    # no model (it is quoted, never phrased). The applicant / application
    # records on the envelope are what ground it.
    if str(envelope.get("intent") or "").upper() == "APPLICANT_PROFILE":
        envelope["_composition"] = {"called": False, "skipped": "RECORDED_VALUES",
                                    "language": getattr(request, "language", None) or "en"}
        return grounding.GroundedContext(), False

    applicant_id = str(envelope.get("applicant_id")
                       or request.applicant_id or "").strip()
    if not applicant_id and category != "PROCESS_KNOWLEDGE":
        # No applicant means no scope, and there is no unscoped
        # retrieval. The structured answer stands alone.
        return grounding.GroundedContext(), False

    # STAGES ARE CHOSEN HERE, by the orchestrator, never by similarity.
    # A journey question asks where the case has BEEN, so it gets the
    # ordered stages up to the current one; everything else gets the
    # stage the case is in now.
    journey = _is_journey(request.message)

    # WHICH STAGE THE GUIDE COMES FROM.
    #
    # A process question is about the stage it NAMES -- "what does RCU
    # check" is an RCU question even on a case sitting at FOS, and
    # answering it from the case's stage would answer a different
    # question than the one asked. Read from the words the user typed,
    # never from similarity.
    #
    # A CASE question is about the stage the case is IN, which the
    # record decides and the caller cannot override.
    named = intents.stage_in(request.message)
    if category == "PROCESS_KNOWLEDGE":
        chosen = named or (context.stage.value if context.stage else None)
        stage_set = (chosen,) if chosen else ()
    else:
        stage_set = retrieval.stages_for(
            context.stage.value if context.stage else None, journey=journey)

    # A PROCESS QUESTION HAS NO CASE SCOPE, because the stage guides
    # belong to no case. `gather` only builds a case search for the
    # categories that need one.
    #
    # ONE CASE, OR NONE. A case search without a case_id spans every case
    # the applicant holds; that is only the question when it is about the
    # applicant's cases as a whole (CASE_PORTFOLIO). Any other case
    # question asked without a case gets no case evidence -- evidence from
    # a sibling case is not evidence about the one being asked about.
    answered = str(envelope.get("base_intent") or envelope.get("intent") or "").upper()
    if (not request.case_id and category != "PROCESS_KNOWLEDGE"
            and answered != "CASE_PORTFOLIO"):
        applicant_id = ""
    try:
        scope = (Scope(app_id=applicant_id, case_id=request.case_id,
                       stages=() if category == "PROCESS_KNOWLEDGE"
                       else stage_set)
                 if applicant_id else None)
    except UnscopedSearch:
        return grounding.GroundedContext(), False

    timings = envelope.setdefault("_timings", {})
    with timed(timings, "rag", category=category):
        # OFF THE EVENT LOOP. Retrieval embeds the question (a blocking
        # HTTP call with an Ollama embedder) and queries the vector store
        # synchronously; run inline it stalled every other request on the
        # worker for its duration.
        from app.knowledge.embeddings import EMBED_STATS

        embed_stats: dict[str, Any] = {}
        token = EMBED_STATS.set(embed_stats)
        try:
            gathered = await asyncio.to_thread(
                grounding.gather, request.message, category=category,
                scope=scope, stages=stage_set)
        finally:
            EMBED_STATS.reset(token)
        timings.update(embed_stats)

    # A QUESTION ABOUT ONE DOCUMENT IS ANSWERED FROM THAT DOCUMENT.
    #
    # Retrieval returns what the case has, which for a case under
    # review is dominated by its problems. Asked "was the bank
    # statement verified", the model received the address mismatch,
    # the profile mismatch and the review decision -- all true, none
    # of them about the bank statement -- and concluded the bank
    # details were not verified. Narrowing first is what stops broad
    # case context from answering a document-level question.
    focus = _understood(request).document_type
    if focus:
        gathered = _focused(gathered, focus)

    # A RECORDED VALUE IS QUOTED, NEVER REPHRASED.
    #
    # These answers quote what the pipeline recorded -- two names from a
    # KYC comparison, a FOIR, the name on a PAN -- and the agent builds
    # them with no model at any setting. Handing them to the model here
    # undid that: with retrieval confident, a generated sentence replaced
    # the recorded one, checked only for contradicting the verdict, so a
    # name could be dropped or changed and the response still said
    # STRUCTURED. The evidence still counts towards `grounded` and still
    # appears in `sources`; the model is simply not asked, which also
    # takes its latency off the questions that need it least.
    answered_by = str(envelope.get("base_intent")
                      or envelope.get("intent") or "").upper()
    # WHY NO MODEL, recorded before any early return: every skip says why.
    envelope["_composition"] = {
        "called": False, "skipped": _composition_skip(envelope, category),
        "language": getattr(request, "language", None) or "en"}
    if answered_by in _QUOTED:
        return gathered, gathered.grounded
    # A PER-PARTY ANSWER IS PUBLISHED AS BUILT: a composer held to two
    # sentences is how "the co-applicant's PAN" becomes "the PAN". So is a
    # next-best-action answer: the action is established, not phrased.
    if envelope.get("subject") or envelope.get("next_actions"):
        return gathered, gathered.grounded
    # A MIXED ANSWER WITH BOTH HALVES IS PUBLISHED AS BUILT. The agent wrote
    # the case half from the records and the general half from the handbook,
    # labelled apart; a composer held to two sentences keeps one of them,
    # and nothing checks that the other survived.
    if category == "MIXED" and str(envelope.get("response_source") or "") \
            == routing.ResponseSource.MIXED.value:
        return gathered, gathered.grounded
    # WHETHER A MODEL IS WORTH CALLING AT ALL (Slice 11): ONE policy,
    # the same simple-intent rule the agent applies. A deterministic answer
    # that is already complete is published as it is -- no model, no wait.
    skip = _composition_skip(envelope, category)
    budget_left = _budget_left(envelope)
    if skip is None and budget_left < _MIN_COMPOSE_SECONDS:
        skip = "REQUEST_BUDGET"
    composition = envelope.setdefault("_composition", {})
    composition.update({"called": False, "skipped": skip})

    structured = str(envelope.get("answer") or "")
    facts = _facts(envelope, context)
    await _annotate(envelope, facts, timings)
    from app.agents.applicant import config as agent_config

    stats: dict[str, Any] = {}
    with timed(timings, "compose"):
        answer, grounded = await grounding.answer(
            request.message, structured=structured,
            facts=facts, context=gathered,
            compose_structured=skip is None,
            # BOUNDED TWICE: the composer's own ceiling, and whatever is left
            # of the request's budget.
            timeout=min(agent_config.compose_timeout_seconds(), budget_left),
            stats=stats,
        )
    composition.update({k: v for k, v in stats.items()
                        if k in ("called", "qwen_ms", "error",
                                 "composer_context_ms", "prompt_build_ms")})

    # CHECKED BEFORE IT IS PUBLISHED. A composed answer that leaks an id,
    # drops the recorded reason or a pending document, invents a hold, a
    # name, an action or a party, or runs long is replaced by the structured
    # answer -- which is then what `response_source` truthfully reports.
    # NO RETRY: a rejected phrasing costs a second model call and a second
    # wait for nothing the recorded answer does not already say.
    def checked_composition(text: str) -> tuple[bool, str]:
        from app.observability.tracing import annotate

        with span("copilot.validator", surface="copilot_composer") as current:
            verdict = _accept_composed(
                text, structured=grounding._readable(structured), facts=facts,
                identifiers=(request.case_id, request.applicant_id,
                             envelope.get("case_id"), envelope.get("applicant_id")),
                evidence=" ".join(item.text for item in gathered.case.evidence),
                stage=context.stage.value if context.stage else None,
            )
            annotate(current, accepted=verdict[0])
            return verdict

    if structured and answer.strip() != grounding._readable(structured).strip():
        started = time.perf_counter()
        # WHOLE SENTENCES UP TO THE LIMIT, THEN VALIDATION: a correct answer
        # that ran one sentence long is kept when every recorded fact
        # survives the trim -- required_facts decides, so meaning is never
        # silently dropped.
        accepted, checked = checked_composition(
            _whole_sentences(answer, agent_config.max_sentences()))
        timings["validation_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if not accepted:
            logger.info("Composed answer rejected (%s); published the "
                        "structured answer", checked)
            answer = grounding._readable(structured)
            envelope["_validation"] = "REJECTED_FALLBACK"
            composition["outcome"] = "REJECTED"
        else:
            answer = checked
            envelope["_validation"] = "PASSED"
            composition["outcome"] = "ACCEPTED"
    elif composition.get("called"):
        composition["outcome"] = "FALLBACK" if stats.get("error") else "UNCHANGED"
    if composition.get("outcome") in ("REJECTED", "FALLBACK"):
        # A FALLBACK IS A TRACED EVENT: why the recorded answer was published
        # instead of the model's (codes only -- never the rejected text).
        with span("copilot.fallback", outcome=composition.get("outcome"),
                  model_error=stats.get("error")):
            pass
    envelope["answer"] = answer

    # WHO WROTE THE SENTENCE. A model-phrased answer was published as
    # STRUCTURED (a case answer) or KNOWLEDGE (a handbook or stage-guide
    # answer), which says no model touched it. `LLM` is defined as "a model
    # phrased it" -- the facts still came from the records or the retrieved
    # evidence -- so it is what every category reports when the published
    # sentence is the model's.
    # A MODEL WROTE IT only when one was actually called, answered, and its
    # words are what is published -- never inferred from the text differing
    # (a retrieved guide's own sentences, published when the model is down,
    # differ from an empty structured answer and are nobody's phrasing).
    model_wrote = (bool(stats.get("called")) and not stats.get("error")
                   and bool(answer.strip())
                   and answer.strip() != grounding._readable(structured).strip()
                   and answer.strip() != grounding.NO_EVIDENCE)
    if model_wrote:
        envelope["response_source"] = routing.ResponseSource.LLM.value

    # WHAT THE ANSWER WAS ACTUALLY BUILT FROM.
    #
    # A process question carries no structured answer -- the agent
    # publishes STRUCTURED only because that is the envelope default,
    # and on this path it is not true of anything. When a stage guide
    # was retrieved, the answer came from the knowledge base, which
    # is what the FOS knowledge path reports for the same reason.
    #
    # ONLY THIS CATEGORY. The others already report a source the
    # agent computed, and overwriting those would claim retrieval
    # decided an answer the records decided.
    if category == "PROCESS_KNOWLEDGE" and grounded and not model_wrote:
        envelope["response_source"] = routing.ResponseSource.KNOWLEDGE.value

    return gathered, grounded


#: Words that make a question about where the case HAS BEEN rather
#: than where it is. Deliberately a small, explicit list: widening a
#: search across stages is a decision, and a decision belongs in
#: something a reader can see.
_JOURNEY_WORDS = ("before", "previously", "earlier", "history", "journey",
                  "moved", "progress", "so far", "until now", "led to")


def _is_journey(message: str) -> bool:
    lowered = (message or "").lower()
    return any(word in lowered for word in _JOURNEY_WORDS)


def _deduplicated(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    One entry per piece of evidence.

    TWO ROUTES REACH THE SAME FINDING. The agent cites case memory
    directly, with the reason code it recorded; retrieval cites the
    chunk derived from that same finding, which carries the document
    but not the code. Published as they came, a reviewer saw the same
    document listed three and four times, some entries richer than
    others, and had no way to tell whether that meant several
    findings or one finding seen twice.

    THE LESS SPECIFIC ENTRY LOSES. A pointer at a document with no
    reason code, where another entry names a code on that same
    document, is the same evidence with less said about it -- so it
    is dropped rather than merged, which would attach a code to a
    pointer that never carried one.

    TWO CODES ON ONE DOCUMENT ARE TWO FINDINGS and both survive: the
    key includes the code, so ADDRESS_MISMATCH and PROFILE_MISMATCH
    on one deed stay separate.

    ORDER IS KEPT, and so are null fields -- the shape callers parse
    does not change, only the repetition.
    """
    def identity(source: dict[str, Any]) -> tuple:
        # NEITHER case_id NOR stage IS PART OF THE IDENTITY. Every
        # source in one response belongs to one case, so neither says
        # anything here -- and the two routes disagree about them:
        # case memory cites a finding without repeating the case, and
        # retrieval labels the same finding with the case and the
        # stage it was indexed under. Keyed on those, one finding
        # looked like two.
        return (
            str(source.get("kind") or source.get("source_type") or ""),
            str(source.get("document_id") or source.get("source_id") or ""),
            str(source.get("reason_code") or ""),
        )

    #: Where a code IS known, for the same kind and document.
    specific = {
        identity(s)[:2] for s in sources if s.get("reason_code")
    }

    kept: dict[tuple, dict[str, Any]] = {}
    for source in sources:
        key = identity(source)
        if not source.get("reason_code") and source.get("document_id"):
            if key[:2] in specific:
                continue
        if key in kept:
            # Same evidence, twice. Keep whichever said more.
            for field, value in source.items():
                if value and not kept[key].get(field):
                    kept[key][field] = value
            continue
        kept[key] = dict(source)

    return list(kept.values())


def _focused(gathered: "grounding.GroundedContext",
             document_type: str) -> "grounding.GroundedContext":
    """
    The same context, with case evidence about one kind of document.

    KEPT WHOLE WHEN NOTHING MATCHES. A question naming a document the
    case does not hold would otherwise be answered from no evidence
    at all, which reads as "I have nothing" when the honest answer is
    "no such document is on file" -- and that answer comes from the
    records, not from here.

    PROCESS EVIDENCE IS UNTOUCHED. It belongs to a stage rather than
    to a document, and a mixed question still needs it.
    """
    from app.knowledge.retrieval import RetrievalResult

    wanted = str(document_type).strip().upper()
    kept = tuple(
        item for item in gathered.case.evidence
        if str(item.provenance.get("document_type") or "").upper() == wanted
    )
    if not kept:
        return gathered

    return grounding.GroundedContext(
        case=RetrievalResult(evidence=kept,
                             sufficient=gathered.case.sufficient),
        process=gathered.process,
    )


def _relevant(sources: list[dict[str, Any]],
              document_type: str | None) -> list[dict[str, Any]]:
    """
    The sources that support THIS answer, not every source the case has.

    A document question cited the whole case: the address mismatch on
    the deed, the profile mismatch on the PAN and the review decision
    all appeared under "was the bank statement verified", which
    invites a reader to connect them to an answer they have nothing
    to do with.

    THE DECISION AND THE GUIDANCE STAY. The case decision is the
    context any answer sits in, and process knowledge answers the
    half of a mixed question that a document cannot.
    """
    if not document_type:
        return sources

    wanted = str(document_type).strip().upper()
    kept = [
        source for source in sources
        if str(source.get("document_type") or "").upper() == wanted
        or str(source.get("type") or "") in {"CASE_DECISION",
                                             "PROCESS_KNOWLEDGE"}
    ]
    return kept or sources


def _document_types(envelope: dict[str, Any]) -> dict[str, str]:
    """
    document_id -> the KIND of document, from what the agent cited.

    Retrieval labels a document chunk with its type; case memory cites
    a finding on the same document without one. Collected here so the
    second can borrow it from the first rather than a reader seeing a
    finding against a bare file name.
    """
    known: dict[str, str] = {}
    for source in envelope.get("sources") or []:
        document, kind = source.get("document_id"), source.get("document_type")
        if document and kind:
            known[str(document)] = str(kind)
    return known


def _normalised(sources: list[dict[str, Any]], *, case_id: str | None,
                stage: str | None,
                documents: dict[str, str]) -> list[dict[str, Any]]:
    """
    Fill in what is known, and say the kind of each source out loud.

    WHY ANY OF THIS IS EMPTY TO BEGIN WITH. Two routes build sources.
    Case memory cites a finding it holds in hand and repeats neither
    the case nor the stage, because the caller asked about that case;
    retrieval cites a chunk and labels it with both. Published
    together, the same finding appeared once with `case_id: null` and
    once with it filled, which reads like two different records.

    EVERY SOURCE IN ONE RESPONSE BELONGS TO ONE CASE, and the stage is
    the one the case resolved to, so neither is a guess -- they are
    the values the response already states at the top level.

    `type` IS ADDED, `kind` IS KEPT. The uppercase form is what the
    frontend reads and what the rest of the response uses for a
    source type; removing the old key would break callers that read
    it, and it costs one field to keep both.
    """
    filled: list[dict[str, Any]] = []

    for source in sources:
        published = dict(source)

        published["type"] = str(
            source.get("source_type")
            or str(source.get("kind") or "evidence").upper()
        )
        # NOT ON PROCESS KNOWLEDGE. A stage guide describes a desk
        # and belongs to no case; stamping this case onto it would
        # claim the guidance was recorded against this file, which is
        # what `case_id: null` on that source exists to deny.
        if (case_id and not published.get("case_id")
                and published["type"] != "PROCESS_KNOWLEDGE"):
            published["case_id"] = case_id
        if stage and not published.get("stage"):
            published["stage"] = stage

        document = published.get("document_id")
        if document and not published.get("document_type"):
            borrowed = documents.get(str(document))
            if borrowed:
                published["document_type"] = borrowed

        filled.append(published)

    return filled


def _facts(envelope: dict[str, Any],
           context: stages.StageContext | None = None) -> dict[str, Any]:
    """
    The structured facts the model may see. AN ALLOWLIST.

    No MCP envelopes, no agent state, no trace, no prompts -- the
    model gets the same verdicts the caller does.
    """
    # NO IDENTIFIERS. These carried the case id, the applicant id and
    # the internal intent, and a model given an identifier prints it:
    # "The bank statement document for applicant DEMO-APP-002 has
    # verified bank account details." The reader knows whose case they
    # opened, the response states both ids at the top level, and
    # neither helps answer a question.
    #
    # WHAT THE MODEL NEEDS IS THE VERDICT AND THE EVIDENCE, and it gets
    # the verdict as `established`. Beside it: the stage the case is in
    # (a label, never an id), and -- only when JEV is on -- its notes,
    # labelled as annotations that settle nothing.
    facts: dict[str, Any] = {}
    if context is not None and context.stage is not None:
        from app.agents.applicant import config as agent_config

        # THE STAGE IS GIVEN, NEVER ASKED FOR: the model is told where the
        # case is and must not describe it anywhere else (the validator
        # rejects an answer naming a stage the records do not).
        facts["current_stage"] = agent_config.stage_label(context.stage.value)
        if context.status:
            facts["stage_status"] = context.status.replace("_", " ").lower()
    # THE EVIDENCE PACKET (app/agents/applicant/evidence.py): the recorded
    # problems with their evidence chain, what is pending, the next action
    # the workflow set, and -- for a stage question -- the stage history.
    # Copied from records; the model phrases, it does not decide.
    if "_evidence_packet" not in envelope:
        envelope["_evidence_packet"] = _evidence(envelope, context)
    facts.update(envelope["_evidence_packet"])
    return facts


async def _annotate(envelope: dict[str, Any], facts: dict[str, Any],
                    timings: dict[str, float]) -> None:
    """
    OPTIONAL JEV notes for the composer -- additive, non-authoritative, and
    awaited with a timeout so a slow provider never stalls other requests.
    Runs only when a composition is actually going to happen.
    """
    from app.agents.applicant import jev

    if not jev.active():
        return
    started = time.perf_counter()
    notes = await jev.annotate_async({
        "question": str(envelope.get("intent") or ""),
        "stage": facts.get("current_stage"),
        "established": str(envelope.get("answer") or ""),
    })
    timings["jev_ms"] = round((time.perf_counter() - started) * 1000, 2)
    if notes:
        facts["annotations_not_authoritative"] = notes
        envelope["_jev_notes"] = notes


#: The categories whose answer is ABOUT THIS CASE, and so may carry its facts.
#: A knowledge answer, a routed refusal and a clarification read no case
#: record, and must not publish one -- or hand one to the model phrasing
#: them -- because a case is open on the screen.
_CASE_CATEGORIES = {"CASE_ONLY", "MIXED"}


def _provenance(request: "CopilotQueryRequest", envelope: dict[str, Any],
                context: Any, retrieved: Any, request_id: str
                ) -> dict[str, Any] | None:
    """
    Build and validate the answer's provenance; answer "why this answer?"
    from it. Never raises: provenance failing costs the explanation, never
    the answer. Logged as ids, kinds and verdicts -- never values.
    """
    from app.agents.applicant import followup
    from app.agents.applicant import provenance as chain
    from app.security import guardrails

    try:
        built = chain.build(question=request.message, envelope=envelope,
                            context=context,
                            evidence_items=envelope.get("_evidence_items"),
                            retrieved=retrieved)
        chain.validate(built, envelope.get("_ledger"), context)
    except Exception as exc:
        logger.warning("Provenance unavailable request_id=%s (%s)", request_id,
                       type(exc).__name__)
        return None
    logger.info("copilot_provenance request_id=%s validated=%s nodes=%s "
                "problems=%s", request_id, built["validated"],
                ",".join(f"{n['kind']}:{n.get('source_type')}:"
                         f"{'ok' if n['verified'] else 'UNVERIFIED'}"
                         for n in built["nodes"]),
                ";".join(built["problems"]) or "-")
    # "WHY THIS ANSWER?" -- the verified sources, in business words.
    followed = envelope.get("followed_up") or {}
    if followed.get("reason") == followup.EXPLAIN_REASON:
        said, _ = guardrails.published(chain.explain(built))
        envelope["answer"] = said
        envelope["_explained"] = True
    return built


def _provenance_public(built: dict[str, Any] | None) -> dict[str, Any] | None:
    from app.agents.applicant import provenance as chain

    return chain.public(built) if built else None


def _telemetry(request_id: str, envelope: dict[str, Any],
               published: dict[str, Any]) -> None:
    """
    Analytics hooks, safe to ship: counts and codes, never values, names,
    ids or text. (The OTel/CloudWatch slice attaches its exporter here.)
    """
    impacts = [p.get("impact") for p in envelope.get("_evidence_items") or []
               if isinstance(p.get("impact"), dict)]
    nba = envelope.get("next_actions") or {}
    primary = nba.get("primary") or {}
    logger.info(
        "copilot_intelligence request_id=%s intent=%s impacts=%d "
        "impact_codes=%s blocking=%s nba=%s nba_additional=%d handoff=%s "
        "guardrail=%s", request_id, published.get("intent"), len(impacts),
        ",".join(sorted({str(i.get("impact_code")) for i in impacts})) or "-",
        any(i.get("blocking") is True for i in impacts),
        primary.get("action_code") or "-", len(nba.get("additional") or []),
        bool((nba.get("handoff") or {}).get("required")),
        (envelope.get("guardrail") or {}).get("action", "PASSED"))


def _routing_basis(request: CopilotQueryRequest, envelope: dict[str, Any],
                   tools: list[dict[str, Any]],
                   retrieved: "grounding.GroundedContext", *,
                   gated: bool) -> dict[str, Any]:
    """
    WHICH ROUTE ANSWERED, AND WHAT IT CONSULTED -- read from what ran, never
    from what the route was meant to do.

        route        CASE_ONLY / KNOWLEDGE_ONLY / MIXED / PROCESS_KNOWLEDGE /
                     DOWNSTREAM / UNSUPPORTED -- the published `category`
        understood   rule | semantic | follow-up: how the question was read
        consulted    case_tools (governed tools that answered),
                     case_records (retrieved from THIS case's own records),
                     knowledge (handbook / stage guide)
    """
    followed = envelope.get("followed_up") or {}
    asked = str(followed.get("interpreted_as") or request.message)
    matched = str(intents.understand(
        asked, has_case=bool(request.case_id)).matched_on or "")
    understood = ("follow-up" if followed
                  else "semantic" if matched.startswith("semantic:")
                  else "rule")

    knowledge_used = bool((envelope.get("knowledge") or {}).get("grounded")) \
        or bool(retrieved.process.evidence)
    consulted = [name for name, used in (
        ("case_tools", any(t.get("ok") for t in tools)),
        ("case_records", bool(retrieved.case.evidence)
         or bool(envelope.get("case_memory"))),
        ("knowledge", knowledge_used),
    ) if used and not gated]
    return {
        "route": str(envelope.get("category") or "UNSUPPORTED").upper(),
        "understood": understood,
        "consulted": consulted,
    }


def _impact_words(value: Any) -> str | None:
    from app.agents.applicant import impact

    return impact.text(value) if isinstance(value, dict) else None


def _for_composer(problem: dict[str, Any]) -> dict[str, Any]:
    """A problem as the model may see it: the verdict and the values behind
    it, and no identifier of any kind -- a model shown an id prints it."""
    return {k: v for k, v in {
        "type": problem.get("type"), "message": problem.get("message"),
        "finding": problem.get("finding"), "status": problem.get("status"),
        "document": problem.get("document"),
        "party_role": problem.get("party_role"),
        # WHAT IT MEANS, by rule (Slice 8) -- words, never the rule reference.
        "impact": (_impact_words(problem.get("impact"))),
        "evidence": [{"source": e.get("source"), "field": e.get("field"),
                      "value": e.get("value")}
                     for e in problem.get("evidence") or []] or None,
    }.items() if v}


def _evidence(envelope: dict[str, Any], context) -> dict[str, Any]:
    """The Evidence Builder's case half, for the composer and the response."""
    from app.agents.applicant import case_memory_facts, evidence

    if str(envelope.get("category") or "").upper() not in _CASE_CATEGORIES:
        return {}
    case_id = envelope.get("case_id")
    packet: dict[str, Any] = {}
    from app.agents.applicant import ledger

    # THE CASE LEDGER (app/agents/applicant/ledger.py): the same records,
    # normalised the same way, as "what changed" reads. The full chain --
    # record ids included -- stays internal in `_evidence_items`; the
    # composer is given it without any identifier, and the response
    # without record ids or values.
    started = time.perf_counter()
    book = ledger.load(case_id) if case_id else None
    envelope["_ledger"] = book
    found = (book.evidence(
        since=getattr(context, "hold_since", None),
        party_id=envelope.get("party_id"),
        stage=context.stage.value if getattr(context, "stage", None) else None)
        if case_id else [])
    envelope.setdefault("_timings", {})["evidence_ms"] = round(
        (time.perf_counter() - started) * 1000, 2)
    # A QUESTION ABOUT A PARTY CARRIES THAT PARTY'S EVIDENCE ONLY -- never
    # the other party's, never the case's attributed to them (Slice 4/10).
    subject = envelope.get("subject") or {}
    named = {p.get("party_id") for p in subject.get("parties") or []
             if p.get("party_id")}
    if named:
        found = [p for p in found if p.get("party_id") in named]
    envelope["_evidence_items"] = found
    if found:
        packet["problems"] = [_for_composer(p)
                              for p in found[:evidence.MAX_PROBLEMS]]
    pending = [i.get("detail") or i.get("slot") for i in
               envelope.get("pending_items") or [] if isinstance(i, dict)]
    if pending:
        packet["pending"] = pending
    action = envelope.get("next_action")
    if isinstance(action, dict) and action.get("detail"):
        packet["next_action"] = action["detail"]
    recorded = evidence.history(context)
    if len(recorded) > 1:
        packet["stage_history"] = recorded
    return packet


@router.post(
    "/query",
    summary="Ask the Universal LOS Copilot about a case",
    description=(
        "Natural-language questions about one case, answered from stored "
        "records.\n\n"
        "**Case history** — *\"why is this case in review?\"* — is answered "
        "from the findings the LOS pipeline recorded at the time, with the "
        "reason codes it wrote down. Nothing is inferred: when no findings "
        "were recorded, the answer says so rather than offering a plausible "
        "explanation.\n\n"
        "Requires `case_id`, and the caller must own it. Credit, risk and "
        "underwriting decisions are out of scope and are never generated "
        "here — recorded ones are reported as recorded."
    ),
    responses={
        200: {"model": CopilotQueryResponse, "description": "The answer."},
    },
)
async def query(
    request: CopilotQueryRequest,
    claims: dict[str, Any] = Depends(require_jwt),
):
    """
    Delegates to the one agent.

    Authorisation is NOT performed here and must not be: the agent runs
    capability and case-ownership checks before it reads anything, and a
    second check at this layer would be a second answer to the same
    question -- with the weaker one winning whenever they disagreed.
    """
    request_id = f"cp_{uuid.uuid4().hex}"
    started = time.perf_counter()
    timings: dict[str, float] = {}

    # SECURITY AND SMALL TALK FIRST (decision order: 1. what kind of request
    # is this, 2. is the caller allowed ...). A request the input policy
    # refuses, or a greeting, is answered by the agent before any read -- so
    # the route does not read the case for it either: no ownership lookup,
    # no stage resolution, no evidence packet. The refusal says nothing
    # about the case, so there is nothing to authorise.
    early = _answered_without_the_case(request)

    # OWNERSHIP BEFORE ANYTHING ABOUT THE CASE IS READ OR SAID. The stage
    # below is a fact about the case, and every response publishes it --
    # including the ones the agent returns before its own ownership check
    # (a clarification, a knowledge answer, a guardrail refusal). Resolving
    # it first told a caller who does not hold a case which desk it sits
    # at. The SAME check the agent runs (permissions.check_ownership), so
    # there is still one answer to "may this caller see this case".
    if request.case_id and not early:
        from app.agents.applicant import permissions
        from app.security import access

        caller = permissions.Caller.from_claims(claims)
        try:
            if request.applicant_id:
                permissions.check_ownership(
                    request.applicant_id, request.case_id, caller=caller,
                    write=False)
            else:
                # No applicant named: the caller must still hold the case.
                try:
                    access.authorize(caller.subject, caller.scopes,
                                     case_id=request.case_id, write=False)
                except access.AccessDenied as denied:
                    raise permissions.PermissionDenied(
                        denied.code, denied.message) from None
            # A CUSTOMER-FACING DEPLOYMENT: service scopes do not open another
            # customer's case in a conversation -- only ownership does.
            if (access.conversation_service_access() == "deny"
                    and access.is_service(caller.scopes, write=False)
                    and not access.holds(caller.subject, request.case_id)):
                raise permissions.PermissionDenied(
                    "CASE_NOT_ACCESSIBLE", "Not the caller's case.")
        except permissions.PermissionDenied:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"request_id": request_id,
                        "code": "CASE_ACCESS_DENIED",
                        "message": "You are not authorized to access "
                                   "this case."},
            ) from None

    # WHICH DESK THIS QUESTION BELONGS TO, read from the case rather than
    # taken from the caller. `resolve` prefers the case's own timeline,
    # then its application status, and only falls back to what the caller
    # said when the record establishes nothing.
    with timed(timings, "stage", request_id=request_id):
        context = (stages.resolve(None, None) if early
                   else stages.resolve(request.case_id, request.stage))

    # AUDIT CONTEXT for every line this request writes: stage, where it came
    # from, channel, auth mode and language -- codes only.
    from app.agents.applicant import audit as _audit

    _audit.set_context(
        stage=context.stage.value if context.stage else "UNRESOLVED",
        stage_source=context.public().get("stage_resolution"),
        channel=(request.channel or "api").strip().lower()
        if (request.channel or "api").strip().lower() in _CHANNELS else "api",
        auth_mode="DISABLED_DEV" if claims.get("auth_disabled") else "JWT",
        language=language.detect(request.message).code)

    try:
        with timed(timings, "agent", request_id=request_id,
                   stage=context.stage.value if context.stage else None):
            envelope = await answer_question(
                message=request.message,
                applicant_id=request.applicant_id,
                # None when the question is about the applicant rather than
                # one case. `check_ownership` enforces case -> applicant
                # when a case is given, and the applicant-level read is
                # scoped by applicant_id at the tool.
                case_id=request.case_id,
                party_id=request.party_id,
                claims=claims,
                request_id=request_id,
                context=request.context,
                # The stage the case record established above -- so a case at
                # CPA is answered as a CPA case, never as a FOS one.
                stage_context=context,
                # One model call: when the Copilot phrases the answer, the
                # agent does not.
                compose_with_model=not _agent_config.compose_case_answers(),
            )
    except NotOwned:
        # A REFUSAL, NOT AN ERROR, AND NOT A DISCLOSURE. Phrased
        # identically whether the case belongs to somebody else or
        # does not exist: confirming which would itself be a leak.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"request_id": request_id,
                    "code": "CASE_ACCESS_DENIED",
                    "message": "You are not authorized to access this case."},
        ) from None
    except AgentError as exc:
        # ONE SHAPE FOR ONE OUTCOME. The agent refuses ownership before
        # retrieval ever runs, and retrieval refuses it again for a
        # caller that reached it another way. A client should not have
        # to recognise two different refusals for the same thing, so
        # the agent's access denial is published in the same shape.
        if exc.code in _ACCESS_DENIED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"request_id": request_id,
                        "code": "CASE_ACCESS_DENIED",
                        "message": "You are not authorized to access "
                                   "this case."},
            ) from None
        raise HTTPException(
            status_code=exc.http_status,
            detail={"request_id": request_id, "error": exc.code,
                    "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Copilot query failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"request_id": request_id, "error": "COPILOT_FAILED",
                    "message": "The request could not be completed."},
        ) from exc

    envelope["_request_started"] = started

    # A QUESTION THIS STAGE CANNOT ANSWER IS SAID SO, NOT ANSWERED.
    #
    # Six of the seven stages have no corpus and no capability. A
    # PROCESS_KNOWLEDGE question for one of them would otherwise be
    # answered out of the only corpus that exists -- the FOS handbook --
    # and a BOPS officer would receive authoritative-sounding FOS policy.
    # Reported as its own outcome: the caller may be perfectly
    # authorised, the case may be complete and the question perfectly
    # understood, so this is none of 403, missing data or CLARIFICATION.
    blocked = _unavailable_for(context, envelope)
    if blocked is not None:
        return blocked

    # A CASE QUESTION THE CURRENT STAGE CANNOT SERVE. Checked AFTER the
    # agent, so ownership has been established before anything about the
    # case -- even its stage -- is said. "Is it ready for CPA?" on a case
    # at CREDIT is answered with the stage and a plain "not available",
    # never with a FOS readiness answer.
    gated = _not_served_at_stage(context, envelope)

    # EVIDENCE, BESIDE THE STRUCTURED ANSWER -- never instead of it.
    # The agent has already answered from authoritative records; this
    # adds retrieved context and lets the model phrase the two
    # together. A retrieval failure costs the phrasing and nothing
    # else, because `structured` is what comes back.
    try:
        if gated is not None:
            envelope["answer"] = gated
            evidence, grounded = grounding.GroundedContext(), False
        else:
            evidence, grounded = await _grounded(request, envelope, context,
                                                 request_id)
    except NotOwned:
        # THE RETRIEVAL OWNERSHIP RE-CHECK, surfaced. It fires for a
        # caller the agent's own check let through -- or when the
        # store could not confirm ownership at all, which is not
        # permission. Same wording either way: confirming that a case
        # exists under another applicant is itself a disclosure.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"request_id": request_id,
                    "code": "CASE_ACCESS_DENIED",
                    "message": "You are not authorized to access this case."},
        ) from None
    except Exception as exc:
        # RETRIEVAL OR PHRASING FAILED IN A WAY IT DID NOT ANTICIPATE. The
        # structured answer the agent built from the records is already in
        # the envelope and is published as it is: a knowledge outage must
        # never cost a case question its answer.
        logger.warning("Copilot grounding failed request_id=%s (%s); "
                       "publishing the structured answer",
                       request_id, type(exc).__name__)
        evidence, grounded = grounding.GroundedContext(), False

    # A PROCESS QUESTION CARRIES NO STRUCTURED ANSWER, by design: its
    # text comes from the retrieved stage guide. When retrieval could
    # not run at all -- no vector store, no embedding provider -- the
    # answer is still empty here, and an empty answer is not an answer.
    # Said plainly rather than published as a blank.
    if not str(envelope.get("answer") or "").strip():
        envelope["answer"] = grounding.NO_EVIDENCE

    # THE CONVERSATION LAYER (language, tone, channel) -- AFTER every
    # business fact is settled, and only around it. See _converse.
    conversation = _converse(request, envelope, context, gated=gated)

    # THE LAST CHECK ON THIS SURFACE. The agent guarded its answer; this
    # surface may have replaced it since (a composed phrasing, a stage
    # gate), so what is about to be published is checked once more. A
    # composed answer that leaked was already discarded by its validator;
    # what this can still catch is internal text in a record or passage.
    from app.security import guardrails

    guard_started = time.perf_counter()
    with span("copilot.guardrail", phase="output") as guard_span:
        final, screened = guardrails.published(str(envelope.get("answer") or ""))
        from app.observability.tracing import annotate as _annotate

        _annotate(guard_span, allowed=screened.allowed,
                  category=None if screened.allowed else screened.category.value)
    timings["guardrail_ms"] = round((time.perf_counter() - guard_started) * 1000, 2)
    if not screened.allowed:
        envelope["answer"] = final
        envelope["guardrail"] = {"stage": "output", "action": "REDACTED",
                                 "category": screened.category.value}

    # PROVENANCE (Slice 10): the answer's chain, from what this request
    # already holds, checked against the case's own records.
    if gated is None and envelope.get("case_id") and \
            str(envelope.get("category") or "").upper() in _CASE_CATEGORIES and \
            "_evidence_packet" not in envelope:
        envelope["_evidence_packet"] = _evidence(envelope, context)
    provenance = _provenance(request, envelope, context, evidence, request_id)

    published = {key: envelope.get(key) for key in _PUBLIC}
    published.update(context.public())
    # GROUNDED MEANS AUTHORITATIVE EVIDENCE SUPPORTS THE ANSWER: either
    # retrieval was confident, or the case's own records answered it.
    # It used to mean the first only, so an answer built entirely from a
    # recorded KYC finding and decision -- and citing both -- published
    # `grounded: false`. Evaluated on the agent's own envelope, before
    # retrieval's sources are merged in below.
    # AN ANSWER THAT SAYS IT HAS NO EVIDENCE IS NOT GROUNDED, however much
    # was retrieved: the published sentence rests on nothing.
    published["grounded"] = (bool(grounded or supported_by_case_evidence(envelope))
                             and gated is None
                             and not _says_no_evidence(envelope.get("answer")))
    if gated is not None:
        published["status"] = "CAPABILITY_UNAVAILABLE"
    published["party_id"] = request.party_id
    published["conversation_id"] = request.conversation_id
    # THE NEXT TURN'S CONTEXT, built from this answer, and what a
    # follow-up was taken to mean when one was resolved.
    published["context"] = followup.context_from_response(envelope)
    published["followed_up"] = envelope.get("followed_up")
    # The case-memory sources the agent already cites, plus whatever
    # retrieval found. Both are pointers into records the caller can
    # already reach.
    published["sources"] = _relevant(
        _normalised(
            _deduplicated(
                list(envelope.get("sources") or []) + evidence.sources()),
            case_id=envelope.get("case_id") or request.case_id,
            stage=context.stage.value if context.stage else None,
            documents=_document_types(envelope),
        ),
        _understood(request).document_type,
    )
    # WHAT ACTUALLY RAN, as the agent recorded it. Read from a `trace`
    # key the agent never set, this was empty on every answer.
    published["tool_invoked"] = list(envelope.get("tools_invoked") or [])

    # THE FRONTEND-READY CASE VIEW, from the same evidence packet the
    # composer was given (built here when no composition ran). A gated
    # answer carries none of it: the stage cannot serve that question.
    from app.agents.applicant import config as agent_config
    from app.agents.applicant import evidence as evidence_builder

    packet = {}
    if gated is None and (envelope.get("case_id") or request.case_id) and \
            str(envelope.get("category") or "").upper() in _CASE_CATEGORIES:
        packet = envelope.get("_evidence_packet")
        if packet is None:
            packet = _evidence(envelope, context)
    published["stage_label"] = (agent_config.stage_label(context.stage.value)
                                if context.stage else None)
    from app.agents.applicant import evidence as evidence_chain
    from app.agents.applicant import ledger as case_ledger

    # THE EVIDENCE CHAIN, frontend-ready: problem, subject, source type,
    # the fields compared, when observed, and the Impact / Next Action
    # placeholders later slices fill. Never a record id or a value.
    published["problems"] = [
        case_ledger.public_problem(p)
        for p in (envelope.get("_evidence_items") or [])
        [:evidence_chain.MAX_PROBLEMS]] if packet else []
    # WHAT CHANGED, when that was asked: the structured changes the answer
    # was built from, without record ids.
    published["history"] = envelope.get("history")
    # WHAT IS NEXT (Slice 9), when that was asked or computed: the primary
    # action and the additional ones, each for its subject, and the handoff
    # signal. Codes are language-neutral; `text` is the English phrasing.
    published["next_actions"] = envelope.get("next_actions")
    # WHY A CASE IS HELD, when that was asked: codes only (Slice 10).
    published["delay"] = envelope.get("delay")
    published["handoff"] = (envelope.get("next_actions") or {}).get("handoff")
    # TONE ONLY (sentiment.py): computed from the user's message, after the
    # answer; it never alters a business fact.
    published["sentiment"] = conversation["sentiment"].public()
    published["language"] = conversation["language"]
    published["channel"] = conversation["channel"]
    _telemetry(request_id, envelope, published)
    published["subject"] = envelope.get("subject")
    published["pending_items"] = [str(i) for i in packet.get("pending") or []]
    published["next_action"] = packet.get("next_action")
    # The case's stage HISTORY is a case fact too; the stage the answer was
    # scoped to (`stage`) is published for every category, as before.
    category = str(published.get("category") or "").upper()
    published["timeline"] = (evidence_builder.history(context)
                             if category in _CASE_CATEGORIES else [])

    tools = list(envelope.get("tool_trace") or [])
    knowledge = [str(src.get("title") or src.get("type"))
                 for src in evidence.sources()
                 if str(src.get("type") or "").upper() in
                 {"PROCESS_KNOWLEDGE", "KNOWLEDGE", "POLICY", "STAGE_GUIDE"}]
    validation = envelope.get("_validation") or (
        "NOT_REQUIRED" if published.get("response_source") != "LLM" else "PASSED")
    published["answer_basis"] = {
        **evidence_builder.answer_basis(
            packet or {}, tools=tools, knowledge_sources=knowledge,
            semantic_sources=["JEV"] if (envelope.get("_jev_notes")) else [],
            validated=validation in {"PASSED", "NOT_REQUIRED"},
            response_source=str(published.get("response_source") or "")),
        "validation": validation,
        "routing": _routing_basis(request, envelope, tools, evidence,
                                  gated=gated is not None),
        # WHAT THE SECURITY BOUNDARY DID: PASSED, or which stage BLOCKED /
        # REDACTED and under which category. Never what it matched.
        "guardrail": envelope.get("guardrail") or {"action": "PASSED"},
        # WHY THIS ANSWER -- business labels and codes; never an id, a rule
        # reference or a tool. The internal chain stays internal.
        "provenance": _provenance_public(provenance),
        # WHETHER A MODEL WORDED THIS, AND WHY NOT when it did not: called,
        # skipped (reason), outcome (ACCEPTED / REJECTED / FALLBACK). No
        # prompt, no model output, no model name.
        # HOW THE ANSWER WAS PRESENTED -- a deterministic localized template
        # and/or an empathetic opening line. Never a change to a fact.
        "presentation": conversation["presentation"],
        "composition": {k: v for k, v in (envelope.get("_composition") or {
            "called": False, "skipped": "NOT_REACHED"}).items()
            if k in ("called", "skipped", "outcome", "language")},
    }
    timings.update(envelope.get("_timings") or {})
    if getattr(claims, "auth_ms", None) is not None:
        timings["auth_ms"] = claims.auth_ms
    composition = envelope.get("_composition") or {"called": False,
                                                   "skipped": "NOT_REACHED"}
    timings["qwen_ms"] = float(composition.get("qwen_ms") or 0.0) +         float(envelope.get("llm_ms") or 0.0)
    timings["qwen_calls"] = float(bool(composition.get("called"))) +         float(bool(envelope.get("llm_ms")))
    timings["fallback_count"] = float(composition.get("outcome") in
                                      ("REJECTED", "FALLBACK"))
    for key in ("composer_context_ms", "prompt_build_ms"):
        if composition.get(key) is not None:
            timings[key] = composition[key]
    timings["tools_ms"] = round(sum(float(t.get("duration_ms") or 0)
                                    for t in tools), 2)
    # MCP OVERHEAD, when the tools crossed the protocol: the calls' total
    # time and the part of it the transport cost (serialisation, the hop,
    # the session) as opposed to the tools themselves.
    carried = [t for t in tools
               if (t.get("transport") or "in_process") != "in_process"]
    timings["mcp_ms"] = round(sum(float(t.get("duration_ms") or 0)
                                  for t in carried), 2)
    if carried:
        timings["mcp_transport_ms"] = round(
            sum(float(t.get("transport_ms") or 0) for t in carried), 2)
    if envelope.get("llm_ms"):
        timings["agent_llm_ms"] = float(envelope["llm_ms"])
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    published["timings"] = timings
    published["processing_ms"] = timings["total_ms"]
    # HANDOFF (handoff.py): the Slice 9 signal plus explicit requests and
    # repeated unresolved turns; the counter travels in `context`.
    published["handoff"] = handoffs.evaluate(
        published, message=request.message,
        canonical=conversation["canonical"],
        sentiment_level=conversation["sentiment"].level,
        context=request.context)
    published["context"] = {
        **(published.get("context") or {}),
        # CONVERSATION STATE -- advisory, never authoritative: the next turn
        # still resolves the stage, subject and facts from the records.
        "language": conversation["language"]["response_language"],
        "last_stage": published.get("stage"),
        "unresolved_turns": published["handoff"].get("unresolved_turns", 0),
    }
    # AGGREGATE SIGNALS (no content) and the optional CloudWatch export.
    # Neither can fail the request.
    try:
        analytics.record(published)
        cloudwatch.publish(published)
    except Exception as exc:  # pragma: no cover - observability never fails a request
        logger.warning("Copilot analytics skipped (%s)", type(exc).__name__)
    published["correlation_id"] = request_id
    logger.info("copilot_timings request_id=%s stage=%s intent=%s %s",
                request_id, published.get("stage"), published.get("intent"),
                " ".join(f"{k}={v}" for k, v in timings.items()))
    # A STATUS QUESTION PUBLISHES THE STATUS IT ANSWERED FROM -- the value
    # on the application record the tool read, never one phrased or
    # inferred. Only for these intents: elsewhere `status` keeps its one
    # existing meaning, and `_unavailable_for` has already returned.
    if str(envelope.get("intent") or "").upper() in _STATUS_INTENTS:
        recorded = (envelope.get("application") or {}).get("status")
        if recorded:
            published["status"] = str(recorded)

    return CopilotQueryResponse(**published)


__all__ = ["router"]
