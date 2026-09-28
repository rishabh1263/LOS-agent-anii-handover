"""
SHORT / AMBIGUOUS QUERIES -- "name", "status", "documents", "PAN" ...

ONE WORD HAS SEVERAL MEANINGS. "name" may be the name on the application,
the name on the PAN card or the co-applicant's name; "status" the
application's status, a document's verification or the stage. Mapping it
to the first tool that fits would be a guess published as an answer.

CONTEXT FIRST, WHEN IT SETTLES THE MATTER: after a documents answer,
"status" is the documents' verification status; after a stage answer,
"documents" are the documents this stage requires; after an answer about
one document, "name" is the name on that document. The settled question is
then understood exactly as if it had been typed. Otherwise the reply is a
question naming the meaningful choices -- and no tool, retrieval or model
runs for it. Nothing here ever guesses the field the person meant.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*|[ऀ-ॿ]+", re.IGNORECASE)
_FILLER = frozenset("""my mine our the a an please pls plz tell me show give kya hai hain mera meri
mere ka ki ke batao bata do dikhao about of for and is what""".split())

#: Ambiguous heads -> the choices, each a full question the service answers.
AMBIGUOUS: dict[str, dict[str, Any]] = {
    "name": {
        "question": ("Do you mean the name on your application, the name on your PAN, "
                     "or the co-applicant's name?"),
        "options": ["What name is on my application?", "What name is on my PAN?",
                    "What is the co-applicant's name?"]},
    "status": {
        "question": ("Do you mean your application status, your document verification "
                     "status, or the current stage?"),
        "options": ["What is my application status?", "Are my documents verified?",
                    "What stage is my application at?"]},
    "documents": {
        "question": ("About the documents, do you mean the list that is required, the ones "
                     "still pending, the ones already submitted, or their verification status?"),
        "options": ["What documents are required?", "Which documents are pending?",
                    "Which documents have been submitted?", "Are my documents verified?"]},
    "income": {
        "question": ("Do you mean the income declared on your application, the income "
                     "shown on your salary slip, or the income evidence from your bank "
                     "statement?"),
        "options": ["What income did I declare?", "What salary is on my salary slip?",
                    "Does my bank statement support my income?"]},
    "address": {
        "question": ("Do you mean the address on your application, the address on your "
                     "ID document, or the address proof that is required?"),
        "options": ["What address is on my application?", "What address is on my PAN?",
                    "Is address proof required?"]},
    "mobile": {
        "question": "Do you mean the mobile number on your application, or updating it?",
        "options": ["What mobile number is on my application?", "Update my mobile number"]},
    "pan": {
        "question": ("Do you mean the PAN number on your application, whether your PAN "
                     "document is verified, or the name on your PAN?"),
        "options": ["What is my PAN number?", "Is my PAN verified?", "What name is on my PAN?"]},
    "stage": {
        "question": ("About the stage: which stage the case is at, or what is required at "
                     "this stage?"),
        "options": ["What stage is my application at?",
                    "What documents are required at this stage?"]},
}
_ALIASES = {"doc": "documents", "docs": "documents", "document": "documents",
            "paperwork": "documents", "papers": "documents", "kagaz": "documents",
            "phone": "mobile", "number": "mobile", "salary": "income", "naam": "name",
            "pata": "address", "sthiti": "status", "state": "status", "step": "stage"}

#: (head, the previous turn's object) -> the whole question the head means.
_BY_CONTEXT: dict[tuple[str, str], str] = {
    ("status", "DOCUMENTS"): "Are my documents verified?",
    ("status", "DOCUMENT"): "Is that document verified?",
    ("status", "APPLICATION"): "What is my application status?",
    ("status", "STAGE"): "What stage is my application at?",
    ("documents", "STAGE"): "What documents are required at this stage?",
    ("name", "DOCUMENT"): "What name is on that document?",
    ("address", "DOCUMENT"): "What address is on that document?",
    ("pan", "DOCUMENT"): "Is my PAN verified?",
}
_AGAIN = {"LIST_REQUIREMENTS": "What documents are required?",
          "LIST_PENDING": "Which documents are pending?",
          "LIST_SUBMITTED": "Which documents have been submitted?",
          "CHECK_VERIFICATION": "Are my documents verified?"}


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text or "")
    return [t.lower().strip("'-") for t in _WORD.findall(text) if t.strip("'-")]


def short_head(message: str) -> str | None:
    """The ambiguous head of a very short query, or None for a real question."""
    raw = _tokens(message)
    if not raw or len(raw) > 2:
        return None                       # a real question, not a bare word
    tokens = [t for t in raw if t not in _FILLER]
    if len(tokens) != 1:
        return None
    head = _ALIASES.get(tokens[0], tokens[0])
    return head if head in AMBIGUOUS else None


@dataclass
class ShortQuery:
    head: str
    #: The whole question the context settled it to, when it did.
    resolved_to: str | None = None
    resolved_by: str | None = None
    clarification: dict[str, Any] | None = None


def _readable_slot(slot: str) -> str:
    text = str(slot or "").replace("_", " ").strip().lower()
    return text.upper() if text.upper() in ("PAN", "ITR", "DL", "KYC") else text


def short_query(message: str, context: Any) -> ShortQuery | None:
    """
    Resolve or clarify a one-word query. None when the message is a real
    question. With no settling context the result is the clarification.
    """
    head = short_head(message)
    if head is None:
        return None
    last_object = str(getattr(context, "last_object", None) or "").upper()
    last_task = str(getattr(context, "last_task", None) or "").upper()
    last_slot = getattr(context, "last_slot", None)

    if last_slot and head in ("name", "address"):
        return ShortQuery(head, resolved_to=f"What {head} is on my {_readable_slot(last_slot)}?",
                          resolved_by=f"the previous answer was about {last_slot}")
    if head == "documents" and last_task in _AGAIN:
        return ShortQuery(head, resolved_to=_AGAIN[last_task],
                          resolved_by=f"the previous question was {last_task}")
    settled = _BY_CONTEXT.get((head, last_object))
    if settled:
        if last_object == "DOCUMENT" and last_slot:
            settled = settled.replace("that document", f"my {_readable_slot(last_slot)}")
        return ShortQuery(head, resolved_to=settled,
                          resolved_by=f"the previous answer was about {last_object}")

    choice = AMBIGUOUS[head]
    return ShortQuery(head, clarification={
        "reason": "AMBIGUOUS_SHORT_QUERY", "head": head,
        "question": choice["question"], "options": list(choice["options"]),
        "original_message": str(message or "").strip()[:200]})


__all__ = ["AMBIGUOUS", "ShortQuery", "short_head", "short_query"]
