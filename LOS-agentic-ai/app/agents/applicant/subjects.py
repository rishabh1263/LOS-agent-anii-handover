"""
WHO A QUESTION IS ABOUT -- the primary applicant, the co-applicant, or both.

    "What is pending for my co-applicant?"      -> CO_APPLICANT
    "Are both applicants verified?"             -> BOTH
    "Which applicant has the issue?"            -> BOTH (named per party)
    "What is pending?"                          -> CASE (the whole application)

THE WORDS PICK A ROLE; THE RECORD PICKS THE PERSON. The message is read for a
role only. Which party holds that role is read from the application record
(`applicant_id`, `co_applicant_id` -- app/store/models.py), never from the
question, the conversation or a model. A case with no co-applicant answers
"there is no co-applicant", whatever was asked or remembered.

ONE PARTY'S FACTS ARE THAT PARTY'S. A document is a party's by its stamped
owner (Document.owner_id / parties.owned_by), a finding by its `party_id`.
Nothing is attributed by filename, document type or guess, and two parties'
facts are never merged into one sentence about "the applicant".

WHAT IS NOT PER PARTY IS SAID SO. The document checklist, readiness and the
next action are kept for the APPLICATION (app/agents/los/response.py
party_section: "there is ONE decision on a loan"). A per-party question about
them is answered at application level and says that it is -- there is no
per-party policy to answer it from, and none is invented here.

AUTHORISATION IS NOT DECIDED HERE. The caller's case ownership is checked
before this runs (permissions.check_ownership); this module only confirms that
a named party belongs to that case, and refuses otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.agents.applicant.intents import Intent


class Kind(str, Enum):
    CASE = "CASE"
    PRIMARY = "PRIMARY_APPLICANT"
    CO = "CO_APPLICANT"
    BOTH = "BOTH"


_I = re.IGNORECASE
_POSSESSIVE = r"(?:(?:my|our|the|his|her|their|this)\s+)?"
_OWN = r"(?:'s|s'|’s)?"

#: The co-applicant, however it is written. "The other applicant" and "my
#: other applicant" are the co-applicant; "another applicant" is not
#: (that asks about somebody else, which the input guardrail refuses).
_CO = (r"(?:co[\s-]?(?:applicants?|apps?|borrowers?)\b|second\s+applicant|"
       r"joint\s+applicant|(?:the|my|our)\s+other\s+applicant)")
#: Both parties, or a question that asks WHICH of them.
_BOTH = (r"(?:both(?:\s+(?:of\s+)?(?:the\s+)?(?:applicants|parties|borrowers|"
         r"of\s+us|of\s+them))?(?!\s+(?:documents?|docs?|the\s+documents?|"
         r"pan|names?|papers?))"
         r"|each\s+(?:applicant|party|borrower)|all\s+(?:the\s+)?applicants"
         r"|every\s+applicant|which\s+(?:applicant|party|borrower|person)"
         r"|(?:the\s+)?applicant\s+and\s+(?:the\s+|my\s+)?co[\s-]?applicant"
         r"|me\s+and\s+(?:my\s+)?co[\s-]?applicant"
         r"|co[\s-]?applicant\s+and\s+(?:the\s+|my\s+)?(?:primary\s+)?applicant)")
#: The primary applicant, named as such. A bare "my" is NOT this: "my
#: application" is the case.
_PRIMARY = r"(?:(?:primary|main|first)\s+applicant|for\s+me\b|my\s+own)"

_BOTH_RE = re.compile(rf"\b{_POSSESSIVE}{_BOTH}{_OWN}", _I)
_CO_RE = re.compile(rf"\b{_POSSESSIVE}{_CO}{_OWN}", _I)
_PRIMARY_RE = re.compile(rf"\b{_POSSESSIVE}{_PRIMARY}{_OWN}", _I)


def mentioned(message: str) -> Kind | None:
    """The role the question names, or None when it names none."""
    text = message or ""
    if _BOTH_RE.search(text):
        return Kind.BOTH
    if _CO_RE.search(text):
        return Kind.CO
    if _PRIMARY_RE.search(text):
        return Kind.PRIMARY
    return None


def neutral(message: str) -> str:
    """
    The question with its subject phrase replaced by "my documents", so the
    ORDINARY classifier decides what is being asked: "is my co-applicant
    verified?" is classified as "is my documents verified?". One set of
    intent rules, not a second copy for every subject.
    """
    # A POSSESSIVE KEEPS ITS NOUN: "the co-applicant's PAN name" becomes
    # "my PAN name" (a document-details question), where a bare subject
    # becomes "my documents" ("is my co-applicant verified?").
    def swap(match: re.Match[str]) -> str:
        return "my" if re.search(r"(?:'s|s'|’s)$", match.group(0)) \
            else "my documents"

    text = message or ""
    for pattern in (_BOTH_RE, _CO_RE, _PRIMARY_RE):
        text = pattern.sub(swap, text)
    return re.sub(r"\bmy\s+my\b", "my", text, flags=_I)


# ==========================================================================
# WHO IS ON THE CASE -- from the record
# ==========================================================================

@dataclass(frozen=True)
class Party:
    party_id: str
    role: Kind

    @property
    def label(self) -> str:
        return ("the primary applicant" if self.role is Kind.PRIMARY
                else "the co-applicant")

    def public(self) -> dict[str, str]:
        return {"party_id": self.party_id, "party_role": self.role.value}


_UNREAD = object()


def parties_of(case_id: str | None, *, application: Any = _UNREAD,
               documents: Any = _UNREAD) -> list[Party]:
    """
    The parties the case record names, primary first. Empty when there is
    no case or no record -- never a guess.

    `application` / `documents`, when a caller already read them (the case
    ledger), are used instead of reading them again.
    """
    if not case_id:
        return []
    from app.store import get_repository

    if application is _UNREAD:
        try:
            application = get_repository().get_application(case_id)
        except Exception:
            return []
    if application is None:
        return []
    found = [Party(application.applicant_id, Kind.PRIMARY)]
    co = str(getattr(application, "co_applicant_id", "") or "").strip()
    if not co:
        # A CASE STORED BEFORE THE APPLICATION CARRIED ITS CO-APPLICANT:
        # the flow still stamped the co-applicant's documents with their id
        # and role. Exactly one such id is the co-applicant; more than one
        # is a record this service cannot interpret, and none is adopted.
        try:
            if documents is _UNREAD:
                documents = get_repository().list_documents(case_id)
            stamped = {str(d.party_id).strip()
                       for d in documents or ()
                       if str(getattr(d, "party_role", "") or "").upper()
                       == Kind.CO.value and str(d.party_id or "").strip()}
        except Exception:
            stamped = set()
        co = next(iter(stamped)) if len(stamped) == 1 else ""
    if co and co != application.applicant_id:
        found.append(Party(co, Kind.CO))
    return found


def belongs(case_id: str | None, party_id: str | None) -> bool:
    """
    Whether `party_id` is a party of THIS case, by the case's own records:
    its application, its stamped documents, or the findings recorded on it.
    Read only within the case, so an id from any other case is never found.
    """
    wanted = str(party_id or "").strip()
    if not wanted or not case_id:
        return False
    if any(p.party_id == wanted for p in parties_of(case_id)):
        return True
    from app.store import get_repository

    try:
        repository = get_repository()
        if any(str(d.party_id or "").strip() == wanted
               for d in repository.list_documents(case_id)):
            return True
        return any(str(f.party_id or "").strip() == wanted
                   for f in repository.get_case_findings(case_id))
    except Exception:
        return False


@dataclass(frozen=True)
class Subject:
    """Who the answer is about, resolved against the record."""

    kind: Kind
    parties: tuple[Party, ...] = ()
    #: Set when the question named a role the case does not have.
    missing: str | None = None

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind.value,
                "parties": [p.public() for p in self.parties],
                **({"note": self.missing} if self.missing else {})}


def resolve(kind: Kind | None, parties: list[Party]) -> Subject | None:
    """The subject for a named role, or None for a case-level question."""
    if kind is None or kind is Kind.CASE:
        return None
    primary = [p for p in parties if p.role is Kind.PRIMARY]
    co = [p for p in parties if p.role is Kind.CO]
    if kind is Kind.PRIMARY:
        return Subject(Kind.PRIMARY, tuple(primary))
    if kind is Kind.CO:
        return Subject(Kind.CO, tuple(co),
                       None if co else "There is no co-applicant on this "
                                       "application.")
    return Subject(Kind.BOTH, tuple(primary + co),
                   None if co else "This application has only one "
                                   "applicant.")


# ==========================================================================
# WHAT IS BEING ASKED ABOUT THEM
# ==========================================================================

class Capability(str, Enum):
    VERIFICATION = "VERIFICATION"
    PENDING = "PENDING"
    ISSUES = "ISSUES"
    READINESS = "READINESS"
    HISTORY = "HISTORY"          # what changed, from the case ledger
    DETAILS = "DETAILS"          # one document's recorded values -- the
                                 # existing party-scoped path answers it


_ISSUE_WORDS = re.compile(
    r"\b(issues?|problems?|mismatch\w*|wrong|flagged|stuck|review|reviewed|"
    r"delay\w*|hold|concerns?)\b", _I)

_PENDING = {Intent.DOCUMENTS_PENDING, Intent.DOCUMENTS_MISSING,
            Intent.PENDING_ITEMS, Intent.COMPLETENESS, Intent.NEXT_ACTION}


def capability_for(intent: Intent, message: str,
                   document_type: str | None) -> Capability | None:
    """What a subject question asks for, from the classified intent."""
    from app.agents.applicant.history import asks_what_changed

    if asks_what_changed(message):
        return Capability.HISTORY
    if intent is Intent.READINESS:
        return Capability.READINESS
    if intent in _PENDING:
        return Capability.PENDING
    if intent in (Intent.DOCUMENT_DETAILS, Intent.ELIGIBILITY,
                  Intent.INCOME_EVIDENCE):
        return Capability.DETAILS
    if intent in (Intent.DOCUMENT_VERIFICATION, Intent.DOCUMENTS_UPLOADED):
        if document_type or not _ISSUE_WORDS.search(message or ""):
            return Capability.VERIFICATION
        return Capability.ISSUES
    if intent in (Intent.CASE_HISTORY, Intent.APPLICATION_STATUS,
                  Intent.FULL_SUMMARY):
        return Capability.ISSUES
    if _ISSUE_WORDS.search(message or ""):
        return Capability.ISSUES
    if re.search(r"\bverif\w*\b", message or "", _I):
        return Capability.VERIFICATION
    if re.search(r"\b(pending|left|missing|outstanding|remaining)\b",
                 message or "", _I):
        return Capability.PENDING
    return None


#: The tools each capability needs. Governed reads, like every other plan.
PLANS: dict[Capability, tuple[str, ...]] = {
    Capability.VERIFICATION: ("documents.get",),
    Capability.PENDING: ("documents.get", "documents.checklist"),
    Capability.ISSUES: ("documents.get",),
    Capability.READINESS: ("documents.get", "workflow.readiness"),
}


# ==========================================================================
# ANSWERS -- one sentence per party, from that party's own records
# ==========================================================================

_TYPE_WORDS = {
    "PAN": "PAN", "BANK_STATEMENT": "bank statement",
    "SALARY_SLIP": "salary slip", "ADDRESS_PROOF": "address proof",
    "DRIVING_LICENCE": "driving licence", "VOTER_ID": "voter ID",
    "PASSPORT": "passport", "AADHAAR": "Aadhaar", "ITR": "ITR",
    "SALE_DEED": "sale deed", "FORM_16": "Form 16",
}


def _type(value: Any) -> str:
    key = str(value or "").upper()
    return _TYPE_WORDS.get(key, key.replace("_", " ").lower() or "document")


def _state(document: dict[str, Any]) -> str:
    """How a document stands, in words. SKIPPED is NOT verified."""
    status = str(document.get("status") or "").upper()
    verdict = str(document.get("verification_status") or "").upper()
    if status == "VERIFIED" or (verdict == "PASS" and status != "REVIEW"):
        return "verified"
    if status == "REJECTED" or verdict == "FAIL":
        return "rejected"
    if status == "REVIEW" or verdict == "REVIEW":
        return "under review"
    return "awaiting verification"


def _owned(documents: list[dict[str, Any]], party: Party) -> list[dict[str, Any]]:
    """This party's documents. The stamped owner only (parties.owned_by)."""
    from app.agents.los.parties import owned_by

    return owned_by(documents, party.party_id,
                    is_primary=party.role is Kind.PRIMARY)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _grouped(label: str, documents: list[dict[str, Any]]) -> str:
    by_state: dict[str, list[str]] = {}
    for document in documents:
        by_state.setdefault(_state(document), []).append(
            _type(document.get("document_type")))
    parts = []
    for state in ("verified", "under review", "rejected",
                  "awaiting verification"):
        names = list(dict.fromkeys(by_state.get(state, [])))
        if names:
            verb = "is" if len(names) == 1 else "are"
            parts.append(f"{_and(names)} {verb} {state}")
    return f"{_cap(label)}'s {'; '.join(parts)}."


