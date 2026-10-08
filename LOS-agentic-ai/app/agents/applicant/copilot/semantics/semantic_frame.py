"""
SEMANTIC FRAMES -- what a question MEANS, separated from how it was said.

    "What documents are mandatory for this stage?"
    "Is stage pe kaunse papers chahiye?"
    "Which paperwork is compulsory at this stage?"
        -> task=LIST_REQUIREMENTS  object=DOCUMENTS  qualifiers=[MANDATORY]
           referents={stage: CURRENT}  party=SELF  scope=CURRENT_CASE

THE UNIT OF UNDERSTANDING IS THE FRAME, NOT THE SENTENCE. The parser reads
the question as a bag of concepts (semantic_concepts.yaml: DOCUMENTS,
REQUIRED, PENDING, VERIFICATION, STAGE ...) and composes a frame from
them by rules about CONCEPTS -- "a document object with a requirement cue
is a requirements question, whatever else the sentence says". A question
never matches a listed phrasing, so an unseen paraphrase, a typo, a short
form or another language reaches the same frame as the canonical wording.

THE ROUTE IS DETERMINISTIC AND CLOSED. `route(frame)` maps a frame to one of
the existing intents, which carry the same PLANS, capability checks and
tools as before. A document object can never route to the current-stage
answer: that follows from the frame's structure, not from a negative
pattern.

REFERENTS ARE RESOLVED, NOT GUESSED. "this stage" resolves to the case's
own recorded stage; "that document" to the document the previous answer
was about; "the co-applicant" to that party. A referent that cannot be
resolved safely becomes a clarification that names what is missing.

QWEN IS A FALLBACK FOR UNDERSTANDING, BOUNDED. When the deterministic parser
has no confident frame, ONE model call may propose a frame as strict JSON
in the same closed enums. It is validated like any other frame; it chooses
no tool, states no fact, and a timeout or an invalid answer falls back to
a clarification. When the parser is confident, the model is not called.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import get_close_matches
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().with_name("semantic_concepts.yaml")


# ==========================================================================
# THE CLOSED VOCABULARY OF MEANING
# ==========================================================================

class Task(str, Enum):
    LIST_REQUIREMENTS = "LIST_REQUIREMENTS"     # what is required / needed
    LIST_PENDING = "LIST_PENDING"               # what is still outstanding
    LIST_SUBMITTED = "LIST_SUBMITTED"           # what has been given already
    CHECK_VERIFICATION = "CHECK_VERIFICATION"   # is it verified / checked
    CURRENT_STAGE = "CURRENT_STAGE"             # which stage the case is at
    STATUS = "STATUS"                           # where the application stands
    NEXT_ACTION = "NEXT_ACTION"                 # what to do next
    READINESS = "READINESS"                     # ready to hand over?
    EXPLAIN = "EXPLAIN"                         # what does X mean / how does X work
    UNKNOWN = "UNKNOWN"


class Object(str, Enum):
    DOCUMENTS = "DOCUMENTS"
    DOCUMENT = "DOCUMENT"                       # one named document type
    APPLICATION = "APPLICATION"
    STAGE = "STAGE"
    PROCESS_TERM = "PROCESS_TERM"               # KYC, verification, policy ...
    NONE = "NONE"


class Qualifier(str, Enum):
    MANDATORY = "MANDATORY"
    STILL = "STILL"
    NOW = "NOW"
    ALL = "ALL"
    WHY = "WHY"                                 # "... and why?" -- with the reasons


class Party(str, Enum):
    SELF = "SELF"
    CO_APPLICANT = "CO_APPLICANT"
    THAT = "THAT"                               # "same for them" -> from context


class Scope(str, Enum):
    CURRENT_CASE = "CURRENT_CASE"
    GENERAL = "GENERAL"                         # the rules, not this case


CONFIDENT = ("high", "medium")


@dataclass
class SemanticFrame:
    task: Task = Task.UNKNOWN
    object: Object = Object.NONE
    qualifiers: list[Qualifier] = field(default_factory=list)
    #: stage: "CURRENT" | "<STAGE CODE>" | None; document: "THAT" | "<TYPE>" | None
    referents: dict[str, str] = field(default_factory=dict)
    party: Party = Party.SELF
    stage: str | None = None                    # the RESOLVED stage, when known
    scope: Scope = Scope.CURRENT_CASE
    confidence: str = "none"                    # high | medium | low | none
    language: str = "en"
    document_type: str | None = None
    concepts: list[str] = field(default_factory=list)
    source: str = "PARSER"                      # PARSER | LLM
    parse_ms: float = 0.0

    def is_confident(self) -> bool:
        return self.confidence in CONFIDENT and self.task is not Task.UNKNOWN

    def public(self) -> dict[str, Any]:
        """The frame as published: enums as strings, nothing sensitive."""
        return {
            "task": self.task.value, "object": self.object.value,
            "qualifiers": [q.value for q in self.qualifiers],
            "referents": dict(self.referents), "party": self.party.value,
            "stage": self.stage, "scope": self.scope.value,
            "confidence": self.confidence, "language": self.language,
            "document_type": self.document_type, "source": self.source,
            "concepts": list(self.concepts),
        }


# ==========================================================================
# CONCEPTS
# ==========================================================================

@lru_cache(maxsize=1)
def _config() -> dict[str, Any]:
    try:
        return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - configuration failure
        logger.error("semantic_concepts.yaml unreadable: %s", type(exc).__name__)
        return {}


def reload() -> None:
    _config.cache_clear()
    _tables.cache_clear()


_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*|[ऀ-ॿ]+", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text or "")
    return [t.lower().strip("'-") for t in _WORD.findall(text) if t.strip("'-")]


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, str], dict[tuple[str, ...], str], int, frozenset[str],
                       frozenset[str], frozenset[str]]:
    """(single word -> concept, phrase -> concept, longest phrase, fuzzy words,
    protected words, products)."""
    # A WORD MAY CARRY MORE THAN ONE CONCEPT ("mandatory" is both a
    # requirement cue and the MANDATORY qualifier), so each maps to a list.
    words: dict[str, list[str]] = {}
    phrases: dict[tuple[str, ...], list[str]] = {}
    longest = 1
    for concept, entries in (_config().get("concepts") or {}).items():
        for entry in entries or []:
            parts = tuple(_tokens(str(entry)))
            if not parts:
                continue
            target = words if len(parts) == 1 else phrases
            key = parts[0] if len(parts) == 1 else parts
            if concept not in target.setdefault(key, []):
                target[key].append(concept)
            if len(parts) > 1:
                longest = max(longest, len(parts))
    fuzzy = frozenset(w for w in words if re.fullmatch(r"[a-z-]{5,}", w))
    protected = frozenset(str(w).lower() for w in _config().get("protected") or [])
    products = frozenset(str(p).lower() for p in _config().get("products") or [])
    return words, phrases, longest, fuzzy, protected, products


def concepts_in(text: str) -> list[tuple[str, str]]:
    """
    (surface, CONCEPT) for every concept the text carries, in order.

    Phrases first, longest first; then single words; then a fuzzy pass for a
    Latin word of five or more letters that is a near miss of a concept word
    ("mandtory", "documnts") -- never for a protected everyday word.
    """
    words, phrases, longest, fuzzy, protected, _ = _tables()
    tokens = _tokens(text)
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        hit = False
        for size in range(min(longest, len(tokens) - i), 1, -1):
            phrase = tuple(tokens[i:i + size])
            if phrase in phrases:
                out.extend((" ".join(phrase), c) for c in phrases[phrase])
                i += size
                hit = True
                break
        if hit:
            continue
        token = tokens[i]
        if token in words:
            out.extend((token, c) for c in words[token])
        elif len(token) >= 5 and token not in protected and re.fullmatch(r"[a-z-]+", token):
            near = get_close_matches(token, fuzzy, n=1, cutoff=0.84)
            if near:
                out.extend((token, c) for c in words[near[0]])
        i += 1
    return out


# ==========================================================================
# THE PARSER
# ==========================================================================

_QUESTION = re.compile(r"\?|\b(what|which|kya|kaunse|kaunsa|konse|कौन|काय|कोणत|किस|कौनसे)\b",
                       re.IGNORECASE)
_PAST = re.compile(r"\b(have|has|had|already|been|were|was|did|gaya|gayi|gaye|hua|hui|hue|"
                   r"diya|diye|kiya|kiye|chuka|chuki|chuke|ho gaya|ho gaye)\b", re.IGNORECASE)
_HOW_WORKS = re.compile(r"\bhow\s+(do|does|is|are|can|should)\b.{0,48}\b(work|works|verified|"
                        r"classified|handled|processed|decided|checked|evaluated)\b", re.IGNORECASE)
_WHATS_PENDING = re.compile(r"^\s*what'?s?\s*(is\s+)?(still\s+)?(pending|outstanding|left)\b",
                            re.IGNORECASE)


def _document_type(text: str) -> str | None:
    from app.agents.applicant.copilot.semantics import intents

    return intents._document_type(text)


def _named_stage(text: str) -> str | None:
    from app.agents.applicant.copilot.semantics import intents

    return intents.stage_in(text)


def _language(text: str) -> str:
    try:
        from app.agents.applicant import language

        return language.detect(text).code
    except Exception:  # pragma: no cover
        return "en"


def parse(message: str, *, original: str | None = None) -> SemanticFrame:
    """
    The frame a question means. Deterministic; no model.

    `message` is the normalised / canonicalised text; `original` the text as
    typed, for language detection and native-script concepts.
    """
    started = time.perf_counter()
    text = " ".join(str(message or "").split())
    raw = " ".join(str(original or message or "").split())
    frame = SemanticFrame(language=_language(raw))
    if not text:
        return frame

    found = concepts_in(text)
    if raw != text:
        seen = {c for _, c in found}
        found += [(s, c) for s, c in concepts_in(raw) if c not in seen]
    # Marathi "का" closing a sentence is a question tag ("... तपासले का?"),
    # not the "why" it means elsewhere.
    if (raw or text).rstrip(" ?？").endswith("का") and all(s == "का" for s, c in found if c == "WHY"):
        found = [(s, c) for s, c in found if c != "WHY"]
    present = {c for _, c in found}
    frame.concepts = sorted(present)
    _, _, _, _, _, products = _tables()
    lowered = (text + " " + raw).lower()

    # -- object --------------------------------------------------------------
    document_type = _document_type(text) or _document_type(raw)
    if "DOCUMENTS" in present:
        frame.object = Object.DOCUMENTS
    elif document_type:
        frame.object = Object.DOCUMENT
        frame.document_type = document_type
    elif "STAGE" in present:
        frame.object = Object.STAGE
    elif "APPLICATION" in present or "STATUS" in present:
        frame.object = Object.APPLICATION
    elif "PROCESS_TERM" in present:
        frame.object = Object.PROCESS_TERM
    if frame.object is Object.DOCUMENTS and document_type:
        frame.document_type = document_type

    # -- qualifiers --------------------------------------------------------------
    if "MANDATORY" in present:
        frame.qualifiers.append(Qualifier.MANDATORY)
    if "STILL" in present:
        frame.qualifiers.append(Qualifier.STILL)
    if "NOW" in present:
        frame.qualifiers.append(Qualifier.NOW)
    if "ALL" in present:
        frame.qualifiers.append(Qualifier.ALL)

    # -- party -----------------------------------------------------------------------
    if "CO_APPLICANT" in present:
        frame.party = Party.CO_APPLICANT

    # -- scope: the rules in general, or a product's policy ----------------------------
    from app.agents.applicant.copilot.semantics import intents as _intents

    general = "GENERAL" in present or bool(re.search(
        r"\bfor\s+(a|an|any)\s+(" + "|".join(re.escape(p) for p in products) + r")[\s_-]*loan\b",
        lowered)) or bool(_intents._STRONG_KNOWLEDGE.search(lowered))
    if general:
        frame.scope = Scope.GENERAL

    # -- referents -------------------------------------------------------------------
    named = _named_stage(text) or _named_stage(raw)
    if named:
        frame.referents["stage"] = named
    elif "STAGE" in present and "NEXT" in present and frame.object is Object.STAGE and (
            "DEIXIS" in present
            or re.search(r"\b(after|comes|happens|following|baad|बाद|नंतर)\b", lowered)):
        # "what happens after this step" / "which stage comes next" -- not
        # "next step?", which asks what to do
        frame.referents["stage"] = "NEXT"
    elif "STAGE" in present and ({"DEIXIS", "OWN", "NOW"} & present):
        frame.referents["stage"] = "CURRENT"
    elif "STAGE" in present and frame.object is not Object.STAGE:
        frame.referents["stage"] = "CURRENT"
    elif "NOW" in present and frame.object in (Object.DOCUMENTS, Object.DOCUMENT):
        frame.referents["stage"] = "CURRENT"
    if frame.object is Object.DOCUMENT and frame.document_type:
        frame.referents["document"] = frame.document_type
    elif frame.object in (Object.DOCUMENTS, Object.DOCUMENT) and "DEIXIS" in present \
            and re.search(r"\b(this|that|same|the\s+same|isi|usi|wahi|यह|वह|तेच|तोच)\s+"
                          r"(document|doc|paper|one|wala|वाला)\b", lowered):
        frame.referents["document"] = "THAT"
    if "DEIXIS" in present and frame.object is Object.NONE and re.search(
            r"\b(it|that|this|same|wahi|usi)\b", lowered):
        frame.referents["it"] = "PREVIOUS"

    # -- task: composed from the concept cues, by precedence of MEANING ---------------
    explain = "EXPLAIN" in present or bool(_HOW_WORKS.search(lowered))
    # "What is KYC?" -- a process term asked about with a question word and
    # no ownership is a definition, whatever language it is asked in.
    if frame.object is Object.PROCESS_TERM and _QUESTION.search(lowered) \
            and not ({"OWN", "DEIXIS"} & present):
        explain = True
    # "what is address proof?" -- a document TYPE asked about as a term.
    cue_concepts = {"REQUIRED", "SUBMIT", "SUBMITTED", "PENDING", "MISSING", "VERIFICATION",
                    "STATUS", "NEXT", "READY", "PROCEED", "MANDATORY", "STILL", "DONE"}
    if frame.object in (Object.DOCUMENT, Object.DOCUMENTS) and not (cue_concepts & present) \
            and not ({"OWN", "DEIXIS"} & present) \
            and re.match(r"^\s*(what|which)\s+(is|are)\s+(a|an|the)?\s*\w", lowered):
        explain = True
    # "WHAT IS THE NEXT ACTION / STEP?" names the case's next step, not a
    # term to define: the noun after "next" is the object of NEXT.
    if "NEXT" in present and frame.object is Object.PROCESS_TERM \
            and re.search(r"\bnext\s+(action|step|thing|task|move)\b", lowered) \
            and not re.search(r"\b(mean|means|meaning|definition|define|matlab)\b", lowered):
        frame.object = Object.NONE
        explain = False
    # A "why" question asks for a reason or a policy, never for a list; the
    # rules that own explanations (case history, policy) keep it.
    # "WHY IS IT UNDER REVIEW?" asks for the recorded reason (case history),
    # not for a list: a review-state word is not "pending" here.
    review_state = any(str(s).lower() in ("under review", "in review", "review", "being reviewed")
                       for s, c in found if c == "PENDING")
    if "WHY" in present and not explain and not (
            frame.document_type and ({"PENDING", "STILL", "MISSING"} & present)
            and not review_state) and not (
            frame.object in (Object.DOCUMENTS, Object.DOCUMENT)
            and ({"PENDING", "MISSING"} & present) and not review_state):
        frame.confidence = "low"
        frame.parse_ms = round((time.perf_counter() - started) * 1000, 2)
        return frame
    if "WHY" in present:
        # "what documents are pending, and why?" -- the list, with each
        # item's recorded reason (a qualifier, never a re-route).
        frame.qualifiers.append(Qualifier.WHY)
    task_cues: list[Task] = []
    if explain and frame.object in (Object.PROCESS_TERM, Object.NONE, Object.DOCUMENTS,
                                    Object.STAGE, Object.APPLICATION) \
            and not ({"OWN"} & present and frame.object is not Object.PROCESS_TERM):
        task_cues.append(Task.EXPLAIN)
    if frame.object is Object.DOCUMENTS and _WHATS_PENDING.match(lowered):
        # "what is pending ...?" asks for everything pending on the case,
        # whatever noun follows (a neutralised party reads "my documents").
        frame.object = Object.NONE
        task_cues.append(Task.LIST_PENDING)
    elif frame.object in (Object.DOCUMENTS, Object.DOCUMENT):
        if frame.document_type and "WHY" in present:
            # "why is address proof still pending?" -- that document's status
            frame.object = Object.DOCUMENT
            task_cues.append(Task.CHECK_VERIFICATION)
        elif "VERIFICATION" in present:
            task_cues.append(Task.CHECK_VERIFICATION)
        elif "STATUS" in present and not explain:
            # "what is the status of my documents?" -- each document with its
            # status, the long-standing document-status list; "of ALL of them"
            # is every required document, uploaded or not (the checklist)
            if frame.object is Object.DOCUMENTS and "ALL" in present:
                task_cues.append(Task.LIST_REQUIREMENTS)
            else:
                task_cues.append(Task.LIST_SUBMITTED if frame.object is Object.DOCUMENTS
                                 else Task.CHECK_VERIFICATION)
        if "PENDING" in present or "MISSING" in present or (
                "STILL" in present and ({"REQUIRED", "SUBMIT", "SUBMITTED"} & present)):
            task_cues.append(Task.LIST_PENDING)
        if "SUBMITTED" in present and "REQUIRED" not in present and (
                _PAST.search(lowered) or frame.object is Object.DOCUMENT
                or re.match(r"\s*(are|is|kya|क्या)\b", lowered)):
            task_cues.append(Task.LIST_SUBMITTED)     # "is my salary slip uploaded?"
        if "DONE" in present and not task_cues:
            task_cues.append(Task.LIST_PENDING)          # "am I done with the paperwork?"
        if not task_cues and frame.object is Object.DOCUMENT and re.search(
                r"\b(is|are|has|have)\b.*\b(in|in\s+yet|come\s+in|reached)\s*\??$", lowered):
            task_cues.append(Task.LIST_SUBMITTED)         # "is my bank statement in?"
        if {"REQUIRED", "MANDATORY", "SUBMIT"} & present and Task.LIST_PENDING not in task_cues:
            task_cues.append(Task.LIST_REQUIREMENTS)
        if not task_cues and re.search(r"\b(for|of)\s+(it|this|that|this\s+one|the\s+"
                                       r"(stage|step))\b", lowered) and frame.object is Object.DOCUMENTS:
            # "and the documents for it?" -- the documents a stage asks for
            frame.referents.setdefault("stage", "CURRENT")
            task_cues.append(Task.LIST_REQUIREMENTS)
        # A bare object ("which docs?") is AMBIGUOUS: required, pending,
        # submitted or verified. No task is guessed; the agent asks.
    elif frame.object is Object.STAGE:
        if "PROCEED" in present or ("NEXT" in present and "READY" in present):
            task_cues.append(Task.READINESS)            # "can I proceed to the next stage?"
        elif "NEXT" in present:
            task_cues.append(Task.NEXT_ACTION)
        elif "DONE" in present:
            # "is everything in for this step?" -- what is still pending
            frame.referents.setdefault("stage", "CURRENT")
            task_cues.append(Task.LIST_PENDING)
        elif {"REQUIRED", "SUBMIT", "PENDING"} & present:
            # "what is needed at this stage" -- the stage's requirements
            frame.object = Object.DOCUMENTS
            frame.referents.setdefault("stage", "CURRENT")
            task_cues.append(Task.LIST_PENDING if "PENDING" in present
                             else Task.LIST_REQUIREMENTS)
        elif not explain:
            task_cues.append(Task.CURRENT_STAGE)
    elif frame.object is Object.APPLICATION:
        if "VERIFICATION" in present and "APPLICATION" not in present and not explain:
            # "what is my verification status?" -- the object was implied by
            # "my"; the verification asked about is the documents'
            frame.object = Object.DOCUMENTS
            task_cues.append(Task.CHECK_VERIFICATION)
        elif "NEXT" in present:
            task_cues.append(Task.NEXT_ACTION)
        elif "READY" in present:
            task_cues.append(Task.READINESS)
        elif "PENDING" in present or ({"REQUIRED", "SUBMIT"} & present):
            frame.object = Object.DOCUMENTS
            task_cues.append(Task.LIST_PENDING if "PENDING" in present
                             else Task.LIST_REQUIREMENTS)
        elif "STATUS" in present and not explain:
            task_cues.append(Task.STATUS)
        elif "VERIFICATION" in present and not explain:
            # "has anything on my file been looked at yet" -- the documents
            frame.object = Object.DOCUMENTS
            task_cues.append(Task.CHECK_VERIFICATION)
        elif "DONE" in present and not explain:
            task_cues.append(Task.LIST_PENDING)
    else:
        if "FIX" in present and not ({"DOCUMENTS", "PENDING", "MISSING"} & present):
            # "what do I need to fix?" asks what went wrong -- recorded case
            # history, which the frame does not model. It declines rather
            # than read "need" as a requirements list.
            pass
        elif "NEXT" in present and not explain:
            task_cues.append(Task.NEXT_ACTION)
        elif "READY" in present and not explain:
            task_cues.append(Task.READINESS)
        elif "PROCEED" in present and frame.referents.get("stage") and not explain:
            # "CPA mein kab jayega?" -- moving to a NAMED stage is the readiness question
            task_cues.append(Task.READINESS)
        elif ("PENDING" in present or "MISSING" in present) and not explain:
            # "what is pending?" asks for everything pending on the case
            # (PENDING_ITEMS); "anything pending?" / "kya pending hai" ask
            # about the documents, which is what is usually pending.
            if not _WHATS_PENDING.search(lowered):
                frame.object = Object.DOCUMENTS
            task_cues.append(Task.LIST_PENDING)
        elif "REQUIRED" in present and "NOW" in present and "SUBMIT" not in present \
                and not explain:
            task_cues.append(Task.NEXT_ACTION)          # "what do I need now?"
        elif "VERIFICATION" in present and not explain and (
                _QUESTION.search(lowered) or _PAST.search(lowered)):
            frame.object = Object.DOCUMENTS             # "kya sab kuch verify ho gaya"
            task_cues.append(Task.CHECK_VERIFICATION)
        elif "SUBMITTED" in present and _PAST.search(lowered) and not explain:
            frame.object = Object.DOCUMENTS             # "jo submit ho gaye"
            task_cues.append(Task.LIST_SUBMITTED)
        elif "DONE" in present and not explain:
            task_cues.append(Task.LIST_PENDING)         # "have you got everything you need?"
        elif "STATUS" in present and not explain:
            frame.object = Object.APPLICATION           # "what's the holdup"
            task_cues.append(Task.STATUS)
        elif {"REQUIRED", "SUBMIT"} & present and not explain and (
                "SUBMIT" in present or _QUESTION.search(lowered)
                or ({"NOW", "STILL", "STAGE", "DEIXIS"} & present)):
            # "what do I need to submit?" / "what is needed now?" -- the
            # thing given or needed on a case is its documents, implied.
            frame.object = Object.DOCUMENTS
            task_cues.append(Task.LIST_REQUIREMENTS)
            frame.confidence = "medium"

    if task_cues:
        frame.task = task_cues[0]
        if frame.confidence == "none":
            frame.confidence = "high" if len(set(task_cues)) == 1 else "medium"
    elif frame.object is not Object.NONE:
        frame.confidence = "low"

    frame.parse_ms = round((time.perf_counter() - started) * 1000, 2)
    return frame


# ==========================================================================
# ROUTING -- frame -> the existing intent (and so its PLANS and tools)
# ==========================================================================

def route(frame: SemanticFrame) -> str | None:
    """The intent name a frame maps to, or None when it maps to nothing."""
    task, obj = frame.task, frame.object
    if task is Task.UNKNOWN:
        return None
    if task is Task.EXPLAIN:
        return "FOS_KNOWLEDGE"
    if obj in (Object.DOCUMENTS, Object.DOCUMENT):
        if task is Task.LIST_REQUIREMENTS:
            if frame.scope is Scope.GENERAL:
                return "FOS_KNOWLEDGE"          # the product's policy, not this case
            if Qualifier.STILL in frame.qualifiers or "SUBMIT" in frame.concepts:
                # what still has to be GIVEN: the checklist's gaps
                return "DOCUMENTS_MISSING"
            return "DOCUMENTS_REQUIRED"
        if task is Task.LIST_PENDING:
            if "MISSING" in frame.concepts and "PENDING" not in frame.concepts:
                return "DOCUMENTS_MISSING"
            if Qualifier.STILL in frame.qualifiers and "PENDING" not in frame.concepts:
                return "DOCUMENTS_MISSING"
            return "DOCUMENTS_PENDING" if obj is Object.DOCUMENTS or frame.document_type \
                else "PENDING_ITEMS"
        if task is Task.LIST_SUBMITTED:
            return "DOCUMENTS_UPLOADED"
        if task is Task.CHECK_VERIFICATION:
            return "DOCUMENT_VERIFICATION"
        if task is Task.NEXT_ACTION:
            return "NEXT_ACTION"
        # A DOCUMENT OBJECT NEVER ROUTES TO THE STAGE ANSWER. A stage word in
        # a documents question is a qualifier (which stage's documents), and
        # is carried as a referent.
        return None
    if obj is Object.STAGE and task is Task.NEXT_ACTION and frame.referents.get("stage") == "NEXT":
        return "STAGE_PROCESS"                  # the next stage, by the lifecycle
    if task is Task.CURRENT_STAGE:
        return "APPLICATION_STAGE" if obj in (Object.STAGE, Object.APPLICATION,
                                              Object.NONE) else None
    if task is Task.STATUS:
        return "APPLICATION_STATUS"
    if task is Task.NEXT_ACTION:
        return "NEXT_ACTION"
    if task is Task.READINESS:
        return "READINESS"
    if task is Task.LIST_PENDING:
        return "PENDING_ITEMS"
    if task is Task.LIST_REQUIREMENTS and obj is Object.NONE:
        return "DOCUMENTS_MISSING"
    return None


def is_knowledge(frame: SemanticFrame) -> bool:
    """Whether a genuine KNOWLEDGE task was understood (RAG may answer it)."""
    return frame.task is Task.EXPLAIN or (
        frame.scope is Scope.GENERAL and frame.task is not Task.UNKNOWN)


# ==========================================================================
# REFERENTS
# ==========================================================================

@dataclass
class Resolved:
    frame: SemanticFrame
    #: What a referent was resolved to, by name -- reported, never hidden.
    resolutions: dict[str, str] = field(default_factory=dict)
    #: A targeted question, when a referent could not be resolved safely.
    clarification: str | None = None


def resolve_referents(frame: SemanticFrame, context: Any, *,
                      case_stage: str | None) -> Resolved:
    """
    Bind the frame's referents to what they mean HERE.

    THE CASE RECORD IS AUTHORITATIVE FOR THE STAGE: "this stage" is the
    stage the case is recorded at, never something the conversation said.
    The conversation context resolves only what the previous ANSWER was
    about (a document, a party).
    """
    out = Resolved(frame=frame)
    stage_ref = frame.referents.get("stage")
    if stage_ref == "NEXT":
        out.resolutions["stage"] = "NEXT (bound to the lifecycle after the case's " \
                                   "recorded stage once ownership is checked)"
    elif stage_ref == "CURRENT":
        if case_stage:
            frame.stage = str(case_stage).upper()
            out.resolutions["stage"] = f"CURRENT -> {frame.stage} (case record)"
        else:
            out.resolutions["stage"] = "CURRENT (case stage not recorded; the checklist " \
                                       "is resolved against the case as it stands)"
    elif stage_ref:
        frame.stage = stage_ref
        out.resolutions["stage"] = f"named {stage_ref}"
        if case_stage and str(case_stage).upper() != stage_ref:
            out.resolutions["stage_note"] = (
                f"the case is recorded at {str(case_stage).upper()}; requirements are "
                f"resolved for the case's own stage")

    document_ref = frame.referents.get("document")
    if document_ref == "THAT":
        slot = getattr(context, "last_slot", None)
        if slot:
            frame.document_type = str(slot)
            frame.referents["document"] = str(slot)
            out.resolutions["document"] = f"THAT -> {slot} (previous answer)"
        else:
            out.clarification = ("Which document do you mean? The previous answer "
                                 "was not about one document in particular.")

    if frame.party is Party.THAT:
        subject = getattr(context, "last_subject", None)
        if subject in ("CO_APPLICANT", "PRIMARY_APPLICANT"):
            frame.party = Party.CO_APPLICANT if subject == "CO_APPLICANT" else Party.SELF
            out.resolutions["party"] = f"THAT -> {subject}"
        else:
            out.clarification = "Do you mean the applicant or the co-applicant?"

    return out


# ==========================================================================
# CLARIFICATION -- targeted, from the partial frame
# ==========================================================================

def clarification(frame: SemanticFrame | None, *, has_case: bool) -> dict[str, Any] | None:
    """A question that names the missing piece, or None when nothing was understood."""
    if frame is None:
        return None
    if frame.party is Party.CO_APPLICANT and frame.task is Task.UNKNOWN \
            and frame.object in (Object.NONE, Object.DOCUMENTS):
        # "what about the other applicant?" -- the party is known, the ask is not
        return {"reason": "TASK_UNCLEAR",
                "question": ("About the co-applicant: do you mean their documents, their "
                             "verification, or what is pending for them?"),
                "options": ["What documents does the co-applicant need?",
                            "Are the co-applicant's documents verified?",
                            "What is pending for the co-applicant?"]}
    if frame.object is Object.NONE and frame.task is Task.UNKNOWN:
        return None
    if frame.object in (Object.DOCUMENTS, Object.DOCUMENT) and frame.task is Task.UNKNOWN \
            and frame.document_type:
        # "help with PAN": the question is about ONE document -- ask about it
        name = "PAN" if frame.document_type == "PAN" else frame.document_type.replace("_", " ").title()
        return {"reason": "TASK_UNCLEAR",
                "question": (f"What would you like to do with the {name} -- check its verification, "
                             f"upload it, or know why it's needed?"),
                "options": [f"Is my {name} verified?", f"How do I upload my {name}?",
                            f"Why is {name} required?"]}
    if frame.object in (Object.DOCUMENTS, Object.DOCUMENT) and frame.task is Task.UNKNOWN:
        return {"reason": "TASK_UNCLEAR",
                "question": ("About the documents: do you want the list that is required, "
                             "the ones still pending, the ones already submitted, or their "
                             "verification status?"),
                "options": ["What documents are required?", "Which documents are pending?",
                            "Which documents have been submitted?",
                            "Are my documents verified?"]}
    if frame.task is Task.LIST_REQUIREMENTS and frame.object is Object.NONE:
        return {"reason": "OBJECT_UNCLEAR",
                "question": ("Required for what -- the documents for this case, or "
                             "something else about the application?"),
                "options": ["What documents are required?", "What is pending on this case?"]}
    if frame.task is Task.EXPLAIN and frame.object is Object.NONE:
        return {"reason": "OBJECT_UNCLEAR",
                "question": "What would you like explained -- a term, a stage, or a document?",
                "options": ["What is KYC?", "What happens at the CPA stage?"]}
    if frame.object is Object.STAGE and frame.task is Task.UNKNOWN:
        return {"reason": "TASK_UNCLEAR",
                "question": ("About the stage: do you want to know which stage the case is "
                             "at, or what is required at this stage?"),
                "options": ["What stage is my application at?",
                            "What documents are required at this stage?"]}
    return None


# ==========================================================================
# QWEN -- one bounded call that proposes a frame, never a fact
# ==========================================================================

_SCHEMA = {
    "task": [t.value for t in Task if t is not Task.UNKNOWN] + ["UNKNOWN"],
    "object": [o.value for o in Object],
    "qualifiers": [q.value for q in Qualifier],
    "party": [p.value for p in Party],
    "scope": [s.value for s in Scope],
}

_SYSTEM = (
    "Classify the question into JSON only: {task,object,qualifiers,referents,party,scope,language}. "
    "task: LIST_REQUIREMENTS|LIST_PENDING|LIST_SUBMITTED|CHECK_VERIFICATION|CURRENT_STAGE|STATUS|"
    "NEXT_ACTION|READINESS|EXPLAIN|UNKNOWN. object: DOCUMENTS|DOCUMENT|APPLICATION|STAGE|"
    "PROCESS_TERM|NONE. qualifiers: subset of [MANDATORY,STILL,NOW,ALL]. referents: {stage: "
    "CURRENT or FOS/CPA/CREDIT/RCU/BOPS/HOPS/DISBURSEMENT, document: THAT or a type} or {}. "
    "party: SELF|CO_APPLICANT|THAT. scope: CURRENT_CASE, or GENERAL for rules/products in "
    "general. language: ISO code (en, hi, hi-Latn, mr). Question may be English, Hinglish, "
    "Hindi or Marathi. Never answer it; never name tools, data or facts. Reply with ONE "
    "compact single-line JSON object; omit empty fields."
)


def llm_enabled() -> bool:
    import os

    override = os.getenv("COPILOT_UNDERSTANDING_LLM")
    if override is not None:
        return override.lower() == "true"
    try:
        from app.agents.applicant import config

        return bool((config.chatbot("understanding") or {}).get("llm_fallback", True))
    except Exception:
        return True


def llm_timeout_seconds() -> float:
    try:
        from app.agents.applicant import config

        return float((config.chatbot("understanding") or {}).get("llm_timeout_seconds", 1.5))
    except Exception:
        return 1.5


def validate_llm_frame(data: Any, *, language: str = "en") -> SemanticFrame | None:
    """A model's JSON -> a frame, or None if it breaks the closed schema."""
    if not isinstance(data, dict):
        return None
    try:
        task = Task(str(data.get("task") or "UNKNOWN").upper())
        obj = Object(str(data.get("object") or "NONE").upper())
        raw_q = data.get("qualifiers") or []
        raw_q = [raw_q] if isinstance(raw_q, str) else list(raw_q)
        qualifiers = [Qualifier(str(q).upper()) for q in raw_q
                      if str(q).upper() in _SCHEMA["qualifiers"]]
        party = Party(str(data.get("party") or "SELF").upper())
        scope = Scope(str(data.get("scope") or "CURRENT_CASE").upper())
    except ValueError:
        return None
    referents: dict[str, str] = {}
    raw_refs = data.get("referents") if isinstance(data.get("referents"), dict) else {}
    stage = str(raw_refs.get("stage") or "").upper()
    if stage == "CURRENT" or stage in ("FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS",
                                       "DISBURSEMENT"):
        referents["stage"] = stage
    document = str(raw_refs.get("document") or "").upper()
    if document == "THAT":
        referents["document"] = "THAT"
    elif re.fullmatch(r"[A-Z0-9_ ]{2,32}", document):
        referents["document"] = re.sub(r"\s+", "_", document)
    if task is Task.UNKNOWN:
        return None
    frame = SemanticFrame(task=task, object=obj, qualifiers=qualifiers, referents=referents,
                          party=party, scope=scope, confidence="medium",
                          language=str(data.get("language") or language)[:8],
                          source="LLM")
    if obj is Object.DOCUMENT and referents.get("document") not in (None, "THAT"):
        frame.document_type = referents["document"]
    return frame


