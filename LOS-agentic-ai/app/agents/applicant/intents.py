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
    # ONE recorded detail from the FOS form -- "what loan amount did I
    # enter?", "which mobile is registered?" -- answered from the applicant /
    # application record (profile.py). `fields["field"]` names it; ALL means
    # "what have I submitted".
    APPLICANT_PROFILE = "APPLICANT_PROFILE"

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

    # -- HOW A STAGE WORKS, as opposed to what happened on a case
    #
    # "What does RCU check?" asks about the process. It is not a
    # question about this case, and it is not a request to do
    # something downstream -- but it contains the word RCU, which the
    # out-of-scope rule routes away on sight. That rule exists so the
    # FOS stage never answers a FRAUD question out of the FOS
    # handbook, and it is right to. This intent is matched BEFORE it,
    # narrowly enough that "is this case fraudulent" still routes
    # downstream while "what does RCU check" is answered from the
    # stage guide.
    STAGE_PROCESS = "STAGE_PROCESS"

    # -- the APPLICANT, across all their cases
    #
    # Every other intent answers about ONE case. This one answers about
    # the person: how many applications they have and where each stands.
    # It is the only read that runs without a case_id, and it is still
    # scoped -- `applications.list` takes an applicant_id and returns
    # that applicant's own rows.
    CASE_PORTFOLIO = "CASE_PORTFOLIO"

    # -- what was FOUND, as opposed to what is true now
    #
    # "Why is this case in review?" is not answerable from current state:
    # the state says REVIEW, not why. The reasons were recorded by the LOS
    # pipeline at the time and read back from case memory.
    CASE_HISTORY = "CASE_HISTORY"

    # -- what the documents SAY about income, and what they EVIDENCE
    #
    # "Is my salary verified?" has three honest answers depending on
    # what was uploaded: a salary slip STATES a figure, a bank statement
    # EVIDENCES credits arriving, and only where both exist is there a
    # comparison to report. None of the three is "your salary is X",
    # which is the answer a model would reach for.
    INCOME_EVIDENCE = "INCOME_EVIDENCE"

    # -- whether the loan asked for is affordable on the evidence
    #
    # READ, NEVER COMPUTED. The verdict, the FOIR and the instalment were
    # produced by the Eligibility stage and recorded; this intent reads
    # them back. A chat answer that worked out its own FOIR would give
    # one applicant two percentages.
    ELIGIBILITY = "ELIGIBILITY"

    # -- what ONE DOCUMENT on this case says
    #
    # "What is the name on my PAN?" had no case route at all: it fell
    # through to the knowledge base and came back with a handbook
    # paragraph about PAN cards. The answer is a recorded value, read
    # from that applicant's released extraction of that document type --
    # and from no other document.
    DOCUMENT_DETAILS = "DOCUMENT_DETAILS"

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
    Intent.APPLICANT_PROFILE,
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
    Intent.CASE_HISTORY,
    Intent.CASE_PORTFOLIO,
    Intent.STAGE_PROCESS,
    # A recorded value is quoted, never phrased: a paraphrased name is a
    # different name.
    Intent.DOCUMENT_DETAILS,
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
    #: The message as classified, when normalisation changed it.
    normalized: str | None = None
    #: "Which documents are verified / under review / rejected" -- the
    #: document status the list is filtered to. None lists every document.
    status_filter: str | None = None

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
    # A KIND OF APPLICANT is a general rule too: a case already has one.
    r"|\bfor\s+(a|an|any)\s+(self[\s_-]*employed|salaried|business|"
    r"professional|non[\s_-]*resident)\s+(applicant|customer|borrower|"
    r"person|individual|case)s?\b"
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
     # "Is my KYC done?" is the same question as "is the KYC done?" -- the
     # KYC decision is the downstream KYC process's, whoever's KYC it is.
     r"|\bis\s+(the\s+|my\s+|our\s+)?kyc\s+(clear|clean|done|ok|passed|complete|"
     r"completed|cleared|finished)\b"
     r"|\bhas\s+(the\s+|my\s+|our\s+)?kyc\s+(been\s+)?(done|completed|cleared|passed)\b",
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
    r"voter|passport|bank\s*statement|bank\s*account|bank\s*details|"
    r"account\s*statement|address\s*proof|"
    # The lender's taxonomy: income, property and business evidence.
    r"salary\s*slip|pay\s*slip|payslip|itr|income\s*tax\s*return|form\s*16|"
    r"sale\s*deed|mark\s*sheet|marksheet|business\s*proof\s*[12])"
)

#: The seven stage names as a question writes them. Kept here rather
#: than imported so the classifier has no dependency on the stage
#: model, and spelled with the long forms an officer actually types.
_STAGE_WORDS = {
    "FOS": ("fos", "field officer", "field stage"),
    "CPA": ("cpa", "central processing"),
    "CREDIT": ("credit",),
    "RCU": ("rcu", "risk containment"),
    "BOPS": ("bops", "back office", "back-office"),
    "HOPS": ("hops", "head office", "head-office"),
    "DISBURSEMENT": ("disbursement", "disbursal", "disburse"),
}

#: Asking HOW something works, rather than what happened on a case.
_PROCESS_VERB = (r"(check|checks|checked|verify|verifies|verified|"
                 r"validate|validates|validated|do|does|done|"
                 r"happen|happens|happened|require|requires|required|"
                 r"involve|involves|involved|mean|means|"
                 r"review|reviews|reviewed|cover|covers|covered|"
                 r"entail|entails|perform|performs|performed)")

#: "what does RCU check", "what happens at CPA", "what is verified in
#: CREDIT". BOTH ORDERS, because both are natural and a question that
#: matched only one would look arbitrary to whoever typed the other.
_STAGE_PROCESS = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\b(what|which|how)\b[^?]{0,40}\b(%s)\b[^?]{0,40}\b%s\b"
        % ("|".join(w for words in _STAGE_WORDS.values() for w in words),
           _PROCESS_VERB),
        r"\b(what|which|how)\b[^?]{0,30}\b%s\b[^?]{0,40}\b(%s)\b"
        % (_PROCESS_VERB,
           "|".join(w for words in _STAGE_WORDS.values() for w in words)),
        # A STAGE NAMED BARE. "What is RCU?" asks what the stage is; the
        # out-of-scope rule refused it on the word "rcu" while "what does
        # RCU mean" was answered from the stage guide.
        r"^\s*what\s+(is|are)\s+(the\s+)?(%s)(\s+stage)?\s*\??\s*$"
        % "|".join(w for words in _STAGE_WORDS.values() for w in words),
        r"\bwhat\s+does\s+(%s)\s+stand\s+for\b"
        % "|".join(w for words in _STAGE_WORDS.values() for w in words),
        # "THIS STAGE" names no stage: the route answers it from the stage
        # the case record establishes (stage_in returns None).
        #
        # A PROCESS VERB IS REQUIRED. "What IS my stage?" asks WHICH stage
        # the case is at -- a case fact -- and an earlier form of this
        # pattern accepted a bare "is", so the commonest wording of the
        # current-stage question was answered with a stage guide while
        # "where is my stage?" was answered from the case.
        r"\b(what|how)\s+(does|do|happens)\b[^?]{0,20}\b(this|my|the\s+current|current)\s+stage\b"
        r"|\bhow\s+is\s+(this|my|the\s+current|current)\s+stage\s+%s\b"
        r"|\bwhat\s+happens\s+(in|at|during)\s+(this|my|the\s+current|current)\s+stage\b"
        r"|\bwhat\s+(is|does)\s+(this|my|the\s+current)\s+stage\s+(mean|about|for|involve)" % _PROCESS_VERB,
    )
)


