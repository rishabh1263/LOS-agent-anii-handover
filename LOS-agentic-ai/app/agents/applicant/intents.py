"""
What the FOS asked, and which tools answer it.

RULES BEFORE MODEL. Intent is decided by matching phrases, not by asking a
language model to classify. A FOS asking "is PAN verified?" should not wait a
second and a half for a model to decide that the question is about a document,
and a classifier that occasionally routes "what's pending" to the credit agent
is worse than no classifier.

The patterns are ordered: the most specific intent that matches wins. Anything
that matches nothing becomes UNKNOWN, which is answered with a list of what
this agent can actually do rather than a guess.

Out-of-scope intents are matched FIRST and deliberately. A question about a
credit score must be routed on before anything tries to answer it from FOS
data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    """Everything this agent recognises."""

    # -- applicant
    APPLICANT_DETAILS = "APPLICANT_DETAILS"
    APPLICANT_MISSING_INFO = "APPLICANT_MISSING_INFO"

    # -- application
    APPLICATION_STATUS = "APPLICATION_STATUS"
    APPLICATION_STAGE = "APPLICATION_STAGE"

    # -- documents
    DOCUMENTS_UPLOADED = "DOCUMENTS_UPLOADED"
    DOCUMENTS_REQUIRED = "DOCUMENTS_REQUIRED"
    DOCUMENTS_MISSING = "DOCUMENTS_MISSING"
    DOCUMENTS_PENDING = "DOCUMENTS_PENDING"
    # WHY the checklist is what it is -- which rules fired, which could not
    # be evaluated, and what made a conditional requirement apply.
    #
    # Distinct from DOCUMENTS_REQUIRED, which answers WHAT is needed. An
    # officer who has to tell a customer why they are being asked for an
    # extra document needs the rule, not the list, and the checklist
    # answer cannot carry the whole explanation without becoming unreadable.
    POLICY_EXPLANATION = "POLICY_EXPLANATION"
    DOCUMENT_VERIFICATION = "DOCUMENT_VERIFICATION"

    # -- workflow
    PENDING_ITEMS = "PENDING_ITEMS"
    NEXT_ACTION = "NEXT_ACTION"
    READINESS = "READINESS"
    COMPLETENESS = "COMPLETENESS"

    # -- composite
    FULL_SUMMARY = "FULL_SUMMARY"

    # -- writes
    CREATE_APPLICANT = "CREATE_APPLICANT"
    UPDATE_APPLICANT = "UPDATE_APPLICANT"
    CREATE_APPLICATION = "CREATE_APPLICATION"
    MARK_FOR_REUPLOAD = "MARK_FOR_REUPLOAD"

    # -- knowledge, not case data
    #
    # A question about how the FOS stage WORKS rather than about this case.
    # "What can be used as address proof" has no answer in the store; it has
    # an answer in the FOS knowledge base.
    FOS_KNOWLEDGE = "FOS_KNOWLEDGE"

    # A question that needs BOTH: the case's own facts and the rule that
    # explains them. "Why is this not ready and what should I collect?"
    MIXED = "MIXED"

    # -- not ours
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNKNOWN = "UNKNOWN"


#: Intents answered deterministically, with no model call at all.
#:
#: Two kinds qualify. SINGLE FACTS -- "is PAN verified?" -- where phrasing adds
#: latency and a chance of drift without adding meaning. And ENUMERATIONS --
#: a checklist, a document list, a list of pending items -- where the
#: deterministic wording carries a status against every entry and prose
#: reliably loses some of them: asked to phrase a five-slot checklist the model
#: returned "PAN, ADDRESS_PROOF, BANK_STATEMENT missing SALARY_SLIP, PHOTO",
#: which is grounded, shorter, and worse.
#:
#: What is left on the model path is the prose that genuinely reads better for
#: it: the applicant narrative and the case briefing.
SIMPLE_INTENTS = frozenset({
    Intent.DOCUMENT_VERIFICATION,
    Intent.NEXT_ACTION,
    Intent.READINESS,
    Intent.COMPLETENESS,
    Intent.DOCUMENTS_MISSING,
    Intent.DOCUMENTS_REQUIRED,
    Intent.DOCUMENTS_UPLOADED,
    Intent.DOCUMENTS_PENDING,
    Intent.PENDING_ITEMS,
    Intent.APPLICATION_STAGE,
    Intent.POLICY_EXPLANATION,
})

#: Intents that change stored data. Every one needs a write scope and an
#: explicit confirmation before it runs.
WRITE_INTENTS = frozenset({
    Intent.CREATE_APPLICANT,
    Intent.UPDATE_APPLICANT,
    Intent.CREATE_APPLICATION,
    Intent.MARK_FOR_REUPLOAD,
})


@dataclass
class Classification:
    """What the message was taken to mean."""

    intent: Intent
    confidence: str = "high"
    route_to: str | None = None
    document_type: str | None = None
    matched_on: str | None = None
    fields: dict[str, str] = field(default_factory=dict)

    #: For MIXED, the case intent underneath it. The case half of a mixed
    #: question is answered from exactly the same tools and the same
    #: deterministic rules as it would be on its own -- adding a knowledge
    #: paragraph must not change what the facts are.
    base_intent: Intent | None = None


# ==========================================================================
# MIXED -- a case question with a knowledge question attached
#
# Detected structurally rather than by listing phrasings: the message matches
# a case intent AND carries a second clause asking what/which/how. That second
# clause is the part the store cannot answer.
#
#   "Why is this applicant not ready for CPA and what should I collect?"
#    |____________ case: READINESS ____________| |__ knowledge __|
#
# The case half is answered from the store exactly as it would be alone. The
# knowledge half is retrieved. Neither is allowed to stand in for the other.
# ==========================================================================

_MIXED_TAIL = re.compile(
    r"\b(and|also|plus)\s+(what|which|how|who|can|could|should|"
    r"tell\s+me|explain)\b|,\s*(and\s+)?(what|which|how)\b",
    re.IGNORECASE,
)

#: A question about the RULES rather than about this case.
#:
#: Checked only after every case pattern has failed, so "what documents are
#: pending" stays a case question. The ordering is the whole distinction:
#: these phrasings are generic, and a generic phrasing about a specific case
#: is still about the case.
_KNOWLEDGE: list[str] = [
    r"\bwhat\s+(\w+\s+){0,3}(can|could|may)\s+be\s+used\b",
    r"\bwhat\s+(can|could|may)\s+(i|we|you)\s+use\b",
    r"\bwhat\s+(does|do|is)\s+.{0,48}\bmean(s|ing)?\b",
    r"\bwhat\s+happens\s+(during|in|at|when|to|if)\b",
    r"\bwhat\s+is\s+(a|an|the)\s+(checklist|slot|verification|document\s+"
    r"type|address\s+proof|pending\s+item|next\s+action|kyc\s+check)\b",
    r"\b(explain|describe|define)\b",
    r"\bfor\s+(a|an)\s+\w+\s*loan\b",
    r"\b(policy|policies|rule|rules|guideline|guidelines|procedure)\b",
    r"\bhow\s+(do|does|is|are|can|should)\s+.{0,48}\b(work|works|verified|"
    r"classified|handled|processed|decided)\b",
    r"\bwhat\s+are\s+the\s+(possible|valid|accepted|allowed|different)\b",
    r"\b(acceptable|accepted|allowed|valid)\s+(document|documents|type|types)\b",
    r"\bwhat\s+(document|documents)\s+(are|is)\s+(required|needed|accepted)\s+"
    r"for\b",
    r"\bcan\s+i\s+(upload|use|submit)\b",
    r"\bwhy\s+(did|does|would)\s+(my|a|the)\s+document\b",
]

_COMPILED_KNOWLEDGE = [re.compile(p, re.IGNORECASE) for p in _KNOWLEDGE]

#: Markers strong enough to outrank a case pattern.
#:
#: Only phrasings that CANNOT be about one case. Naming a product is the
#: clear one: a case already has a product, so spelling it out means the
#: question is about what that product requires in general. Everything softer
#: stays below the case patterns, where a generic phrasing about a specific
#: case is still about the case.
_STRONG_KNOWLEDGE = re.compile(
    r"\bfor\s+(a|an|any)\s+\w+[\s_-]*loan\b"
    r"|\bin\s+general\b"
    r"|\bnormally\s+(required|needed|accepted)\b"
    r"|\bwhat\s+(is|are)\s+the\s+(fos|kyc)\s+(process|stage|workflow)\b",
    re.IGNORECASE,
)


def looks_like_knowledge(message: str) -> bool:
    """Whether the message asks about the rules rather than about a case."""
    return any(p.search(message or "") for p in _COMPILED_KNOWLEDGE)


# ==========================================================================
# OUT OF SCOPE -- matched first
# ==========================================================================

_OUT_OF_SCOPE: list[tuple[str, str]] = [
    # "creditworthy" is the plainest way to ask the question and was not
    # matched at all -- it fell through to UNKNOWN, and from there to the
    # knowledge base, which would have answered a creditworthiness question
    # out of the FOS handbook.
    (r"\b(credit\s*score|cibil|bureau|credit\s*report|credit\s*history"
     r"|credit\s*worth\w*|creditworth\w*|credit\s*(assessment|evaluation)"
     r"|eligib\w*\s+for\s+(the\s+|a\s+)?loan)\b", "CREDIT_SCORE"),
    # ANY mention of risk routes, for the same reason fraud does: the FOS
    # stage does not own it in any form, so there is nothing to gain from
    # being precise about the phrasing and something to lose -- "what is the
    # risk?" fell through to UNKNOWN and then to the knowledge base.
    (r"\brisk\w*\b", "RISK"),
    # "Is KYC clear?" -- the adjective, not only the noun "clearance".
    (r"\b(full|complete|final)\s*kyc\b"
     r"|\bkyc\s*(decision|verdict|clearance|result|status)\b"
     r"|\bis\s+(the\s+)?kyc\s+(clear|clean|done|ok|passed|complete)\b",
     "KYC_DECISION"),
    # ANY mention of fraud routes. Narrowing this to "fraud check" and
    # "fraud investigation" left "are there fraud concerns?" unmatched, and
    # an unmatched question now falls through to the knowledge base -- which
    # would answer a fraud question out of the FOS handbook. The FOS stage
    # does not own fraud in any form, so routing is always the right answer
    # and there is nothing to be gained by being precise about the phrasing.
    (r"\b(rcu|fraud|fraudulent|forged|forgery|tamper\w*)\b", "RCU_FRAUD"),
    # Transactions too. "Analyze the bank transactions" is asking for exactly
    # the financial analysis the FOS stage must not perform on a statement it
    # has only verified as a document.
    (r"\b(analys\w+|analyz\w+)\s+(the\s+)?(bank\s*statement|bank\s*"
     r"transaction\w*|transaction\w*|income|salary|spending|cash\s*flow)\b",
     "FINANCIAL_ANALYSIS"),
    (r"\bbank\s*statement\s+analys\w+\b", "FINANCIAL_ANALYSIS"),
    (r"\b(transaction|spending|cash\s*flow)\s+(analysis|pattern\w*|behaviour|"
     r"behavior)\b", "FINANCIAL_ANALYSIS"),
    # "approved" and "rejected" as well as "approve" -- "will the loan be
    # approved?" and "should this application be rejected?" are the two most
    # natural ways to ask, and both fell through to UNKNOWN.
    (r"\b(approve[ds]?|approval|sanction(ed)?|disburse[ds]?|reject(ed|ion)?)\b",
     "LOAN_DECISION"),
    (r"\b(is\s+.{0,20}loan\s+safe|should\s+we\s+approve|eligib\w+\s+amount)\b", "LOAN_DECISION"),
]


# ==========================================================================
# IN SCOPE
#
# Ordered most specific first. `document_type` is captured where the question
# names one, so "is PAN verified" reaches the verification tool with PAN.
# ==========================================================================

_DOC_TYPES = (
    r"(pan|aadhaar|aadhar|driving\s*licence|driving\s*license|dl|voter\s*id|"
    r"voter|passport|bank\s*statement|address\s*proof)"
)

_PATTERNS: list[tuple[str, Intent]] = [
    # writes, before the reads they resemble
    (r"\b(create|add|register)\s+(a\s+)?(new\s+)?applicant\b", Intent.CREATE_APPLICANT),
    (r"\b(create|start|open)\s+(a\s+)?(new\s+)?application\b", Intent.CREATE_APPLICATION),
    (r"\b(update|change|set|correct)\b.{0,40}\b(phone|mobile|number|email|address|name|dob|date\s+of\s+birth)\b",
     Intent.UPDATE_APPLICANT),
    (r"\b(mark|flag|request)\b.{0,30}\b(re-?upload|reupload|again)\b", Intent.MARK_FOR_REUPLOAD),

    # composite summary
    (r"\b(complete|full|entire|overall)\s+(applicant\s+)?(summary|overview|picture|status)\b",
     Intent.FULL_SUMMARY),
    (r"\b(summari[sz]e|briefing|brief\s+me|overview\s+of\s+(this\s+)?(case|applicant))\b",
     Intent.FULL_SUMMARY),
    (r"\bwhat('?s| is)\s+done\b.{0,40}\bpending\b", Intent.FULL_SUMMARY),
    (r"\b(tell|give)\s+me\s+(the\s+)?(complete|full|everything)\b", Intent.FULL_SUMMARY),
    (r"\bwhere\s+(does|is)\s+(this|the)\s+case\s+stand\b", Intent.FULL_SUMMARY),
    (r"\bquick\s+overview\b", Intent.FULL_SUMMARY),
    # "Give me the current status of Rahul's application" wants the briefing,
    # not the one-line application status -- a FOS asking this way is opening
    # the case, not checking one field. "What's the application status?" is
    # the concise question and is matched further down.
    (r"\b(give|show|tell)\s+me\s+the\s+(current\s+)?status\b", Intent.FULL_SUMMARY),
    (r"\b(current\s+)?status\s+of\s+(this|the|\w+'s|\w+s')\s+(application|case|applicant)\b",
     Intent.FULL_SUMMARY),
    (r"\bhow\s+is\s+(this|the|\w+'s)\s+(case|application)\s+(doing|going|looking)\b",
     Intent.FULL_SUMMARY),

    # readiness
    (r"\b(ready|readiness)\b.{0,20}\bcpa\b", Intent.READINESS),
    # A field officer says "this case"; an applicant-facing screen says "my
    # application". Both are the same question and both must reach it.
    (r"\bis\s+(my|this|the)\s+(case|application|file)\b.{0,20}\bready\b",
     Intent.READINESS),
    (r"\bwhy\s+is\s+(my|this|the)\s+(case|application|file|applicant)\b"
     r".{0,30}\bnot\s+ready\b",
     Intent.READINESS),
    (r"\b(am\s+i|are\s+we)\s+ready\b", Intent.READINESS),
    (r"\bcan\s+i\s+(submit|send|hand)\b", Intent.READINESS),
    (r"\b(send|move|hand)\s+(this\s+)?(to\s+)?cpa\b", Intent.READINESS),

    # completeness
    (r"\bis\s+(everything|it|this|the\s+application)\s+complete\b", Intent.COMPLETENESS),
    (r"\b(application|profile)\s+complete\b", Intent.COMPLETENESS),
    (r"\ball\s+(mandatory|required)\s+(fields|information)\b", Intent.COMPLETENESS),
    (r"\bwhat\s+is\s+missing\s+before\s+submission\b", Intent.COMPLETENESS),

    # verification, before the generic document patterns
    #
    # "is", but also "has ... been", "was", "were", "have". A field officer
    # asks the same question five ways and none of them is unusual; matching
    # only "is the PAN verified" sent "has the PAN been verified" to UNKNOWN.
    (rf"\b(is|are|was|were|has|have)\s+(the\s+)?{_DOC_TYPES}\b"
     rf".{{0,24}}\b(verified|ok|okay|valid|done|fine|passed|cleared)\b",
     Intent.DOCUMENT_VERIFICATION),
    (rf"\bwhy\b.{{0,40}}\b{_DOC_TYPES}\b.{{0,30}}\b(fail|failed|review|rejected)\b",
     Intent.DOCUMENT_VERIFICATION),
    (r"\bwhy\s+did\s+(this|the|it)\b.{0,30}\b(fail|rejected|review)\b",
     Intent.DOCUMENT_VERIFICATION),
    (r"\b(verification\s+(status|result)|which\s+documents?\s+(failed|passed))\b",
     Intent.DOCUMENT_VERIFICATION),
    # The same question about the whole bundle rather than one type. Without
    # this it fell through to UNKNOWN and then to the knowledge base, which
    # would answer what verification MEANS to someone asking whether THIS
    # case's documents passed.
    (r"\b(is|are|was|were|has|have)\s+(the\s+|all\s+(the\s+)?|any\s+(of\s+the\s+)?)?"
     r"documents?\b.{0,24}\b(verified|passed|cleared|ok|okay|done)\b",
     Intent.DOCUMENT_VERIFICATION),
    # "Which documents have been verified?" -- asking the set, not one type.
    (r"\b(which|what)\s+documents?\b.{0,24}\b(verified|passed|cleared|"
     r"rejected|failed)\b",
     Intent.DOCUMENT_VERIFICATION),
    (r"\bdocuments?\s+(issues?|problems?|need\s+attention)\b", Intent.DOCUMENT_VERIFICATION),

    # documents
    (r"\b(which|what)\s+documents?\s+(are\s+)?(missing|not\s+uploaded|left|remaining|still\s+needed)\b",
     Intent.DOCUMENTS_MISSING),
    (r"\b(missing|outstanding)\s+documents?\b", Intent.DOCUMENTS_MISSING),
    (r"\bwhich\s+docs?\s+are\s+(left|missing|pending)\b", Intent.DOCUMENTS_MISSING),
    # THE QUESTION WITHOUT THE WORD "DOCUMENT" IN IT.
    #
    # "Show me what I still have to collect" is the plainest way a field
    # officer asks this and it matched nothing -- it fell through to
    # UNKNOWN, and from there to the knowledge base, which answered it
    # confidently out of the FOS handbook with a paragraph about
    # verification states. A confident irrelevant answer is worse than no
    # answer: the officer reads it, learns nothing, and stops trusting the
    # copilot.
    #
    # The verb carries the meaning here, not the noun. "Collect", "gather"
    # and "get" with a still/left/remaining marker are asking what is
    # outstanding whether or not the word "document" appears.
    (r"\b(still|left|yet|remaining)\b.{0,24}\b(collect|gather|obtain|get)\b",
     Intent.DOCUMENTS_MISSING),
    (r"\b(collect|gather|obtain)\b.{0,16}\b(still|left|yet|remaining)\b",
     Intent.DOCUMENTS_MISSING),
    (r"\bwhat\s+(else\s+)?(do|does|should)\s+(i|we|the\s+customer|"
     r"the\s+applicant)\s+need\s+to\s+(collect|bring|submit|upload|provide)\b",
     Intent.DOCUMENTS_MISSING),
    (r"\bwhat\s+(else\s+)?is\s+(still\s+)?(needed|required|outstanding)\b",
     Intent.DOCUMENTS_MISSING),
    (r"\bwhat\s+(else\s+)?(do|should)\s+i\s+(need|have)\s+to\s+ask\s+"
     r"(for|the\s+customer)\b", Intent.DOCUMENTS_MISSING),
    # WHY, before WHAT. These resemble the checklist patterns below and
    # would be swallowed by them; a question asking for the reason must not
    # be answered with the list.
    (r"\bwhy\s+(does|do)\s+(the\s+)?(checklist|list|requirements?)\b",
     Intent.POLICY_EXPLANATION),
    (r"\bwhy\s+(is|are)\s+(the\s+)?(checklist|list)\b.{0,24}"
     r"\b(provisional|not\s+final|incomplete|changing)\b",
     Intent.POLICY_EXPLANATION),
    (r"\bwhy\s+(is|are|do|does)\b.{0,40}\b(required|needed|asked\s+for|"
     r"mandatory)\b.{0,30}\b(for\s+this|on\s+this|here)\b",
     Intent.POLICY_EXPLANATION),
    (r"\b(which|what)\s+(policy|rule|rules)\b.{0,30}"
     r"\b(applied|apply|applies|used)\b", Intent.POLICY_EXPLANATION),
    (r"\b(explain|show\s+me)\s+(the\s+)?(policy|rules?)\b.{0,24}"
     r"\b(checklist|documents?|case|application)\b",
     Intent.POLICY_EXPLANATION),
    (r"\bwhat\s+(policy|rule|version)\b.{0,24}\b(is|was)\s+(this|it)\b",
     Intent.POLICY_EXPLANATION),
    (r"\bwhat\s+else\s+(do\s+you|does\s+the\s+system)\s+need\s+to\s+know\b",
     Intent.POLICY_EXPLANATION),

    # The checklist patterns come FIRST. "Show me the document checklist" also
    # matches the generic show/list-documents pattern below, and whichever is
    # listed first wins -- so the more specific question has to be.
    (r"\b(document|documents)\s+checklist\b", Intent.DOCUMENTS_REQUIRED),
    (r"\b(what|which)\s+documents?\s+(are\s+)?(required|needed|do\s+we\s+need)\b",
     Intent.DOCUMENTS_REQUIRED),
    (r"\b(show|list)\s+.{0,25}\bchecklist\b", Intent.DOCUMENTS_REQUIRED),
    (r"\b(which|what)\s+documents?\s+(have\s+been\s+)?(uploaded|collected|received|submitted)\b",
     Intent.DOCUMENTS_UPLOADED),
    (r"\b(show|list)\s+.{0,20}\bdocuments?\b", Intent.DOCUMENTS_UPLOADED),
    (r"\b(which|what)\s+documents?\s+(are\s+)?(pending|processing|under\s+review|in\s+review)\b",
     Intent.DOCUMENTS_PENDING),
    (r"\ball\s+required\s+documents?\s+(available|uploaded|there)\b", Intent.DOCUMENTS_MISSING),

    # pending / next action
    (r"\bwhat\s+(should|do)\s+i\s+do\s+next\b", Intent.NEXT_ACTION),
    (r"\bnext\s+(action|step)\b", Intent.NEXT_ACTION),
    (r"\bwhat\s+should\s+i\s+(ask|collect|complete)\b", Intent.NEXT_ACTION),
    (r"\bwhat('?s| is)\s+(pending|outstanding|left)\b", Intent.PENDING_ITEMS),
    (r"\bwhat\s+is\s+blocking\b", Intent.PENDING_ITEMS),
    (r"\bpending\s+(items?|actions?|things?)\b", Intent.PENDING_ITEMS),
    (r"\bwhat('?s| is)\s+stopping\b", Intent.PENDING_ITEMS),

    # application
    (r"\b(application|case)\s+(status|state)\b", Intent.APPLICATION_STATUS),
    (r"\bwhat('?s| is)\s+the\s+(application|case)\s+status\b", Intent.APPLICATION_STATUS),
    (r"\b(current\s+)?stage\b", Intent.APPLICATION_STAGE),
    (r"\bwhere\s+is\s+(this|the)\s+application\b", Intent.APPLICATION_STAGE),
    (r"\b(loan\s+)?product\s+(selected|chosen|is)\b", Intent.APPLICATION_STATUS),
    (r"\bwhen\s+was\s+the\s+application\s+created\b", Intent.APPLICATION_STATUS),

    # applicant
    (r"\bwhat\s+information\s+is\s+(still\s+)?missing\b", Intent.APPLICANT_MISSING_INFO),
    (r"\bmissing\s+(applicant\s+)?(information|details|fields)\b", Intent.APPLICANT_MISSING_INFO),
    (r"\b(applicant|customer)\s+(details|information|profile|data)\b", Intent.APPLICANT_DETAILS),
    (r"\bwho\s+is\s+the\s+applicant\b", Intent.APPLICANT_DETAILS),
    (r"\b(phone|mobile|email|address|name|dob|date\s+of\s+birth)\s+(number\s+)?(of|for)?\b.{0,20}\bapplicant\b",
     Intent.APPLICANT_DETAILS),
    (r"\bapplicant('?s)?\s+(phone|mobile|email|address|name)\b", Intent.APPLICANT_DETAILS),
    (r"\bshow\s+(me\s+)?(the\s+)?applicant\b", Intent.APPLICANT_DETAILS),
    (r"\bwhat\s+(information|data)\s+have\s+we\s+captured\b", Intent.APPLICANT_DETAILS),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), i) for p, i in _PATTERNS]
_COMPILED_OOS = [(re.compile(p, re.IGNORECASE), r) for p, r in _OUT_OF_SCOPE]
_DOC_RE = re.compile(_DOC_TYPES, re.IGNORECASE)

#: Spoken forms -> the stored document_type.
_DOC_ALIASES = {
    "pan": "PAN",
    "aadhaar": "AADHAAR", "aadhar": "AADHAAR",
    "driving licence": "DRIVING_LICENCE", "driving license": "DRIVING_LICENCE",
    "dl": "DRIVING_LICENCE",
    "voter id": "VOTER_ID", "voter": "VOTER_ID",
    "passport": "PASSPORT",
    "bank statement": "BANK_STATEMENT",
    "address proof": "ADDRESS_PROOF",
}


def _document_type(message: str) -> str | None:
    match = _DOC_RE.search(message)
    if not match:
        return None
    key = re.sub(r"\s+", " ", match.group(0).strip().lower())
    return _DOC_ALIASES.get(key)


_MOBILE_RE = re.compile(r"\b(\d{10})\b")
_EMAIL_RE = re.compile(r"\b([\w.+-]+@[\w-]+\.[\w.]+)\b")


def _write_fields(message: str) -> dict[str, str]:
    """
    Values the FOS stated in the message, for a proposed write.

    Extracted here so the proposal can be shown back for confirmation. Nothing
    is applied from this: the write tool is called with these values only
    after the FOS confirms.
    """
    fields: dict[str, str] = {}
    mobile = _MOBILE_RE.search(message)
    if mobile and re.search(r"\b(phone|mobile|number|contact)\b", message, re.I):
        fields["mobile"] = mobile.group(1)
    email = _EMAIL_RE.search(message)
    if email:
        fields["email"] = email.group(1)
    return fields


def classify(message: str) -> Classification:
    """Decide what the message is asking for. Deterministic; no model."""
    text = (message or "").strip()
    if not text:
        return Classification(Intent.UNKNOWN, confidence="low")

    for pattern, route in _COMPILED_OOS:
        if pattern.search(text):
            return Classification(
                Intent.OUT_OF_SCOPE, route_to=route, matched_on=pattern.pattern[:60]
            )

    # A STRONG knowledge marker outranks the case patterns.
    #
    # "What documents are required for a personal loan?" names a product, so
    # it is asking what the product requires -- policy -- not what THIS case
    # is missing. The generic case pattern matched it first and answered from
    # the case, which is a different question and a different answer.
    if _STRONG_KNOWLEDGE.search(text):
        return Classification(Intent.FOS_KNOWLEDGE, matched_on="product")

    for pattern, intent in _COMPILED:
        if pattern.search(text):
            # A case question with a second clause asking what/which/how is
            # asking two things. Answering only the first leaves the officer
            # with "three documents are missing" and no idea what satisfies
            # them; answering only the second recites policy at someone who
            # asked about a specific case.
            if intent not in WRITE_INTENTS and _MIXED_TAIL.search(text):
                return Classification(
                    Intent.MIXED,
                    document_type=_document_type(text),
                    matched_on=pattern.pattern[:60],
                    base_intent=intent,
                )
            return Classification(
                intent,
                document_type=_document_type(text),
                matched_on=pattern.pattern[:60],
                fields=_write_fields(text) if intent in WRITE_INTENTS else {},
            )

    # Nothing about this case matched. It may still be a question about how
    # the FOS stage works, which the knowledge base answers.
    if looks_like_knowledge(text):
        return Classification(Intent.FOS_KNOWLEDGE, matched_on="knowledge")

    # UNKNOWN is not the end of the road. The knowledge base gets a chance,
    # and its own confidence threshold decides whether there is an answer --
    # which is a better judge than a list of phrasings somebody has to keep
    # extending. If retrieval is not confident, the agent says so.
    return Classification(Intent.UNKNOWN, confidence="low")


#: Intent -> the tools that answer it. The planner reads this; it does not
#: invent tool names, and a model cannot add one.
PLANS: dict[Intent, tuple[str, ...]] = {
    Intent.APPLICANT_DETAILS: ("applicant.get",),
    Intent.APPLICANT_MISSING_INFO: ("applicant.get", "workflow.pending_items"),
    Intent.APPLICATION_STATUS: ("application.get",),
    Intent.APPLICATION_STAGE: ("applicant.360",),
    Intent.DOCUMENTS_UPLOADED: ("documents.get",),
    Intent.DOCUMENTS_REQUIRED: ("documents.checklist",),
    Intent.POLICY_EXPLANATION: ("documents.checklist",),
    Intent.DOCUMENTS_MISSING: ("documents.checklist",),
    Intent.DOCUMENTS_PENDING: ("documents.get", "workflow.pending_items"),
    Intent.DOCUMENT_VERIFICATION: ("documents.verification",),
    Intent.PENDING_ITEMS: ("workflow.pending_items",),
    Intent.NEXT_ACTION: ("workflow.next_action",),
    Intent.READINESS: ("workflow.readiness",),
    Intent.COMPLETENESS: ("workflow.readiness", "workflow.pending_items"),
    Intent.FULL_SUMMARY: ("applicant.360",),
}


#: Tools whose result already carries the checklist.
_CARRIES_CHECKLIST = frozenset({"applicant.360", "documents.checklist"})


def _with_checklist(plan: tuple[str, ...], has_case: bool) -> tuple[str, ...]:
    """
    Every case-scoped answer carries the document checklist.

    A FOS who asks "what should I do next?" gets the action AND the checklist
    it was derived from, so the screen behind the answer renders from one call
    instead of two. What makes a call case-scoped is that a case_id came with
    it, NOT which tools the intent happens to need: "show me the applicant"
    asked against an open case is a question asked from a case screen, and
    that screen has a checklist on it.

    Without a case_id the checklist is not planned -- there is nothing to
    build one for, and asking would only produce a NOT_FOUND to report.
    """
    if not plan or not has_case:
        return plan
    if any(tool in _CARRIES_CHECKLIST for tool in plan):
        return plan
    return plan + ("documents.checklist",)


def plan_for(
    classification: Classification,
    *,
    has_case: bool = True,
) -> tuple[str, ...]:
    """
    Which tools to call. Empty for out-of-scope, unknown and write intents,
    which are handled before any read happens.

    `has_case` says whether a case_id was supplied; without one the checklist
    tool is not appended, because it would only fail.
    """
    if classification.intent in (Intent.OUT_OF_SCOPE, Intent.UNKNOWN,
                                 Intent.FOS_KNOWLEDGE):
        return ()

    # A mixed question needs the case data its case half would have needed.
    if classification.intent is Intent.MIXED:
        base = classification.base_intent or Intent.FULL_SUMMARY
        return _with_checklist(PLANS.get(base, ("applicant.360",)), has_case)
    if classification.intent in WRITE_INTENTS:
        return ()
    # A verification question that named no document falls back to the whole
    # document list, so "which documents failed?" still answers.
    if (classification.intent is Intent.DOCUMENT_VERIFICATION
            and not classification.document_type):
        return _with_checklist(("documents.get",), has_case)
    return _with_checklist(PLANS.get(classification.intent, ()), has_case)


__all__ = [
    "Classification", "Intent", "PLANS", "SIMPLE_INTENTS", "WRITE_INTENTS",
    "looks_like_knowledge",
    "classify", "plan_for",
]