async def llm_frame(message: str, *, timeout: float | None = None,
                    generator: Any = None) -> tuple[SemanticFrame | None, dict[str, Any]]:
    """
    ONE bounded model call proposing a frame. Returns (frame or None, trace).

    `generator(message) -> str` is injectable for tests. Never raises; a
    timeout, transport failure or invalid JSON is (None, {"status": ...}).
    """
    trace: dict[str, Any] = {"consulted": False, "status": "SKIPPED", "ms": 0.0}
    if not llm_enabled():
        trace["status"] = "DISABLED"
        return None, trace
    if generator is None:
        from app.llm import availability

        if not availability.provider_reachable():
            trace["status"] = "UNAVAILABLE"
            return None, trace
        generator = _ollama_generate

    import asyncio

    limit = timeout or llm_timeout_seconds()
    started = time.perf_counter()
    trace["consulted"] = True
    real = generator is _ollama_generate

    def _ledger(outcome: str, output: str = "", used: bool = False) -> None:
        # this call bypasses the client factory (raw /api/chat): recorded here
        if not real:
            return
        from app.llm import trace as _llm_trace
        from app.llm.config import ollama_model

        _llm_trace.record({"caller": f"{__name__}.llm_frame", "model": ollama_model(), "host": "local",
                           "prompt_chars": len(_SYSTEM) + min(len(str(message)), 400), "max_tokens": 64,
                           "outcome": outcome, "ms": trace["ms"], "output_chars": len(output or ""),
                           "used": used})

    try:
        call = generator(message, limit) if generator is _ollama_generate else generator(message)
        text = await asyncio.wait_for(call, timeout=limit)
    except (asyncio.TimeoutError, TimeoutError):
        trace.update(status="TIMEOUT", ms=round((time.perf_counter() - started) * 1000, 2))
        _ledger("TIMEOUT")
        return None, trace
    except Exception as exc:
        trace.update(status=f"ERROR:{type(exc).__name__}",
                     ms=round((time.perf_counter() - started) * 1000, 2))
        _ledger("ERROR")
        return None, trace
    trace["ms"] = round((time.perf_counter() - started) * 1000, 2)
    try:
        data = json.loads(_json_only(text))
    except (TypeError, ValueError):
        trace["status"] = "INVALID_JSON"
        _ledger("OK", text, used=False)
        return None, trace
    frame = validate_llm_frame(data, language=_language(message))
    trace["status"] = "OK" if frame else "INVALID_FRAME"
    _ledger("OK", text, used=frame is not None)
    return frame, trace


def _json_only(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


async def _ollama_generate(message: str, timeout: float | None = None) -> str:
    import httpx

    from app.llm.config import ollama_host, ollama_model
    from app.security import guardrails

    body = {
        "model": ollama_model(),
        "messages": [{"role": "system", "content": _SYSTEM + " " + guardrails.UNTRUSTED_NOTICE},
                     {"role": "user", "content": "QUESTION: " + str(message)[:400]}],
        "stream": False, "format": "json",
        "options": {"temperature": 0.0, "num_predict": 64},
    }
    async with httpx.AsyncClient(timeout=(timeout or llm_timeout_seconds()) + 0.5) as client:
        response = await client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body)
        response.raise_for_status()
        return str((response.json().get("message") or {}).get("content") or "")


__all__ = ["CONFIDENT", "Object", "Party", "Qualifier", "Resolved", "Scope", "SemanticFrame",
           "Task", "clarification", "concepts_in", "is_knowledge", "llm_enabled", "llm_frame",
           "parse", "reload", "resolve_referents", "route", "validate_llm_frame"]
