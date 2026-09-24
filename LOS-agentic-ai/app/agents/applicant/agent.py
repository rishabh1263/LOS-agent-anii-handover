"""
The Applicant Agent.

THE ORDER MATTERS, and it is the whole design:

    classify  ->  route out of scope  ->  check capability  ->  check ownership
              ->  plan  ->  call tools  ->  deterministic rules
              ->  phrase (model, validated)  ->  respond  ->  audit

Authorisation happens BEFORE any tool runs, and the business answer is
computed BEFORE the model is consulted. A model that is off, slow or wrong
costs the phrasing of the answer and nothing else -- never its content, never
what was read, never whether it was allowed.

Write intents never execute on the first pass. They come back as a proposed
action the FOS has to confirm, and only the confirmation carries out the work.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from app.agents.applicant import (
    audit,
    case_memory_facts,
    config,
    counters,
    facts,
    document_facts,
    eligibility_facts,
    grounding,
    income_facts,
    knowledge_answer,
    permissions,
    routing,
)
from app.agents.applicant.answer import (
    NOTHING_AVAILABLE,
    deterministic_answer,
    generate_answer,
)
from app.agents.applicant import followup
from app.agents.applicant.query_types import QueryType, clarification_for, type_for
from app.agents.applicant.intents import (
    Classification,
    Intent,
    SIMPLE_INTENTS,
    WRITE_INTENTS,
    classify,
    plan_for,
)
from app.agents.applicant.permissions import Caller, PermissionDenied

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """A failure that should reach the caller as a clean structured error."""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _executed(step: dict[str, Any],
              caller: "permissions.Caller | None") -> bool:
    """
    Whether a traced tool actually ran.

    A tool the caller's scope did not cover was refused BEFORE running and
    is traced as a failure. It is told apart by asking the same check that
    refused it, so the trace itself keeps its shape.
    """
    if step.get("ok") or caller is None:
        return True
    try:
        permissions.check_tool(caller, step["tool"])
    except permissions.PermissionDenied:
        return False
    return True


async def _call_tools(
    plan: tuple[str, ...],
    *,
    applicant_id: str | None,
    case_id: str | None,
    document_type: str | None,
    # WHO IS ASKING, so each capability can be checked against the scope
    # its own contract declares. Optional so an internal caller that has
    # already established authorisation another way is unaffected; every
    # request path supplies it.
    caller: "permissions.Caller | None" = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """
    Run the planned tools.

    Returns (results, trace, errors). A tool that fails does not take the
    request down: its failure is recorded and the answer is built from what
    did come back, because a partial answer with a named gap is more useful to
    a FOS than a 500.

    A TOOL THE CALLER MAY NOT USE IS REFUSED, NOT RUN, and refused the
    same way a failure is handled: recorded and skipped. The request has
    already passed the capability check for the KIND of work it is; a
    plan that reaches one capability beyond the caller's scopes should
    cost that capability, not the whole answer.
    """
    from app.mcp import applicant as tools

    results: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for capability in plan:
        handler = tools.ALL_TOOLS.get(capability)
        if handler is None:
            errors.append({"code": "UNKNOWN_TOOL",
                           "message": f"No such capability: {capability}."})
            continue

        if caller is not None:
            try:
                permissions.check_tool(caller, capability)
            except permissions.PermissionDenied as denied:
                errors.append({"code": denied.code, "message": denied.message})
                trace.append({"tool": capability, "ok": False,
                              "processing_ms": 0.0})
                continue

        if capability == "applicant.get":
            envelope = await handler(applicant_id)
        elif capability == "applications.list":
            envelope = await handler(applicant_id)
        elif capability == "documents.verification":
            envelope = await handler(case_id, document_type or "")
        else:
            envelope = await handler(case_id)

        trace.append({
            "tool": capability,
            "ok": envelope.ok,
            "processing_ms": envelope.processing_ms,
        })

        if envelope.ok and envelope.result is not None:
            results[capability] = envelope.result
        elif envelope.error is not None:
            errors.append({"code": envelope.error.code,
                           "message": envelope.error.message})

    return results, trace, errors


def _proposed_action(
    classification,
    applicant_id: str | None,
    case_id: str | None,
) -> dict[str, Any]:
    """
    A write, described but not performed.

    The FOS sees exactly what would change and confirms it by action_id. The
    model is not involved in either half: it does not decide that a write is
    wanted, and it cannot carry one out.
    """
    intent = classification.intent
    fields = dict(classification.fields or {})

    summaries = {
        Intent.CREATE_APPLICANT: "Create a new applicant record.",
        Intent.CREATE_APPLICATION: "Create a new application for this applicant.",
        Intent.UPDATE_APPLICANT: (
            "Update " + ", ".join(f"{k} to {v}" for k, v in fields.items())
            if fields else "Update the applicant's details."
        ),
        Intent.MARK_FOR_REUPLOAD: (
            f"Mark {(classification.document_type or 'the document').replace('_', ' ').title()} "
            "for re-upload."
        ),
    }

    tool = {
        Intent.CREATE_APPLICANT: "applicant.create",
        Intent.CREATE_APPLICATION: "application.create",
        Intent.UPDATE_APPLICANT: "applicant.update",
        Intent.MARK_FOR_REUPLOAD: "documents.mark_for_reupload",
    }[intent]

    arguments: dict[str, Any] = dict(fields)
    if intent in (Intent.UPDATE_APPLICANT,):
        arguments["applicant_id"] = applicant_id
    if intent is Intent.CREATE_APPLICATION:
        arguments["applicant_id"] = applicant_id
    if intent is Intent.MARK_FOR_REUPLOAD:
        arguments["case_id"] = case_id
        arguments["document_type"] = classification.document_type

    return {
        "action_id": f"act_{uuid.uuid4().hex[:12]}",
        "type": intent.value,
        "tool": tool,
        "arguments": arguments,
        "requires_confirmation": True,
        "summary": summaries.get(intent, "Perform this change."),
    }


def _case_qualifier(case_id: str | None, party_id: str | None) -> str:
    """
    What the CASE concluded, when that differs from the documents.

    Empty when nothing was recorded, and empty when the recorded
    decision is not a review -- a case that is progressing needs no
    caveat, and adding one to every answer would train a reader to
    skip the sentence that matters.
    """
    if not case_id:
        return ""

    try:
        memory = case_memory_facts.case_memory(case_id, party_id)
        decisions = memory.get("decisions") or []
        if not decisions:
            return ""

        latest = decisions[-1]
        decision = str(latest.get("decision") or "").upper()
        if decision not in {"REVIEW", "REJECT"}:
            return ""

        # THE REASON, NOT THE CASE-HISTORY SENTENCE.
        #
        # This used to borrow `explain`, whose first sentence names the
        # processing status as well -- "recorded as PARTIAL with a
        # REVIEW decision" -- and a reader given both took PARTIAL for
        # the decision. PARTIAL is how far processing got; REVIEW is
        # what was decided. Here only the decision and its reason are
        # wanted, because the sentence in front of this one has already
        # said what the documents did.
        clause, _ = case_memory_facts.review_reason(memory)
        if not clause:
            return ""

        verb = ("was declined" if decision == "REJECT"
                else "is under review")
        return f"However, your application {verb} because {clause}."
    except Exception:
        return ""


def _portfolio_answer(results: dict[str, Any]) -> str:
    """
    The applicant's applications, counted and listed separately.

    NEVER MERGED. Two cases are two answers; a sentence that blended
    them would report a status no single case holds -- and a reviewer
    acting on "the application is in review" would not know which one.
    The count leads, then each case is named with its own status.

    Deterministic: the rows come from `applications.list`, which is
    scoped to one applicant_id at the tool, and nothing here is
    inferred or summarised by a model.
    """
    payload = (results.get("applications.list") or {})
    applications = payload.get("applications") or []
    total = int(payload.get("count") or len(applications))

    if not total:
        return "No applications are on file for this applicant."

    lines = []
    for record in applications:
        case_id = str(record.get("case_id") or "unknown")
        status = str(record.get("status") or "UNKNOWN")
        lines.append(f"{case_id} ({status})")

    noun = "case" if total == 1 else "cases"
    return f"Across {total} {noun}: " + "; ".join(lines) + "."


async def answer_question(
    *,
    message: str,
    applicant_id: str | None,
    case_id: str | None,
    claims: dict[str, Any],
    request_id: str | None = None,
    # Which party on the case the question is about. Optional: without
    # it a case question answers across the whole case, which is what
    # every existing caller already gets.
    party_id: str | None = None,
    concise: bool = True,
    context: dict[str, Any] | None = None,
    # WHEN THE CALLER ALREADY KNOWS WHAT WAS ASKED.
    #
    # A dropdown action is not a question: the client picked
    # "Document checklist" from a list this service published, so the
    # intent is already decided and classifying an English sentence back
    # into it can only lose. It did, repeatedly -- every routing defect
    # found in review was a pattern-ordering bug in a regex ladder that
    # a named action never needed to enter.
    #
    # EVERYTHING ELSE IS UNCHANGED. Capability check, ownership check,
    # the tool plan, the deterministic answer, the audit record and the
    # response shape are the ones a typed question gets. This skips the
    # inference step and nothing else.
    intent_override: Intent | None = None,
) -> dict[str, Any]:
    """
    One FOS question, answered.

    Never raises for an ordinary refusal -- an unauthorised, unknown or
    out-of-scope request is a structured response, not an exception. AgentError
    is reserved for conditions the route turns into an HTTP status.
    """
    started = time.perf_counter()
    request_id = request_id or f"aa_{uuid.uuid4().hex}"
    caller = Caller.from_claims(claims)

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    def envelope(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "request_id": request_id,
            "applicant_id": applicant_id,
            "case_id": case_id,
            "intent": Intent.UNKNOWN.value,
            "answer": "",
            "applicant": None,
            "application": None,
            "stage": None,
            "documents": [],
            "checklist": [],
            # Which policy produced the checklist, and which rules fired.
            # Always alongside it, never instead of it.
            "policy": None,
            "pending_items": [],
            "next_action": None,
            "readiness": None,
            "actions": [],
            # Where a knowledge-grounded answer came from. Absent on a pure
            # case answer, because no knowledge was consulted and saying
            # otherwise would imply a source the facts did not have.
            "knowledge": None,
            # Recorded findings, for a case-history question only.
            "case_memory": None,
            "sources": [],
            # Which of the four kinds of question this was, so a caller can
            # tell "the store answered it" from "the handbook did" without
            # inferring it from which fields happen to be populated.
            "category": routing.QueryCategory.CASE_ONLY.value,
            # WHAT WAS ASKED FOR, as opposed to what was consulted. See
            # app/agents/applicant/query_types.py for why both exist.
            "query_type": QueryType.CASE_FACT.value,
            # -- the frontend contract. Derived, never phrased by a model.
            "case_state": None,
            "suggested_questions": [],
            "available_actions": [],
            "document_highlights": [],
            # Present ONLY when the service declined to guess. A null here
            # is a claim that the request was understood.
            "clarification_required": None,
            # What a bare follow-up was taken to mean, when one was
            # resolved. Reported so a misunderstanding is visible rather
            # than leaving an officer wondering why the answer does not
            # match the question.
            "followed_up": None,
            # The block the caller echoes back on the next question. This
            # service holds no conversation state.
            "context": None,
            "base_intent": None,
            "route_to": None,
            "response_source": routing.ResponseSource.STRUCTURED.value,
            "processing_ms": 0.0,
            "errors": [],
        }
        base.update(overrides)
        base["processing_ms"] = elapsed()
        return base

    if not config.enabled():
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent="DISABLED", tools=[], status="DISABLED")
        return envelope(
            intent="DISABLED",
            answer="The Applicant Agent is switched off in this deployment.",
            errors=[{"code": "AGENT_DISABLED",
                     "message": "The Applicant Agent is disabled."}],
        )

    # A BARE FOLLOW-UP BECOMES A WHOLE QUESTION FIRST.
    #
    # The rewrite produces a MESSAGE, which is then classified by exactly
    # the same patterns as anything typed by a person. It selects no
    # intent, reaches no tool and skips no check -- see
    # app/agents/applicant/followup.py for why that boundary is where it
    # is, given the context comes from the caller.
    resolution = followup.resolve(
        message, followup.Context.from_payload(context))
    message = resolution.message

    if intent_override is not None:
        # No follow-up resolution either: a named action carries no
        # pronoun to resolve and no previous turn to resolve it against.
        classification = Classification(
            intent_override, confidence="high", matched_on="action")
    else:
        classification = classify(message)

    intent = classification.intent

    # ---- out of scope, before anything is read -------------------------
    if intent is Intent.OUT_OF_SCOPE:
        route = config.routing_table().get(classification.route_to or "", {})
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="ROUTED",
                     message=message, detail=classification.route_to)
        return envelope(
            intent=intent.value,
            category=routing.QueryCategory.DOWNSTREAM.value,
            query_type=QueryType.DOWNSTREAM.value,
            followed_up=resolution.public(),
            response_source=routing.ResponseSource.ROUTED.value,
            route_to=route.get("route_to", classification.route_to),
            answer=route.get(
                "message",
                "That question is handled by a downstream process.",
            ),
        )

    # ---- a question about the rules, not about this case ---------------
    #
    # Answered from the FOS knowledge base. No tool runs and no record is
    # read: this path cannot reach case data, which is what stops a policy
    # answer from ever appearing to be a statement about an applicant.
    if intent is Intent.FOS_KNOWLEDGE:
        text, source, detail = await _knowledge_reply(message)
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=["knowledge.fos"],
                     status="OK" if detail["confident"] else "NO_KNOWLEDGE",
                     message=message)
        return envelope(intent=intent.value, answer=text,
                        category=routing.QueryCategory.KNOWLEDGE_ONLY.value,
                        query_type=QueryType.PROCESS_KNOWLEDGE.value,
                        followed_up=resolution.public(),
                        response_source=source,
                        knowledge=_public_knowledge(detail))

    if intent is Intent.UNKNOWN:
        # Not understood as a case question -- but the knowledge base gets a
        # chance before the agent gives up, and its own confidence threshold
        # decides. A phrase list can always be out of date; retrieval scoring
        # degrades gracefully instead of failing at an exact wording.
        #
        # EXCEPT FOR AN UNRESOLVED FOLLOW-UP. "why?" is not a question about
        # the FOS handbook, but retrieval scored it confident and answered
        # it with a paragraph about document states. A message that only
        # means something in context, and whose context did not resolve,
        # goes straight to the clarification.
        bare = followup.is_bare(message)
        text, source, detail = (
            ("", "", {"confident": False}) if bare
            else await _knowledge_reply(message)
        )
        if detail["confident"]:
            audit.record(request_id=request_id, subject=caller.subject,
                         applicant_id=applicant_id, case_id=case_id,
                         intent=Intent.FOS_KNOWLEDGE.value,
                         tools=["knowledge.fos"], status="OK", message=message)
            return envelope(intent=Intent.FOS_KNOWLEDGE.value, answer=text,
                            category=routing.QueryCategory.KNOWLEDGE_ONLY.value,
                            query_type=QueryType.PROCESS_KNOWLEDGE.value,
                            followed_up=resolution.public(),
                            response_source=source,
                            knowledge=_public_knowledge(detail))

        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="UNSUPPORTED",
                     message=message)
        # A QUESTION BACK, NOT A DEAD END. The request was not understood
        # and the service will not guess -- but "could you rephrase?" puts
        # the work back on the officer with no help. The clarification
        # carries what this desk can answer, so the next click succeeds.
        #
        # The error stays. A caller distinguishing "answered" from "not
        # answered" reads `errors`, and dropping it to make the response
        # look friendlier would make an unanswered question look answered.
        clarification = clarification_for(message, has_case=bool(case_id))
        return envelope(
            intent=intent.value,
            category=routing.QueryCategory.UNSUPPORTED.value,
            query_type=QueryType.CLARIFICATION.value,
            clarification_required=clarification,
            followed_up=resolution.public(),
            suggested_questions=list(clarification["options"]),
            answer=clarification["question"],
            errors=[{"code": "UNSUPPORTED_REQUEST",
                     "message": "The request was not understood."}],
        )

    # ---- authorisation, before any tool runs ---------------------------
    try:
        permissions.check_capability(caller, intent)
        if intent not in WRITE_INTENTS or intent is not Intent.CREATE_APPLICANT:
            permissions.check_ownership(applicant_id or "", case_id)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="DENIED",
                     message=message, detail=exc.code)
        raise AgentError(exc.code, exc.message, http_status=403) from exc

    # ---- how a named stage works ---------------------------------------
    #
    # RETURNS BEFORE THE READ PATH, and must. No MCP tool answers "what
    # does RCU check", so `plan_for` returns nothing for it -- and the
    # read path treats an empty plan as an empty RESULT, replying "No
    # data is available for this request." with the default CASE_ONLY
    # category. A process question is not a case question that failed.
    #
    # AFTER THE OWNERSHIP CHECK, THOUGH IT READS NO CASE. A stage
    # guide describes a desk and nothing on this path can reach an
    # applicant record -- but the request still names a case, and a
    # caller who does not hold that case is refused before anything
    # answers them. Ownership is a property of the request, not a
    # question about which branch happens to need the data.
    #
    # THE ANSWER IS LEFT EMPTY ON PURPOSE. The text comes from the
    # indexed stage guide, retrieved and phrased by the caller that has
    # a vector store -- this module has no retrieval of its own, and a
    # deterministic sentence invented here would be a second answer
    # competing with the real one.
    if intent is Intent.STAGE_PROCESS:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], status="OK",
                     message=message)
        return envelope(
            intent=intent.value,
            answer="",
            category=routing.QueryCategory.PROCESS_KNOWLEDGE.value,
            query_type=QueryType.PROCESS_KNOWLEDGE.value,
            followed_up=resolution.public(),
            response_source=routing.ResponseSource.STRUCTURED.value,
        )

    # ---- writes are proposed, never performed here ---------------------
    if intent in WRITE_INTENTS:
        action = _proposed_action(classification, applicant_id, case_id)
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[], write=True,
                     confirmed=False, status="PROPOSED", message=message)
        return envelope(
            intent=intent.value,
            query_type=QueryType.ACTION_REQUEST.value,
            followed_up=resolution.public(),
            answer=f"{action['summary'].rstrip('.')}. Please confirm and I will apply it.",
            actions=[action],
        )

    # ---- read path -----------------------------------------------------
    plan = plan_for(classification, has_case=bool(case_id))
    results, trace, errors = await _call_tools(
        plan,
        applicant_id=applicant_id,
        case_id=case_id,
        document_type=classification.document_type,
        # Each capability is checked against the scope its own contract
        # declares, in addition to the capability check this request has
        # already passed.
        caller=caller,
    )

    if not results:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=applicant_id, case_id=case_id,
                     intent=intent.value, tools=[t["tool"] for t in trace],
                     status="NO_DATA", message=message)
        return envelope(
            intent=intent.value,
            answer=(errors[0]["message"] if errors
                    else "No data is available for this request."),
            errors=errors or [{"code": "NO_DATA",
                               "message": "No data was returned."}],
        )

    # ---- phrasing ------------------------------------------------------
    #
    # A MIXED question is answered from the case FIRST and always
    # deterministically. The facts half of "why is this not ready and what
    # should I collect?" is the same computation as the question asked alone,
    # and it must not become a model's paraphrase because a second clause was
    # attached to it.
    answering_intent = (classification.base_intent or Intent.FULL_SUMMARY
                        if intent is Intent.MIXED else intent)

    # CASE HISTORY IS ANSWERED FROM WHAT WAS RECORDED, AND ONLY THAT.
    #
    # Reached here and not earlier because ownership has now been checked
    # -- case memory must never be read for a case the caller has not
    # been cleared for, and the check above is the one that clears it.
    #
    # No model on this path at any setting. The answer is a list of
    # reason codes the pipeline wrote down; a paraphrase can drop one,
    # and a dropped finding reads as a finding that did not happen.
    case_memory_block = None
    case_sources: list[dict[str, Any]] = []

    # WHAT THE CASE ITSELF RECORDED, read once before anything phrases
    # an answer, because its presence decides who does the phrasing.
    recorded = (_case_qualifier(case_id, party_id)
                if intent in (Intent.DOCUMENT_VERIFICATION,
                              Intent.APPLICATION_STATUS)
                else "")

    if intent is Intent.CASE_PORTFOLIO:
        answer = _portfolio_answer(results)
        source, llm_ms = "deterministic", 0.0

    elif intent is Intent.ELIGIBILITY:
        # READ, NOT COMPUTED, and no model on this path at any setting.
        # A FOIR is the one figure in this system most likely to be
        # repeated back confidently and wrongly: it is a percentage, it
        # sounds like arithmetic anyone could redo, and redoing it from
        # a chat turn's context would produce a second answer for one
        # applicant.
        memory = case_memory_facts.case_memory(case_id or "", party_id)
        answer, case_sources = eligibility_facts.answer(memory)
        case_memory_block = memory
        source, llm_ms = "deterministic", 0.0

    elif intent is Intent.DOCUMENT_DETAILS:
        # QUOTED FROM THE RECORD, for the applicant who was asked about.
        # Scoped to that party -- in a two-party case "my PAN" is the
        # applicant's own, never the co-applicant's -- and to the case,
        # whose ownership the check above has already cleared.
        answer, case_sources = document_facts.answer(
            case_id or "", party_id or applicant_id, message)
        source, llm_ms = "deterministic", 0.0

    elif intent is Intent.INCOME_EVIDENCE:
        # THE SAME PLACE THE REST OF THE CASE'S CONCLUSIONS COME FROM,
        # and no model on this path at any setting. Income is the one
        # subject where a plausible sentence is most tempting and least
        # acceptable: "your salary is 50,000" reads perfectly and is a
        # figure nobody recorded.
        memory = case_memory_facts.case_memory(case_id or "", party_id)
        answer, case_sources = income_facts.answer(memory)
        case_memory_block = memory
        source, llm_ms = "deterministic", 0.0

    elif intent is Intent.CASE_HISTORY:
        memory = case_memory_facts.case_memory(case_id or "", party_id)
        answer, case_sources = case_memory_facts.explain(memory)
        case_memory_block = memory
        source, llm_ms = "deterministic", 0.0

    elif intent is Intent.MIXED:
        # THE CASE HALF IS ANSWERED THE WAY THAT HALF IS ANSWERED.
        #
        # A mixed question whose case half is a history question --
        # "why is this case not ready AND what does the bank statement
        # show" -- was sent to `deterministic_answer`, which has no
        # case-history branch, so it returned "No answer is available
        # for this request." and the handbook paragraph was appended
        # underneath it. The explanation was in case memory all along,
        # which is where the same question asked alone reads it from.
        if answering_intent is Intent.CASE_HISTORY:
            memory = case_memory_facts.case_memory(case_id or "", party_id)
            answer, case_sources = case_memory_facts.explain(memory)
            case_memory_block = memory
        else:
            answer = deterministic_answer(answering_intent, results)
        source, llm_ms = "deterministic", 0.0
    else:
        use_llm = config.llm_enabled() and (
            answering_intent not in SIMPLE_INTENTS
            or config.llm_for_simple_intents()
        )

        # AND WHERE THERE IS ONE, NOTHING PARAPHRASES IT.
        #
        # Asked for the status of a case recorded as REVIEW with
        # NAME_MISMATCH, the model wrote "there wasn't enough matching
        # information to make a decision" -- a generalisation of a
        # finding that names two people. The reason is a recorded
        # fact, and a fact is reported rather than rewritten. The
        # model keeps every answer where the case recorded nothing
        # specific, which is most of them.
        if recorded:
            use_llm = False

        if use_llm:
            answer, source, llm_ms = await generate_answer(
                message, answering_intent, results,
            )
        else:
            answer = deterministic_answer(answering_intent, results)
            source, llm_ms = "deterministic", 0.0

    # A DOCUMENT VERDICT IS NOT THE CASE'S VERDICT.
    #
    # "No documents currently have verification issues" is true of the
    # documents and misleading about the application: this case had
    # both documents passing and a recorded REVIEW, because the name
    # on the PAN and the name on the bank account belong to different
    # people. A reader told only the first half walks away believing
    # the case is clear.
    #
    # THE QUALIFIER IS RECORDED, NOT REASONED. It is the decision and
    # the reason codes the pipeline wrote down, phrased by the same
    # function the case-history answer uses. Nothing new is concluded
    # here.
    if intent in (Intent.DOCUMENT_VERIFICATION, Intent.APPLICATION_STATUS):
        if recorded:
            answer = answer.rstrip() + " " + recorded

    knowledge_block = None
    category = routing.category_for(intent)

    # SOURCE IS DECIDED BY WHAT ACTUALLY CONTRIBUTED, not by the branch.
    #
    # A mixed question whose retrieval came back unconfident produced only a
    # structured answer, and reporting MIXED for it would claim the handbook
    # had a say when it did not.
    if source == "llm":
        response_source = routing.ResponseSource.LLM.value
    else:
        response_source = routing.ResponseSource.STRUCTURED.value

    if intent is Intent.MIXED:
        # The knowledge half, appended -- never substituted. If retrieval is
        # not confident the case answer still stands on its own; a question
        # the handbook cannot help with is not a question the case facts
        # failed to answer.
        # NOT PHRASED. The answer already has a computed half; phrasing the
        # other one costs the model budget and adds a hallucination surface
        # for a sentence nobody reads differently. Trimmed too: the handbook
        # section behind it is a numbered list, and a chat reply is not the
        # place for it.
        text, knowledge_source, detail = await _knowledge_reply(
            message, allow_model=False, max_sentences=2,
        )
        knowledge_block = _public_knowledge(detail)
        if detail["confident"]:
            # NEVER A REFUSAL IN FRONT OF AN ANSWER. Where the case
            # half genuinely has nothing to say, prefixing the
            # knowledge half with "No answer is available" tells the
            # reader the service failed at the moment it succeeded.
            settled = answer.rstrip()
            if settled == NOTHING_AVAILABLE:
                settled = ""
            joined = "\n\n".join(p for p in (settled, text) if p)
            answer = joined
            response_source = routing.ResponseSource.MIXED.value
        else:
            # Only the store contributed, so say so.
            category = routing.QueryCategory.CASE_ONLY

    payload = _shape(results)

    # WHAT IS STILL BEING WORKED ON. A document queued for background
    # reading is neither finished nor forgotten, and an officer asking
    # about the case has no way to tell those apart unless the answer
    # says so.
    if case_id:
        try:
            from app.store import get_repository, ocr_queue

            queued = ocr_queue.jobs_for_case(get_repository(), case_id)
            if queued:
                payload["processing_queue"] = queued
        except Exception:
            pass
    response = envelope(
        intent=intent.value,
        # The kind of request, resolved from the intent the classifier
        # settled on -- including MIXED, whose case half decides nothing
        # here: a mixed question is its own kind because a UI renders the
        # two halves differently.
        query_type=type_for(intent).value,
        followed_up=resolution.public(),
        # The case intent under a MIXED answer. The public boundary prunes
        # with it, because a mixed answer is about whatever its case half was
        # about.
        base_intent=(classification.base_intent.value
                     if classification.base_intent else None),
        answer=answer,
        category=category.value,
        response_source=response_source,
        knowledge=knowledge_block,
        # What the case actually recorded, and the findings the answer
        # rests on. Both absent unless case history was asked for, so no
        # existing response grows a key.
        **({"case_memory": case_memory_block} if case_memory_block else {}),
        **({"sources": case_sources} if case_sources else {}),
        # THE TOOLS THAT ACTUALLY RAN, in order. A refused tool never ran
        # and is absent. Case-memory reads are not tools and are not
        # listed as if they were.
        tools_invoked=[step["tool"] for step in trace
                       if _executed(step, caller)],
        errors=errors,
        **payload,
    )

    # CARRY ONLY WHAT THIS ANSWER IS ABOUT.
    #
    # Every action used to return the whole case -- the applicant record,
    # every document, the full checklist -- for a question like "what is the
    # next action?". That is a payload a frontend has to ignore, and it puts
    # extracted applicant details into a chat reply with no reason to hold
    # them. The shape is unchanged; the fields this answer is not about come
    # back empty.
    # NOT PRUNED HERE.
    #
    # Conciseness is a property of the PUBLIC contract, so it is applied at
    # the FOS boundary in app/api/routes/fos_api.py and nowhere else. This
    # envelope stays complete for its internal consumers -- the deprecated
    # applicant-agent routes and the audit record among them.
    #
    # Pruning in both places also pruned twice, and the second pass could
    # only remove what the first had already left.

    logger.info(
        "applicant_agent request_id=%s subject=%s applicant=%s case=%s "
        "intent=%s tools=%s source=%s llm_ms=%.1f total_ms=%.1f",
        request_id, caller.subject, applicant_id, case_id, intent.value,
        ",".join(t["tool"] for t in trace) or "-", source, llm_ms,
        response["processing_ms"],
    )
    audit.record(request_id=request_id, subject=caller.subject,
                 applicant_id=applicant_id, case_id=case_id,
                 intent=intent.value, tools=[t["tool"] for t in trace],
                 status="OK", message=message)

    return response


async def _knowledge_reply(
    message: str,
    *,
    product: str | None = None,
    allow_model: bool = True,
    max_sentences: int | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """
    A knowledge answer. Configuration wins outright where it has one.

    THE ORDER IS THE FIX. A configuration question is answered from the same
    `accepts` list the upload endpoint enforces, the model is not called, and
    the retrieved passage is NOT appended.

    Appending it is what shipped the live failure. The exact answer was
    produced correctly and then followed by a handbook paragraph saying
    utility bills and rent agreements are common in the industry -- true, and
    read by an officer as a second list of things they could bring. The
    passage explains a slot; the question asked for document names; putting
    both in one answer let the wrong half be the memorable one.

    Retrieval still answers everything else: how verification works, what a
    status means, why a document failed. Those have no configured value to
    read and prose is the right form for them.
    """
    facts_for_product = facts.fact_set(facts.product_in(message) or product)

    try:
        exact = facts.authoritative_answer(message, product)
    except Exception:
        logger.exception("authoritative fact lookup failed")
        exact = None

    if exact is not None:
        # No model, no retrieval text. The answer is a configured value and
        # there is nothing a model could add to it that is not a risk.
        counters.record(called=False)
        detail = {
            "stage": knowledge_answer.STAGE,
            "retrieved": 0,
            "top_score": 1.0,
            "threshold": None,
            "confident": True,
            "authoritative": True,
            "citations": [f"configuration:{exact.kind}"],
        }
        return exact.text, routing.ResponseSource.KNOWLEDGE.value, detail

    text, source, detail = await knowledge_answer.answer(
        message, allow_model=allow_model, max_sentences=max_sentences,
    )

    # A MODEL-WRITTEN ANSWER IS CHECKED BEFORE IT IS RETURNED.
    #
    # Retrieval being confident says the passages were relevant. It says
    # nothing about whether the sentence built from them kept their meaning,
    # and the live failure was exactly a reversed negation inside a relevant
    # passage.
    if source == "llm":
        verdict = grounding.validate(text, facts_for_product)
        if not verdict:
            logger.warning(
                "FOS knowledge answer rejected by grounding (%s); using the "
                "retrieved text.", grounding.describe(verdict),
            )
            detail = dict(detail)
            detail["grounding_rejected"] = True
            return (knowledge_answer.retrieved_text_for(message),
                    routing.ResponseSource.KNOWLEDGE.value, detail)

    return text, _knowledge_source(source, detail), detail


def _knowledge_source(source: str, detail: dict[str, Any]) -> str:
    if source == "llm":
        return routing.ResponseSource.LLM.value
    if detail.get("confident"):
        return routing.ResponseSource.KNOWLEDGE.value
    return routing.ResponseSource.STRUCTURED.value


def _public_knowledge(detail: dict[str, Any]) -> dict[str, Any]:
    """
    What a caller is told about the retrieval behind a knowledge answer.

    Citations and a score, so an officer can see which part of the FOS
    handbook an answer came from and a reviewer can tell a refusal caused by
    a thin corpus from one caused by an off-topic question.

    NOT the retrieved text, the chunk ids or the prompt. Those are internals,
    and the answer already carries what they said.
    """
    return {
        "stage": detail.get("stage"),
        "grounded": bool(detail.get("confident")),
        "sources": list(detail.get("citations") or []),
        "top_score": detail.get("top_score", 0.0),
    }


def _shape(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Lift tool results into the published response fields."""
    out: dict[str, Any] = {}

    view = results.get("applicant.360")
    if view:
        out.update({
            "applicant": view.get("applicant"),
            "application": view.get("application"),
            "stage": view.get("stage"),
            "documents": view.get("documents") or [],
            "checklist": view.get("checklist") or [],
            "policy": view.get("policy"),
            "pending_items": view.get("pending_items") or [],
            "next_action": view.get("next_action"),
            "readiness": view.get("readiness"),
        })
        return out

    if "applicant.get" in results:
        out["applicant"] = results["applicant.get"].get("applicant")
    if "application.get" in results:
        out["application"] = results["application.get"].get("application")
        out["stage"] = (out["application"] or {}).get("status")
    if "documents.get" in results:
        out["documents"] = results["documents.get"].get("documents") or []
    if "documents.checklist" in results:
        out["checklist"] = results["documents.checklist"].get("checklist") or []
        out["policy"] = results["documents.checklist"].get("policy")
    if "documents.verification" in results:
        payload = results["documents.verification"]
        if payload.get("found"):
            out["documents"] = [payload]
    if "workflow.pending_items" in results:
        out["pending_items"] = results["workflow.pending_items"].get("pending_items") or []
    if "workflow.next_action" in results:
        out["next_action"] = results["workflow.next_action"].get("next_action")
    if "workflow.readiness" in results:
        out["readiness"] = results["workflow.readiness"].get("readiness")

    return out


