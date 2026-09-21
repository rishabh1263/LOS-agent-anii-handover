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
    config,
    counters,
    facts,
    grounding,
    knowledge_answer,
    permissions,
    routing,
)
from app.agents.applicant.answer import deterministic_answer, generate_answer
from app.agents.applicant import followup
from app.agents.applicant.query_types import QueryType, clarification_for, type_for
from app.agents.applicant.intents import (
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


async def _call_tools(
    plan: tuple[str, ...],
    *,
    applicant_id: str | None,
    case_id: str | None,
    document_type: str | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """
    Run the planned tools.

    Returns (results, trace, errors). A tool that fails does not take the
    request down: its failure is recorded and the answer is built from what
    did come back, because a partial answer with a named gap is more useful to
    a FOS than a 500.
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


async def answer_question(
    *,
    message: str,
    applicant_id: str | None,
    case_id: str | None,
    claims: dict[str, Any],
    request_id: str | None = None,
    concise: bool = True,
    context: dict[str, Any] | None = None,
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

    if intent is Intent.MIXED:
        answer = deterministic_answer(answering_intent, results)
        source, llm_ms = "deterministic", 0.0
    else:
        use_llm = config.llm_enabled() and (
            answering_intent not in SIMPLE_INTENTS
            or config.llm_for_simple_intents()
        )
        if use_llm:
            answer, source, llm_ms = await generate_answer(
                message, answering_intent, results,
            )
        else:
            answer = deterministic_answer(answering_intent, results)
            source, llm_ms = "deterministic", 0.0

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
            answer = answer.rstrip() + "\n\n" + text
            response_source = routing.ResponseSource.MIXED.value
        else:
            # Only the store contributed, so say so.
            category = routing.QueryCategory.CASE_ONLY

    payload = _shape(results)
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