#: THE CURRENT-STAGE QUESTION, in its canonical English forms.
#:
#: Every language and short form reaches this after normalisation
#: (language.py -> normalize.py): "mera stage kya hai", "main kis stage pe
#: hu", "मेरा आवेदन किस चरण में है" all arrive as English words and are
#: matched by the SAME rules as "what is my stage?". Matched before the
#: stage-history and stage-process rules, which would otherwise take it on
#: the word "stage". A named stage ("what is CPA?") or a process verb
#: ("what does my stage involve?") never matches: the owner word is
#: followed only by the stage noun and an optional "now".
_STAGE_NOUN = r"(stage|step|phase)"
_OWNER = r"(my|our|this|the)"
_THING = r"(loan\s+)?(application|case|file|loan)"
_CURRENT_STAGE = re.compile(
    "|".join((
        # what is my stage / where is my current stage / what's the stage now
        rf"^\s*(what|where|which)\s+is\s+{_OWNER}\s+(current\s+|present\s+|"
        rf"application\s+|case\s+)?{_STAGE_NOUN}(\s+(now|right\s+now|currently|"
        rf"at\s+the\s+moment))?\s*[?.!]*\s*$",
        # what stage am i in / which stage is my application at / what step
        # is it on
        rf"\b(what|which)\s+(current\s+)?{_STAGE_NOUN}\s+(am\s+i|are\s+we|is\s+"
        rf"(it|{_OWNER}\s+{_THING}))\b(?!\s+(going|moving|headed)\b)",
        # tell me my stage / show my current stage
        rf"^\s*(tell|show|give)\s+(me\s+)?({_OWNER}\s+)?(current\s+)?{_STAGE_NOUN}"
        rf"\s*[?.!]*\s*$",
        # current stage? / my current stage? / my stage?
        rf"^\s*({_OWNER}\s+)?(current\s+|present\s+)?{_STAGE_NOUN}\s*\??\s*$",
        # where is my application in the process / at which stage is my case
        rf"\bwhere\s+is\s+{_OWNER}\s+{_THING}\s+(in|on)\s+the\s+(process|"
        rf"pipeline|journey|workflow|lifecycle)\b",
        rf"\b(at|in|on)\s+(what|which)\s+{_STAGE_NOUN}\s+is\s+{_OWNER}\s+{_THING}\b",
        rf"\bwhat\s+{_STAGE_NOUN}\s+is\s+{_OWNER}\s+{_THING}\s+(at|on|in)\b",
    )),
    re.IGNORECASE,
)


def asks_current_stage(message: str) -> bool:
    """Whether this asks which stage the case is at now (a CASE question)."""
    text = (message or "").strip()
    return bool(text) and bool(_CURRENT_STAGE.search(text)) and (
        stage_in(text) is None)


def stage_in(message: str) -> str | None:
    """
    The stage a question NAMES, or None.

    EXPLICIT ONLY. This reads the words the user typed; it never
    infers a stage from similarity or from the case. A process
    question that names no stage returns None, and the caller falls
    back to the case's own stage rather than guessing.
    """
    lowered = (message or "").lower()
    for stage, words in _STAGE_WORDS.items():
        for word in words:
            if re.search(r"\b" + re.escape(word) + r"\b", lowered):
                return stage
    return None


#: A QUESTION ABOUT WHERE THE CASE HAS BEEN, not where it is or how a
#: stage works: "where was my application before CPA", "when did my
#: application move to CPA", "why did it move to CPA", "stage history".
#: Answered from the recorded stage history (status_facts.
#: stage_history_answer) -- never inferred.
_STAGE_HISTORY_ALWAYS = re.compile(
    r"\bstage\s+history\b|\b(previous|prior|earlier|last)\s+stage\b"
    # "What changed?" is answered from the recorded stage history.
    r"|^\s*what(\s+has|'s|\s+have)?\s+changed\b"
    r"|\bwhere\s+was\s+(it|my|the|this|our)\b[^?]{0,30}\bbefore\b"
    r"|\bstages?\b[^?]{0,30}\b(been|gone|passed)\s+through\b",
    re.IGNORECASE)
_STAGE_HISTORY_BEFORE = re.compile(
    r"\b(where|which|what)\b[^?]{0,50}\b(was|were)\b[^?]{0,40}\bbefore\b",
    re.IGNORECASE)
_STAGE_HISTORY_AFTER = re.compile(
    r"\bwhat\s+(has\s+)?happened\s+(after|since)\b", re.IGNORECASE)
_STAGE_HISTORY_MOVE = re.compile(
    r"\b(when|why)\b[^?]{0,50}\b(move|moved|go|went|sent|enter|entered|"
    r"reach|reached|transfer\w*|shift\w*|hand\w*|promot\w*|advanc\w*|"
    r"come|came|get|got)\b",
    re.IGNORECASE)
_STAGE_HISTORY_SUBJECT = re.compile(
    r"\b(my|this|our|the)\s+(loan\s+)?(application|case|file|loan)\b|\bit\b",
    re.IGNORECASE)


#: WHAT FOLLOWS FROM A GAP: "what happens because this document is
#: pending", "what if this isn't fixed". Answered from the workflow's own
#: readiness record (what blocks the handoff) -- never from a model's idea
#: of the consequence. Where no readiness is recorded for the stage, the
#: stage gate says it is not available.
_IMPACT = re.compile(
    r"\bwhat\s+(happens|will\s+happen|would\s+happen|is\s+the\s+impact)\b"
    r"[^?]{0,60}\b(pending|missing|not\s+(fixed|resolved|provided|submitted|"
    r"uploaded)|isn'?t\s+(fixed|resolved|provided)|if\s+i\s+don'?t|if\s+not)\b"
    r"|\b(impact|consequence|effect)s?\s+of\b[^?]{0,40}\b(pending|missing|"
    r"issue|problem|document|mismatch)",
    re.IGNORECASE)


def asks_impact(message: str) -> bool:
    return bool(_IMPACT.search(message or ""))