def verification(subject: Subject, documents: list[dict[str, Any]],
                 document_type: str | None = None) -> str:
    sentences = []
    for party in subject.parties:
        owned = _owned(documents, party)
        if document_type:
            owned = [d for d in owned
                     if str(d.get("document_type") or "").upper()
                     == document_type.upper()]
        if not owned:
            what = f"a {_type(document_type)}" if document_type else \
                "any documents"
            sentences.append(f"{_cap(party.label)} has not uploaded {what} yet.")
        else:
            sentences.append(_grouped(party.label, owned))
    return " ".join(sentences)


def pending(subject: Subject, documents: list[dict[str, Any]],
            checklist: list[dict[str, Any]] | None) -> str:
    sentences = []
    for party in subject.parties:
        waiting = [d for d in _owned(documents, party)
                   if _state(d) != "verified"]
        if waiting:
            sentences.append(_grouped(party.label, waiting))
        else:
            sentences.append(f"Nothing uploaded by {party.label} is awaiting "
                             f"verification.")
    missing = [str(e.get("slot") or "") for e in checklist or []
               if isinstance(e, dict) and e.get("mandatory", True)
               and str(e.get("status") or "").upper() == "MISSING"]
    if missing:
        sentences.append(
            "The document checklist is kept for the application as a whole, "
            f"not for each applicant, and it still needs "
            f"{_and([_type(m) for m in missing])}.")
    return " ".join(sentences)


