"""
Answering from the FOS knowledge base, and refusing to.

THE RULE: NOTHING HERE MAY STATE A FACT ABOUT A CASE. Retrieval returns
policy — what the product requires, what satisfies a slot, what a verdict
means. It never returns whether THIS applicant's PAN passed. Case facts come
from the store, and the two are kept apart deliberately: a knowledge base
that appears to answer case questions is a system that will one day answer
one wrongly, fluently, with a citation.

THE OTHER RULE: A RETRIEVAL THAT IS NOT CONFIDENT PRODUCES A REFUSAL. Not a
hedged answer, not the best paragraph available with a disclaimer -- a plain
statement that the knowledge base does not cover it. A grounded system's
worst failure is a confident sourced answer to a question its sources never
addressed, because it is indistinguishable from a correct one.

The model, when it runs at all, is given the retrieved passages and asked to
phrase them. It is not given the question alone, it cannot reach the store,
and if it is unavailable or slow the retrieved text is returned directly.
Losing the model costs the prose and nothing else.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app import knowledge as knowledge_layer
from app.agents.applicant import config, counters

logger = logging.getLogger(__name__)

#: The stage this agent retrieves from. Never widened at runtime: a FOS
#: question answered from credit knowledge would be authoritative and out of
#: scope, which is the failure the stage scoping exists to prevent.
STAGE = "FOS"

#: What is said when retrieval is not confident. Deliberately plain, and
#: deliberately not an apology with a guess attached.
NO_ANSWER = (
    "I don't have enough information in the FOS knowledge base to answer "
    "that."
)

_SYSTEM = (
    "You are a field officer's assistant for the FOS stage of a loan "
    "application.\n"
    "Answer ONLY from the reference material supplied below.\n"
    "If the material does not answer the question, say you do not know.\n"
    "Never invent a document type, a rule, a threshold or a status.\n"
    "Never state anything about a specific applicant, case or document -- "
    "you have no access to case data and must not appear to.\n"
    "Be brief: two or three sentences, plain prose, no preamble, no "
    "markdown headings."
)


def retrieve(question: str, *, limit: int = 3):
    """Retrieve FOS knowledge for a question. Never raises."""
    if not knowledge_layer.enabled():
        return None
    try:
        return knowledge_layer.get_retriever().retrieve(
            question, STAGE, limit=limit,
        )
    except Exception:
        # The knowledge base failing must not take a case question with it.
        logger.exception("FOS knowledge retrieval failed")
        return None


def deterministic_answer(result, *, max_sentences: int | None = None) -> str:
    """
    The retrieved passage, returned as written.

    Used when the model is off, unavailable, too slow or not wanted. Wordier
    than a phrased answer and entirely correct, which is the right trade when
    the alternative is nothing.

    `max_sentences` trims it. A chat reply carrying a whole handbook section
    -- numbered list, table and all -- is not an answer anybody reads.
    """
    if result is None or not result.confident or not result.hits:
        return NO_ANSWER

    top = result.hits[0].chunk
    body = " ".join(top.text.split())

    if max_sentences:
        import re as _re

        sentences = _re.split(r"(?<=[.!?])\s+", body)
        body = " ".join(sentences[:max_sentences]).strip()

    if len(body) > 700:
        body = body[:700].rsplit(" ", 1)[0] + "…"
    return body


async def answer(
    question: str,
    *,
    limit: int = 3,
    allow_model: bool = True,
    max_sentences: int | None = None,
) -> tuple[str, str, dict]:
    """
    Answer a FOS knowledge question.

    Returns (answer, response_source, detail). `detail` carries the retrieval
    evidence -- citations and the top score -- so a caller can show where an
    answer came from, and so a reviewer can tell a refusal caused by a thin
    corpus from one caused by an off-topic question.

    `allow_model=False` skips phrasing entirely. The MIXED path uses it: that
    answer is already being composed from a computed half and a retrieved
    half, so phrasing one of them buys nothing and costs the model budget.
    Measured on a reachable-but-slow provider, phrasing the knowledge half of
    a mixed answer took the request from 3 ms to 2773 ms and then timed out
    and fell back to the retrieved text anyway.

    `max_sentences` trims the retrieved passage. A chat answer must not carry
    a handbook section.
    """
    started = time.perf_counter()
    result = retrieve(question, limit=limit)

    detail: dict[str, Any] = {
        "stage": STAGE,
        "retrieved": len(result.hits) if result else 0,
        "top_score": round(result.top_score, 4) if result else 0.0,
        "threshold": result.threshold if result else None,
        "confident": bool(result and result.confident),
        "citations": result.citations() if result else [],
    }

    if result is None or not result.confident:
        detail["refused"] = True
        return NO_ANSWER, "deterministic", detail

    if not allow_model or not config.llm_enabled():
        counters.record(called=False)
        return (deterministic_answer(result, max_sentences=max_sentences),
                "deterministic", detail)

    # GUARDED AT THE CALL SITE AS WELL, not only inside `_phrase`.
    #
    # `_phrase` promises to return None on any failure, and that promise was
    # broken once already -- an import above its try block raised straight
    # through and turned an answerable question into a 500. Phrasing is the
    # optional half of this path, so the caller does not rely on the callee
    # keeping its word about something this cheap to guarantee here.
    try:
        phrased = await _phrase(question, result.context(limit=limit))
    except Exception:
        logger.exception("FOS knowledge phrasing raised; using retrieved text")
        phrased = None

    if phrased is None:
        return (deterministic_answer(result, max_sentences=max_sentences),
                "deterministic", detail)

    detail["processing_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return phrased, "llm", detail


async def _phrase(question: str, context: str) -> str | None:
    """
    Ask the model to phrase the retrieved material. None on any failure.

    Follows the same shape as every other model call here -- reachability
    checked first, one bounded wait, availability marked on failure -- so a
    slow knowledge answer trips the same cooldown as a slow summary and stops
    being attempted for a while, rather than making every later request pay
    the timeout.

    A GENERATED ANSWER IS ONLY EVER PHRASING. It is built from the retrieved
    passages, never from the question alone, and returning None simply falls
    back to those passages as written.
    """
    # EVERY import is inside the try, including the framework's own.
    #
    # They were above it, and an import that failed raised straight out of a
    # function whose entire contract is "returns None on any failure" -- a
    # 500 on a question the service could answer perfectly well from the
    # retrieved text. Phrasing is the optional half of this path; nothing in
    # it may take down the answer.
    try:
        import asyncio

        from agent_framework import Message

        from app.llm import availability
        from app.llm.provider import create_ollama_client

        budget = config.llm_timeout_seconds()

        if not availability.provider_reachable():
            raise ConnectionError("model provider is not reachable")

        client = create_ollama_client()
        response = await asyncio.wait_for(
            client.get_response(
                [
                    Message(role="system", contents=[_SYSTEM]),
                    Message(role="user", contents=[
                        f"Reference material:\n\n{context}\n\n"
                        f"Question: {question}"
                    ]),
                ],
                stream=False,
                options={
                    "max_tokens": 160,
                    "temperature": config.temperature(),
                    "keep_alive": _keep_alive(),
                },
            ),
            timeout=budget,
        )
        counters.record(called=True)
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            return None
        return text.strip()

    except Exception as exc:
        from app.llm import availability as _availability

        _availability.mark_slow("FOS knowledge phrasing failed")
        logger.info(
            "FOS knowledge answer fell back to the retrieved text (%s: %s)",
            type(exc).__name__, exc,
        )
        return None


def _keep_alive() -> str:
    from app.agents.los.summary import keep_alive

    return keep_alive()


__all__ = ["NO_ANSWER", "STAGE", "answer", "deterministic_answer", "retrieve"]


def retrieved_text_for(question: str) -> str:
    """
    The retrieved passage for a question, as written.

    Used when a generated answer was rejected by grounding: the fallback must
    be the source material, never the rejected sentence with a note attached.
    """
    result = retrieve(question, limit=1)
    if result is not None and result.confident and result.hits:
        return deterministic_answer(result)
    return NO_ANSWER