async def confirm_action(
    *,
    action: dict[str, Any],
    claims: dict[str, Any],
    request_id: str | None = None,
) -> dict[str, Any]:
    """
    Carry out a write the FOS has confirmed.

    The scope is checked again here rather than trusted from the proposal:
    the two calls are separate requests and may carry different tokens.
    """
    from app.mcp import applicant as tools

    started = time.perf_counter()
    request_id = request_id or f"aa_{uuid.uuid4().hex}"
    caller = Caller.from_claims(claims)

    try:
        intent = Intent(str(action.get("type") or ""))
    except ValueError as exc:
        raise AgentError("INVALID_ACTION", "Unknown action type.", 400) from exc

    if intent not in WRITE_INTENTS:
        raise AgentError("INVALID_ACTION", "That action is not a write.", 400)

    try:
        permissions.check_capability(caller, intent)
    except PermissionDenied as exc:
        audit.record(request_id=request_id, subject=caller.subject,
                     applicant_id=str(action.get("arguments", {}).get("applicant_id")),
                     case_id=str(action.get("arguments", {}).get("case_id")),
                     intent=intent.value, tools=[], write=True, confirmed=True,
                     status="DENIED", detail=exc.code)
        raise AgentError(exc.code, exc.message, http_status=403) from exc

    capability = str(action.get("tool") or "")
    handler = tools.WRITE_TOOLS.get(capability)
    if handler is None:
        raise AgentError("INVALID_ACTION", f"Unknown capability: {capability}", 400)

    arguments = {k: v for k, v in (action.get("arguments") or {}).items()
                 if v is not None}
    envelope = await handler(**arguments)

    audit.record(
        request_id=request_id, subject=caller.subject,
        applicant_id=str(arguments.get("applicant_id") or ""),
        case_id=str(arguments.get("case_id") or ""),
        intent=intent.value, tools=[capability], write=True, confirmed=True,
        status="OK" if envelope.ok else "FAILED",
        detail=None if envelope.ok else (envelope.error.code if envelope.error else None),
    )

    if not envelope.ok:
        error = envelope.error
        raise AgentError(
            error.code if error else "ACTION_FAILED",
            error.message if error else "The action could not be completed.",
            http_status=404 if error and error.code == "NOT_FOUND" else 400,
        )

    return {
        "request_id": request_id,
        "action_id": action.get("action_id"),
        "type": intent.value,
        "applied": True,
        "result": envelope.result,
        "processing_ms": round((time.perf_counter() - started) * 1000, 2),
    }


__all__ = ["AgentError", "answer_question", "confirm_action"]
