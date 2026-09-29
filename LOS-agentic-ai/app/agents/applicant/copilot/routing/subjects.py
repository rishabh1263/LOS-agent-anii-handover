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

from app.store import request_cache

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.agents.applicant.copilot.semantics.intents import Intent


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
         r"|dono\s+(?:ke|ka|ki)\b|donon\s+(?:ke|ka|ki)\b"
         r"|every\s+applicant|which\s+(?:applicant|party|borrower|person)(?!\s+(?:id|number|no|ref)\b)"
         r"|(?:the\s+)?applicant\s+and\s+(?:the\s+|my\s+)?co[\s-]?applicant"
         r"|me\s+and\s+(?:my\s+)?co[\s-]?applicant"
         r"|co[\s-]?applicant\s+and\s+(?:the\s+|my\s+)?(?:primary\s+)?applicant)")
#: The primary applicant, named as such. A bare "my" is NOT this: "my
#: application" is the case.
_PRIMARY = r"(?:(?:primary|main|first)\s+applicant(?!\s+(?:id|number|no)\b)|for\s+me\b|my\s+own)"

_BOTH_RE = re.compile(rf"\b{_POSSESSIVE}{_BOTH}{_OWN}", _I)
_CO_RE = re.compile(rf"\b{_POSSESSIVE}{_CO}{_OWN}", _I)
_PRIMARY_RE = re.compile(rf"\b{_POSSESSIVE}{_PRIMARY}{_OWN}", _I)


def mentioned(message: str) -> Kind | None:
    """The role the question names, or None when it names none."""
    text = message or ""
    both = _BOTH_RE.search(text)
    if both and not (both.group(0).strip().lower() == "both"
                     and re.search(r"\bme\b[^?]{0,30}\bboth\b|\band\s+\S+\s+both\b", text, _I)):
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
            application = request_cache.read(get_repository(), "get_application", case_id)
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
                documents = request_cache.read(get_repository(), "list_documents", case_id)
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
    PROFILE = "PROFILE"          # the party's own recorded details (name, mobile ...)
    KYC = "KYC"                  # the party's own recorded KYC check


_ISSUE_WORDS = re.compile(
    r"\b(issues?|problems?|mismatch\w*|wrong|flagged|stuck|review|reviewed|"
    r"delay\w*|hold|concerns?)\b", _I)

_PENDING = {Intent.DOCUMENTS_PENDING, Intent.DOCUMENTS_MISSING,
            Intent.PENDING_ITEMS, Intent.COMPLETENESS, Intent.NEXT_ACTION}


def capability_for(intent: Intent, message: str,
                   document_type: str | None) -> Capability | None:
    """What a subject question asks for, from the classified intent."""
    from app.agents.applicant.history import asks_what_changed

    if intent is Intent.KYC_RESULT:
        return Capability.KYC
    if intent is Intent.APPLICANT_PROFILE:
        return Capability.PROFILE
    if asks_what_changed(message):
        return Capability.HISTORY
    if intent is Intent.READINESS:
        return Capability.READINESS
    if intent in _PENDING or intent is Intent.DOCUMENTS_REQUIRED:
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
            named = _type(document_type) if document_type else ""
            what = (("an " if named[:1].lower() in "aeiou" else "a ") + named) if document_type \
                else "any documents"
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


# ==========================================================================
# A PARTY'S OWN DETAILS AND KYC -- the co-applicant is a person on the case,
# with a record of their own; nothing of the primary applicant's is said of
# them, and nothing is read from what the user typed.
# ==========================================================================

_DOCUMENT_NOUN = re.compile(r"\b(documents?|docs?|papers?|kagaz\w*|pan|aadhaar|statement|"
                            r"proof|slip|upload\w*|verif\w*|pending|missing)\b", _I)
_PROFILE_ASK = re.compile(r"^\s*(who|kaun)\b|\b(who\s+is|kaun\s+(hai|h)|details?|information|info|"
                          r"profile|jaankari|jankari|batao\s+(uske|unke)\s+baare|"
                          r"tell\s+me\s+(more\s+)?about|more\s+about|baare\s+(me|mein)\s+batao|"
                          r"kaun\s+(is|hai|h)|who\s+(is|are))\b", _I)


# ==========================================================================
# PEOPLE NAMED BY NAME -- "Priya ka mobile", "What's Priya's DOB?". A name is
# matched against the parties the CASE RECORD names (read only after the
# caller's ownership of the case is proven); a person who is not on the case
# is never looked up.
# ==========================================================================