def asks_stage_history(message: str) -> bool:
    """Whether the question asks where the case HAS BEEN in the lifecycle."""
    text = message or ""
    if _STAGE_HISTORY_ALWAYS.search(text):
        return True
    names_stage = (stage_in(text) is not None
                   or re.search(r"\bstage\b", text, re.IGNORECASE))
    if not names_stage:
        return False
    if _STAGE_HISTORY_BEFORE.search(text):
        return True
    # "What happened after FOS?" -- PAST tense about a named stage is this
    # case's journey; "what happens after CPA" (present) is how the process
    # works, and stays with the stage guides.
    if _STAGE_HISTORY_AFTER.search(text):
        return True
    # "Why is my case in RCU?" -- why it is AT a stage is why it moved there.
    if (stage_in(text) is not None
            and re.search(r"\bwhy\b[^?]{0,40}\b(in|at)\s+(the\s+)?\w+", text,
                          re.IGNORECASE)
            and _STAGE_HISTORY_SUBJECT.search(text)):
        return True
    return bool(_STAGE_HISTORY_MOVE.search(text)
                and _STAGE_HISTORY_SUBJECT.search(text))


#: Words that make a question about THIS CASE. A message carrying
#: one of them is asking what happened here, not how a desk works --
#: "what happened to this case from FOS to RCU" names two stages and
#: a process verb, and is a case journey question. Answering it from
#: a stage guide would describe the process to somebody who asked
#: about their file.
_CASE_DEIXIS = re.compile(
    r"\b(this|my|the|our)\s+(case|application|applicant|file|loan|"
    r"customer)\b|\bcase[ _-]?id \b", re.IGNORECASE)


def looks_like_stage_process(message: str) -> bool:
    """Whether this is a question about how a stage works."""
    text = (message or "").strip()
    if _CASE_DEIXIS.search(text):
        # ASKED ABOUT A CASE. "What happened to this case from FOS to
        # RCU" names two stages and a process verb and is still a
        # question about a file, not about a desk. Answering it from a
        # stage guide would describe the process to somebody who asked
        # what happened to them. It keeps its existing case route --
        # MIXED when it asks both things, which answers the case half
        # from records and the rest from knowledge.
        return False
    return any(pattern.search(text) for pattern in _STAGE_PROCESS)


def _asks_how_a_stage_works(message: str) -> bool:
    """
    The phrasing alone, WITHOUT the case test.

    Used only to decide whether the out-of-scope rule should stand
    aside. See `classify`.
    """
    return any(pattern.search((message or "").strip())
               for pattern in _STAGE_PROCESS)


