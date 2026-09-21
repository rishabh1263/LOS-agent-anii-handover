"""
Does this generated sentence say anything the data does not support?

WHAT THIS CATCHES, taken from a live failure. Asked "what can be used as
address proof?", the service answered:

    "Utility bills, rent agreements, and ration cards can be used as address
    proof."

Every word of that came from a retrieved passage. The passage said those
documents are common in the industry AND THAT THIS SERVICE CANNOT ACCEPT
THEM; the generated answer kept the list and dropped the negation. Retrieval
had done its job, the citation was real, and the answer was false.

That is the failure mode a grounded system actually has. It is not making
things up out of nothing -- it is reproducing a fragment of true context with
its meaning reversed, which is why "the chunks were relevant" is not evidence
of anything.

THE RULE HERE IS DELIBERATELY BLUNT: a FOS answer may not name a document
this service cannot process. Not as an example, not as a comparison, not as
something the industry does. An officer reading "utility bill" in an answer
about what to collect will collect one, and the upload will be refused.

Where the truthful answer genuinely needs to mention an unsupported document
-- "no, a utility bill is not accepted" -- that sentence is produced by the
deterministic path in `facts.py`, which knows it is a refusal. A model is not
trusted to hold the polarity.

VALIDATION FAILURE IS NOT AN ERROR. It falls back to the deterministic
answer, which was computed first and is always available. The caller gets a
correct answer and a `STRUCTURED` source; nobody is shown a failure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.agents.applicant.facts import FactSet, label

logger = logging.getLogger(__name__)

#: Documents people ask about that this service cannot process.
#:
#: Named explicitly so a rejection can say WHICH term was the problem. The
#: check does not depend on this list being complete -- anything outside the
#: configured taxonomy is caught by the positive check below -- but a named
#: term produces a log line somebody can act on.
UNSUPPORTED_TERMS: dict[str, str] = {
    "utility bill": "UTILITY_BILL",
    "electricity bill": "UTILITY_BILL",
    "telephone bill": "UTILITY_BILL",
    "gas bill": "UTILITY_BILL",
    "water bill": "UTILITY_BILL",
    "rent agreement": "RENT_AGREEMENT",
    "rental agreement": "RENT_AGREEMENT",
    "lease agreement": "RENT_AGREEMENT",
    "ration card": "RATION_CARD",
    "municipal": "MUNICIPAL_RECORD",
    "property tax": "PROPERTY_TAX_RECEIPT",
    "gas connection": "UTILITY_BILL",
    "post office": "POST_OFFICE_RECORD",
    "bank passbook": "PASSBOOK",
    "passbook": "PASSBOOK",
    "marriage certificate": "MARRIAGE_CERTIFICATE",
    "birth certificate": "BIRTH_CERTIFICATE",
    "nrega": "NREGA_CARD",
    "pension": "PENSION_DOCUMENT",
}

#: Verdict words an answer may use. A generated answer claiming a status
#: outside this set is describing something the pipeline does not produce.
KNOWN_STATUSES = frozenset({
    "PASS", "FAIL", "REVIEW", "SKIPPED", "PARTIAL",
    "VERIFIED", "REJECTED", "MISSING", "UPLOADED", "PROCESSING",
    "READY_FOR_CPA", "NOT_READY",
    "APPLICATION_CREATED", "DOCUMENT_COLLECTION",
    "BASIC_DOCUMENT_VERIFICATION",
    "DOCUMENT_TYPE_MISMATCH", "REQUIRED_FIELD_MISSING", "NOT_ESTABLISHED",
})

#: Capitalised words that are part of the vocabulary, not a document type.
#: Without these the check would reject its own domain language.
VOCABULARY = frozenset({
    "FOS", "CPA", "KYC", "RCU", "OCR", "API", "PDF", "ID", "NO", "YES",
    "AND", "OR", "NOT", "THE", "A", "AN",
})

#: Any run of capitals long enough to be a type or slot name.
#:
#: The underscore used to be mandatory, which let every single-word type
#: through -- PASSPORT, AADHAAR, PAN -- and those are exactly the ones a
#: model is most likely to name on a case that does not have them.
_CAPS = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)*\b")


@dataclass
class Verdict:
    """Whether a generated answer may be returned, and why not."""

    grounded: bool
    reasons: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.grounded


def _mentions(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}s?\b", text, re.IGNORECASE) is not None


def validate(
    answer: str,
    facts: FactSet,
    *,
    allow: frozenset[str] | None = None,
) -> Verdict:
    """
    Check a generated answer against the authoritative taxonomy.

    `allow` widens the permitted vocabulary for a specific answer -- a
    deterministic refusal that legitimately names an unsupported document
    passes it in. Nothing a model wrote ever sets it.
    """
    text = answer or ""
    if not text.strip():
        return Verdict(False, ["the generated answer was empty"])

    permitted = set(allow or ())
    reasons: list[str] = []
    unsupported: list[str] = []

    # -- named documents this service cannot process ----------------------
    for phrase, canonical in UNSUPPORTED_TERMS.items():
        if canonical in permitted:
            continue
        if _mentions(text, phrase):
            unsupported.append(phrase)
            reasons.append(
                f"names {phrase!r}, which this service cannot process"
            )

    # -- document types outside the configured taxonomy -------------------
    #
    # Any run of capitals, not only SCREAMING_SNAKE. Requiring an
    # underscore let every single-word type through -- PASSPORT, AADHAAR --
    # and those are the ones a model is most likely to name on a case that
    # does not have them. Domain acronyms are allowed by VOCABULARY.
    for token in set(_CAPS.findall(text)):
        if token in VOCABULARY or token in KNOWN_STATUSES:
            continue
        if token in facts.mentionable() or token in permitted:
            continue
        unsupported.append(token)
        reasons.append(f"names {token!r}, which is not a configured type or slot")

    return Verdict(not reasons, reasons, unsupported)


def validate_case_claims(
    answer: str,
    *,
    document_types: frozenset[str],
    statuses: frozenset[str],
) -> Verdict:
    """
    Check an answer about a CASE against what that case actually holds.

    Separate from the taxonomy check because the question is different: a
    document type can be perfectly valid and still not be on this case, and
    "your passport is verified" on a case with no passport is the more
    damaging of the two errors.
    """
    text = answer or ""
    reasons: list[str] = []

    for token in set(_CAPS.findall(text)):
        if token in VOCABULARY or token in KNOWN_STATUSES:
            continue
        if token in document_types:
            continue
        reasons.append(f"claims {token!r}, which is not on this case")

    for status in set(re.findall(r"\b(VERIFIED|REJECTED|REVIEW|MISSING)\b", text)):
        if status not in statuses:
            reasons.append(
                f"claims a document is {status}, which no document on this "
                "case is"
            )

    return Verdict(not reasons, reasons)


def describe(verdict: Verdict) -> str:
    """A log line. Never returned to a caller."""
    return "; ".join(verdict.reasons) or "grounded"


__all__ = [
    "KNOWN_STATUSES", "UNSUPPORTED_TERMS", "VOCABULARY", "Verdict",
    "describe", "validate", "validate_case_claims",
]
