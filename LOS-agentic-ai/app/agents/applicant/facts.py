"""
The authoritative answer to a configuration question.

ONE PLACE, READING ONE CONFIGURATION. The checklist a field officer is shown,
the types the upload endpoint will accept, the slots readiness waits on and
the answer the chatbot gives all come from `app/config/applicant_agent.yaml`
through this module. There is no second list to keep in step, so a
configuration change moves all four together or none of them.

WHY THIS EXISTS RATHER THAN LETTING RETRIEVAL ANSWER. A live query --
"what can be used as address proof?" -- came back saying utility bills, rent
agreements and ration cards. None of those is accepted. The handbook passage
retrieved for that question says, correctly:

    Utility bills, rent agreements and ration cards are commonly used as
    address proof in the industry, BUT this service has no classifier or
    extractor for them, so it cannot accept them.

A language model handed that passage and asked what can be used produced the
list and dropped the negation. The sentence is true; the answer built from it
was false, and it was false in the most expensive direction -- an officer
collects a utility bill, the upload is refused, and the applicant is asked
back for a document nobody should have requested.

So a question of this shape is not answered from prose at all. It is answered
from the same `accepts` list the upload endpoint enforces, which cannot
disagree with itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from app.agents.applicant import config

#: Human labels for document classes. `.title()` alone gives "Pan".
_LABELS = {
    "PAN": "PAN",
    "AADHAAR": "Aadhaar",
    "VOTER_ID": "Voter ID",
    "DRIVING_LICENCE": "Driving Licence",
    "PASSPORT": "Passport",
    "BANK_STATEMENT": "Bank Statement",
    "SALARY_SLIP": "Salary Slip",
    "ITR": "ITR",
    "FORM_16": "Form 16",
    "EMPLOYMENT_PROOF": "Employment Proof",
    "PHOTO": "Photograph",
    "SIGNATURE": "Signature",
}


def label(value: str) -> str:
    key = str(value or "").upper()
    return _LABELS.get(key, key.replace("_", " ").title())


def join(values) -> str:
    """"A, B or C" -- how a person reads a list of alternatives."""
    names = [label(v) for v in values]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


# ==========================================================================
# THE FACTS
# ==========================================================================

@dataclass(frozen=True)
class FactSet:
    """
    Everything authoritative about one product, in one object.

    Built from configuration on every request rather than cached across
    them: a configuration reload has to reach the chatbot answer, and a
    cached fact set is exactly how the answer and the enforcement drift.
    """

    product: str | None
    required_slots: tuple[str, ...]
    optional_slots: tuple[str, ...]
    accepts: dict[str, tuple[str, ...]]
    known_types: frozenset[str]
    known_slots: frozenset[str]

    def accepted_for(self, slot: str) -> tuple[str, ...]:
        return self.accepts.get(str(slot).upper(), ())

    def satisfies(self, slot: str, document_type: str) -> bool:
        return str(document_type).upper() in self.accepted_for(slot)

    def mentionable(self) -> frozenset[str]:
        """
        Every document term an answer may legitimately name.

        The grounding validator checks against this. A term outside it is
        either a document this service cannot process or one it has never
        heard of, and in an answer about what to collect both are the same
        mistake.
        """
        return self.known_types | self.known_slots


def fact_set(product: str | None = None) -> FactSet:
    """The authoritative facts for a product."""
    try:
        entries = config.checklist_for(product)
    except Exception:
        entries = []

    accepts: dict[str, tuple[str, ...]] = {}
    required: list[str] = []
    optional: list[str] = []

    for entry in entries:
        slot = str(entry.get("slot") or "").upper()
        if not slot:
            continue
        accepts[slot] = tuple(str(a).upper() for a in (entry.get("accepts") or []))
        (required if entry.get("mandatory", True) else optional).append(slot)

    try:
        known_types = frozenset(str(t).upper() for t in config.document_types())
    except Exception:
        known_types = frozenset()

    # EVERYTHING THE PRODUCT'S POLICY COULD EVER ASK FOR, not only what this
    # case's checklist resolved to.
    #
    # The checklist is amount- and attribute-dependent, so a perfectly
    # correct answer explaining that a larger loan would also need income
    # proof named a slot this case did not have, and the grounding
    # validator threw it away. The vocabulary a term is checked against has
    # to be the product's, not the case's -- narrowing it does not make the
    # validator stricter, it makes it wrong.
    try:
        from app.agents.policy.engine import vocabulary_for

        policy_slots, policy_types = vocabulary_for(product)
    except Exception:  # pragma: no cover - configuration failure
        policy_slots, policy_types = frozenset(), frozenset()

    return FactSet(
        product=product,
        required_slots=tuple(required),
        optional_slots=tuple(optional),
        accepts=accepts,
        known_types=(known_types | policy_types
                     | {t for types in accepts.values() for t in types}),
        known_slots=frozenset(accepts) | policy_slots,
    )


@lru_cache(maxsize=1)
def _products() -> tuple[str, ...]:
    try:
        return tuple(p for p in config.products() if p != "default")
    except Exception:
        return ()


def product_in(message: str) -> str | None:
    """
    The product the question names, if it names one.

    "What documents are required for a personal loan?" is asking about
    PERSONAL_LOAN. Answering it from the `default` checklist gave a shorter
    list than the product requires -- it omitted the bank statement -- and a
    wrong-but-plausible list is worse than none, because the officer collects
    to it and arrives short.
    """
    text = re.sub(r"[^a-z]+", " ", (message or "").lower())
    for product in _products():
        if product.lower().replace("_", " ") in text:
            return product
    return None


# ==========================================================================
# THE QUESTIONS THIS LAYER OWNS
# ==========================================================================

@dataclass(frozen=True)
class Fact:
    """An authoritative answer, and what it was built from."""

    text: str
    kind: str
    values: tuple[str, ...] = ()


#: "What can be used as / accepted for / valid for <slot>?"
_ACCEPTS_QUESTION = re.compile(
    r"\b(what|which)\b.{0,40}\b(can|could|may|is|are)\b.{0,24}"
    r"\b(used|use|accepted|acceptable|valid|submit|count|provide|give)\b"
    r"|\b(accepted|acceptable|allowed|valid)\s+(document|documents|type|types)\b"
    r"|\bwhat\s+(can|could|may)\s+(i|we|you)\s+(use|submit|provide|give)\b",
    re.IGNORECASE,
)

#: "Can I use a driving licence as address proof?"
_SATISFIES_QUESTION = re.compile(
    r"\b(can|could|may|will|does|is)\b.{0,24}\b(use|used|using|submit|count|"
    r"work|accepted|valid|ok)\b",
    re.IGNORECASE,
)

#: "What documents are required?"
_REQUIRED_QUESTION = re.compile(
    r"\b(what|which)\s+documents?\b.{0,30}"
    r"\b(required|require|need|needed|mandatory|must)\b"
    r"|\bdocuments?\s+(checklist|requirements)\b",
    re.IGNORECASE,
)

#: "What makes a case ready for CPA?"
_READINESS_QUESTION = re.compile(
    r"\b(what|when|how)\b.{0,40}\b(ready|readiness)\b.{0,20}\bcpa\b"
    r"|\bcpa\s+readiness\s+(mean|means|require|requires|rules)\b",
    re.IGNORECASE,
)


def _named_slot(message: str, facts: FactSet) -> str | None:
    text = (message or "").upper().replace("-", "_")
    compact = re.sub(r"[^A-Z0-9]+", "", text)
    for slot in facts.known_slots:
        if slot in text or re.sub(r"[^A-Z0-9]+", "", slot) in compact:
            return slot
    return None


def _named_unsupported(message: str) -> str | None:
    """
    A document this service cannot process, named in the question.

    Detected so the refusal can be written HERE, where the polarity is known.
    The handbook mentions utility bills in a sentence saying they are not
    accepted, and a model summarising that sentence produced the opposite.
    """
    from app.agents.applicant.grounding import UNSUPPORTED_TERMS

    text = (message or "").lower()
    for phrase in sorted(UNSUPPORTED_TERMS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}s?\b", text):
            return phrase.title()
    return None


def _named_type(message: str, facts: FactSet) -> str | None:
    """A document class the question names, matched on its human label too."""
    upper = (message or "").upper().replace("-", "_")
    lower = (message or "").lower()
    for document_type in sorted(facts.known_types, key=len, reverse=True):
        if document_type in upper:
            return document_type
        if label(document_type).lower() in lower:
            return document_type
    return None


def authoritative_answer(
    message: str,
    product: str | None = None,
) -> Fact | None:
    """
    The exact answer, or None to leave the question to retrieval.

    None is the common case and the right default. This owns the handful of
    question shapes whose answer is a configured value, and declines the rest
    rather than inventing coverage it does not have.
    """
    if not (message or "").strip():
        return None

    product = product_in(message) or product
    facts = fact_set(product)
    if not facts.accepts:
        return None

    slot = _named_slot(message, facts)

    # "Can I use a utility bill as address proof?"
    #
    # ANSWERED HERE, NOT BY RETRIEVAL, and this is the half that matters. The
    # handbook paragraph about address proof mentions utility bills in a
    # sentence saying they are NOT accepted; a model summarising it produced
    # the opposite. A refusal has a polarity, so it is written by something
    # that knows which way round it goes.
    unsupported = _named_unsupported(message)
    if slot and unsupported and _SATISFIES_QUESTION.search(message):
        accepted = facts.accepted_for(slot)
        return Fact(
            f"No. {unsupported} is not accepted by this service. "
            f"{join(accepted)} can satisfy the {slot} requirement.",
            kind="refusal", values=tuple(accepted),
        )

    # "Can I use a driving licence as address proof?"
    if slot and _SATISFIES_QUESTION.search(message):
        named = _named_type(message, facts)
        if named and named != slot:
            accepted = facts.accepted_for(slot)
            if named in accepted:
                return Fact(
                    f"Yes. {label(named)} satisfies the {slot} requirement.",
                    kind="satisfies", values=(named,),
                )
            return Fact(
                f"No. {label(named)} does not satisfy the {slot} "
                f"requirement. {join(accepted)} can.",
                kind="satisfies", values=tuple(accepted),
            )

    # "What can be used as address proof?"
    if slot and _ACCEPTS_QUESTION.search(message):
        accepted = facts.accepted_for(slot)
        if accepted and accepted != (slot,):
            return Fact(
                f"{join(accepted)} can satisfy the {slot} requirement.",
                kind="accepts", values=tuple(accepted),
            )
        if accepted:
            return Fact(
                f"{label(slot)} satisfies the {slot} requirement.",
                kind="accepts", values=tuple(accepted),
            )

    # "What documents are required for a personal loan?"
    if _REQUIRED_QUESTION.search(message):
        if not facts.required_slots:
            return None
        parts = []
        for name in facts.required_slots:
            accepted = facts.accepted_for(name)
            if accepted and accepted != (name,):
                parts.append(f"{name} ({join(accepted)})")
            else:
                parts.append(label(name))
        text = "Required: " + ", ".join(parts) + "."
        if facts.optional_slots:
            text += (" Optional: "
                     + ", ".join(label(s) for s in facts.optional_slots) + ".")
        return Fact(text, kind="required",
                    values=tuple(facts.required_slots) + tuple(
                        t for s in facts.required_slots
                        for t in facts.accepted_for(s)))

    # "What does a case need to be ready for CPA?"
    if _READINESS_QUESTION.search(message):
        rules = config.readiness_rules()

        # The slots are named only when the PRODUCT is known. Listing the
        # `default` checklist for a question that named no product told an
        # officer a personal loan needs two documents when it needs three.
        if product:
            documents = (f"every mandatory document is verified "
                         f"({join(facts.required_slots)})")
        else:
            documents = "every mandatory document for the product is verified"

        needs = []
        if rules.get("require_applicant_fields", True):
            needs.append("the applicant's details are captured")
        needs.append(documents)
        if rules.get("block_on_review", True):
            needs.append("nothing is left under review")

        return Fact(
            "A case is ready for CPA once "
            + ", ".join(needs[:-1]) + f" and {needs[-1]}. "
            "It is a FOS completeness check, not a credit decision.",
            kind="readiness", values=tuple(facts.required_slots),
        )

    return None


def is_configuration_question(message: str, product: str | None = None) -> bool:
    """Whether this layer owns the question outright."""
    return authoritative_answer(message, product) is not None


__all__ = [
    "Fact", "FactSet", "authoritative_answer", "fact_set",
    "is_configuration_question", "join", "label", "product_in",
]