_PATTERNS: list[tuple[str, Intent]] = [
    # -- about the PERSON, not about one case ----------------------------
    #
    # FIRST, because "how many cases do I have" also matches the generic
    # status patterns, and answering it from the current case reports one
    # case's status to somebody who asked about all of them.
    (r"\bhow\s+many\b.{0,20}\b(cases?|applications?|loans?)\b",
     Intent.CASE_PORTFOLIO),
    # PLURAL, OR AN EXPLICIT MULTI-CASE WORD. A bare "my" was too
    # greedy: "what is the status of my application?" is a question
    # about THE case in front of the officer, and routing it here
    # answered with a list of every application the person has.
    (r"\b(all|other|previous|past)\s+(of\s+)?(my\s+)?(cases?|applications?)\b",
     Intent.CASE_PORTFOLIO),
    (r"\bmy\s+(cases|applications)\b", Intent.CASE_PORTFOLIO),
    (r"\bacross\b.{0,20}\b(cases?|applications?)\b", Intent.CASE_PORTFOLIO),
    (r"\b(list|show)\b.{0,20}\b(cases?|applications?)\b",
     Intent.CASE_PORTFOLIO),
    # AFFORDABILITY, BEFORE INCOME AND BEFORE VERIFICATION.
    #
    # "Why is my eligibility under review" contains "review" and would
    # otherwise reach the case-history patterns, which answer from the
    # KYC findings -- a real answer to a different question.
    (r"\beligib\w*\b", Intent.ELIGIBILITY),
    (r"\bfoir\b", Intent.ELIGIBILITY),
    (r"\b(afford|affordab\w+)\b", Intent.ELIGIBILITY),
    (r"\b(emi|instal?ment)\b[^?]{0,30}"
     r"\b(proposed|calculated|computed|what|how\s+much)\b"
     r"|\b(what|how\s+much)\b[^?]{0,20}\b(emi|instal?ment)\b",
     Intent.ELIGIBILITY),
    (r"\b(obligations?|liabilit\w+)\b[^?]{0,30}"
     r"\b(considered|counted|used|recorded|captured)\b",
     Intent.ELIGIBILITY),
    (r"\bwhat\s+income\b[^?]{0,30}\b(used|considered|counted)\b",
     Intent.ELIGIBILITY),

    # WHAT ONE DOCUMENT SAYS, before income and verification.
    #
    # "What is my PAN name?", "which name is on my PAN?", "what name was
    # extracted from my PAN?", "the account holder name on my bank
    # statement". A field AND a document, in the case's own voice.
    # NEVER A COMPARISON: anything asking whether two things match is a
    # finding, answered from the recorded KYC comparison below -- hence
    # the guard at the front of each pattern. And never a definition:
    # "what does PAN name mismatch mean" has reached the handbook before
    # these are tried.
    (r"^(?!.*\b(mis)?match)(?!.*\bdiffer).*\b(my|the|this|his|her|their)\s+(full\s+|complete\s+|entire\s+)?(pan(\s*card)?|salary\s*slip|pay\s*slip|payslip|bank\s*statement|bank\s*account|driving\s*licen[cs]e|voter\s*id|passport|aadhaa?r)\s+(card\s+)?(name|number|dob|date\s+of\s+birth)\b",
     Intent.DOCUMENT_DETAILS),
    (r"^(?!.*\b(mis)?match)(?!.*\bdiffer).*"
     r"\b(name|father'?s?\s*name|date\s+of\s+birth|dob|birth\s*date|"
     r"pan\s*(number|no)|employer|account\s*holder)\b[^?]{0,40}"
     r"\b(on|in|from|of)\s+(my|the|this|his|her|their)?\s*(pan(\s*card)?|salary\s*slip|pay\s*slip|payslip|bank\s*statement|bank\s*account|driving\s*licen[cs]e|voter\s*id|passport|aadhaa?r)\b",
     Intent.DOCUMENT_DETAILS),
    (r"\bwho\s+is\s+the\s+(bank\s+)?account\s+holder\b",
     Intent.DOCUMENT_DETAILS),

    # INCOME, BEFORE THE VERIFICATION PATTERNS.
    #
    # "Is my salary verified?" matches the generic
    # <thing> + verified pattern, which answers from document
    # verification -- a statement that the salary slip is a readable,
    # coherent document, which is not what was asked. What the slip
    # states and what the statement evidences are recorded separately,
    # and the difference between them is the answer.
    (r"\b(salary|income|pay|earnings)\b[^?]{0,40}"
     r"\b(shown|stated|state|says?|on)\b[^?]{0,25}"
     r"\b(salary\s*slip|slip|payslip|pay\s*slip)\b",
     Intent.INCOME_EVIDENCE),
    (r"\b(salary|income|credits?)\b[^?]{0,40}"
     r"\b(bank\s*statement|statement|account)\b",
     Intent.INCOME_EVIDENCE),
    (r"\b(bank\s*statement|statement)\b[^?]{0,30}"
     r"\b(support|match\w*|confirm\w*|back\s*up)\b[^?]{0,25}"
     r"\b(salary|income|pay)\b",
     Intent.INCOME_EVIDENCE),
    (r"\b(salary\s*slip|payslip|pay\s*slip)\b[^?]{0,30}"
     r"\b(match\w*|agree\w*|consistent|compare\w*)\b",
     Intent.INCOME_EVIDENCE),
    (r"\b(income|salary)\b[^?]{0,20}\bmismatch\b"
     r"|\bmismatch\b[^?]{0,20}\b(income|salary)\b",
     Intent.INCOME_EVIDENCE),
    (r"\bwhy\b[^?]{0,30}\b(income|salary)\b[^?]{0,30}"
     r"\b(review|checked|flagged|pending)\b",
     Intent.INCOME_EVIDENCE),
    (r"\b(how\s+much|what)\b[^?]{0,30}"
     r"\b(monthly\s+)?(income|salary|earnings)\b[^?]{0,30}"
     r"\b(evidenc\w+|observ\w+|support\w+|verified|shown)\b",
     Intent.INCOME_EVIDENCE),
    (r"\bis\b[^?]{0,15}\b(my|the|his|her|their)\s+"
     r"(salary|income)\b[^?]{0,20}\b(verified|confirmed|checked)\b",
     Intent.INCOME_EVIDENCE),

    # -- why is it like this? --------------------------------------------
    #
    # FIRST, because "why is this case in review" also matches the generic
    # status patterns further down, and the status answer ("it is in
    # review") is not what was asked.
    (r"\bwhy\b.{0,40}\b(in\s+)?(review|pending|rejected|failed|flagged)\b",
     Intent.CASE_HISTORY),
    (r"\bwhy\b.{0,30}\b(this\s+)?case\b", Intent.CASE_HISTORY),
    # "Summarise this document / my documents" -- the case's documents and
    # where each stands (one named document: its verification).
    (r"\bsumm?ar(y|i[sz]e|i[sz]ing)\b[^?]{0,25}\b(this|the|my|these|all|uploaded)?\s*"
     r"(documents?|docs?|files?|uploads?|papers?)\b", Intent.DOCUMENTS_UPLOADED),
    # "I uploaded it three times and it's STILL pending" -- a complaint that
    # is a pending-documents question. Narrow: "still pending" said of it /
    # them; "why is it still pending" is taken by the rule above.
    (r"\b(it'?s|it\s+is|they'?re|they\s+are|is|are)\s+still\s+pending\b",
     Intent.DOCUMENTS_PENDING),
    # "Why hasn't my application moved?" -- the recorded reasons, and the
    # delay explanation the agent attaches to a CASE_HISTORY delay question.
    (r"\bwhy\b[^?]{0,40}\b(hasn'?t|has\s+not|isn'?t|is\s+not|not|didn'?t|did\s+not)"
     r"\s+(it\s+|my\s+\w+\s+|this\s+\w+\s+)?(moved|moving|move|progress\w*|advanced|"
     r"gone\s+ahead)\b", Intent.CASE_HISTORY),
    # WHAT IS HOLDING IT UP. "What's causing the delay?", "why is my file
    # on hold?", "what is holding up my case?" ask for the reason, which is
    # recorded -- not for the status, which is what "file" alone suggested.
    (r"\b(why|what)\b[^?]{0,30}\b(on\s+hold|held\s+up|delay\w*|stuck|"
     r"holding\s+(up|back)|holding\b[^?]{0,20}\b(up|back))\b",
     Intent.CASE_HISTORY),
    # WHAT THE MISMATCH ACTUALLY IS, on this case.
    #
    # "What exactly is the mismatch in my documents?" routed to the
    # knowledge base and came back with a definition of the word -- to
    # an officer looking at a case whose PAN and bank statement name two
    # different people, with both names already recorded. The question
    # has a specific, recorded answer and it is in case memory.
    #
    # THE DEFINITIONAL GUARD ABOVE IS WHAT KEEPS THIS HONEST. "What is a
    # document mismatch" reaches the handbook before these patterns are
    # tried, so widening the case route did not narrow the generic one.
    (r"\bwhat\b[^?]{0,20}\bis\b[^?]{0,20}\bthe\s+mismatch\b",
     Intent.CASE_HISTORY),
    (r"\bmismatch\b[^?]{0,30}\b(in|on|with|for)\b[^?]{0,20}"
     r"\b(my|our|his|her|their|this|the)\b[^?]{0,20}"
     r"\b(documents?|case|application|file)\b",
     Intent.CASE_HISTORY),
    (r"\bwhy\b[^?]{0,40}\b(is|are|was|were)\b[^?]{0,30}\bmismatch\b",
     Intent.CASE_HISTORY),
    (r"\bwhat\s+mismatch\b[^?]{0,30}\b(was|were|is|are)\b[^?]{0,20}"
     r"\b(found|detected|identified|recorded|reported)\b",
     Intent.CASE_HISTORY),
    # WHICH ONES DISAGREE. "Which details don't match?" and "which names
    # don't match?" name the field, and the recorded finding names the
    # values -- which is the whole of what is being asked.
    (r"\b(which|what)\b[^?]{0,20}"
     r"\b(details?|names?|fields?|values?|dates?)\b[^?]{0,25}"
     r"\b(do\s*n.?t|do\s+not|does\s*n.?t|does\s+not|mismatch\w*|"
     r"disagree\w*|differ\w*)\b",
     Intent.CASE_HISTORY),
    # ANYTHING WRONG WITH THE CASE ITSELF, as opposed to with one
    # document -- the document pattern further down keeps that.
    (r"\bwhat\b[^?]{0,15}\b(is|are)\b[^?]{0,15}\bwrong\b[^?]{0,20}"
     r"\b(with|on|in)\b[^?]{0,20}"
     r"\b(my|our|his|her|their|this|the)\b[^?]{0,20}"
     r"\b(case|application|file|documents?)\b",
     Intent.CASE_HISTORY),
    # WHAT WAS FOUND. "What issue was found in my documents?" is asking
    # for the finding, which is recorded; answered from the checklist it
    # would list every document instead.
    (r"\b(what|which)\b[^?]{0,20}"
     r"\b(issues?|problems?|errors?|discrepanc\w+|concerns?)\b[^?]{0,30}"
     r"\b(found|detected|identified|raised|recorded|reported)\b",
     Intent.CASE_HISTORY),
    (r"\bwhat\s+(findings?|reasons?)\b", Intent.CASE_HISTORY),
    # "Show me the important issues" -- the case's recorded problems, asked
    # for without naming a document (a named one is a verification question
    # further down).
    (r"^(?![^?]*\b(bank|statement|documents?|docs?|pan|deed|passport|slip|"
     r"licen[cs]e|aadhaa?r|voter)\b)"
     r"[^?]*\b(show|list|what\s+are)\b[^?]{0,25}\b(issues|problems|concerns)\b",
     Intent.CASE_HISTORY),
    # DO TWO IDENTITY DOCUMENTS AGREE? The recorded KYC comparison is the
    # answer, naming both values; neither document alone is.
    (r"\b(does|do|is|are)\b[^?]{0,20}"
     r"\b(pan|aadhaa?r|driving\s*licen[cs]e|voter\s*id|passport)\b"
     r"[^?]{0,20}\b(match\w*|agree\w*|same)\b",
     Intent.CASE_HISTORY),
    (r"\bwhich\s+documents?\b[^?]{0,30}"
     r"\b(mismatch\w*|do\s*n.?t\s+match|does\s*n.?t\s+match|differ\w*)\b",
     Intent.CASE_HISTORY),
    # THE RECORDED DECISION -- what the pipeline concluded, which is not
    # the loan decision the out-of-scope rule keeps downstream.
    (r"\b(current|latest|recorded)\s+decision\b", Intent.CASE_HISTORY),
    (r"\b(findings?|reasons?)\b.{0,30}\b(caused|led\s+to|behind)\b",
     Intent.CASE_HISTORY),
    (r"\bwhat\s+happened\b.{0,30}\b(with|to)\b", Intent.CASE_HISTORY),
    (r"\bcase\s+history\b", Intent.CASE_HISTORY),
    # ANYTHING WRONG WITH A PARTICULAR DOCUMENT. Kept to documents
    # deliberately: "what is wrong with this case" is a findings
    # question, and the case history above already answers it.
    (r"\b(issues?|problems?|wrong|errors?|concerns?)\b[^?]{0,40}"
     r"\b(bank|account|statement|document|pan|deed)\b",
     Intent.DOCUMENT_VERIFICATION),
    # WHAT IS ON FILE -- the document list, not the findings.
    (r"\b(what|which|any)\b[^?]{0,30}\bdocuments?\b[^?]{0,40}"
     r"\b(available|uploaded|submitted|on\s+file|received|do\s+we\s+have|are\s+there)\b",
     Intent.DOCUMENTS_UPLOADED),
    # WHAT A DOCUMENT ESTABLISHED. "Which bank details were
    # verified" and "was the bank statement verified" are asking
    # about a verification outcome, and fell to UNKNOWN -- answered
    # with a menu of other questions. Routed to the verification
    # intent, they are answered from what verification actually
    # recorded, which is a status and a reason, never the account
    # number: extracted identity values are deliberately not
    # indexed and cannot be retrieved by a question.
    (r"\b(bank|account|statement|salary|income)\b[^?]{0,40}"
     r"\b(verified|verif\w+|checked|confirmed|validated)\b",
     Intent.DOCUMENT_VERIFICATION),
    (r"\b(verified|verif\w+|checked|confirmed)\b[^?]{0,40}"
     r"\b(bank|account|statement)\b",
     Intent.DOCUMENT_VERIFICATION),
    # WHICH DOCUMENTS CAUSED IT is a question about the findings,
    # not about the document list. Answered from the checklist it
    # would name every document on the case, including the ones
    # that passed -- and the two or three that did not are the
    # whole of what was asked.
    (r"\b(which|what)\s+documents?\b[^?]{0,40}"
     r"\b(caus\w+|led\s+to|trigger\w*|responsible|behind|flagg\w+)\b",
     Intent.CASE_HISTORY),
    # "What happened BEFORE this reached Credit" is a question about
    # the trail, and the trail is recorded. Without this it fell to
    # UNKNOWN and was answered with a menu of other questions.
    (r"\bwhat\s+happened\b.{0,40}\b(before|prior|earlier|leading)\b",
     Intent.CASE_HISTORY),
    # writes, before the reads they resemble
    (r"\b(create|add|register)\s+(a\s+)?(new\s+)?applicant\b", Intent.CREATE_APPLICANT),
    (r"\b(create|start|open)\s+(a\s+)?(new\s+)?application\b", Intent.CREATE_APPLICATION),
    (r"\b(update|change|set|correct)\b.{0,40}\b(phone|mobile|number|email|address|name|dob|date\s+of\s+birth)\b",
     Intent.UPDATE_APPLICANT),
    (r"\b(mark|flag|request)\b.{0,30}\b(re-?upload|reupload|again)\b", Intent.MARK_FOR_REUPLOAD),

    # composite summary
    (r"\b(complete|full|entire|overall)\s+(applicant\s+|case\s+)?(summary|overview|picture|status)\b",
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
    # "Can I proceed to the next stage?" asks whether the handoff is ready,
    # not which stage the case is in -- the stage pattern below would take
    # it on the word "stage".
    (r"\bcan\s+(i|we|it|this|(my|this|the)\s+(case|application|file))\s+"
     r"(proceed|move|go|progress|advance)\b[^?]{0,25}"
     r"\b(next\s+stage|forward|ahead|further|cpa)\b",
     Intent.READINESS),
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
    # "Is MY PAN verified?" too -- the possessive sent it to UNKNOWN.
    (rf"\b(is|are|was|were|has|have)\s+(the\s+|my\s+|his\s+|her\s+|their\s+)?{_DOC_TYPES}\b"
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
    (r"\bdocuments?\s+(issues?|problems?|needs?\s+attention|requires?\s+attention"
     r"|has\s+(an\s+)?(issue|problem))\b", Intent.DOCUMENT_VERIFICATION),

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
    (r"\b(what|which)\s+documents?\s+(are\s+|is\s+)?(required|needed|do\s+(we|i)\s+need)\b",
     Intent.DOCUMENTS_REQUIRED),
    (r"\b(show|list)\s+.{0,25}\bchecklist\b", Intent.DOCUMENTS_REQUIRED),
    (r"\b(which|what)\s+documents?\s+(have\s+been\s+)?(uploaded|collected|received|submitted)\b",
     Intent.DOCUMENTS_UPLOADED),
    (r"\b(show|list)\s+.{0,20}\bdocuments?\b", Intent.DOCUMENTS_UPLOADED),
    # AN ADVERB DOES NOT CHANGE THE QUESTION. "What documents are
    # STILL pending" fell through this pattern to UNKNOWN and was
    # answered out of the handbook -- policy text, about no case,
    # to somebody asking what is outstanding on the one in front
    # of them.
    (r"\b(which|what)\s+documents?\s+(are\s+|is\s+)?(still\s+|currently\s+|yet\s+to\s+be\s+)?(pending|processing|outstanding|awaited|under\s+review|in\s+review)\b",
     Intent.DOCUMENTS_PENDING),
    (r"\ball\s+required\s+documents?\s+(available|uploaded|there)\b", Intent.DOCUMENTS_MISSING),
    (r"\bstatus\s+of\s+(all\s+)?(my|the|these|our)?\s*(documents|docs)\b", Intent.DOCUMENTS_UPLOADED),
    (r"\b(what|which)\s+(documents|docs)\s+(have|has)\s+(i|we|been)\s+(submitted|uploaded|given|sent)\b",
     Intent.DOCUMENTS_UPLOADED),

    # pending / next action
    (r"\bwhat\s+(should|do)\s+i\s+do\s+next\b", Intent.NEXT_ACTION),
    (r"\bnext\s+(action|step)\b", Intent.NEXT_ACTION),
    (r"\bwhat\s+should\s+i\s+(ask|collect|complete)\b", Intent.NEXT_ACTION),
    (r"\bwhat('?s| is)\s+(pending|outstanding|left)\b", Intent.PENDING_ITEMS),
    (r"\bwhat\s+is\s+blocking\b", Intent.PENDING_ITEMS),
    # "How can I complete this application?" -- what is still outstanding.
    (r"\bhow\s+(can|do|should)\s+(i|we)\s+(complete|finish|finali[sz]e)\b",
     Intent.PENDING_ITEMS),
    (r"\bpending\s+(items?|actions?|things?)\b", Intent.PENDING_ITEMS),
    (r"\bwhat('?s| is)\s+stopping\b", Intent.PENDING_ITEMS),

    # application
    (r"\b(application|case)\s+(status|state)\b", Intent.APPLICATION_STATUS),
    # THE SAME QUESTION, WORDED THE WAY PEOPLE WORD IT.
    #
    # The pattern above needs "application status" side by side, so
    # "what is the status OF MY application" missed it, fell to
    # UNKNOWN, and was handed to the knowledge base -- which
    # answered confidently, out of the handbook, about no case in
    # particular. An officer asking what is happening with a file
    # got a paragraph of process documentation.
    (r"\b(status|state|progress|update)\b[^?]{0,30}\b(of|on|for|with)\b[^?]{0,20}\b(my|the|this|his|her|their)?\s*(application|case|loan|file)\b",
     Intent.APPLICATION_STATUS),
    (r"\bwhat\s+(is|\'s)\s+happening\b[^?]{0,30}\b(application|case|loan|file)\b",
     Intent.APPLICATION_STATUS),
    # A BARE "what is the status?" IS ABOUT THE CASE IN HAND. The
    # request carries an applicant and a case; there is nothing
    # else it could be asking about.
    (r"^\s*what\s+(is|\'s)\s+the\s+(current\s+)?(status|state)\s*\??\s*$",
     Intent.APPLICATION_STATUS),
    # A DOCUMENT NAMED WITH A STATUS WORD, OR ALONE. "pan status",
    # "bank statement status", "my pan?" -- the officer names the
    # document and wants to know where it stands.
    (r"^\s*(is\s+)?(my|the|this)?\s*%s(\s+card)?\s+(status|verified|verification)\b" % _DOC_TYPES,
     Intent.DOCUMENT_VERIFICATION),
    (r"^\s*(my|the|this)?\s*%s(\s+card)?\s*\??\s*$" % _DOC_TYPES,
     Intent.DOCUMENT_VERIFICATION),
    # "address proof pending?" -- is this document still outstanding.
    (r"^\s*(is\s+)?(my|the)?\s*%s\s+(still\s+)?(pending|missing|outstanding)\b" % _DOC_TYPES,
     Intent.DOCUMENTS_PENDING),
    # "What exactly is wrong?" asked against a case is about that case.
    (r"^\s*what\s+(exactly\s+)?(is|'s)\s+(wrong|the\s+(problem|issue))\s*\??\s*$",
     Intent.CASE_HISTORY),
    (r"\bwhat\s+(exactly\s+)?(is|'s)\s+wrong\s+with\s+(my|the|this)\s+documents?\b",
     Intent.DOCUMENT_VERIFICATION),
    # "Are my documents verified" is a question about the documents
    # on this case, not about what verification means.
    (r"\b(are|is|have|has)\b[^?]{0,20}\bdocuments?\b[^?]{0,20}\b(verified|verif\w+|checked|cleared|passed)\b",
     Intent.DOCUMENT_VERIFICATION),
    (r"\bwhat('?s| is)\s+the\s+(application|case)\s+status\b", Intent.APPLICATION_STATUS),
    (r"\b(current\s+)?stage\b", Intent.APPLICATION_STAGE),
    (r"\bwhere\s+is\s+(this|the)\s+application\b", Intent.APPLICATION_STAGE),
    # A STATUS QUESTION ABOUT ONE'S OWN CASE, IN THE REMAINING WORDINGS.
    #
    # "Where does my application stand", "what is my application's
    # status", "where is my case", "how is my loan doing" matched none
    # of the patterns above, fell to UNKNOWN, and the knowledge
    # fallback answered them confidently out of the handbook -- a
    # paragraph about policy versions, published as FOS_KNOWLEDGE with
    # no tool run, to an officer asking where one case stands. Every
    # pattern here needs an owner word (my / this / our / the) against
    # application, case, loan or file, so a generic "what are
    # application statuses" still goes to the handbook.
    (r"\b(my|this|our|the)\s+(loan\s+)?(application|case|loan|file)(\s*['’]\s*s)?\s+(status|state|progress)\b",
     Intent.APPLICATION_STATUS),
    # "Where DOES my application STAND" asks for its status; "where IS my
    # application" asks where it is -- the stage.
    (r"\bwhere\s+(does|do)\s+(my|this|our|the)\s+(loan\s+)?(application|case|loan|file)\b",
     Intent.APPLICATION_STATUS),
    (r"\bwhere\s+is\s+(my|our)\s+(loan\s+)?(application|case|loan|file)\b",
     Intent.APPLICATION_STAGE),
    (r"\bhow\s+is\s+(my|this|our|the)\s+(loan\s+)?(application|case|loan|file)\s+(doing|going|progressing|coming\s+along)\b",
     Intent.APPLICATION_STATUS),
    (r"\b(has|have)\s+(my|this|our|the)\s+(loan\s+)?(application|case|loan|file)\s+(moved|progressed|advanced)\b",
     Intent.APPLICATION_STATUS),
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
    # THE STATEMENT IS THE ONLY BANK EVIDENCE THERE IS. "Which bank
    # account details were verified" names no document, and without
    # these it was answered from the whole case -- which is how a
    # question about a statement that passed came back saying the
    # bank details were not verified.
    "bank account": "BANK_STATEMENT",
    "bank details": "BANK_STATEMENT",
    "account statement": "BANK_STATEMENT",
    "address proof": "ADDRESS_PROOF",
    "salary slip": "SALARY_SLIP", "pay slip": "SALARY_SLIP",
    "payslip": "SALARY_SLIP",
    "itr": "ITR", "income tax return": "ITR",
    "form 16": "FORM_16",
    "sale deed": "SALE_DEED",
    "mark sheet": "MARK_SHEET", "marksheet": "MARK_SHEET",
    "business proof 1": "BUSINESS_PROOF_1", "business proof1": "BUSINESS_PROOF_1",
    "business proof 2": "BUSINESS_PROOF_2", "business proof2": "BUSINESS_PROOF_2",
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


#: Asking what something MEANS, rather than what happened.
#:
#: WHY THIS IS A GUARD AND NOT JUST PATTERN ORDER. The case patterns
#: below deliberately catch "what is the mismatch in my documents", and
#: a rule that catches that will catch "what is a document mismatch"
#: unless something stands in the way. The two questions differ by one
#: article and by everything else: one wants this applicant's PAN and
#: bank statement, the other wants the handbook.
_DEFINITION_RE = re.compile(
    r"\bwhat\s+(do(es)?|did)\b[^?]{0,40}\bmean\b"
    r"|\bwhat\s+(is|are)\s+(a|an)\b"
    r"|\bhow\s+do(es)?\b[^?]{0,40}\bwork\b"
    r"|\bwhat\s+are\s+(the\s+)?(different|possible|various)?\s*"
    r"[a-z ]{0,30}\b(statuses|stages|types|states)\b"
    r"|\bdefinition\s+of\b"
    r"|\bmeaning\s+of\b"
    # A BARE TERM. "What is FOIR?" and "what is eligibility?" ask what
    # the word means; "what is my eligibility?" asks about this case and
    # does not match -- nothing may stand between the verb and the term.
    r"|^\s*what\s+(is|are)\s+(pan|kyc|foir|ltv|emi|eligibility|"
    r"affordability|aadhaa?r|income\s+consistency|"
    r"(pan\s+)?name\s+mismatch)\s*\??\s*$"
    # A BARE DOCUMENT NAME. "What is address proof?" asks what the thing
    # is; "what is my address proof status" has a word in between.
    rf"|^\s*what\s+(is|are)\s+(an?\s+)?{_DOC_TYPES}(\s+card)?\s*\??\s*$",
    re.IGNORECASE,
)


def asks_for_a_definition(text: str) -> bool:
    """Whether this asks what a thing is, rather than what happened here."""
    return bool(_DEFINITION_RE.search(text or ""))


#: "my application", "this case", "our loan file" -- the question names
#: the case in hand as its subject.
_OWN_CASE_RE = re.compile(
    r"\b(my|this|our)\s+(loan\s+)?(application|case|loan|file)s?\b",
    re.IGNORECASE,
)


def asks_about_own_case(text: str) -> bool:
    """Whether the question is about the caller's own case, by its words."""
    return bool(_OWN_CASE_RE.search(text or ""))


_DOCUMENT_STATUS_LIST = re.compile(
    r"\b(which|what|any|list|show)\b[^?]{0,12}\b(documents?|docs?)\b\s+"
    r"(are|is|were|was|have\s+been|has\s+been|got)\s+(still\s+|currently\s+)?"
    r"(?P<status>verified|approved|accepted|cleared|under\s+review|in\s+review|"
    r"being\s+reviewed|rejected|declined|failed)\b",
    re.IGNORECASE,
)

_STATUS_WORDS_TO_FILTER = {
    "verified": "VERIFIED", "approved": "VERIFIED", "accepted": "VERIFIED",
    "cleared": "VERIFIED",
    "under review": "REVIEW", "in review": "REVIEW", "being reviewed": "REVIEW",
    "rejected": "REJECTED", "declined": "REJECTED", "failed": "REJECTED",
}


#: "Why was my PAN rejected?" / "why did the bank statement fail?" -- a
#: named document and a failure word.
_DOCUMENT_REJECTED = re.compile(
    rf"\bwhy\b.{{0,40}}\b{_DOC_TYPES}\b.{{0,30}}\b(rejected|declined|failed)\b",
    re.IGNORECASE)
#: ...unless it is the loan or application that was rejected.
_LOAN_REJECTED = re.compile(
    r"\b(loan|application|case|file)\s+(was\s+|is\s+|been\s+|got\s+)?"
    r"(rejected|declined)\b", re.IGNORECASE)

def _mixed_with_knowledge_tail(text: str) -> Classification | None:
    """
    MIXED, when the message is a case question AND a knowledge question.

    STRUCTURAL, NOT A PHRASE LIST: the message is split at the clause the
    MIXED tail marks ("and what ...", ", which ..."), the first clause is
    classified by the same rules as any question, and the second must be
    a knowledge question by the same knowledge rules. Neither half is
    guessed: a first clause that is not a case question, or a second that
    asks something downstream, leaves the message to the rules below.
    """
    match = _MIXED_TAIL.search(text)
    if not match or match.start() == 0:
        return None
    head = text[:match.start()].strip(" ,;")
    tail = re.sub(r"^[\s,;]*((and|also|plus)\s+)?", "", text[match.start():],
                  flags=re.IGNORECASE)
    if not head or not tail:
        return None

    # THE SECOND CLAUSE IS KNOWLEDGE, and not a downstream question
    # dressed as one ("... and what is the KYC decision?").
    knowledge = (asks_for_a_definition(tail) or looks_like_knowledge(tail)
                 or bool(_STRONG_KNOWLEDGE.search(tail))
                 or _asks_how_a_stage_works(tail))
    if not knowledge:
        return None
    if not _asks_how_a_stage_works(tail) and any(
            pattern.search(tail) for pattern, _ in _COMPILED_OOS):
        return None

    case = classify(head)
    if case.intent not in _MIXED_BASES:
        return None
    return Classification(Intent.MIXED, document_type=case.document_type,
                          matched_on=f"mixed:{case.matched_on}",
                          base_intent=case.intent)


def classify(message: str) -> Classification:
    """Decide what the message is asking for. Deterministic; no model."""
    text = (message or "").strip()
    if not text:
        return Classification(Intent.UNKNOWN, confidence="low")

    # BEFORE THE OUT-OF-SCOPE RULE, and only just. That rule routes
    # anything containing "rcu" or "fraud" downstream so the FOS stage
    # cannot answer a fraud question from the FOS handbook -- correct,
    # and it would also swallow "what does RCU check", which is a
    # question about the process rather than about this case. The
    # match below requires BOTH a stage name and process phrasing, so
    # "is this case fraudulent" is untouched.
    # WHERE THE CASE HAS BEEN. Before the stage-process and out-of-scope
    # rules, which would otherwise take "when did it move to RCU" on the
    # stage name alone. Answered from the recorded stage history.
    # WHICH STAGE THE CASE IS AT NOW. First of the stage rules: the
    # history and process rules below both key on the word "stage" and
    # used to take "what is my stage?" as a question about how a stage
    # works.
    if asks_current_stage(text):
        return Classification(Intent.APPLICATION_STAGE,
                              matched_on="current_stage")

    if asks_stage_history(text):
        return Classification(Intent.APPLICATION_STAGE,
                              matched_on="stage_history")

    # WHAT FOLLOWS FROM A GAP, from the recorded readiness.
    if asks_impact(text):
        return Classification(Intent.READINESS, matched_on="impact")

    if looks_like_stage_process(text):
        return Classification(Intent.STAGE_PROCESS, matched_on="stage")

    # AND THE SAME QUESTION ASKED ABOUT A CASE KEEPS ITS CASE ROUTE.
    #
    # "Why is this case under review and what does the RCU stage
    # check?" is a case question with a process clause -- the MIXED
    # route below is exactly what answers it. The out-of-scope rule
    # would take it first, on the word "rcu" alone, and reply that a
    # downstream process handles it.
    #
    # NARROW ON PURPOSE. Standing aside requires what/which/how, a
    # stage name AND a process verb. "Is this case fraudulent" and
    # "analyse the bank transactions on this case" have none of that
    # shape and still route downstream, so the boundary that keeps FOS
    # out of fraud analysis is untouched.
    asks_process = _asks_how_a_stage_works(text)

    # WHICH DOCUMENTS ARE IN A STATUS. "Which documents were rejected?"
    # is about uploads on this case, not a lending decision -- but the
    # out-of-scope rule routes on the word "rejected" alone. Matched
    # narrowly: a documents noun AND a document status.
    listed = _DOCUMENT_STATUS_LIST.search(text)
    if listed:
        wanted = _STATUS_WORDS_TO_FILTER[
            re.sub(r"\s+", " ", listed.group("status").lower())]
        # A document in review is PENDING -- the long-standing contract for
        # "which documents are under review" -- and is listed on its own.
        return Classification(
            Intent.DOCUMENTS_PENDING if wanted == "REVIEW"
            else Intent.DOCUMENTS_UPLOADED,
            matched_on="document_status_list", status_filter=wanted)

    # A CASE QUESTION WITH A KNOWLEDGE CLAUSE ATTACHED. Before the rules
    # below, which read the WHOLE message and would each take it on the
    # second clause alone: "why is my application under review and what
    # does KYC mean?" is a definition by its tail, and was answered from
    # the handbook with the case's reason dropped.
    split = _mixed_with_knowledge_tail(text)
    if split is not None:
        return split

    # WHY ONE DOCUMENT FAILED. "Why was my PAN rejected?" is about a
    # document on this case -- the out-of-scope rule would route it
    # downstream as a loan decision, on the word "rejected" alone.
    if _DOCUMENT_REJECTED.search(text) and not _LOAN_REJECTED.search(text):
        return Classification(Intent.DOCUMENT_VERIFICATION,
                              document_type=_document_type(text),
                              matched_on="document_rejected")

    if not asks_process:
        for pattern, route in _COMPILED_OOS:
            if pattern.search(text):
                return Classification(
                    Intent.OUT_OF_SCOPE, route_to=route,
                    matched_on=pattern.pattern[:60],
                )

    # THE CALLER'S OWN RECORDED DETAILS: one field from the FOS form, or
    # "what have I submitted". Before the knowledge rules, which took "what
    # name did I provide?" as a question about name matching, and before the
    # generic case patterns, which took "what's my loan amount?" as status.
    # A document named ("the name on my PAN") is left to DOCUMENT_DETAILS.
    from app.agents.applicant import profile

    if _document_type(text) is None:
        if profile.COMPLETENESS.search(text):
            return Classification(Intent.APPLICANT_MISSING_INFO,
                                  matched_on="basic_details_complete")
        asked = profile.detect(text)
        if asked is not None:
            return Classification(Intent.APPLICANT_PROFILE, matched_on="profile",
                                  fields={"field": asked.field})

    # A STRONG knowledge marker outranks the case patterns.
    #
    # "What documents are required for a personal loan?" names a product, so
    # it is asking what the product requires -- policy -- not what THIS case
    # is missing. The generic case pattern matched it first and answered from
    # the case, which is a different question and a different answer.
    if _STRONG_KNOWLEDGE.search(text):
        return Classification(Intent.FOS_KNOWLEDGE, matched_on="product")

    # A DEFINITION IS A HANDBOOK QUESTION, whatever it is a definition
    # of. "What does document mismatch mean" is answered from the
    # knowledge base; "what exactly is the mismatch in my documents" is
    # answered from the case, and the patterns below take it.
    if asks_for_a_definition(text):
        return Classification(Intent.FOS_KNOWLEDGE, matched_on="definition")

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


def understand(message: str, *, has_case: bool = False) -> Classification:
    """
    What the message means, in three steps, the rules always first.

      1. NORMALISE -- short forms, typos, Hinglish -> full words
         (normalize.py, driven by `chatbot.normalization`).
      2. CLASSIFY  -- the ordered rules above, unchanged.
      3. SEMANTIC  -- only when the rules found nothing, a case is in hand,
         and the question is not asking for a definition: the closest
         configured intent example (semantic.py), if close enough.

    Deterministic, no model. The result carries the normalised text when it
    differs, so a caller can report what the question was taken to mean.
    """
    from app.agents.applicant import normalize, semantic

    normalised = normalize.normalise(message)
    text = normalised.text or (message or "").strip()
    classification = classify(text)

    from app.agents.applicant import followup

    # A BARE "which document?" NAMES NOTHING. Unresolved by the follow-up
    # context, it is a clarification -- the semantic layer would otherwise
    # guess a document question about every document on the case.
    if (classification.intent is Intent.UNKNOWN and has_case
            and not asks_for_a_definition(text)
            and not followup.needs_context(text)):
        match = semantic.best(text)
        if match is not None:
            try:
                intent = Intent(match.intent)
            except ValueError:
                intent = None
            if intent is not None and intent not in WRITE_INTENTS and intent not in (
                    Intent.OUT_OF_SCOPE, Intent.UNKNOWN):
                classification = Classification(
                    intent, confidence="medium",
                    document_type=_document_type(text),
                    matched_on=f"semantic:{match.example}",
                )

    if normalised.changed:
        classification.normalized = text
    return classification


#: Intent -> the tools that answer it. The planner reads this; it does not
#: invent tool names, and a model cannot add one.
PLANS: dict[Intent, tuple[str, ...]] = {
    Intent.APPLICANT_DETAILS: ("applicant.get",),
    Intent.APPLICANT_MISSING_INFO: ("applicant.get", "workflow.pending_items"),
    Intent.APPLICANT_PROFILE: ("applicant.get", "application.get"),
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


#: A case half the MIXED route can answer: an intent with a planned tool, or
#: the recorded findings (agent.py answers MIXED from exactly these). Not a
#: write, a refusal, a person-level read, or an intent the agent answers on
#: a path of its own (eligibility, income, one document's values).
_MIXED_BASES = frozenset(PLANS) | {Intent.CASE_HISTORY}


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

    # Case history is read from case memory, not from a tool. The
    # application is still fetched so the answer can name the case it is
    # about rather than answering into the void.
    # The applicant's own applications. NO `has_case` guard: this is the
    # one read that is about the person rather than a case, and it is
    # scoped by applicant_id at the tool.
    # A stage guide is retrieved, not fetched with a tool.
    if classification.intent is Intent.STAGE_PROCESS:
        return ()

    if classification.intent is Intent.CASE_PORTFOLIO:
        return ("applications.list",)

    # One recorded detail needs its two records and nothing else -- no
    # checklist, no documents (minimum necessary).
    if classification.intent is Intent.APPLICANT_PROFILE:
        return PLANS[Intent.APPLICANT_PROFILE] if has_case else ()

    if classification.intent in (Intent.CASE_HISTORY,
                                 Intent.INCOME_EVIDENCE,
                                 Intent.ELIGIBILITY,
                                 Intent.DOCUMENT_DETAILS):
        return ("application.get",) if has_case else ()

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
    "asks_about_own_case", "looks_like_knowledge",
    "classify", "plan_for", "understand",
]