def _problems_for(party: Party | None, memory: dict[str, Any],
                  documents: list[dict[str, Any]],
                  parties: tuple[Party, ...]) -> list[dict[str, Any]]:
    """The recorded problems that are THIS party's (None: the case's own)."""
    from app.agents.applicant import evidence

    return [p for p in evidence.problems(memory, parties=parties,
                                         documents=documents, limit=None)
            if (p.get("party_id") or None) == (party.party_id if party else None)]


def issues(subject: Subject, memory: dict[str, Any],
           documents: list[dict[str, Any]], all_parties: list[Party]) -> str:
    sentences = []
    for party in subject.parties:
        found = _problems_for(party, memory, documents, tuple(all_parties))
        if found:
            said = _and(list(dict.fromkeys(
                p["message"].rstrip(".") + (f" on the {_type(p['document'])}"
                                            if p.get("document") else "")
                for p in found)))
            sentences.append(f"{_cap(party.label)} has a recorded issue: "
                             f"{said}.")
        else:
            sentences.append(f"No issue is recorded for {party.label}.")
    shared = _problems_for(None, memory, documents, tuple(all_parties))
    if shared:
        sentences.append("The application as a whole also has: " + _and(
            list(dict.fromkeys(p["message"].rstrip(".") for p in shared)))
            + ".")
    return " ".join(sentences)


def readiness(subject: Subject, documents: list[dict[str, Any]],
              case_answer: str) -> str:
    per_party = verification(subject, documents)
    return (f"{case_answer.rstrip()} Readiness is assessed for the "
            f"application as a whole, not for each applicant. {per_party}"
            ).strip()


def clarification(subject: Subject) -> str:
    who = ("both applicants" if subject.kind is Kind.BOTH
           else subject.parties[0].label if subject.parties else
           "that applicant")
    return (f"I can tell you about {who}'s documents, their verification and "
            f"any recorded issues. What would you like to know?")


__all__ = ["Capability", "Kind", "PLANS", "Party", "Subject", "belongs",
           "capability_for", "clarification", "issues", "mentioned",
           "neutral", "parties_of", "pending", "readiness", "resolve",
           "verification"]
