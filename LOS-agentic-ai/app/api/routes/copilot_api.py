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

import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.applicant import followup, intents, routing
from app.agents.applicant.validate import check_composed
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


def _understood(request: CopilotQueryRequest) -> intents.Classification:
    """The question as the agent understood it -- normalised, same rules."""
    return intents.understand(request.message,
                              has_case=bool(request.case_id))


#: Intents whose response carries the application's recorded status.
_STATUS_INTENTS = {"APPLICATION_STATUS", "APPLICATION_STAGE"}


#: Intents whose answer quotes recorded values and is never rephrased.
_QUOTED = {"CASE_HISTORY", "ELIGIBILITY", "INCOME_EVIDENCE",
           "DOCUMENT_DETAILS"}


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
    try:
        scope = (Scope(app_id=applicant_id, case_id=request.case_id,
                       stages=() if category == "PROCESS_KNOWLEDGE"
                       else stage_set)
                 if applicant_id else None)
    except UnscopedSearch:
        return grounding.GroundedContext(), False

    gathered = grounding.gather(
        request.message, category=category, scope=scope, stages=stage_set)

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
    if answered_by in _QUOTED:
        return gathered, gathered.grounded

    structured = str(envelope.get("answer") or "")
    facts = _facts(envelope, context)
    answer, grounded = await grounding.answer(
        request.message, structured=structured,
        facts=facts, context=gathered,
    )

    # CHECKED BEFORE IT IS PUBLISHED. A composed answer that leaks an id,
    # drops the recorded reason or a pending document, invents a hold or
    # a name, or runs long is replaced by the structured answer -- which
    # is then what `response_source` truthfully reports.
    if structured and answer.strip() != grounding._readable(structured).strip():
        accepted, checked = check_composed(
            answer, structured=grounding._readable(structured),
            identifiers=(request.case_id, request.applicant_id,
                         envelope.get("case_id"), envelope.get("applicant_id")),
            evidence=" ".join(item.text for item in gathered.case.evidence),
            stage=context.stage.value if context.stage else None,
        )
        if not accepted:
            logger.info("Composed answer rejected (%s); published the "
                        "structured answer", checked)
            answer = grounding._readable(structured)
        else:
            answer = checked
    envelope["answer"] = answer

    # WHO WROTE THE SENTENCE. A model-phrased answer was published as
    # STRUCTURED (a case answer) or KNOWLEDGE (a handbook or stage-guide
    # answer), which says no model touched it. `LLM` is defined as "a model
    # phrased it" -- the facts still came from the records or the retrieved
    # evidence -- so it is what every category reports when the published
    # sentence is the model's.
    model_wrote = (bool(answer.strip())
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
    from app.agents.applicant import jev

    if jev.active():
        notes = jev.annotate({
            "question": str(envelope.get("intent") or ""),
            "stage": facts.get("current_stage"),
            "established": str(envelope.get("answer") or ""),
        })
        if notes:
            facts["annotations_not_authoritative"] = notes
    return facts


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

    # WHICH DESK THIS QUESTION BELONGS TO, read from the case rather than
    # taken from the caller. `resolve` prefers the case's own timeline,
    # then its application status, and only falls back to what the caller
    # said when the record establishes nothing.
    context = stages.resolve(request.case_id, request.stage)

    try:
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
