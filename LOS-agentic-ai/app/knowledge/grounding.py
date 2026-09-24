"""
Evidence, assembled and answered from — never beyond.

WHAT THIS ADDS, AND WHAT IT DOES NOT. The agent already produces a
deterministic answer out of authoritative structured records. This puts
retrieved evidence BESIDE that answer and lets a model phrase the two
together. It never replaces a structured fact with a retrieved one: the
store says what the KYC status is, and a chunk of derived text does not
get a vote on it.

CASE AND PROCESS EVIDENCE STAY LABELLED APART, all the way into the
prompt. The moment they share a list, a model can report "the RCU stage
samples files for authenticity" as something found on THIS case — a
true sentence about the wrong subject, which is the hardest kind of
wrong answer to notice.

INSUFFICIENT EVIDENCE IS SAID OUT LOUD. When nothing clears the score
floor, the answer says the available evidence is insufficient rather
than reaching for the least-bad chunk. A fluent paragraph built on
nothing is indistinguishable from one built on something.

THE MODEL IS GIVEN FACTS AND EVIDENCE, NOTHING ELSE. No prompts, no MCP
envelopes, no OCR, no paths, no agent state, no scores. The evidence it
sees is the same derived text a reviewer can read in `sources`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.knowledge import retrieval
from app.knowledge.embeddings import EmbeddingProvider
from app.knowledge.retrieval import Evidence, RetrievalResult
from app.knowledge.vector_store import Scope, VectorStore

logger = logging.getLogger(__name__)

#: What the model is told it must not do. Short on purpose: a long
#: prompt of prohibitions reads as a negotiation.
_SYSTEM = (
    "You answer questions about one loan application for a bank officer. "
    "Use ONLY the structured facts and the evidence provided. "
    "CASE EVIDENCE describes this specific case. PROCESS EVIDENCE "
    "describes how a stage works in general and is demonstration "
    "material, not policy -- never present it as a fact about this case. "
    "ESTABLISHED is the verdict the system already computed from the "
    "records. It is authoritative and settles the question asked: "
    "phrase it naturally, never contradict it, and never reason around "
    "it from other evidence. A document that verified is verified even "
    "when other checks on the case are unresolved -- those are separate "
    "facts and both can be true. "
    "If the evidence does not answer the question, say you do not have "
    "enough verified information. Never invent a document, a status, a "
    "finding, a date, a stage or an applicant detail. "
    "WRITE FOR A PERSON: one to three sentences, under 80 words, plain "
    "business English. Never output field names, codes, identifiers, "
    "JSON, bullet lists, or words like CASE_EVENT, CASE_FINDING, "
    "PROCESS_KNOWLEDGE, STRUCTURED, MCP or embeddings. Put a reason "
    "code into plain words rather than printing the code itself. "
    "NO EXAMPLE SENTENCE IS GIVEN HERE ON PURPOSE: a specimen about a "
    "mismatch was copied verbatim into answers that had nothing to do "
    "with one, including questions about how a stage works."
)

#: The longest a user-facing answer may be before it stops reading as
#: an answer and starts reading as a report. Enforced rather than
#: requested: a model asked for brevity complies most of the time, and
#: a demo is judged on the time it does not.
MAX_WORDS = 90

#: Said when there is nothing to answer from. Plain, and not an error.
#: Deterministic, so a caller can rely on it rather than on a model
#: remembering to refuse.
#:
#: ONE SENTENCE, NOT TWO. There was a second, "The available evidence
#: is insufficient to answer that for this case." Two wordings for one
#: outcome is two things to keep true, and that one was wrong for a
#: process question, which is about a stage and not about a case.
NO_EVIDENCE = "I don't have enough verified information to answer that yet."


@dataclass(frozen=True)
class GroundedContext:
    """Everything retrieved for one question, kept apart by kind."""

    case: RetrievalResult = field(default_factory=RetrievalResult)
    process: RetrievalResult = field(default_factory=RetrievalResult)

    @property
    def grounded(self) -> bool:
        """Whether anything at all cleared the floor."""
        return bool(self.case.sufficient or self.process.sufficient)

    def sources(self) -> list[dict[str, Any]]:
        """
        Where the answer could have come from, for a reviewer to check.

        NO SCORES, NO VECTOR IDS, NO COLLECTION NAMES. A source is a
        pointer into the case the reviewer already has access to.
        """
        published: list[dict[str, Any]] = []

        for item in self.case.evidence:
            published.append(_source(item))
        for item in self.process.evidence:
            published.append(_source(item))

        return published


#: `source_type` -> the `kind` the response already uses for
#: case-memory sources, so a frontend renders one list, not two.
_KIND = {
    "CASE_EVENT": "case_event",
    "CASE_FINDING": "case_finding",
    "CASE_DECISION": "case_decision",
    "CASE_DOCUMENT": "case_document",
    "PROCESS_KNOWLEDGE": "process_knowledge",
}


def _source(item: Evidence) -> dict[str, Any]:
    provenance = item.provenance
    source_type = provenance.get("source_type")
    return {
        "kind": _KIND.get(str(source_type or ""), "evidence"),
        "source_type": provenance.get("source_type"),
        "case_id": provenance.get("case_id"),
        "stage": provenance.get("stage"),
        "document_id": provenance.get("document_id"),
        "document_type": provenance.get("document_type"),
        "source_id": provenance.get("source_id"),
    }


def gather(
    question: str,
    *,
    category: str,
    scope: Scope | None,
    stages: tuple[str, ...] = (),
    store: VectorStore | None = None,
    embedder: EmbeddingProvider | None = None,
) -> GroundedContext:
    """
    The evidence for one question, by what the question needs.

    THE CATEGORY DECIDES WHICH RETRIEVERS RUN, and the two are never
    merged. A case question does not read the stage guides; a process
    question does not touch a case. A MIXED question runs both and
    keeps the results in separate fields.

    Raises `NotOwned` from `semantic_context` when a named case is not
    the applicant's -- deliberately, so the caller turns it into a
    refusal rather than an empty answer that looks like "nothing
    found".
    """
    wants_case = category in {"CASE_ONLY", "MIXED"} and scope is not None
    wants_process = (category in {"KNOWLEDGE_ONLY", "MIXED",
                                  "PROCESS_KNOWLEDGE"} and bool(stages))

    case = (
        retrieval.semantic_context(question, scope=scope, store=store,
                                   embedder=embedder)
        if wants_case else RetrievalResult()
    )
    process = (
        retrieval.process_context(question, stages=stages, store=store,
                                  embedder=embedder)
        if wants_process else RetrievalResult()
    )

    return GroundedContext(case=case, process=process)


def _payload(question: str, facts: dict[str, Any],
             context: GroundedContext,
             established: str = "") -> dict[str, Any]:
    """
    What the model sees. An allowlist, assembled here.

    The evidence is the derived TEXT only -- the same sentences the
    response publishes as sources. Scores, vector ids, payload
    internals and collection names are not part of answering.
    """
    payload = {
        "question": question,
        "structured_facts": facts,
        "case_evidence": [item.text for item in context.case.evidence],
        "process_evidence": [item.text for item in context.process.evidence],
    }
    # THE VERDICT THE RECORDS ALREADY GIVE, which the model was not
    # being shown. Asked "was the bank statement verified", it saw the
    # case's address findings and no verdict, and answered that the
    # bank details were not verified -- while the stored result for
    # that document was PASS. It was not hallucinating; it was
    # reasoning from the only evidence it had.
    if established.strip():
        payload["established"] = established.strip()
    return payload


async def answer(
    question: str,
    *,
    structured: str,
    facts: dict[str, Any],
    context: GroundedContext,
    timeout: float = 25.0,
) -> tuple[str, bool]:
    """
    A grounded answer, and whether evidence backed it.

    NEVER RAISES, AND NEVER LOSES THE STRUCTURED ANSWER. The
    deterministic answer is computed before this is called and is what
    comes back if the model is off, unreachable, slow or produces
    nothing usable. A model failure costs the phrasing and nothing
    else.
    """
    if not context.grounded:
        # STRUCTURED FACTS ARE AUTHORITATIVE, so a complete answer is
        # NOT annotated as insufficient. "No applications are on file
        # for this applicant" is a definitive answer from the store;
        # appending "the available evidence is insufficient" would
        # contradict it and teach a reader to distrust a correct
        # answer. `grounded=False` is the signal that retrieval added
        # nothing -- the sentence is for when there is nothing else.
        text = structured.rstrip()
        return _readable(text) if text else NO_EVIDENCE, False

    generated = await _generate(question, facts, context, timeout,
                                established=structured)

    # THE COMPUTED VERDICT WINS. A generated sentence that denies what
    # the records establish is worse than a plain one: it is confident,
    # readable and wrong, and a reader has no way to see that the
    # store said otherwise. Rare, and cheap to refuse.
    if generated and _denies(generated, structured):
        logger.warning("Generated answer contradicted the record; "
                       "published the computed answer instead.")
        return _readable(structured), True

    return _readable(generated or structured), True


#: Internal vocabulary that must never reach a reader. A model given
#: evidence labelled CASE_FINDING will sometimes repeat the label.
_INTERNAL = re.compile(
    r"\b(CASE_EVENT|CASE_FINDING|CASE_DECISION|CASE_DOCUMENT|CASE_HISTORY|"
    r"PROCESS_KNOWLEDGE|DERIVED_TEXT|STRUCTURED|MCP|Qdrant|LangGraph|embedding|"
    r"vector|chunk|payload|app_id|case_id|source_type|chunk_type)\b",
    re.IGNORECASE,
)


#: Stored document types, and how a person says them. The model is
#: given evidence labelled BANK_STATEMENT and repeats the label:
#: "The BANK_STATEMENT was verified" is the right fact in the wrong
#: register, and stripping the sentence would lose the answer.
#: Rewritten instead. PAN stays as it is -- it is what anyone calls it.
_TYPE_WORDS = {
    "BANK_STATEMENT": "bank statement",
    "SALE_DEED": "sale deed",
    "DRIVING_LICENCE": "driving licence",
    "DRIVING_LICENSE": "driving licence",
    "VOTER_ID": "voter ID",
    "ADDRESS_PROOF": "address proof",
    "AADHAAR": "Aadhaar",
    "PASSPORT": "passport",
    "INCOME_PROOF": "income proof",
    "PAYSLIP": "payslip",
}

_TYPE_RE = re.compile(
    r"\b(" + "|".join(sorted(_TYPE_WORDS, key=len, reverse=True))
    + r")\b")


def _in_words(text: str) -> str:
    """Document types as words. Applied before the length trim."""
    def swap(match):
        return _TYPE_WORDS[match.group(1)]

    return _TYPE_RE.sub(swap, text or "")


def _readable(text: str) -> str:
    """
    An answer a person can read, of a length they will read.

    TRIMMED RATHER THAN REQUESTED. The prompt asks for brevity and a
    model mostly complies; a demo is judged on the times it does not.
    Cut at a sentence boundary so the result still reads as prose.
    """
    said = " ".join(_in_words(text or "").split())
    if not said:
        return NO_EVIDENCE

    # An internal label in the middle of a sentence cannot be removed
    # without mangling it, so the sentence carrying it goes.
    sentences = re.split(r"(?<=[.!?])\s+", said)
    kept = [s for s in sentences if not _INTERNAL.search(s)] or sentences

    out: list[str] = []
    words = 0
    for sentence in kept:
        count = len(sentence.split())
        if out and words + count > MAX_WORDS:
            break
        out.append(sentence)
        words += count

    said = " ".join(out).strip()

    # A SINGLE OVERLONG SENTENCE STILL HAS TO BE CUT. The loop above
    # keeps the first sentence whatever its length, or a model that
    # answered in one long breath would bypass the limit entirely.
    words_out = said.split()
    if len(words_out) > MAX_WORDS:
        said = " ".join(words_out[:MAX_WORDS]).rstrip(",;:") + "..."

    return said or NO_EVIDENCE


#: A computed answer that SETTLES a verification question.
_AFFIRMS = re.compile(
    r"\b(is|was|were|are)\s+(?:successfully\s+)?"
    r"(verified|pass|passed|success|successful)\b",
    re.IGNORECASE,
)

#: A sentence denying that verification happened or succeeded.
_DENIES = re.compile(
    r"\b(not|never|failed|unverified|unable|could\s+not|cannot|couldn't)\b"
    r"[^.]{0,60}\b(verif\w+|pass\w*|success\w*)\b"
    r"|\bverif\w+\b[^.]{0,40}\b(failed|unsuccessful|not\s+(?:been\s+)?"
    r"(?:complete|possible|done))\b",
    re.IGNORECASE,
)


def _denies(generated: str, structured: str) -> bool:
    """
    Whether the model denied what the computed answer affirmed.

    NARROW ON PURPOSE. It fires only when the structured answer says a
    document verified AND the generated text says verification did not
    happen or did not succeed. It is not a general fact-checker and
    does not try to be: the one contradiction worth catching
    automatically is the one that turns a PASS into a failure.
    """
    if not structured or not _AFFIRMS.search(structured):
        return False
    return bool(_DENIES.search(generated or ""))


async def _generate(question: str, facts: dict[str, Any],
                    context: GroundedContext, timeout: float,
                    established: str = "") -> str | None:
    import asyncio

    try:
        from agent_framework import Message

        from app.llm import availability
        from app.llm.provider import create_ollama_client

        if not availability.provider_reachable():
            return None

        client = create_ollama_client()
        messages = [
            Message(role="system", contents=[_SYSTEM]),
            Message(role="user", contents=[
                json.dumps(_payload(question, facts, context,
                                    established=established),
                           separators=(",", ":"), default=str)
            ]),
        ]

        response = await asyncio.wait_for(
            client.get_response(messages, stream=False), timeout=timeout)
    except Exception as exc:
        # By type: a provider error can carry a URL.
        logger.warning("Grounded generation unavailable: %s",
                       type(exc).__name__)
        return None

    text = _text_of(response)
    return text or None


def _text_of(response: Any) -> str:
    for attribute in ("text", "content"):
        value = getattr(response, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    messages = getattr(response, "messages", None) or []
    for message in reversed(list(messages)):
        for content in getattr(message, "contents", None) or []:
            value = getattr(content, "text", None) or (
                content if isinstance(content, str) else None)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""


__all__ = ["GroundedContext", "MAX_WORDS", "NO_EVIDENCE",
           "answer", "gather"]