_COMMON = frozenset("""
a an the and or but of for to in on at by with from as is are was were be been being am do does did
have has had can could will would should shall may might must not no yes ok okay please pls plz
what which who whom whose where when why how whats hows whos wheres tell show give share send get
find check need want know see list explain say mention mentioned meant mean about also too just
only again more else anything everything something nothing any some all every each both either
neither one two first second last next previous other another same this that these those it its
my mine me i im you your yours our ours us we they them their theirs he him his she her hers
customer customers client applicant applicants borrower person people user someone somebody
loan loans case file application account bank number mobile phone email address name date birth
dob pan aadhaar aadhar kyc document documents doc docs proof statement slip card photo copy status
stage review pending missing verified rejected uploaded submitted details detail info information
record records score result field fields value right wrong now today yesterday tomorrow still yet
here there then than so very really exactly stays live lives lived stay birthdate birthday
kya hai hain ka ki ke ko ne se mein me mera meri mere tera teri aapka aapki unka unki uska uski
iska iski batao bataiye bata dikhao do dijiye chahiye aur bhi nahi haan ji wala wali wale kaun
kaunsa konsa kitna kitni kitne kab kahan kaise kyun kyu kyon sab dono abhi tak toh phir yeh woh
vo ye isme usme apna apni apne hamara humara hum main mai mujhe maine accha acha theek thik
income salary monthly declared obligations obligation property tenure interest rate emi amount
employment employed salaried business type product personal home gold charges fee fees penalty
branch timings timing saturday saturdays sunday monday office hours cibil credit bureau limit
hello hii hey thanks thank bye goodbye welcome sorry help
""".split())
_VOCAB: frozenset[str] | None = None


def _vocabulary() -> frozenset[str]:
    """Every word the semantic and language layers know -- none of them is a name."""
    global _VOCAB
    if _VOCAB is not None:
        return _VOCAB
    words: set[str] = set(_COMMON)

    def walk(node: Any) -> None:
        if isinstance(node, str):
            words.update(w.lower() for w in re.findall(r"[A-Za-z]+", node))
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(k)
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
    try:
        from app.agents.applicant.copilot.semantics import semantic_frame as _frames

        walk(_frames._config())
    except Exception:
        pass
    try:
        from app.agents.applicant import language as _language

        walk(_language._load())
    except Exception:
        pass
    _VOCAB = frozenset(words)
    return _VOCAB


def _is_word(token: str) -> bool:
    from difflib import get_close_matches

    t = token.lower()
    vocab = _vocabulary()
    return t in vocab or len(t) < 3 or bool(get_close_matches(t, vocab, n=1, cutoff=0.86))


def _unspelled(text: str) -> str:
    """ "Z-a-r-a" -> "Zara", "Z.a.r.a" -> "Zara": letters spaced out are one word."""
    return re.sub(r"\b(?:[A-Za-z][-.\s]){2,}[A-Za-z]\b",
                  lambda m: re.sub(r"[-.\s]", "", m.group(0)), str(text or ""))


def name_candidates(message: str) -> list[str]:
    """Tokens that may be a person's name: letters, unknown to every vocabulary,
    and not an all-capitals code or acronym (CPA, UNCONFIRMED)."""
    return [t for t in re.findall(r"[A-Za-z]{3,}", _unspelled(message))
            if not t.isupper() and not _is_word(t)]


_POSSESSIVE_AFTER = r"(?:'s|\u2019s|\s+(?:ji\s+)?(?:ka|ki|ke|ko|ne|chya|cha|chi|che)\b)"


def names_a_person(message: str) -> bool:
    """
    Whether the message is SHAPED like it names a person: an unknown word with
    a possessive after it ("Priya's", "priya ka", "Sharma ji"), or a
    capitalised unknown word that does not open the sentence. A greeting, a
    typo or a bare topic word ("hello", "income") is not -- and costs no read.
    """
    text = _unspelled(message)
    tokens = name_candidates(text)
    if not tokens:
        return False
    alt = "|".join(re.escape(t) for t in tokens)
    if re.search(r"\b(?:" + alt + r")(?:" + _POSSESSIVE_AFTER + r"|\s+ji\b)", text, re.I):
        return True
    first = re.match(r"\s*([A-Za-z]+)", text)
    opener = first.group(1) if first else ""
    return any(t[:1].isupper() and t != opener for t in tokens)


