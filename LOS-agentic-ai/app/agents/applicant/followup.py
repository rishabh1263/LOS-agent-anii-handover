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


@dataclass(frozen=True)
class Context:
    """What the last exchange was about. Supplied by the caller."""

    last_query_type: str | None = None
    last_intent: str | None = None
    #: The checklist slot or document type the last answer was about, when
    #: it was about one.
    last_slot: str | None = None

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
        return cls(
            last_query_type=text("last_query_type"),
            last_intent=text("last_intent"),
            # Normalised the way a slot name is written, so a client echoing
            # "address proof" and one echoing "ADDRESS_PROOF" behave alike.
            last_slot=(re.sub(r"[^A-Z0-9]+", "_", slot.upper()).strip("_")
                       if slot else None),
        )

    def is_empty(self) -> bool:
        return not (self.last_query_type or self.last_intent or self.last_slot)


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
_BARE = (_BARE_WHY, _BARE_MORE, _BARE_WHAT_NOW, _BARE_SUBJECT)


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
    return bool(text) and any(pattern.match(text) for pattern in _BARE)


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

    match = _BARE_SUBJECT.match(text)
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

    return {
        "last_query_type": envelope.get("query_type"),
        "last_intent": envelope.get("intent"),
        "last_slot": slot,
    }


__all__ = ["Context", "Resolution", "context_from_response", "is_bare",
           "resolve"]
