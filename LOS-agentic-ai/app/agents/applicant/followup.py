"""
"Why?" — and knowing what it refers to.

THE PROBLEM. A field officer asks "which documents are missing?", is told
the address proof is, and types "why?". On its own that message means
nothing: it matches no pattern, becomes UNKNOWN, and the copilot asks what
they meant about a question it answered thirty seconds ago. Every real
conversation does this, and a copilot that cannot follow one is a search box
with a chat window around it.

HOW IT IS RESOLVED, AND WHY THAT WAY. The service holds no conversation
state. It is stateless by design -- horizontally scalable, and nothing about
one officer's session can leak into another's. So the CALLER carries the
context: each response returns a small `context` block, and the client sends
it back on the next request. The service resolves the follow-up against
what it is handed.

THAT MAKES THE CONTEXT UNTRUSTED INPUT, and it is treated as such. It can
only ever REWRITE A MESSAGE into another message, which is then classified
by exactly the same patterns as anything a person typed. It cannot select an
intent, skip a permission check, name a case, or reach a tool. The worst a
forged context can do is cause the copilot to answer a different FOS
question about the case the caller is already authorised for -- and
authorisation is checked afterwards, against the token, as it is for every
request.

THE REWRITE IS ALWAYS REPORTED. The response says what the follow-up was
taken to mean, so an officer who is misunderstood can see it immediately
rather than wondering why the answer does not match the question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

#: A message that cannot stand on its own.
#:
#: Deliberately short. A phrase that MIGHT be a standalone question is not
#: on this list: rewriting "why is this not ready?" against a stale context
#: would answer a question nobody asked, which is worse than not following
#: up at all.
_BARE_WHY = re.compile(
    r"^\s*(why|why\s+(is|are|was|were)\s+(that|this|it|they)"
    r"|why\s+though|but\s+why|how\s+come|for\s+what\s+reason)"
    r"\s*[?.!]*\s*$",
    re.IGNORECASE,
)

_BARE_MORE = re.compile(
    r"^\s*(tell\s+me\s+more|more\s+detail(s)?|go\s+on|expand"
    r"|explain\s+(that|this|it)|elaborate)\s*[?.!]*\s*$",
    re.IGNORECASE,
)

_BARE_WHAT_NOW = re.compile(
    r"^\s*(and\s+)?(now\s+what|what\s+now|then\s+what|what\s+next"
    r"|so\s+what\s+do\s+i\s+do)\s*[?.!]*\s*$",
    re.IGNORECASE,
)

#: "And the passport?" -- a new subject, the same question as before.
_BARE_SUBJECT = re.compile(
    r"^\s*(and|what\s+about|how\s+about)\s+(the\s+)?"
    r"([A-Za-z][A-Za-z _-]{2,40}?)\s*[?.!]*\s*$",
    re.IGNORECASE,
)


#: "Which document?", "which one?", "which ones?" -- asking the previous
#: answer to name what it was about. Meaningless on its own.
_BARE_WHICH = re.compile(
    r"^\s*(and\s+)?(which|what)\s+(one|ones|document|documents|doc|docs)"
    r"(\s+(is|was|were|are)\s+(it|that|they|this))?\s*[?.!]*\s*$",
    re.IGNORECASE,
)


#: "What about it?" -- too short for the subject pattern above, and only
#: ever a reference back.
_BARE_PRONOUN = re.compile(
    r"^\s*(and|what\s+about|how\s+about)\s+()(it|that|this|that\s+one|"
    r"this\s+one)\s*[?.!]*\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Context:
    """What the last exchange was about. Supplied by the caller."""

    last_query_type: str | None = None
    last_intent: str | None = None
    #: The checklist slot or document type the last answer was about, when
    #: it was about one.
    last_slot: str | None = None
    #: The party ROLE the last answer was about (PRIMARY_APPLICANT,
    #: CO_APPLICANT, BOTH). A label, never an id: the party is always read
    #: from the case record.
    last_subject: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "Context":
        """
        Read a caller-supplied context defensively.

        Anything unexpected becomes absent rather than raising: a malformed
        context must degrade to "no context", never to an error on a
        question the officer could otherwise have had answered.
        """
        if not isinstance(payload, Mapping):
            return cls()

        def text(key: str, limit: int = 64) -> str | None:
            value = payload.get(key)
            if not isinstance(value, str):
                return None
            value = value.strip()[:limit]
            return value or None

        slot = text("last_slot")
        subject = (text("last_subject") or "").upper()
        return cls(
            last_query_type=text("last_query_type"),
            last_intent=text("last_intent"),
            # Normalised the way a slot name is written, so a client echoing
            # "address proof" and one echoing "ADDRESS_PROOF" behave alike.
            last_slot=(re.sub(r"[^A-Z0-9]+", "_", slot.upper()).strip("_")
                       if slot else None),
            last_subject=(subject if subject in {"PRIMARY_APPLICANT",
                                                 "CO_APPLICANT", "BOTH"}
                          else None),
        )

    def is_empty(self) -> bool:
        return not (self.last_query_type or self.last_intent or self.last_slot
                    or self.last_subject)


@dataclass(frozen=True)
class Resolution:
    """What a follow-up was taken to mean."""

    message: str
    #: None when the message was already self-contained.
    rewritten_from: str | None = None
    reason: str | None = None

    @property
    def followed_up(self) -> bool:
        return self.rewritten_from is not None

    def public(self) -> dict[str, Any] | None:
        if not self.followed_up:
            return None
        return {
            "original_message": self.rewritten_from,
            "interpreted_as": self.message,
            "reason": self.reason,
        }


#: Names that read as a typo in lower case.
_ACRONYMS = frozenset({"PAN", "ITR", "DL", "KYC", "NOC", "GST", "CPA"})


def _readable(slot: str) -> str:
    words = str(slot or "").replace("_", " ").split()
    return " ".join(word.upper() if word.upper() in _ACRONYMS
                    else word.lower()
                    for word in words)


#: Every pattern that marks a message as unable to stand on its own.
_BARE = (_BARE_WHY, _BARE_MORE, _BARE_WHAT_NOW, _BARE_SUBJECT, _BARE_WHICH)


def needs_context(message: str) -> bool:
    """
    Whether this message names nothing and can only point back.

    Narrower than `is_bare`: "which document?" has no subject at all, so no
    rule or semantic example may guess one for it -- unresolved, it is a
    clarification.
    """
    return bool(_BARE_WHICH.match((message or "").strip()))


def is_bare(message: str) -> bool:
    """
    Whether this message only means something as a follow-up.

    USED TO KEEP IT AWAY FROM RETRIEVAL. An unresolved "why?" used to fall
    through to the knowledge base, which scored it confident enough to
    answer and returned a paragraph of FOS handbook. One word cannot be a
    question about the handbook, and a confident irrelevant answer to it
    is worse than admitting the reference was not understood -- so a bare
    follow-up that could not be resolved goes straight to the
    clarification instead.
    """
    text = (message or "").strip()
    # "Why this answer?" with nothing to explain is bare too.
    return bool(text) and any(pattern.match(text)
                              for pattern in (*_BARE, _WHY_ANSWER))


def resolve(message: str, context: Context | None) -> Resolution:
    """
    Expand a bare follow-up into a question that stands on its own.

    Returns the message unchanged whenever it already does, whenever there
    is no context to resolve against, or whenever the context does not
    carry what the follow-up needs. Guessing in any of those cases would
    answer a question the officer did not ask.
    """
    text = (message or "").strip()
    if not text or context is None or context.is_empty():
        return Resolution(message=text)

    # ONLY A SLOT THIS SERVICE RECOGNISES.
    #
    # The context is caller-supplied, so `last_slot` is whatever a client
    # sent. A forged "CIBIL_SCORE" turned "why?" into "why is cibil score
    # required for this application?" -- which the classifier correctly
    # routed downstream and refused, so nothing leaked, but the copilot had
    # still put words in the officer's mouth and echoed a made-up subject
    # back at them. Checking the slot first means an unrecognised one is
    # simply not resolved against, and "why?" falls through to the
    # clarification that asks what they meant.
    slot = context.last_slot if _is_known(context.last_slot or "") else None

    if _BARE_WHY.match(text):
        if slot:
            return Resolution(
                message=f"Why is {_readable(slot)} required for this application?",
                rewritten_from=text,
                reason=f"the previous answer was about {slot}",
            )
        # No slot, but we know the last answer was about the checklist, so
        # "why?" is asking about the policy behind it.
        if context.last_query_type == "POLICY_REQUIREMENT":
            return Resolution(
                message="Why does the checklist require these documents?",
                rewritten_from=text,
                reason="the previous answer was about the document checklist",
            )
        if context.last_query_type in {"CASE_FACT", "DOCUMENT_STATUS"}:
            return Resolution(
                message="What is pending on this case?",
                rewritten_from=text,
                reason="the previous answer was about this case's state",
            )
        return Resolution(message=text)

    if _BARE_MORE.match(text):
        if context.last_query_type == "POLICY_REQUIREMENT":
            return Resolution(
                message="Which policy rules applied to this case?",
                rewritten_from=text,
                reason="the previous answer was about the document checklist",
            )
        if context.last_query_type == "DOCUMENT_STATUS":
            return Resolution(
                message="Show me all document issues.",
                rewritten_from=text,
                reason="the previous answer was about document verification",
            )
        return Resolution(message=text)

    if _BARE_WHAT_NOW.match(text):
        return Resolution(
            message="What should I do next?",
            rewritten_from=text,
            reason="a bare follow-up asking for the next step",
        )

    # "WHICH DOCUMENT?" -- the previous answer, asked to name its document.
    #
    # ONE DOCUMENT, ONLY WHEN THE CONTEXT NAMES EXACTLY ONE (a mismatch
    # between two documents names none, by design -- context_from_response).
    # Otherwise the previous question is asked again for its documents, in a
    # phrasing the classifier answers from the records: the answer lists
    # what was recorded, and nothing is chosen for the officer.
    if _BARE_WHICH.match(text):
        pending_like = {"DOCUMENTS_PENDING", "DOCUMENTS_MISSING",
                        "DOCUMENTS_REQUIRED", "PENDING_ITEMS"}
        if slot:
            if context.last_intent in pending_like:
                return Resolution(
                    message=f"Is {_readable(slot)} still pending?",
                    rewritten_from=text,
                    reason=f"the previous answer was about {slot}",
                )
            return Resolution(
                message=f"Has the {_readable(slot)} been verified?",
                rewritten_from=text,
                reason=f"the previous answer was about {slot}",
            )
        narrowed = {
            "CASE_HISTORY": "Which documents caused this?",
            "APPLICATION_STATUS": "Which documents caused this?",
            "DOCUMENT_VERIFICATION": "Which documents need attention?",
            "DOCUMENTS_UPLOADED": "Which documents need attention?",
        }.get(context.last_intent or "")
        if narrowed is None and context.last_intent in pending_like:
            narrowed = "Which documents are still pending?"
        if narrowed:
            return Resolution(
                message=narrowed, rewritten_from=text,
                reason="the previous answer was about more than one document",
            )
        return Resolution(message=text)

    # "WHY THIS ANSWER?" -- the previous question, asked again from a fixed
    # template (never free text from the context), so its provenance can be
    # explained. The Copilot recognises EXPLAIN_REASON and answers with the
    # explanation of that answer's sources (provenance.explain).
    if _WHY_ANSWER.match(text):
        again = _asked_again(context, slot)
        return Resolution(message=again or text,
                          rewritten_from=text, reason=EXPLAIN_REASON)

    # "WHAT ABOUT MY CO-APPLICANT?" -- the previous question, asked about
    # another party. Only a ROLE is carried forward: who holds it is read
    # from the case record when the rewritten question is answered, so a
    # context naming a co-applicant on a case that has none gets "there is
    # no co-applicant", never an answer about somebody.
    switched = _subject_switch(text, context)
    if switched is not None:
        return switched

    match = _BARE_SUBJECT.match(text) or _BARE_PRONOUN.match(text)

    # "WHAT ABOUT THE DOCUMENT?" -- a pronoun for the last answer's subject.
    # Resolved only against a slot this service recognises (checked
    # above), and phrased as a question the classifier already answers.
    if match and slot and _refers_back(match.group(3)):
        if context.last_intent in {"DOCUMENTS_PENDING", "DOCUMENTS_MISSING",
                                   "DOCUMENTS_REQUIRED", "APPLICATION_STATUS"}:
            return Resolution(
                message=f"Is {_readable(slot)} still pending?",
                rewritten_from=text,
                reason=f"the previous answer was about {slot}",
            )
        return Resolution(
            message=f"Has the {_readable(slot)} been verified?",
            rewritten_from=text,
            reason=f"the previous answer was about {slot}",
        )

    if match and context.last_intent:
        subject = re.sub(r"[^A-Z0-9]+", "_",
                         match.group(3).upper()).strip("_")
        # ONLY a subject the service actually knows. "And the weather?"
        # must not become a document question, and a subject that is not a
        # configured type would produce a question about nothing.
        if subject and _is_known(subject):
            # PHRASED AS A QUESTION THE CLASSIFIER ALREADY HANDLES. "What
            # is the status of the passport?" reads naturally and matches
            # nothing -- it fell through to the knowledge base, which
            # answered with a paragraph about what document states mean.
            # The rewrite target has to be a phrasing that works, not the
            # one that reads best.
            return Resolution(
                message=f"Has the {_readable(subject)} been verified?",
                rewritten_from=text,
                reason=f"the previous question, asked about {subject}",
            )

    return Resolution(message=text)


#: "Why this answer?", "how do you know that?" -- asking for the basis of the
#: previous answer. Recognised by the Copilot through EXPLAIN_REASON.
_WHY_ANSWER = re.compile(
    r"^\s*(why\s+(this|that)\s+answer|why\s+(are|do)\s+you\s+(saying|say)\s+"
    r"(this|that)|how\s+do\s+you\s+know(\s+(that|this))?|what\s+is\s+(this|"
    r"that)\s+based\s+on|where\s+did\s+you\s+get\s+(this|that)(\s+from)?|"
    r"what('?s|\s+is)\s+your\s+source)\s*[?.!]*\s*$",
    re.IGNORECASE)

EXPLAIN_REASON = "asked why the previous answer was given"

#: The previous question, by its intent -- phrasings the classifier answers.
_AGAIN = {
    "CASE_HISTORY": "Why is my application under review?",
    "APPLICATION_STATUS": "What is my application status?",
    "APPLICATION_STAGE": "What stage am I in?",
    "DOCUMENTS_PENDING": "What is pending?", "PENDING_ITEMS": "What is pending?",
    "DOCUMENTS_MISSING": "What is pending?",
    "DOCUMENT_VERIFICATION": "Which documents need attention?",
    "DOCUMENTS_UPLOADED": "Which documents need attention?",
    "NEXT_ACTION": "What should I do now?",
    "READINESS": "Is my application ready for CPA?",
}


def _asked_again(context: Context, slot: str | None) -> str | None:
    intent = context.last_intent or ""
    if context.last_subject in _WHO:
        who, who_s = _WHO[context.last_subject]
        for intents_, template in _FOR_SUBJECT:
            if intent in intents_:
                return template.format(who=who, who_s=who_s)
    if intent in ("DOCUMENT_VERIFICATION",) and slot:
        return f"Has the {_readable(slot)} been verified?"
    return _AGAIN.get(intent)


#: "And her documents?", "what about his?" -- a pronoun for the last
#: answer's PARTY. Resolved only against a party role the context carries.
_BARE_PARTY_PRONOUN = re.compile(
    r"^\s*(and|what\s+about|how\s+about)?\s*(his|her|their|him|them)"
    r"(\s+(documents?|docs?|status|issues?|side|ones?))?\s*[?.!]*\s*$",
    re.IGNORECASE)

#: How each kind of previous question is asked about a named party. Each
#: is a phrasing the classifier already answers; nothing here answers it.
_FOR_SUBJECT = (
    ({"CASE_HISTORY", "APPLICATION_STATUS", "FULL_SUMMARY"},
     "What issues are recorded for {who}?"),
    ({"DOCUMENTS_PENDING", "DOCUMENTS_MISSING", "PENDING_ITEMS",
      "COMPLETENESS", "NEXT_ACTION"}, "What is pending for {who}?"),
    ({"READINESS"}, "Is {who} ready for CPA?"),
    ({"DOCUMENT_VERIFICATION", "DOCUMENTS_UPLOADED"},
     "Are {who_s} documents verified?"),
)

_WHO = {"CO_APPLICANT": ("the co-applicant", "the co-applicant's"),
        "PRIMARY_APPLICANT": ("the primary applicant",
                              "the primary applicant's"),
        "BOTH": ("both applicants", "both applicants'")}


def _subject_switch(text: str, context: Context) -> Resolution | None:
    from app.agents.applicant import subjects

    match = _BARE_SUBJECT.match(text)
    role = subjects.mentioned(match.group(3)) if match else None
    if match and role is None and re.fullmatch(
            r"\s*(me|myself|mine|the\s+applicant)\s*", match.group(3), re.I):
        role = subjects.Kind.PRIMARY
    pronoun = _BARE_PARTY_PRONOUN.match(text)
    if role is None and pronoun \
            and context.last_subject in ("CO_APPLICANT", "PRIMARY_APPLICANT"):
        role = subjects.Kind(context.last_subject)
    if role is None or not context.last_intent:
        return None
    # "AND HER DOCUMENTS?" asks about the documents, whatever the last
    # question was: the noun the person typed outranks the question before.
    noun = (pronoun.group(4) if pronoun else "") or ""
    if noun.lower().startswith("doc"):
        who, who_s = _WHO[role.value]
        return Resolution(message=f"Are {who_s} documents verified?",
                          rewritten_from=text,
                          reason=f"asked about {who_s} documents")
    for intents_, template in _FOR_SUBJECT:
        if context.last_intent in intents_:
            who, who_s = _WHO[role.value]
            return Resolution(
                message=template.format(who=who, who_s=who_s),
                rewritten_from=text,
                reason=f"the previous question, asked about {who}",
            )
    return None


#: Words that point back at the last answer's subject.
_BACK_REFERENCES = frozenset({
    "it", "that", "this", "document", "documents", "doc", "docs",
    "that document", "this document", "that doc", "this doc", "that one",
    "this one", "one", "same", "same document",
})


def _refers_back(subject: str) -> bool:
    return " ".join(str(subject or "").lower().split()) in _BACK_REFERENCES


def _is_known(subject: str) -> bool:
    """Whether a named subject is a configured document type or slot."""
    try:
        from app.agents.applicant.facts import fact_set

        return subject in fact_set(None).mentionable() or _in_any_product(subject)
    except Exception:  # pragma: no cover - configuration failure
        return False


def _in_any_product(subject: str) -> bool:
    from app.agents.applicant import config

    try:
        if subject in {str(t).upper() for t in config.document_types()}:
            return True
        for product in config.products():
            for entry in config.checklist_for(
                    None if product == "default" else product):
                if subject == entry["slot"] or subject in entry["accepts"]:
                    return True
    except Exception:  # pragma: no cover - configuration failure
        return False
    return False


def context_from_response(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """
    The context block a client should send back with the next question.

    Built from the response that is going out, so a client never has to
    work out what "the last answer was about" for itself.
    """
    slot = None
    checklist = envelope.get("checklist") or []
    # The first row that needs something done to it is what the officer is
    # most likely asking about next.
    for state in ("FAILED", "UNDER_REVIEW", "MISSING"):
        row = next((e for e in checklist
                    if isinstance(e, Mapping)
                    and e.get("fulfilment") == state
                    and e.get("mandatory", True)), None)
        if row:
            slot = row.get("slot")
            break
    # NO CHECKLIST ON THIS ANSWER (a concise case-history reply carries
    # none): the document the answer itself cited is what "the document"
    # in the next question means.
    if slot is None:
        slot = _only_implicated_document(envelope)

    subject = envelope.get("subject") or {}
    return {
        "last_query_type": envelope.get("query_type"),
        "last_intent": envelope.get("intent"),
        "last_slot": slot,
        # The party role the answer was about, for "and her documents?".
        "last_subject": (subject.get("kind")
                         if isinstance(subject, Mapping) else None),
    }


def _only_implicated_document(envelope: Mapping[str, Any]) -> str | None:
    """
    The one document the answer's recorded problem is about -- or None.

    ONLY WHEN UNAMBIGUOUS. A name mismatch between the PAN and the bank
    statement implicates two documents, and "the document" after it could
    be either: resolving it to one would answer a question the officer may
    not have asked, so it is left for the clarification to settle.
    """
    types: set[str] = set()
    memory = envelope.get("case_memory") or {}
    for finding in (memory.get("findings") or []) if isinstance(memory, Mapping) else []:
        if not isinstance(finding, Mapping):
            continue
        if str(finding.get("status") or "").upper() in {"PASS", "SKIPPED"}:
            continue
        for field in finding.get("comparisons") or []:
            for source in (field or {}).get("sources") or []:
                if isinstance(source, Mapping) and source.get("document_type"):
                    types.add(str(source["document_type"]).upper())
        if finding.get("document_type"):
            types.add(str(finding["document_type"]).upper())
    for source in envelope.get("sources") or []:
        if isinstance(source, Mapping) and source.get("document_type"):
            types.add(str(source["document_type"]).upper())
    return next(iter(types)) if len(types) == 1 else None


__all__ = ["Context", "Resolution", "context_from_response", "is_bare",
           "resolve"]