def resolve_names(message: str, parties: list["Party"], names: dict[str, str]) -> dict[str, Any]:
    """
    What the NAMES in a message refer to. `names` maps party_id -> recorded
    full name. Returns {"role": Kind | "AMBIGUOUS" | "OTHER" | None, "as_self",
    "as_co"} -- the message rewritten with the name as the caller / as the
    co-applicant, so the ordinary party rules read it.
    """
    text = _unspelled(message)
    tokens = name_candidates(text)
    if not tokens:
        return {"role": None}
    by_token: dict[str, set[Kind]] = {}
    for party in parties:
        for part in re.findall(r"[A-Za-z]{3,}", names.get(party.party_id) or ""):
            by_token.setdefault(part.lower(), set()).add(party.role)
    matched = [t for t in tokens if t.lower() in by_token]
    if matched:
        unique = [by_token[t.lower()] for t in matched if len(by_token[t.lower()]) == 1]
        roles = set.union(*unique) if unique else set.union(*(by_token[t.lower()] for t in matched))
        alt = "|".join(re.escape(t) for t in matched)
        span = re.compile(r"\b(?:" + alt + r")(?:\s+(?:" + alt + r"))*(?:\s+ji)?(?P<pos>"
                          + _POSSESSIVE_AFTER + r")?", re.IGNORECASE)

        def as_co(m: "re.Match[str]") -> str:
            pos = m.group("pos") or ""
            if pos.startswith(("'", "\u2019")):
                return "the co-applicant's"
            return "the co-applicant" + pos

        def as_self(m: "re.Match[str]") -> str:
            pos = (m.group("pos") or "").strip().lower()
            if pos.startswith(("'", "\u2019")):
                return "my"
            return {"ka": "mera", "ki": "meri", "ke": "mere", "ko": "mujhe", "ne": "maine"}.get(pos, "me")
        rewritten = {"as_self": span.sub(as_self, text), "as_co": span.sub(as_co, text)}
        if len(roles) == 1:
            return {"role": next(iter(roles)), **rewritten}
        return {"role": "AMBIGUOUS", **rewritten}
    # A NAME THAT IS NOT ON THE CASE, used as a person ("Zara's PAN", "Zara
    # Qureshi is my co-applicant"): somebody else's record.
    named = [t for t in tokens if t[:1].isupper() and t[1:].islower()]
    alt = "|".join(re.escape(t) for t in named)
    if named and (re.search(r"\b(?:" + alt + r")(?:'s|\u2019s|\s+(?:ji\s+)?(?:ka|ki|ke)\b)", text)
                  or (re.search(r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b", text) and len(named) >= 2)):
        return {"role": "OTHER"}
    return {"role": None}


def party_question(message: str, classification: Any) -> Any | None:
    """
    A question NAMING the co-applicant (or both) that asks for a person's
    details or KYC, read as that -- APPLICANT_PROFILE / KYC_RESULT, with the
    field(s) asked. None when it asks something else (documents, pending).
    """
    from app.agents.applicant.copilot.semantics import intents as _intents
    from app.agents.applicant.copilot.answering import profile as _profile

    text = str(message or "")
    if re.search(r"\bkyc\b", text, _I) and not _intents.asks_for_a_definition(text) \
            and not re.search(r"\bdecision\b", text, _I):
        return _intents.Classification(Intent.KYC_RESULT, matched_on="party_kyc",
                                       fields={"want": _intents.kyc_want(text)},
                                       frame=getattr(classification, "frame", None))
    intent = classification.intent
    profile_like = intent in (Intent.UNKNOWN, Intent.APPLICANT_PROFILE, Intent.APPLICANT_DETAILS,
                              Intent.APPLICANT_MISSING_INFO, Intent.FULL_SUMMARY)
    if not profile_like and (_DOCUMENT_NOUN.search(text) or intent.value in (
            "GUARDRAIL_BLOCKED", "OUT_OF_SCOPE", "FOS_KNOWLEDGE", "STAGE_PROCESS", "MIXED")):
        return None
    as_own = _BOTH_RE.sub(" my ", _CO_RE.sub(" my ", text))
    as_own = re.sub(r"\b(email|mobile|number|phone|name|address|dob)(e?s)\b", r"\1", as_own, flags=_I)
    asked = _profile.detect(as_own) or _profile.detect(text)
    if asked is not None and asked.field not in (_profile.COMPLETENESS,):
        field_ = asked.field if not asked.field.startswith("ALL") else _profile.ALL_APPLICANT
        if re.search(r"^\s*who\b|\bwho\s+is\b|\bkaun\s+(is|hai|h)\b", text, _I) \
                and "full_name" not in field_.split("+") and not field_.startswith("ALL"):
            field_ = "full_name+" + field_
        return _intents.Classification(Intent.APPLICANT_PROFILE, matched_on="party_profile",
                                       fields={"field": field_},
                                       frame=getattr(classification, "frame", None))
    if intent is Intent.UNKNOWN and re.search(r"\b(documents?|docs?|papers?|kagaz\w*)\b", text, _I) \
            and not _profile.detect(text):
        # A PERSON'S DOCUMENTS, no task named: what that person uploaded and
        # its state -- the one per-person document view (the checklist is kept
        # for the application as a whole).
        return _intents.Classification(Intent.DOCUMENTS_UPLOADED, matched_on="party_documents",
                                       frame=getattr(classification, "frame", None))
    if _PROFILE_ASK.search(text) and not _DOCUMENT_NOUN.search(text):
        field_ = ("full_name" if re.search(r"^\s*(who|kaun)\b|\bwho\s+is\b|\bkaun\s+(hai|h)\b",
                                           text, _I) else _profile.ALL_APPLICANT)
        return _intents.Classification(Intent.APPLICANT_PROFILE, matched_on="party_profile",
                                       fields={"field": field_},
                                       frame=getattr(classification, "frame", None))
    return None


#: Application fields that describe a PERSON (the primary applicant's own).
_PERSON_LEVEL = frozenset({"employment_type", "declared_monthly_income",
                           "declared_monthly_obligations"})


def _party_record(party: Party) -> tuple[Any, bool]:
    """The party's own applicant record, and whether it could be read."""
    from app.store import get_repository

    try:
        return request_cache.read(get_repository(), "get_applicant", party.party_id), True
    except Exception:
        return None, False


def _party_field(field: str, party: Party, record: Any, readable: bool, case_id: str,
                 language: str | None, application: Any) -> str:
    from app.agents.applicant.copilot.routing import capabilities
    from app.agents.applicant.copilot.facts import field_state
    from app.agents.applicant.copilot.answering import phrasing
    from app.agents.applicant.copilot.answering import profile as _profile

    who = party.label
    name = _profile.label(field)
    seed = phrasing.current_seed(field)
    if field == "applicant_id":
        # the party's OWN reference, as the case record names it
        return phrasing.party_sentence("PRESENT", who, name, party.party_id,
                                       language=language, seed=seed)
    if field in _PERSON_LEVEL and party.role is not Kind.PRIMARY:
        # the application records these for the primary applicant only; they
        # are never reported as another person's
        return phrasing.party_sentence("NOT_AVAILABLE", who, name, language=language, seed=seed)
    if field in capabilities.APPLICATION_FIELDS:
        value = getattr(application, field, None) if application is not None else None
        if value in (None, ""):
            return f"The {name} isn't recorded on this application yet."
        shown = _profile._shown(field, value)
        return (f"The {name} is recorded for the application as a whole, not for each "
                f"applicant: {shown}.")
    if field in field_state.IDENTITY_FIELDS:
        resolved = field_state.resolve(field, {}, case_id=case_id, party_id=party.party_id)
        state = resolved.state.value
        field_state.record(field, state, resolved.source, party=party.role.value)
        shown = _profile._shown(field, resolved.value) if resolved.value else None
        if state == "PRESENT" and shown is None:
            state = "RESTRICTED"
        return phrasing.party_sentence(state, who, name, shown, language=language, seed=seed)
    source = "applicant_record"
    if not readable:
        state, shown = "UNKNOWN", None
    elif record is None:
        state, shown = "NOT_AVAILABLE", None
    elif getattr(record, field, None) in (None, ""):
        state, shown = "NOT_PROVIDED", None
    else:
        shown = _profile._shown(field, getattr(record, field))
        state = "PRESENT" if shown is not None else "RESTRICTED"
    field_state.record(field, state, source, party=party.role.value)
    return phrasing.party_sentence(state, who, name, shown, language=language, seed=seed)


def profile_answer(subject: Subject, field: str, case_id: str, *,
                   language: str | None = None) -> str:
    """Each named party's own recorded details, from their own record."""
    from app.agents.applicant.copilot.routing import capabilities
    from app.agents.applicant.copilot.answering import profile as _profile
    from app.store import get_repository

    try:
        application = request_cache.read(get_repository(), "get_application", case_id)
    except Exception:
        application = None
    bundle = field.startswith("ALL") or field == _profile.FIELDS_AVAILABLE
    fields = list(capabilities.APPLICANT_FIELDS) if bundle else field.split("+")
    lines = []
    for party in subject.parties:
        record, readable = _party_record(party)
        if bundle:
            if not readable:
                lines.append(f"I couldn't read {party.label}'s details right now.")
                continue
            if record is None:
                lines.append(f"I don't have {party.label}'s details recorded on this "
                             f"application yet.")
                continue
            recorded, absent = [], []
            for f in fields:
                value = getattr(record, f, None)
                if value in (None, ""):
                    absent.append(_profile.label(f))
                    continue
                shown = _profile._shown(f, value)
                recorded.append(f"{_profile.label(f)} {shown}" if shown is not None
                                else f"{_profile.label(f)} withheld for security")
            who = party.label[:1].upper() + party.label[1:]
            said = (f"{who}'s details: {_profile._and(recorded)}." if recorded
                    else f"None of {party.label}'s details are recorded yet.")
            if absent:
                said += f" Not provided yet: {_profile._and(absent)}."
            lines.append(said)
            continue
        lines.append(" ".join(_party_field(f, party, record, readable, case_id, language,
                                           application) for f in fields))
    return " ".join(lines)


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
