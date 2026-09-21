"""
Knowledge questions whose answer is configuration, not prose.

WHY THIS IS SEPARATE FROM RAG. "What can be used as address proof?" has an
exact answer, and it is not in a markdown file -- it is the `accepts` list on
the ADDRESS_PROOF slot in the checklist configuration. Retrieval returns the
paragraph ABOUT address proof, which correctly explains that the slot is not
a document type, and leaves the officer without the three words they asked
for.

Worse, the two can drift. Someone adds AADHAAR to the accepted list in
configuration; the markdown still says three documents; retrieval keeps
answering three. The handbook would then be confidently wrong about this
service's own behaviour.

So a question of this shape is answered from the SAME configuration the
checklist is built from, and the retrieved passage is used for the
explanation around it. The answer cannot disagree with what the upload
endpoint will actually accept, because it is read from the same place.
"""

from __future__ import annotations

import re

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


def join(values: list[str]) -> str:
    """"A, B or C" -- the way a person reads a list of alternatives."""
    names = [label(v) for v in values]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def product_in(message: str) -> str | None:
    """
    The product the question names, if it names one.

    "What documents are required for a personal loan?" is asking about
    PERSONAL_LOAN, and answering it from the `default` checklist gave a
    shorter list than the product actually requires -- it omitted the bank
    statement. A wrong-but-plausible list of required documents is worse than
    no list, because the officer collects to it and arrives short.
    """
    text = re.sub(r"[^a-z]+", " ", (message or "").lower())
    try:
        products = [p for p in config.products() if p != "default"]
    except Exception:
        return None

    for product in products:
        words = product.lower().replace("_", " ")
        if words in text:
            return product
    return None


def _slots(product: str | None) -> list[dict]:
    try:
        return config.checklist_for(product)
    except Exception:
        return []


#: Questions asking what satisfies a named slot.
_SLOT_QUESTION = re.compile(
    r"\b(what|which)\b.{0,40}\b(can|could|may|is|are)\b.{0,20}"
    r"\b(used|use|accepted|acceptable|valid|submit|count)\b",
    re.IGNORECASE,
)

#: Questions asking what a product requires.
_REQUIRED_QUESTION = re.compile(
    r"\b(what|which)\s+documents?\b.{0,30}\b(required|need|needed|mandatory)\b",
    re.IGNORECASE,
)


def _named_slot(message: str, product: str | None) -> str | None:
    """The checklist slot the question names, if any."""
    text = (message or "").upper().replace("-", "_")
    compact = re.sub(r"[^A-Z0-9]+", "", text)

    for entry in _slots(product):
        slot = str(entry.get("slot") or "")
        if not slot:
            continue
        if slot in text or re.sub(r"[^A-Z0-9]+", "", slot) in compact:
            return slot
    return None


def answer_for(message: str, product: str | None = None) -> str | None:
    """
    A configuration-backed answer, or None to leave it to retrieval.

    None is the common case and the right default. This handles the two
    question shapes where an exact list beats a paragraph, and declines
    everything else rather than inventing coverage.
    """
    if not (message or "").strip():
        return None

    # The question may name its own product; that beats the caller's default.
    product = product_in(message) or product
    slots = _slots(product)
    if not slots:
        return None

    # "What can be used as address proof?"
    slot = _named_slot(message, product)
    if slot and _SLOT_QUESTION.search(message):
        entry = next((e for e in slots if e.get("slot") == slot), None)
        if entry:
            accepts = list(entry.get("accepts") or [])
            if accepts and accepts != [slot]:
                required = ("required" if entry.get("mandatory", True)
                            else "optional")
                return (
                    f"{join(accepts)} can satisfy the {slot} requirement. "
                    f"Any one of them is enough, and {slot} is {required} "
                    f"for this product."
                )
            return (
                f"{label(slot)} satisfies the {slot} requirement."
            )

    # "What documents are required for a personal loan?"
    if _REQUIRED_QUESTION.search(message):
        mandatory = [e for e in slots if e.get("mandatory", True)]
        optional = [e for e in slots if not e.get("mandatory", True)]
        if not mandatory:
            return None

        parts = []
        for entry in mandatory:
            accepts = list(entry.get("accepts") or [])
            slot_name = entry.get("slot")
            if accepts and accepts != [slot_name]:
                parts.append(f"{slot_name} ({join(accepts)})")
            else:
                parts.append(label(slot_name))

        answer = "Required: " + ", ".join(parts) + "."
        if optional:
            answer += (" Optional: "
                       + ", ".join(label(e["slot"]) for e in optional) + ".")
        return answer

    return None


__all__ = ["answer_for", "join", "label", "product_in"]
