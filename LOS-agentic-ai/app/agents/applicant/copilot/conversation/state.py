"""
CONVERSATION STATE -- what the conversation is about, kept between turns.

WHERE IT SITS. After the input guardrail and before anything is classified
or read: a turn is first read RELATIVE TO THE CONVERSATION (is there a
question of ours waiting for an answer? is this a correction, a
cancellation, an acknowledgement, "again"?), and only then as a question in
its own right. The state never chooses a tool, never reads a record and
never overrides ownership: it rewrites or annotates the MESSAGE, which the
ordinary pipeline then understands, authorises and answers exactly as if
typed.

WHAT IS KEPT. Labels and semantic metadata only: the last frame, the party
and stage the conversation is about, the documents the last answer listed,
the clarification that is waiting and its options as questions. No case
values, no record payloads, no identifiers beyond the case the conversation
is scoped to. State is keyed by the AUTHENTICATED SUBJECT and the
conversation id; another subject's conversation id opens nothing.

VOCABULARY, NOT SENTENCES. Affirmations, negations, cancellations,
corrections, ordinals and "again" are concept classes in
semantic_concepts.yaml (`conversation:`). An option is chosen by its
ORDINAL or by MEANING (the reply's semantic frame against each option's),
never by matching a sentence.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Outcomes of reading a turn against the conversation.
OPTION_RESOLVED = "OPTION_RESOLVED"
PARTIAL_RESOLUTION = "PARTIAL_RESOLUTION"
STILL_AMBIGUOUS = "STILL_AMBIGUOUS"
USER_REJECTED_CLARIFICATION = "USER_REJECTED_CLARIFICATION"
NEW_TOPIC = "NEW_TOPIC"
CANCELLATION = "CANCELLATION"
YES_NO_RESPONSE = "YES_NO_RESPONSE"
INVALID_OPTION = "INVALID_OPTION"
CORRECTION = "CORRECTION_OF_PREVIOUS_INTENT"
ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
NEGATION = "NEGATION"
REPLAY = "REPLAY"
PENDING_EXPIRED = "PENDING_EXPIRED"
ASKED = "CLARIFICATION_ASKED"          # the conversation layer asks, from state alone
REFUSED = "REFUSED_IN_CONTEXT"         # a pronoun pointing at a refused person
NO_STATE = "NO_STATE"

#: Questions that can be asked about ONE PARTY on the case (subjects.py).
PER_PARTY_INTENTS = frozenset({"DOCUMENT_VERIFICATION", "DOCUMENTS_PENDING", "DOCUMENTS_MISSING",
                               "PENDING_ITEMS", "READINESS", "DOCUMENTS_UPLOADED",
                               "DOCUMENT_DETAILS", "CASE_HISTORY", "APPLICANT_PROFILE",
                               "KYC_RESULT"})

#: The caller's own words ("my", "mera") -- an explicit self beats any
#: party the conversation was about.
_ADDRESSING = re.compile(r"\b(tell|show|give|send|help|let)\s+me\b|\bmujhe\s+(batao|bataiye|dikhao)\b",
                         re.IGNORECASE)


class _SelfWords:
    """The caller's own words ("my", "mera") -- never "tell me", which only
    addresses the assistant."""

    def __init__(self, pattern: "re.Pattern[str]") -> None:
        self._pattern = pattern

    def search(self, text: str):
        return self._pattern.search(_ADDRESSING.sub(" ", str(text or "")))


_SELF_WORDS_RAW = re.compile(r"\b(my|mine|me|i|myself|mera|meri|mere|mujhe|mujhko|apna|apni|apne)\b"
                         r"|मेरा|मेरी|मेरे|माझा|माझी|माझे|माझं", re.IGNORECASE)
_SELF_WORDS = _SelfWords(_SELF_WORDS_RAW)
#: A PERSON pronoun -- "their KYC", "for them", "unka mobile".
_PERSON_POSSESSIVE = re.compile(r"\b(their|his|her|unka|unki|unke|inka|inki|inke|uska|uski|uske)\b"
                                r"|उनका|उनकी|उनके|उसका|उसकी", re.IGNORECASE)
_PERSON_OBJECT = re.compile(r"\b(for|of|about|to)\s+(them|him|her)\b|\b(unko|unhe|unhen|usko)\b",
                            re.IGNORECASE)
_PERSON_SUBJECT = re.compile(r"\b(she|he|they)\b", re.IGNORECASE)
#: Refusals whose person a following pronoun may point at.
#: "that loan" / "the same case" / "us file" right after a refusal points at
#: the record that was refused -- never at the caller's own ("this" / "my").
_REFUSED_RECORD = re.compile(r"\b(that|same|us|woh|wo|usi)\s+(loan|case|file|application|"
                             r"customer|account|person|record)\b", re.IGNORECASE)
_PERSON_REFUSALS = frozenset({"CROSS_CUSTOMER_DATA", "UNAUTHORIZED_SUBJECT", "OTHER_CONVERSATION",
                              "BULK_DATA"})
#: "tell me more" / "what else?" / "detail mein batao" -- the next level of the
#: last answer; "why?" / "kyun?" -- its recorded reason.
_MORE = re.compile(r"^\s*(and\s+|aur\s+|ok\s*,?\s*|okay\s*,?\s*|accha\s*,?\s*)?"
                   r"(tell\s+me\s+more|more(\s+details?|\s+info)?|elaborate|go\s+on|what\s+else|"
                   r"anything\s+else|aur\s+kya|aur\s+kuch|kya\s+aur|"
                   r"detail\s+(me|mein|mai)\s+batao|details?\s+(do|dikhao|batao)|aur\s+batao|"
                   r"aur\s+bataiye|thoda\s+aur|और\s+बताओ|विस्तार\s+से\s+बताओ|आणखी\s+सांगा)"
                   r"\s*[?.!]*\s*$", re.IGNORECASE)
#: "and then?" / "phir?" -- what FOLLOWS from the last answer (its next step),
#: not more of the same answer.
_THEN = re.compile(r"^\s*(and\s+|aur\s+|so\s+|ok\s*,?\s*)?(then|then\s+what|what\s+now|what\s+next|phir|fir|"
                   r"phir\s+kya|uske\s+baad|aage\s+kya|फिर|फिर\s+क्या|आगे\s+क्या)\s*[?.!]*\s*$", re.IGNORECASE)
_WHY = re.compile(r"^\s*(and\s+|aur\s+|but\s+|par\s+|lekin\s+)?(why|why\s+so|how\s+come|kyun|kyon|kyu|"
                  r"kyun\s+atka(\s+hai)?|kyu\s+atka(\s+hai)?|क्यों)\s*[?.!]*\s*$", re.IGNORECASE)
#: WHERE AN ANSWER GOES NEXT: each capability's next level, as the question
#: the ordinary pipeline answers (never a sentence composed here).
_EXPAND_MORE = {
    "KYC_RESULT": "Which KYC fields did not match?",
    "APPLICATION_STATUS": "Why is my application under review?",
    "APPLICATION_STAGE": "Why is my application under review?",
    "DOCUMENTS_PENDING": "What is pending on my case?",
    "DOCUMENTS_MISSING": "What is pending on my case?",
    "DOCUMENTS_UPLOADED": "What is pending on my case?",
    "DOCUMENTS_REQUIRED": "What is pending on my case?",
    "DOCUMENT_VERIFICATION": "What is pending on my case?",
    "PENDING_ITEMS": "What should I do next?",
    "FULL_SUMMARY": "What is pending on my case?",
    # after the reason, more is EVERY recorded hold, per person, and its step
    "CASE_HISTORY": "Why is my application held up?",
}
#: What follows from an answer: its next step.
_EXPAND_THEN = {
    "CASE_HISTORY": "What should I do next?",
    "APPLICATION_STATUS": "What should I do next?",
    "APPLICATION_STAGE": "What should I do next?",
    "KYC_RESULT": "What should I do next?",
    "DOCUMENT_VERIFICATION": "What should I do next?",
    "DOCUMENTS_PENDING": "What should I do next?",
    "DOCUMENTS_MISSING": "What should I do next?",
    "PENDING_ITEMS": "What should I do next?",
    "READINESS": "What should I do next?",
    "NEXT_ACTION": "What is pending on my case?",
}
_EXPAND_WHY = {
    "KYC_RESULT": "Which KYC fields did not match?",
    "APPLICATION_STATUS": "Why is my application under review?",
    "APPLICATION_STAGE": "Why is my application under review?",
    "DOCUMENTS_PENDING": "Why are my documents still pending?",
    "DOCUMENTS_MISSING": "Why are my documents still pending?",
    "PENDING_ITEMS": "Why is my application under review?",
    "READINESS": "Why is my application under review?",
}
_BARE_THEIRS = re.compile(r"^\s*(and\s+|aur\s+|what\s+about\s+|how\s+about\s+)?"
                          r"(theirs|their\s+one|unka|unki|unke|unka\s+kya|unki\s+kya|unke\s+bare\s+mein)"
                          r"\s*[?.!]*\s*$", re.IGNORECASE)
#: A turn that continues the last one ("what about the mobile?", "and email?").
_FOLLOW_FORM = re.compile(r"^\s*(what\s+about|how\s+about|and|aur|also|or|then|uska|iska)\b",
                          re.IGNORECASE)


def _party_askable(state: "ConversationState") -> bool:
    """
    Whether the previous question means something asked of another PERSON.
    Documents, verification, pending items, KYC and a person's own details
    do; an application-wide field (loan amount, tenure, product) does not --
    "and the other applicant?" after "what is my loan amount?" is asked back.
    """
    last_intent = str((state.last_answer_reference or {}).get("intent") or "")
    if last_intent not in PER_PARTY_INTENTS:
        return False
    if last_intent != "APPLICANT_PROFILE":
        return True
    from app.agents.applicant.copilot.routing import capabilities
    from app.agents.applicant.copilot.answering import profile as _profile

    asked = _profile.detect(state.last_message or "")
    if asked is None:
        return True          # a person's detail, unless shown to be the loan's
    person = set(capabilities.APPLICANT_FIELDS) | {"aadhaar_number", "pan_number",
                                                   "bank_account_number", "applicant_id",
                                                   "employment_type", "declared_monthly_income",
                                                   "declared_monthly_obligations"}
    return asked.field.startswith("ALL") or all(f in person for f in asked.fields)


def _names_co(text: str) -> bool:
    """The co-applicant named in any language the lexicons read."""
    from app.agents.applicant import normalize
    from app.agents.applicant.copilot.routing import subjects as _subjects

    lowered = _strip(text)
    if re.search(r"\b(applicant|him|her)\b|सह-?\s?आवेदक|सह-?\s?अर्जदार", lowered):
        return True
    return _subjects.mentioned(normalize.normalise(text).text) is _subjects.Kind.CO


def _same_document(text: str, state: "ConversationState") -> bool:
    """ "the other PAN" after a PAN answer: the same document, another person's."""
    from app.agents.applicant.copilot.semantics import semantic_frame as _frames

    return bool(state.last_document) and _frames._document_type(text) == state.last_document


def _expanded(text: str, state: "ConversationState") -> "Reading | None":
    """ "tell me more" / "why?" after an answer: the next level of THAT answer."""
    if not state.last_message:
        return None
    last_intent = str((state.last_answer_reference or {}).get("intent") or "")
    about_co = state.active_party == "CO_APPLICANT"
    if _MORE.match(text) and last_intent == "FOS_KNOWLEDGE":
        topic = _knowledge_topic(state.last_message)
        if topic:
            return Reading(REPLAY, f"Explain {topic} in detail", note="the previous answer, expanded")
    if _MORE.match(text) and last_intent == "STAGE_PROCESS":
        # the stage guide was already given whole: more is the handbook's detail
        from app.agents.applicant.copilot.semantics.intents import stage_in as _stage_in

        stage = _stage_in(state.last_message)
        if stage:
            return Reading(REPLAY, f"Explain {stage} readiness in detail", note="the previous answer, expanded")
    if _MORE.match(text):
        if last_intent == "APPLICANT_PROFILE":
            from app.agents.applicant.copilot.answering import profile as _profile
            from app.agents.applicant.copilot.routing import capabilities

            asked = _profile.detect(state.last_message)
            fields = asked.fields if asked is not None else []
            if about_co:
                question = "Tell me the co-applicant details."
            elif asked is not None and asked.field == _profile.ALL_APPLICATION:
                # the whole application was already shown: the next level is
                # where it stands, not the applicant's contact details
                question = "What is the status and stage of my application?"
            elif fields and all(f in capabilities.APPLICATION_FIELDS for f in fields):
                question = "What are my application details?"
            else:
                question = "What are my applicant details?"
        else:
            question = _EXPAND_MORE.get(last_intent)
    elif _THEN.match(text):
        question = _EXPAND_THEN.get(last_intent)
    elif _WHY.match(text):
        question = _EXPAND_WHY.get(last_intent)
    else:
        return None
    if not question:
        return None
    if about_co and "co-applicant" not in question and last_intent in PER_PARTY_INTENTS:
        question = _for_co(question)
    return Reading(REPLAY, question, note="the previous answer, expanded")


#: What is left of a turn that is ONLY a party ("and the co-applicant's?",
#: "aur co-applicant ka?", "आणि सह-अर्जदाराचा?") once the filler is gone.
_PARTY_NEGATION = re.compile(r"\b(nahi|nahin|nai|no|not|nope|galat|arre|actually|i\s+mean|matlab)\b",
                             re.IGNORECASE)
_PARTY_FILLER = re.compile(r"\b(and|aur|what|about|how|the|my|our|of|for|ka|ki|ke|kya|bare|baare|mein|"
                           r"nahi|nahin|nai|no|not|nope|galat|arre|actually|mean|matlab|i|"
                           r"me|batao|tell|please|pls|jo|hai|uska|uski|accha|ok|okay|theek|thik|also|"
                           r"bhi|same|details?|info|s|is|are|hai|hain|ho|ji|kya|thik|theek|"
                           r"do|dena|dijiye|de|batao|bataiye|chahiye|dikhao|please|too|also|"
                           r"ठीक|है|और|आणि|का|की|के|चा|ची|चे)\b|[?.!,'\u2019-]|ठीक|है|और|आणि", re.IGNORECASE)


#: "the other one / guy / person", "dusra wala": a person named RELATIVE to the current one.
_RELATIVE_OTHER = re.compile(r"\b(other|dusr[aie]|doosr[aie])\b", re.IGNORECASE)
#: The whole relative reference, with the word for "one" that follows it.
_RELATIVE_PERSON = re.compile(r"\b(the\s+)?(other|dusr\w*|doosr\w*)(\s+(one|guy|person|wala|wale|wali|vala|vale))?\b",
                              re.IGNORECASE)
#: The co-applicant named as such (not relatively).
_NAMED_CO = re.compile(r"co[\s-]?(applicant|app|borrower)|सह-?\s?(आवेदक|अर्जदार)", re.IGNORECASE)


def _bare_party_turn(text: str, state: "ConversationState") -> "Reading | None":
    from app.agents.applicant import normalize as _normalize
    from app.agents.applicant.copilot.routing import subjects as _subjects

    said = _normalize.normalise(text).text or text
    relative = bool(_RELATIVE_OTHER.search(text))
    if _subjects.mentioned(said) is not _subjects.Kind.CO and not relative:
        return None
    if _subjects.mentioned(said) is not _subjects.Kind.CO and state.last_document             and any(d and d != state.last_document for d in state.last_documents):
        return None        # "the other one" may be the other DOCUMENT: offered, not chosen
    remainder = _subjects._CO_RE.sub(" ", said)
    remainder = _RELATIVE_PERSON.sub(" ", remainder)
    remainder = _PARTY_FILLER.sub(" ", remainder)
    if re.search(r"\w", remainder):
        return None
    if state.last_message and _party_askable(state) and state.active_party == "CO_APPLICANT"             and _RELATIVE_OTHER.search(text) and not _NAMED_CO.search(text):
        # "the other one" is RELATIVE to whoever the conversation is about:
        # while it is about the co-applicant, the other person is the caller
        return Reading(REPLAY, _for_self(state.last_message),
                       note="the previous question, for the other person (the primary applicant)")
    if state.last_message and _party_askable(state) and state.active_party != "CO_APPLICANT":
        if _PARTY_NEGATION.search(text):
            return Reading(CORRECTION, _for_co(state.last_message),
                           note="corrected to the co-applicant")
        return Reading(REPLAY, _for_co(state.last_message),
                       note="the previous question, for the co-applicant")
    return None


def _same_question_other_document(text: str, state: "ConversationState") -> "Reading | None":
    """ "Is my PAN verified?" then "aur bank statement?": the same question, of that document."""
    from app.agents.applicant import normalize as _normalize
    from app.agents.applicant.copilot.semantics import semantic_frame as _frames

    if not (state.last_message and state.last_document):
        return None
    bare = re.sub(r"^\s*(haan|han|ha|yes|ok|okay|accha|theek|thik)\b[\s,.-]*", "", text, flags=re.IGNORECASE)
    bare = re.sub(r"^\s*(and|aur|also|what\s+about|how\s+about|और|आणि)\s+", "", bare, flags=re.IGNORECASE)
    said = _normalize.normalise(bare).text or bare
    named = _frames._document_type(said)
    if not named or named == state.last_document or len(_strip(bare).split()) > 3:
        return None
    from app.agents.applicant.copilot.semantics import intents as _dintents

    leftover = _dintents._DOC_RE.sub(" ", said)
    leftover = re.sub(r"\b(what|about|how|the|a|an|and|aur|ka|ki|ke|kya|bhi|also|too|one|wala|wali|"
                      r"card|is|hai)\b|[?.!,]", " ", leftover, flags=re.IGNORECASE)
    if leftover.strip():
        return None            # "Is Aadhaar compulsory?" asks its own question
    old = _frames._document_type(state.last_message)
    if old != state.last_document:
        return None
    from app.agents.applicant.copilot.conversation.followup import _display

    # "What documents are accepted as address proof?" then "and passport?":
    # asks whether THAT document serves the slot -- not "accepted as passport"
    if (re.search(r"\b(accepted|accept|accepts|valid|allowed|satisf\w*|count\w*|chalega|chalta)\s+(as|for)\b",
                  state.last_message, re.IGNORECASE)
            or (old.endswith("_PROOF")
                and str((state.last_answer_reference or {}).get("intent") or "") == "FOS_KNOWLEDGE")) \
            and named != old:
        return Reading(REPLAY, f"Is a {_display(named)} accepted as {_display(old).lower()}?",
                       note="the previous question, for another document")
    spoken = re.compile("|".join(re.escape(v) for v in {
        _display(old), _display(old).lower(), old.replace("_", " ").lower(), old}), re.IGNORECASE)
    replayed, n = spoken.subn(_display(named), state.last_message, count=1)
    if not n:
        return None
    return Reading(REPLAY, replayed, note="the previous question, for another document")


_GOAL = re.compile(r"^\s*(i|we)\s+(want|would\s+like|need|wanted|wish)\s+to\s+(know|understand|learn|find\s+out|"
                   r"see|check)\s+(about\s+)?(?P<rest>.+?)\s*[.?!]*\s*$", re.IGNORECASE)


def _goal_question(text: str) -> str | None:
    """ "I want to know what's missing" -> "what's missing?"; "I want to
    understand the CPA process" -> "How does the CPA process work?"."""
    m = _GOAL.match(text)
    if not m:
        return None
    rest, verb = m.group("rest"), m.group(3).lower()
    if re.match(r"(what|why|which|who|how|when|where)\b", rest, re.IGNORECASE):
        return rest[0].upper() + rest[1:] + "?"
    if re.match(r"(whether|if)\s+", rest, re.IGNORECASE):
        return re.sub(r"^(whether|if)\s+", "", rest, flags=re.IGNORECASE) + "?"
    if verb in ("understand", "learn"):
        return f"How does {rest} work?"
    return f"What is {rest}?"


def _knowledge_topic(message: str) -> str:
    """What a knowledge question was about: the SLOT it named (address proof,
    income proof) over a document it mentioned, as a person says it."""
    from app.agents.applicant.copilot.semantics import intents as _kintents
    from app.agents.applicant.copilot.conversation.followup import _display

    found = []
    for match in _kintents._DOC_RE.finditer(str(message or "")):
        key = re.sub(r"\s+", " ", match.group(0).strip().lower())
        kind = _kintents._DOC_ALIASES.get(key)
        if kind and kind not in found:
            found.append(kind)
    if not found:
        return ""
    slot = next((k for k in found if k.endswith("_PROOF")), found[0])
    return _display(slot).lower()


def _for_co(message: str) -> str:
    """The previous question, asked of the co-applicant instead of the caller."""
    text = str(message or "").strip()
    swapped, n = re.subn(r"\b(my|mera|meri|mere)\b", "the co-applicant's", text, count=1,
                         flags=re.IGNORECASE)
    if n:
        return swapped
    return f"{text.rstrip('?')} for the co-applicant?"


#: "and mine?", "what about me?", "aur mera?" -- the caller, and nothing else asked.
_BARE_MINE = re.compile(r"^\s*(and\s+|aur\s+|what\s+about\s+|how\s+about\s+)?"
                        r"(mine|me|myself|my\s+one|mera|meri|mere|mera\s+kya|meri\s+kya|mere\s+liye)"
                        r"(\s+(again|too|also|bhi|wala|waala|wali|only|hi|tha|thi|hai))*\s*[?.!]*\s*$",
                        re.IGNORECASE)
_BARE_MINE_IN_CORRECTION = re.compile(r"^\s*(mera|meri|mere|mine|my\s+one|me)(\s+\w+){0,2}\s*[?.!]*\s*$",
                                      re.IGNORECASE)


def _explicit_party_turn(text: str, state: "ConversationState") -> "Reading | None":
    """
    EXPLICIT PARTY > INHERITED PARTY. The current turn NAMES whose question it
    is -- "mine" / "mera" is the caller, "their" / "theirs" / "unka" is the
    other person on the case -- so it decides the party whatever the previous
    turn was about, and whether or not a clarification is still pending.
    """
    if not state.last_message:
        return None
    # a THIRD-PERSON word ("theirs", "unka", "her") is never the caller, however
    # close the fuzzy match to "what about me" -- it is the other person
    third_person = bool(_BARE_THEIRS.match(text) or _PERSON_POSSESSIVE.search(text)
                        or re.search(r"\btheirs\b", text, re.I))
    if (_BARE_MINE.match(text) or (_has("FOR_SELF", text) and not third_person)) \
            and state.active_party == "CO_APPLICANT" and _party_askable(state):
        return Reading(REPLAY, _for_self(state.last_message),
                       note="the previous question, for the primary applicant")
    referred = _pronoun_referent(text, state)
    if referred:
        return Reading(NEW_TOPIC, referred, note="the party the conversation is about")
    return None


def _pronoun_referent(text: str, state: "ConversationState") -> str | None:
    """
    A PERSON PRONOUN in the turn ("their KYC", "for them", "unka mobile",
    "and theirs?") resolved to the co-applicant. None when the turn names a
    party itself or says "my".
    """
    from app.agents.applicant.copilot.routing import subjects as _subjects

    if _subjects.mentioned(text) is not None or _SELF_WORDS.search(text):
        return None
    last_intent = str((state.last_answer_reference or {}).get("intent") or "")
    about_co = state.active_party == "CO_APPLICANT"
    if _BARE_THEIRS.match(text) and _party_askable(state):
        return _for_co(state.last_message)
    person_topic = about_co or last_intent in ("APPLICANT_PROFILE", "KYC_RESULT")
    # A THIRD-PERSON possessive on a person's detail or document ("their
    # mobile", "uska PAN") is never the caller: on a case it is the other
    # party. (Outside the case, a refusal above has already answered it.)
    if _PERSON_POSSESSIVE.search(text) and (person_topic or re.search(
            r"\b(their|unka|unki|unke|uska|uski|uske|his|her)\s+(kyc|name|naam|mobile|phone|number|"
            r"email|address|dob|date\s+of\s+birth|details?|pan|aadhaa?r|passport|voter\s+id|"
            r"documents?|docs?|verification|bank\s+statement)\b", text, re.IGNORECASE)):
        return _PERSON_POSSESSIVE.sub("the co-applicant's", text, count=1)
    if _PERSON_OBJECT.search(text) and person_topic:
        return _PERSON_OBJECT.sub(lambda m: (f"{m.group(1)} the co-applicant" if m.group(1)
                                             else "the co-applicant"), text, count=1)
    if _PERSON_SUBJECT.search(text) and about_co:
        return _PERSON_SUBJECT.sub("the co-applicant", text, count=1)
    return None


#: "iska / iski / this one's" -- a pointer at a PERSON'S record topic.
_THIS_ONES = re.compile(r"\b(iska|iski|iske|isko|inka|inki|inke|uska|uski|uske|its|this\s+one'?s)\b"
                        r"|इसका|इसकी|इसके|उसका|उसकी", re.IGNORECASE)
#: Topics that belong to one person on the case (never to a document).
_PERSON_TOPIC = re.compile(r"\b(kyc|verification|verified|documents?|status|pending|profile|details)\b",
                           re.IGNORECASE)


def _this_persons_topic(text: str, state: "ConversationState") -> "Reading | None":
    """
    "iska KYC?" -- a person-level topic behind a pointer. Resolved to the one
    person the conversation is on; ASKED (yours, or the co-applicant's?) when
    there is no one yet, or the last answer was about BOTH people. Never
    guessed. A document the conversation is about ("iska score?" after a PAN
    answer) is handled before this.
    """
    from app.agents.applicant.copilot.routing import subjects as _subjects
    from app.agents.applicant.copilot.semantics import semantic_frame as _tframes

    pointer = _THIS_ONES.search(text)
    if not pointer or _SELF_WORDS.search(text) or _subjects.mentioned(text) is not None:
        return None
    if not _PERSON_TOPIC.search(text) or _tframes._document_type(text) \
            or len(_strip(text).split()) > 6:
        return None
    rest = " ".join((text[:pointer.start()] + " " + text[pointer.end():]).split()).strip(" ?")
    rest = re.sub(r"^(and|aur|what\s+about)\s+", "", rest, flags=re.IGNORECASE)
    as_self, as_co = f"What is my {rest}?", f"What is the co-applicant's {rest}?"
    whose = state.last_subject_party if state.last_message else None
    if whose == "CO_APPLICANT":
        return Reading(NEW_TOPIC, as_co, note="the person the conversation is on")
    if whose == "PRIMARY_APPLICANT":
        return Reading(NEW_TOPIC, as_self, note="the person the conversation is on")
    return Reading(ASKED, text,
                   reply=f"Whose {rest} do you mean -- yours, or the co-applicant's?",
                   options=[as_self, as_co],
                   note=("the last answer was about both people" if whose == "BOTH"
                         else "a pointer with no one to point at"))


def _pronoun_without_referent(text: str, state: "ConversationState") -> "Reading | None":
    """
    "Their email?" / "uska DOB?" with NO person in the conversation yet: whose
    it is cannot be guessed -- asked, with both readings offered.
    """
    from app.agents.applicant.copilot.answering import profile as _profile
    from app.agents.applicant.copilot.routing import subjects as _subjects

    if state.last_message or _SELF_WORDS.search(text) or _subjects.mentioned(text) is not None:
        return None
    pronoun = re.search(r"\b(their|his|her|unka|unki|unke|uska|uski|uske)\b", text, re.IGNORECASE)
    if not pronoun:
        return None
    as_self = text[:pronoun.start()] + "my" + text[pronoun.end():]
    asked = _profile.detect(as_self)
    if asked is None or asked.field.startswith("ALL"):
        return None
    as_co = text[:pronoun.start()] + "the co-applicant's" + text[pronoun.end():]
    return Reading(ASKED, text, reply="Whose do you mean -- yours, or the co-applicant's?",
                   options=[as_self, as_co], note="a pronoun with no one to point at")


def _party_referent(text: str, state: "ConversationState") -> str | None:
    """
    WHO an un-named follow-up is about: a person pronoun first
    (_pronoun_referent), then INHERITANCE -- "what about the mobile?" after
    a co-applicant answer is still the co-applicant. An explicit "my" or a
    named party always wins (None).
    """
    from app.agents.applicant.copilot.semantics import short_query as _short
    from app.agents.applicant.copilot.routing import subjects as _subjects

    if _subjects.mentioned(text) is not None or _SELF_WORDS.search(text):
        return None
    pronoun = _pronoun_referent(text, state) if state.last_message else None
    if pronoun:
        return pronoun
    about_co = state.active_party == "CO_APPLICANT"
    if about_co and (_FOLLOW_FORM.match(text) or (len(text.split()) <= 4 and (
            _short.short_head(text) is not None or _is_field(text)
            or re.search(r"\bkyc\b", text, re.IGNORECASE)))):
        return f"{text.strip().rstrip('?')} for the co-applicant?"
    return None


def _other_document_or_party(text: str, state: "ConversationState",
                             last_intent: str) -> "Reading | None":
    """
    "the other one" right after a question about ONE document is either the
    other document on the list or the co-applicant's copy of this one. Both
    readings are offered; the conversation state never picks.
    """
    from app.agents.applicant.copilot.conversation import followup as _followup

    if not (state.last_document and state.last_message and _party_askable(state)):
        return None
    lowered = _strip(text)
    if not re.search(r"\b(the\s+)?other\s+one\b|\bdusra\s+wala\b|\bdoosra\s+wala\b", lowered) \
            or re.search(r"\b(applicant|him|her|person|co)\b", lowered):
        return None
    others = [d for d in state.last_documents if d and d != state.last_document]
    if not others:
        return None
    ask = ("Is my {doc} verified?" if last_intent == "DOCUMENT_VERIFICATION"
           else "Has my {doc} been uploaded?" if last_intent == "DOCUMENTS_UPLOADED"
           else "Is my {doc} still pending?")
    options = [ask.format(doc=_followup._display(d)) for d in others[:2]]
    options.append(_for_co(state.last_message))
    names = [_followup._display(d) for d in others[:2]]
    reply = (f"Do you mean the {' or the '.join(names)}, or the co-applicant's "
             f"{_followup._display(state.last_document)}?")
    return Reading(ASKED, text, reply=reply, options=options,
                   note="the other document, or the other person")


def _for_self(message: str) -> str:
    """The previous question, asked of the caller instead of the co-applicant."""
    text = str(message or "")
    text = re.sub(r"\b(the|my|our)\s+co-?\s?applicant'?s\b", "my", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(the|my|our)\s+co-?\s?applicant\s+(details?|info|information|profile)\b",
                  r"my applicant \2", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(for|of|about)\s+(my|the|our)\s+co-?\s?applicant\b", r"\1 me", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\bco-?\s?applicant\s+(ka|ki|ke)\b",
                  lambda m: {"ka": "mera", "ki": "meri", "ke": "mere"}[m.group(1).lower()],
                  text, flags=re.IGNORECASE)
    return re.sub(r"\b(the\s+|my\s+)?co-?\s?applicant\b", "me", text, flags=re.IGNORECASE)

#: The kind of turn being answered (a TURN TYPE below), set by the
#: conversation layer before the pipeline runs, for the model-routing policy.
CURRENT_TURN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "copilot_current_turn", default=None)

YES_NO = "YES_NO"
EITHER_OR = "EITHER_OR"
OPEN = "OPEN"


# ==========================================================================
# STATE
# ==========================================================================

@dataclass
class Option:
    label: str
    intent: str | None = None
    task: str | None = None
    object: str | None = None
    concepts: list[str] = field(default_factory=list)


@dataclass
class PendingClarification:
    status: str = "OPEN"
    reason: str | None = None
    question: str = ""
    question_type: str = EITHER_OR
    original_message: str = ""
    original_frame: dict[str, Any] | None = None
    unresolved_field: str = "INTENT"
    options: list[Option] = field(default_factory=list)
    created_turn_id: int = 0
    expires_at: float = 0.0
    asked_times: int = 1

    def public(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "question_type": self.question_type,
                "unresolved_field": self.unresolved_field,
                "options": [o.label for o in self.options],
                "created_turn_id": self.created_turn_id, "asked_times": self.asked_times}


@dataclass
class ConversationState:
    conversation_id: str
    subject_key: str
    case_id: str | None = None
    turn_id: int = 0
    current_topic: str | None = None          # the last frame's object
    active_subject: str | None = None         # party role label
    active_party: str | None = None
    active_stage: str | None = None           # the stage the last answer reported
    last_semantic_frame: dict[str, Any] | None = None
    last_resolved_frame: dict[str, Any] | None = None
    last_answer_reference: dict[str, Any] | None = None   # intent / query_type / source
    pending_clarification: PendingClarification | None = None
    pending_options: list[str] = field(default_factory=list)
    pending_referents: dict[str, str] = field(default_factory=dict)
    last_document: str | None = None
    last_documents: list[str] = field(default_factory=list)
    #: The category of the last refused turn (a code) -- a pronoun right after
    #: a refused other-customer request points at that customer.
    last_refusal: str | None = None
    last_slot: str | None = None
    last_application_context: dict[str, Any] | None = None  # labels: stage, query type
    last_tool_result_reference: list[str] = field(default_factory=list)
    last_message: str | None = None           # the last QUESTION, masked, for "again"
    language: str | None = None
    #: WHOSE the last answer was: PRIMARY_APPLICANT / CO_APPLICANT / BOTH --
    #: "iska KYC?" after an answer about BOTH people cannot be guessed.
    last_subject_party: str | None = None
    last_activity_at: float = field(default_factory=time.time)
    turns_since_pending: int = 0

    def as_context(self) -> dict[str, Any]:
        """The follow-up context (followup.Context payload) this state implies."""
        frame = self.last_resolved_frame or self.last_semantic_frame or {}
        ref = self.last_answer_reference or {}
        return {
            "conversation_id": self.conversation_id,
            "last_query_type": ref.get("query_type"),
            "last_intent": ref.get("intent"),
            "last_slot": self.last_slot,
            "last_subject": self.active_subject,
            "last_task": frame.get("task"),
            "last_object": frame.get("object"),
            "last_stage": self.active_stage,
            "last_source": ref.get("response_source"),
            "last_language": self.language,
            "last_documents": list(self.last_documents),
            "last_document": self.last_document,
        }

    def public(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id, "turn_id": self.turn_id,
            "current_topic": self.current_topic, "active_party": self.active_party,
            "active_stage": self.active_stage, "language": self.language,
            "pending_clarification": (self.pending_clarification.public()
                                      if self.pending_clarification else None),
            "last_intent": (self.last_answer_reference or {}).get("intent"),
            "last_document": self.last_document, "last_documents": list(self.last_documents),
        }


class ConversationStore:
    """In-process, bounded, expiring. No database round-trip for state."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], ConversationState] = {}
        self._lock = threading.Lock()

    def get(self, subject_key: str, conversation_id: str | None) -> ConversationState | None:
        if not conversation_id:
            return None
        with self._lock:
            state = self._states.get((subject_key, conversation_id))
            if state is None:
                return None
            if time.time() - state.last_activity_at > _cfg_int("ttl_seconds", 1800):
                self._states.pop((subject_key, conversation_id), None)
                return None
            return state

    def new(self, subject_key: str, case_id: str | None) -> ConversationState:
        state = ConversationState(conversation_id=uuid.uuid4().hex, subject_key=subject_key,
                                  case_id=case_id)
        self.put(state)
        return state

    def put(self, state: ConversationState) -> None:
        state.last_activity_at = time.time()
        with self._lock:
            limit = _cfg_int("max_conversations", 2000)
            if len(self._states) >= limit:
                # The oldest go first: a bounded store, never an unbounded one.
                for key in sorted(self._states, key=lambda k: self._states[k].last_activity_at)[
                        : max(1, len(self._states) - limit + 1)]:
                    self._states.pop(key, None)
            self._states[(state.subject_key, state.conversation_id)] = state

    def clear(self) -> None:
        with self._lock:
            self._states.clear()


class SqliteConversationStore(ConversationStore):
    """
    The same contract on the LOS SQLite file (LOS_STORE_PATH), one row per
    conversation, for deployments with several workers. Same TTLs and bound;
    the row holds the state as JSON -- labels only, as in memory.
    """

    def __init__(self, path: str | None = None) -> None:
        super().__init__()
        from app.store import store_path

        self._path = path or store_path()
        self._ready = False

    def _conn(self):
        import sqlite3

        conn = sqlite3.connect(self._path, timeout=10.0)
        if not self._ready:
            conn.execute("CREATE TABLE IF NOT EXISTS conversation_state ("
                         "subject_key TEXT NOT NULL, conversation_id TEXT NOT NULL, "
                         "state TEXT NOT NULL, last_activity_at REAL NOT NULL, "
                         "PRIMARY KEY (subject_key, conversation_id))")
            conn.commit()
            self._ready = True
        return conn

    def get(self, subject_key: str, conversation_id: str | None) -> ConversationState | None:
        if not conversation_id:
            return None
        conn = self._conn()
        try:
            row = conn.execute("SELECT state, last_activity_at FROM conversation_state WHERE "
                               "subject_key = ? AND conversation_id = ?",
                               (subject_key, conversation_id)).fetchone()
            if row is None:
                return None
            if time.time() - float(row[1]) > _cfg_int("ttl_seconds", 1800):
                conn.execute("DELETE FROM conversation_state WHERE subject_key = ? AND "
                             "conversation_id = ?", (subject_key, conversation_id))
                conn.commit()
                return None
            return _from_json(row[0])
        finally:
            conn.close()

    def put(self, state: ConversationState) -> None:
        import json

        state.last_activity_at = time.time()
        conn = self._conn()
        try:
            conn.execute("INSERT OR REPLACE INTO conversation_state VALUES (?, ?, ?, ?)",
                         (state.subject_key, state.conversation_id, json.dumps(_to_json(state)),
                          state.last_activity_at))
            limit = _cfg_int("max_conversations", 2000)
            conn.execute("DELETE FROM conversation_state WHERE rowid IN (SELECT rowid FROM "
                         "conversation_state ORDER BY last_activity_at DESC LIMIT -1 OFFSET ?)",
                         (limit,))
            conn.commit()
        finally:
            conn.close()

    def clear(self) -> None:
        conn = self._conn()
        try:
            conn.execute("DELETE FROM conversation_state")
            conn.commit()
        finally:
            conn.close()


def _to_json(state: ConversationState) -> dict[str, Any]:
    data = asdict(state)
    return data


def _from_json(text: str) -> ConversationState:
    import json

    data = json.loads(text)
    pending = data.pop("pending_clarification", None)
    state = ConversationState(**{k: v for k, v in data.items()
                                 if k in ConversationState.__dataclass_fields__})
    if isinstance(pending, dict):
        options = [Option(**o) for o in pending.pop("options", []) if isinstance(o, dict)]
        state.pending_clarification = PendingClarification(options=options, **{
            k: v for k, v in pending.items() if k in PendingClarification.__dataclass_fields__})
    return state


class _Store:
    """The configured store (`chatbot.conversation.store`: memory | sqlite), resolved lazily."""

    def __init__(self) -> None:
        self._memory = ConversationStore()
        self._sqlite: SqliteConversationStore | None = None

    def _backend(self) -> ConversationStore:
        if str(_cfg().get("store", "memory")).lower() == "sqlite":
            if self._sqlite is None:
                self._sqlite = SqliteConversationStore()
            return self._sqlite
        return self._memory

    def get(self, subject_key: str, conversation_id: str | None) -> ConversationState | None:
        return self._backend().get(subject_key, conversation_id)

    def new(self, subject_key: str, case_id: str | None) -> ConversationState:
        return self._backend().new(subject_key, case_id)

    def put(self, state: ConversationState) -> None:
        self._backend().put(state)

    def clear(self) -> None:
        self._backend().clear()


STORE = _Store()


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("conversation")


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(_cfg().get(name, default))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


# ==========================================================================
# VOCABULARY (semantic_concepts.yaml: conversation)
# ==========================================================================

_VOCAB_CACHE: dict[str, Any] = {}


def _vocab() -> dict[str, list[str]]:
    from app.agents.applicant.copilot.semantics import semantic_frame

    raw = semantic_frame._config().get("conversation") or {}
    out: dict[str, list[str]] = {}
    for key, entries in raw.items():
        if isinstance(entries, list):
            words = []
            for e in entries:
                if isinstance(e, bool):        # YAML reads a bare yes / no as a boolean
                    e = "yes" if e else "no"
                if str(e).strip():
                    words.append(_strip(str(e)))
            out[str(key).upper()] = words
    return out


def _fuzzy(text: str, phrases: list[str]) -> str | None:
    """A short reply's closest phrase when it is a typo of one ("secnd one")."""
    import difflib

    lowered = _strip(text)
    if not lowered or len(lowered.split()) > 3 or len(lowered) < 3:
        return None
    best = difflib.get_close_matches(lowered, phrases, n=1, cutoff=0.8)
    return best[0] if best else None


def _has(kind: str, text: str) -> bool:
    """Whether the whole (punctuation-stripped) text is a phrase of `kind` (typo-tolerant)."""
    phrases = _vocab().get(kind, [])
    lowered = _strip(text)
    if lowered in set(phrases):
        return True
    return len(lowered) >= 5 and _fuzzy(lowered, phrases) is not None


def _completed_fragment(earlier: str, now: str) -> str | None:
    """
    "date" + "birth" -> "date birth" when the two short fragments read as one
    question and neither does alone. Nothing is guessed: the joined text is
    classified exactly as a typed message would be.
    """
    a, b = _strip(earlier), _strip(now)
    if not a or not b or len(a.split()) > 2 or len(b.split()) > 2:
        return None
    from app.agents.applicant.copilot.semantics import intents as _intents
    for joined in (f"{a} {b}", f"{b} {a}"):
        reading = _intents.understand(joined, has_case=True)
        if reading.intent.value not in ("UNKNOWN", "OUT_OF_SCOPE", "GUARDRAIL_BLOCKED"):
            return joined
    return None


def _leading(kind: str, text: str) -> str | None:
    """The remainder after a leading phrase of `kind`, or None."""
    lowered = _strip(text)
    for phrase in sorted(_vocab().get(kind, []), key=len, reverse=True):
        if lowered == phrase:
            return ""
        if lowered.startswith(phrase + " "):
            # THE REMAINDER AS TYPED (apostrophes, case), not the normalised form.
            words = str(text or "").split()
            rest = " ".join(words[len(phrase.split()):]).strip(" ,.;:-")
            return rest or lowered[len(phrase):].strip(" ,.;:-")
    return None


def _strip(text: str) -> str:
    return re.sub(r"[\s,.!?;:\-–—'’\"()]+", " ", str(text or "").lower()).strip()


_NUMBER = re.compile(r"^\s*(?:option\s+|no\.?\s*|number\s+)?(\d)\s*(?:st|nd|rd|th|one|wala|wali)?\s*[.)]?\s*$",
                     re.IGNORECASE)


def _ordinal(text: str, count: int) -> int | None:
    """The option index a reply names, or None. 'the other one' is the second of two."""
    lowered = _strip(text)
    match = _NUMBER.match(lowered)
    if match:
        index = int(match.group(1)) - 1
        return index if 0 <= index < count else -1
    vocab = _vocab()
    keys = ("FIRST", "SECOND", "THIRD", "FOURTH")
    for index, key in enumerate(keys):
        if lowered in set(vocab.get(key, [])):
            return index if index < count else -1
    if lowered in set(vocab.get("LAST", [])):
        return count - 1
    if lowered in set(vocab.get("OTHER", [])):
        return 1 if count == 2 else None
    for index, key in enumerate(keys):                      # a typo of an ordinal
        if _fuzzy(lowered, vocab.get(key, [])):
            return index if index < count else -1
    return None


# ==========================================================================
# READING A TURN AGAINST THE CONVERSATION
# ==========================================================================

@dataclass
class Reading:
    outcome: str
    message: str                       # what the pipeline should now understand
    option_index: int | None = None
    note: str | None = None
    reply: str | None = None           # a reply to publish without reading anything
    options: list[str] = field(default_factory=list)


def _frame_of(text: str) -> Any:
    from app.agents.applicant.copilot.semantics import intents

    return intents.understand(text, has_case=True)


def _option_from_label(label: str) -> Option:
    c = _frame_of(label)
    f = getattr(c, "frame", None)
    return Option(label=label, intent=c.intent.value,
                  task=getattr(getattr(f, "task", None), "value", None),
                  object=getattr(getattr(f, "object", None), "value", None),
                  concepts=list(getattr(f, "concepts", []) or []))


#: Intents that answer the same clarification option ("where am I" chooses
#: "What stage is my application at").
_FAMILIES = ({"APPLICATION_STAGE", "APPLICATION_STATUS"},
             {"DOCUMENTS_MISSING", "DOCUMENTS_PENDING", "PENDING_ITEMS"})


def _same_family(a: str | None, b: str | None) -> bool:
    return bool(a and b and (a == b or any(a in f and b in f for f in _FAMILIES)))


def _match_option(text: str, options: list[Option]) -> tuple[list[int], Any]:
    """Options the reply MEANS: same intent, or same task/object frame."""
    from app.agents.applicant.copilot.semantics import short_query

    c = _frame_of(text)
    from app.agents.applicant.copilot.answering import profile

    words = [w for w in _strip(text).split() if len(w) > 1
             and w not in ("the", "one", "wala", "wali", "wale", "waala", "vala", "ka", "ki", "ke")]
    if 0 < len(words) <= 2:
        # "case" after "application ID or case ID?": the words of one option.
        named = [i for i, o in enumerate(options)
                 if all(w in _strip(o.label).split() for w in words)]
        if len(named) == 1:
            return named, c

    # "the address proof" after "the Address Proof or the PAN?": a reply that
    # NAMES ONE DOCUMENT chooses the one option about that document.
    from app.agents.applicant.copilot.semantics import semantic_frame as _frames

    from app.agents.applicant.copilot.routing import subjects as _subjects

    if _subjects.mentioned(text) is _subjects.Kind.CO and len(words) <= 6:
        by_party = [i for i, o in enumerate(options)
                    if re.search(r"co-?\s?applicant", o.label, re.IGNORECASE)]
        if len(by_party) == 1:
            return by_party, c

    named_document = _frames._document_type(text)
    if named_document and len(words) <= 5:
        by_document = [i for i, o in enumerate(options)
                       if _frames._document_type(o.label) == named_document
                       and not re.search(r"co-?\s?applicant", o.label, re.IGNORECASE)]
        if len(by_document) == 1:
            return by_document, c

    asked = profile.detect(text)
    if asked is not None and not asked.field.startswith("ALL"):
        # "mobile" after "which field: mobile, email or address?" names it;
        # "what about my email?" after a MOBILE clarification names a field
        # no option carries -- it chooses nothing and is a new question.
        by_field = [i for i, o in enumerate(options)
                    if (profile.detect(o.label) or profile.Question("")).field == asked.field]
        if len(by_field) == 1:
            return by_field, c
        if not by_field:
            return [], c
    if short_query.short_head(text) is not None:
        return [], c                    # a bare word chooses nothing
    f = getattr(c, "frame", None)
    task = getattr(getattr(f, "task", None), "value", None)
    obj = getattr(getattr(f, "object", None), "value", None)
    hits: list[int] = []
    exact: list[int] = []
    for index, option in enumerate(options):
        if c.intent.value not in ("UNKNOWN", "OUT_OF_SCOPE") and c.intent.value == option.intent:
            exact.append(index)
        elif c.intent.value not in ("UNKNOWN", "OUT_OF_SCOPE") and _same_family(c.intent.value,
                                                                                option.intent):
            hits.append(index)
        elif task and task != "UNKNOWN" and task == option.task and (
                obj == option.object or obj == "NONE" or option.object == "NONE"):
            hits.append(index)
    if len(exact) > 1:
        # Two options with the SAME intent ("application ID" / "case ID"):
        # the recorded field the reply names decides, when it names one.
        from app.agents.applicant.copilot.answering import profile

        asked = profile.detect(text)
        if asked is not None:
            by_field = [i for i in exact
                        if (profile.detect(options[i].label) or profile.Question("")).field
                        == asked.field]
            if len(by_field) == 1:
                return by_field, c
    return (exact or hits), c


def _reask(pending: PendingClarification, *, after_yes: bool = False,
           after_no: bool = False) -> str:
    labels = [o.label.rstrip("?") for o in pending.options]
    if pending.question_type == YES_NO or len(labels) < 2:
        return pending.question
    choice = " or ".join(f"'{l}'" for l in labels[:-1]) + f" or '{labels[-1]}'" if len(labels) > 2 \
        else f"'{labels[0]}' or '{labels[1]}'"
    if after_yes:
        return f"A yes does not tell me which one -- do you mean {choice}?"
    if after_no:
        return f"Understood. Then which one: {choice}? You can also say 'leave it'."
    return f"Just to be sure, which one do you mean: {choice}?"


def _is_field(text: str) -> bool:
    from app.agents.applicant.copilot.answering import profile

    return profile.detect(text) is not None


def _as_question(rest: str) -> str:
    """A corrected NOUN PHRASE ("mera mobile number") asked as a question."""
    c = _frame_of(rest)
    if c.intent.value != "UNKNOWN" or len(rest.split()) > 5:
        return rest
    if re.search(r"\b(what|which|is|are|kya|kaunsa|kaunse|क्या|काय|कोणत)\b", rest, re.IGNORECASE):
        return rest
    return f"what is {rest}?"


def read_turn(message: str, state: ConversationState | None) -> Reading:
    """
    The turn read relative to the conversation. Pure: reads nothing, decides
    no route. The message it returns is what the pipeline understands next.
    """
    text = " ".join(str(message or "").split())
    if state is None:
        return Reading(NO_STATE, text)
    pending = state.pending_clarification
    if pending is not None and (time.time() > pending.expires_at
                                or state.turns_since_pending >= _cfg_int("pending_turns", 3)):
        state.pending_clarification = None
        pending = None
        expired = True
    else:
        expired = False

    # 1. CANCELLATION -- "leave that", "never mind", "chhodo" -- with or
    #    without a new question after it.
    rest = _leading("CANCEL", text)
    if rest is not None:
        state.pending_clarification = None
        if rest:
            return Reading(CANCELLATION, rest, note="cancelled, then a new question")
        return Reading(CANCELLATION, "", reply="Okay, leaving that. What would you like to know?")

    # 1b. AN ANNOUNCED TOPIC SHIFT -- "changing topic -", "by the way, ..."
    shifted = _leading("TOPIC_SHIFT", text)
    if shifted:
        state.pending_clarification = None
        return Reading(NEW_TOPIC, shifted, note="a new topic, announced")

    # 2. CORRECTION -- "no, I mean ...", "actually ...", "mera matlab ..."
    rest = _leading("CORRECTION", text)
    if rest is None:
        # A negation followed by a correction, in any pairing of the two
        # classes: "nahi, I meant ...", "no, actually ...", "nope, mera matlab ...".
        negated = _leading("NEGATE", text)
        if negated:
            rest = _leading("CORRECTION", negated)
            if rest is None:
                # "no, not the tenure. the product" / "no no, what's blocking my file":
                # what is negated is dropped, what follows is the correction.
                remainder = re.sub(r"^(not\s+(the|that|this|my)\s+[^,.;]+[,.;]\s*)", "",
                                   negated, flags=re.IGNORECASE).strip()
                if remainder and len(remainder.split()) >= 2 and (
                        _frame_of(remainder).intent.value != "UNKNOWN"
                        or _is_field(remainder)):
                    rest = remainder
    if rest:
        state.pending_clarification = None
        # "What is my application reference?" -> "nahi, mera matlab co-applicant
        # ka PAN tha": the correction replaces the OBJECT of the question; the
        # question (a recorded identifier) stays -- the co-applicant's PAN number.
        if str((state.last_answer_reference or {}).get("intent") or "") == "APPLICANT_PROFILE" \
                and re.search(r"\b(pan|aadhaa?r|passport|voter\s*id|driving\s+licen[cs]e)\b", rest, re.I) \
                and not re.search(r"\b(number|no|status|verified|verify|rejected|uploaded|missing)\b", rest, re.I):
            rest = re.sub(r"\s+(tha|thi|the|hai|h)\s*[.?!]*$", "", rest.strip(), flags=re.I) + " number"
        # A CORRECTION THAT NAMES ONLY A PARTY ("I meant the co-applicant") is
        # the previous question again, for that party.
        if _has("PARTY_ONLY", rest) and state.last_message \
                and _party_askable(state):
            return Reading(REPLAY, _for_co(state.last_message),
                           note="the previous question, for the co-applicant")
        party_turn = _explicit_party_turn(rest, state) or _bare_party_turn(rest, state)
        if party_turn is not None:
            return party_turn
        if _BARE_MINE_IN_CORRECTION.search(rest) and state.last_message \
                and state.active_party == "CO_APPLICANT":
            return Reading(REPLAY, _for_self(state.last_message),
                           note="the previous question, for the primary applicant")
        return Reading(CORRECTION, _as_question(rest), note="the previous reading was corrected")

    # A STATED GOAL -- "I want to know what's missing", "I want to understand
    # the CPA process" -- is the question it names.
    goal = _goal_question(text)
    if goal is not None:
        return Reading(NEW_TOPIC, goal, note="the stated goal")

    # A TURN THAT CONTINUES THE CONVERSATION ("and what should I do next?")
    # is the question after the conjunction.
    continued = re.sub(r"^\s*(and|aur|also|then|so|or|और|आणि)\s+(?=\S+\s+\S)", "", text,
                       flags=re.IGNORECASE)
    if continued != text:
        text = continued

    if not state.last_message and not pending:
        first_turn = _pronoun_without_referent(text, state) if not state.last_message else None
        if first_turn is not None:
            return first_turn
        this_one = _this_persons_topic(text, state)
        if this_one is not None:
            return this_one

    # A REFUSAL IS THE CONTEXT: "their KYC" / "that loan" after a refused
    # request points at what was refused -- even when nothing was answered
    # before it. The security decision overrides any conversation referent.
    if state.last_refusal in _PERSON_REFUSALS and not state.last_message and (
            _PERSON_POSSESSIVE.search(text) or _PERSON_OBJECT.search(text)
            or _PERSON_SUBJECT.search(text) or _REFUSED_RECORD.search(text)) \
            and not _SELF_WORDS.search(text) \
            and "co-applicant" not in _strip(text) and "co applicant" not in _strip(text):
        return Reading(REFUSED, text, reply="", note=state.last_refusal)

    # 3. "AGAIN" / "SAME" / "MORE" -- the last question, asked once more
    #    (optionally for the other party).
    if state.last_message:
        last_intent = str((state.last_answer_reference or {}).get("intent") or "")
        # THE DOCUMENT THE CONVERSATION IS ABOUT: "kyun fail hua?" / "why did
        # it fail?" / "iska score?" right after a PAN answer are about that
        # PAN -- for the party the conversation is on.
        if state.last_document and last_intent in ("DOCUMENT_VERIFICATION", "DOCUMENTS_UPLOADED",
                                                   "DOCUMENT_DETAILS") and not _SELF_WORDS.search(text):
            from app.agents.applicant.copilot.semantics import semantic_frame as _dframes
            from app.agents.applicant.copilot.conversation.followup import _display as _ddisplay

            if not _dframes._document_type(text):
                doc = _ddisplay(state.last_document)
                whose = "the co-applicant's " if state.active_party == "CO_APPLICANT" else "the "
                if re.search(r"\b(why|kyu|kyun|kyon|reason|wajah|kaaran)\b", text, re.I) \
                        and re.search(r"\b(fail\w*|reject\w*|review|atk\w*)\b", text, re.I):
                    return Reading(NEW_TOPIC, f"Why was {whose}{doc} rejected?",
                                   note="the document the conversation is about")
                if re.search(r"\b(score|confidence)\b", text, re.I) and not re.search(r"\bkyc\b", text, re.I):
                    return Reading(NEW_TOPIC, f"{doc} ka score kitna hai?",
                                   note="the document the conversation is about")
        # A KNOWLEDGE FOLLOW-UP'S "IT": "Why is it required?" after a question
        # about address proof is about address proof -- the topic of the
        # knowledge exchange, never a record of the case.
        if last_intent in ("FOS_KNOWLEDGE", "STAGE_PROCESS") and not _SELF_WORDS.search(text) \
                and len(_strip(text).split()) <= 4 \
                and re.match(r"^\s*(and\s+|aur\s+)?(what|how)\s+about\b|^\s*(and|aur)\b", text, re.I):
            from app.agents.applicant.copilot.semantics import semantic_frame as _kframes2
            from app.agents.applicant.copilot.conversation.followup import _display as _kdisplay

            named_doc = _kframes2._document_type(text)
            if named_doc and not state.last_document:
                return Reading(NEW_TOPIC, f"What can a {_kdisplay(named_doc)} be used for?",
                               note="the knowledge topic, for another document")
        if last_intent in ("FOS_KNOWLEDGE", "STAGE_PROCESS"):
            topic = _knowledge_topic(state.last_message)
            if topic and re.search(r"\b(it|this|that|iska|uska|ye|yeh)\b", text, re.IGNORECASE) \
                    and not _SELF_WORDS.search(text):
                from app.agents.applicant.copilot.semantics import semantic_frame as _kframes

                if not _kframes._document_type(text):
                    rewritten = re.sub(r"\b(it|this|that|iska|uska|ye|yeh)\b", topic, text, count=1,
                                       flags=re.IGNORECASE)
                    return Reading(NEW_TOPIC, rewritten, note="the knowledge topic of the last answer")
        # A PRONOUN AT A REFUSED PERSON: "and his phone number?" right after
        # "show another customer's ..." was refused is refused too.
        if state.last_refusal in _PERSON_REFUSALS and (
                _PERSON_POSSESSIVE.search(text) or _PERSON_OBJECT.search(text)
                or _PERSON_SUBJECT.search(text) or _REFUSED_RECORD.search(text)) \
                and not _SELF_WORDS.search(text) \
                and "co-applicant" not in _strip(text) and "co applicant" not in _strip(text):
            return Reading(REFUSED, text, reply="", note=state.last_refusal)
        # EXPLICIT PARTY FIRST: "and mine?", "what about theirs?", "their
        # KYC" name whose question it is -- above any replay of recent context
        # ("what about theirs" must not read as "what about that").
        if not pending:
            explicit = _explicit_party_turn(text, state)
            if explicit is not None:
                return explicit
            unreferred = _pronoun_without_referent(text, state)
            if unreferred is not None:
                return unreferred
            this_one = _this_persons_topic(text, state)
            if this_one is not None:
                return this_one
            other_document = _same_question_other_document(text, state)
            if other_document is not None:
                return other_document
            bare_party = _bare_party_turn(text, state)
            if bare_party is not None:
                return bare_party
            expanded = _expanded(text, state)
            if expanded is not None:
                return expanded
        # "what about tenure?" NAMES something of its own: a new question in the
        # same context, not "what about that?" (a fuzzy match of the phrase).
        _after = re.sub(r"^\s*(and\s+|aur\s+)?(what|how)\s+about\s+|[?.!\s]+$", "", text, flags=re.I).strip()
        _names_its_own = bool(_after) and not re.fullmatch(
            r"(it|that|this|those|these|them|ye|yeh|yahi|woh|wo|vo|iska|uska|same)", _after, re.I)
        if _has("REFER_BACK", text) and not pending and not state.last_slot and not _names_its_own:
            # (with ONE document referred to, "what about it?" is that
            # document -- followup.resolve rewrites it)
            # "what about that?" -- the previous answer, referred to (no "Again:")
            return Reading(REPLAY, state.last_message, note="the previous question, referred to")
        again = _leading("AGAIN", text)
        if again is None and _has("AGAIN", text):
            again = ""
        if again is not None and not pending:
            replay = state.last_message
            if again:
                party = _leading("FOR_PARTY", again)
                if party is not None and _party_askable(state):
                    return Reading(REPLAY, _for_co(replay),
                                   note="the previous question, for the co-applicant")
                return Reading(NEW_TOPIC, text)
            if _MORE.match(text):
                return Reading(REPLAY, replay, note="the previous answer, expanded")
            return Reading(REPLAY, replay, note="the previous question, asked again")
        # "what about the other one?" -- the same question for the co-applicant,
        # when that question is one a party can be asked about; otherwise the
        # frame's own co-applicant clarification decides.
        other = _other_document_or_party(text, state, last_intent) if not pending else None
        if other is not None:
            return other
        # "what about tenure?" names a FIELD: a new question in the same
        # context, however close "what about her" is as a phrase
        from app.agents.applicant.copilot.answering import profile as _field_profile

        _named_field = bool(_after) and not _names_co(text) and (
            _field_profile.detect(f"what is my {_after}") is not None)
        if (_has("OTHER_PARTY", text) or _has("FOR_PARTY", text)) and not pending \
                and not _named_field \
                and not (state.last_slot and _has("REFER_BACK", text)) \
                and _party_askable(state) \
                and (state.active_party or "SELF") != "CO_APPLICANT" \
                and not (state.last_document and _has("OTHER_PARTY", text)
                         and not _names_co(text) and not _same_document(text, state)):
            return Reading(REPLAY, _for_co(state.last_message),
                           note="the previous question, for the co-applicant")
        # WHO AN UN-NAMED FOLLOW-UP IS ABOUT: the party the conversation is on.
        if not pending:
            referred = _party_referent(text, state)
            if referred:
                return Reading(NEW_TOPIC, referred, note="the party the conversation is about")

    # 4. A PENDING CLARIFICATION is answered before anything is classified.
    if pending is not None:
        count = len(pending.options)
        if _has("AFFIRM", text):
            if pending.question_type == YES_NO and count == 1:
                state.pending_clarification = None
                return Reading(YES_NO_RESPONSE, pending.options[0].label, option_index=0,
                               note="yes to a yes/no question")
            pending.asked_times += 1
            return Reading(STILL_AMBIGUOUS, text, reply=_reask(pending, after_yes=True),
                           options=[o.label for o in pending.options],
                           note="yes to an either/or question chooses nothing")
        if _has("NEGATE", text) or _has("NEITHER", text):
            if pending.question_type == YES_NO or _has("NEITHER", text):
                state.pending_clarification = None
                return Reading(USER_REJECTED_CLARIFICATION, "",
                               reply="Okay. What would you like to know instead?")
            pending.asked_times += 1
            return Reading(NEGATION, text, reply=_reask(pending, after_no=True),
                           options=[o.label for o in pending.options])
        this_one = _this_persons_topic(text, state)
        if this_one is not None:
            # "iska KYC?" while another question is open: a new question about
            # a person's topic -- the open one is dropped, not re-asked
            state.pending_clarification = None
            return this_one
        if pending.reason == "ACTION_OFFER":
            # an OFFER ("Shall I start the verification?") is answered yes or
            # no; any other turn is a request of its own
            state.pending_clarification = None
            return Reading(NEW_TOPIC, text, note="an offer is answered yes or no; this stands alone")
        from app.agents.applicant.copilot.routing import subjects as _pending_subjects

        if _pending_subjects.mentioned(text) is not None and len(_match_option(text, pending.options)[0]) != 1:
            # "what about the other applicant?" while "which document?" is open:
            # a new question about a PERSON, not an answer to the old one
            state.pending_clarification = None
            return Reading(NEW_TOPIC, text, note="a question about a person supersedes the clarification")
        index = _ordinal(text, count)
        if index is not None:
            if index < 0:
                pending.asked_times += 1
                return Reading(INVALID_OPTION, text, reply=_reask(pending),
                               options=[o.label for o in pending.options])
            state.pending_clarification = None
            return Reading(OPTION_RESOLVED, pending.options[index].label, option_index=index,
                           note="chosen by ordinal")
        if _has("ACK", text):
            pending.asked_times += 1
            return Reading(STILL_AMBIGUOUS, text, reply=_reask(pending),
                           options=[o.label for o in pending.options])
        if pending.reason == "INTENT_NOT_RECOGNISED":
            # The generic offer is a menu, not a question: whatever comes next
            # is read on its own -- unless two fragments ("date", then
            # "birth") complete each other into one question.
            state.pending_clarification = None
            completed = _completed_fragment(pending.original_message, text)
            if completed:
                return Reading(NEW_TOPIC, completed, note="completed the earlier fragment")
            return Reading(NEW_TOPIC, text, note="after a generic offer the turn stands alone")
        from app.agents.applicant.copilot.semantics import short_query as _short

        head = _short.short_head(text)
        explicit = _explicit_party_turn(text, state)
        if explicit is not None and len(_match_option(text, pending.options)[0]) != 1:
            # "What about their address?" while "which document?" is open: a
            # new question naming its party, not an answer to the old one.
            state.pending_clarification = None
            return explicit
        if head is not None and not any(head in o.label.lower() for o in pending.options):
            # "stage" while a MOBILE clarification is pending: another subject.
            state.pending_clarification = None
            return Reading(NEW_TOPIC, text, note="a bare word of another subject supersedes")
        hits, classified = _match_option(text, pending.options)
        if len(hits) == 1:
            state.pending_clarification = None
            return Reading(OPTION_RESOLVED, pending.options[hits[0]].label,
                           option_index=hits[0], note="chosen by meaning")
        if len(hits) > 1:
            pending.asked_times += 1
            kept = [pending.options[i] for i in hits]
            pending.options = kept
            return Reading(PARTIAL_RESOLUTION, text, reply=_reask(pending),
                           options=[o.label for o in kept])
        frame = getattr(classified, "frame", None)
        from app.agents.applicant.copilot.semantics import short_query

        confident = (bool(frame is not None and frame.is_confident())
                     or classified.intent.value not in ("UNKNOWN",)) \
            and short_query.short_head(text) is None       # a bare word settles nothing
        if confident:
            state.pending_clarification = None
            return Reading(NEW_TOPIC, text, note="a new question supersedes the clarification")
        from app.agents.applicant.copilot.answering import profile as _profile_pending

        named_field = _profile_pending.detect("what is my " + re.sub(
            r"\b(wala|wali|wale|vala|vali|one|the\s+one|chahiye|batao|please)\b", " ", text,
            flags=re.IGNORECASE).strip(" ?.!,"))
        if named_field is not None and "+" not in named_field.field \
                and not named_field.field.startswith("ALL"):
            # the reply names a detail of its own: that detail was meant
            state.pending_clarification = None
            return Reading(OPTION_RESOLVED, f"What is my {_profile_pending.label(named_field.field)}?",
                           note="the reply named the detail")
        pending.asked_times += 1
        return Reading(STILL_AMBIGUOUS, text, reply=_reask(pending),
                       options=[o.label for o in pending.options])

    # 5. NO PENDING QUESTION: a bare yes / no is answered without a read.
    if _has("AFFIRM", text) or _has("ACK", text):
        from app.agents.applicant import conversation

        if conversation.classify(text) is not None:
            # Small talk the conversation module already answers, with its
            # own kind ("ACKNOWLEDGEMENT") and language, from nothing.
            return Reading(ACKNOWLEDGEMENT, text)
        return Reading(ACKNOWLEDGEMENT, "",
                       reply="Okay. What would you like to know about your application?")
    if _has("NEGATE", text) or _has("NEITHER", text):
        return Reading(NEGATION, "", reply="Alright. What would you like instead?")
    return Reading(PENDING_EXPIRED if expired else NEW_TOPIC, text)


# ==========================================================================
# TURN TYPES
# ==========================================================================

NEW_REQUEST = "NEW_REQUEST"
FOLLOW_UP = "FOLLOW_UP"
CLARIFICATION_RESPONSE = "CLARIFICATION_RESPONSE"
TURN_CORRECTION = "CORRECTION"
TURN_ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
TURN_NEW_TOPIC = "NEW_TOPIC"
TURN_CANCELLATION = "CANCELLATION"
AMBIGUOUS = "AMBIGUOUS"

_TURN_TYPES = {
    NO_STATE: NEW_REQUEST, NEW_TOPIC: TURN_NEW_TOPIC, PENDING_EXPIRED: TURN_NEW_TOPIC,
    REPLAY: FOLLOW_UP, OPTION_RESOLVED: CLARIFICATION_RESPONSE,
    YES_NO_RESPONSE: CLARIFICATION_RESPONSE, PARTIAL_RESOLUTION: CLARIFICATION_RESPONSE,
    STILL_AMBIGUOUS: AMBIGUOUS, INVALID_OPTION: AMBIGUOUS, NEGATION: AMBIGUOUS, ASKED: AMBIGUOUS,
    REFUSED: NEW_REQUEST,
    USER_REJECTED_CLARIFICATION: TURN_CANCELLATION, CANCELLATION: TURN_CANCELLATION,
    CORRECTION: TURN_CORRECTION, ACKNOWLEDGEMENT: TURN_ACKNOWLEDGEMENT,
}


def turn_type(reading: Reading, response: dict[str, Any], state: ConversationState | None) -> str:
    """The kind of turn this was: what the reading found, refined by the outcome."""
    kind = _TURN_TYPES.get(reading.outcome, NEW_REQUEST)
    if response.get("clarification_required"):
        return AMBIGUOUS
    if kind in (NEW_REQUEST, TURN_NEW_TOPIC):
        followed = response.get("followed_up") or {}
        short = (response.get("understanding") or {}).get("short_query") or {}
        referents = (response.get("understanding") or {}).get("referents") or {}
        if followed or (isinstance(short, dict) and short.get("resolved_to")) or any(
                str(v).startswith(("CURRENT ->", "THAT ->", "NEXT ->")) for v in referents.values()):
            return FOLLOW_UP
        if state is not None and state.turn_id == 0:
            return NEW_REQUEST
    return kind


# ==========================================================================
# UPDATING THE STATE FROM AN ANSWER
# ==========================================================================

def _question_type(clarification: dict[str, Any]) -> str:
    options = clarification.get("options") or []
    if len(options) == 1:
        return YES_NO
    return EITHER_OR if len(options) >= 2 else OPEN


def update_from_response(state: ConversationState, message: str,
                         response: dict[str, Any], reading: Reading) -> None:
    """Record what this turn was about. Labels only."""
    from app.security import sensitivity

    state.turn_id += 1
    understanding = response.get("understanding") or {}
    frame = understanding.get("frame") if isinstance(understanding, dict) else None
    if isinstance(frame, dict):
        state.last_semantic_frame = {k: frame.get(k) for k in
                                     ("task", "object", "party", "scope", "stage", "language",
                                      "confidence", "document_type", "referents", "qualifiers")}
        state.current_topic = frame.get("object") or state.current_topic
        state.language = frame.get("language") or state.language
        if frame.get("party"):
            state.active_party = frame.get("party")
    answered = (response.get("category") not in ("CONVERSATION", "UNSUPPORTED")
                and str(response.get("intent") or "") not in ("UNKNOWN", "GUARDRAIL_BLOCKED"))
    guard = response.get("guardrail") or {}
    if str(response.get("intent") or "") == "GUARDRAIL_BLOCKED" and isinstance(guard, dict):
        state.last_refusal = str(guard.get("category") or "") or None
    elif answered:
        state.last_refusal = None
    context = response.get("context") or {}
    if isinstance(context, dict) and answered:
        # ONLY A REAL ANSWER moves the referents: "haan" or a refusal after a
        # document answer leaves "that" pointing at the document.
        state.last_slot = context.get("last_slot") or None
        listed = context.get("last_documents")
        state.last_documents = [str(d) for d in listed] if isinstance(listed, list) else []
        state.last_document = context.get("last_document") or None
        state.active_subject = context.get("last_subject") or state.active_subject
    stage = understanding.get("case_stage") if isinstance(understanding, dict) else None
    if stage:
        state.active_stage = str(stage)
    if answered:
        subject = response.get("subject") if isinstance(response.get("subject"), dict) else {}
        whose = str((subject or {}).get("kind") or (frame or {}).get("party") or "").upper()
        state.last_subject_party = {"BOTH": "BOTH", "CO": "CO_APPLICANT", "CO_APPLICANT": "CO_APPLICANT",
                                    "SELF": "PRIMARY_APPLICANT", "PRIMARY": "PRIMARY_APPLICANT",
                                    "PRIMARY_APPLICANT": "PRIMARY_APPLICANT"}.get(whose)
        state.last_answer_reference = {
            "intent": response.get("intent"), "query_type": response.get("query_type"),
            "response_source": response.get("response_source"),
            "category": response.get("category")}
    state.last_application_context = {"stage": state.active_stage,
                                      "query_type": response.get("query_type")}
    state.last_tool_result_reference = [str(t) for t in (response.get("tools_invoked") or [])][:8]

    clarification = response.get("clarification_required")
    if isinstance(clarification, dict) and (clarification.get("question")
                                            or clarification.get("options")):
        options = [_option_from_label(str(o)) for o in (clarification.get("options") or [])
                   if str(o).strip()][:6]
        state.pending_clarification = PendingClarification(
            reason=clarification.get("reason"), question=str(response.get("answer") or
                                                             clarification.get("question") or ""),
            question_type=_question_type(clarification), original_message=message[:200],
            original_frame=state.last_semantic_frame,
            unresolved_field=("REFERENT" if clarification.get("reason") == "REFERENT_UNRESOLVED"
                              else "INTENT"),
            options=options, created_turn_id=state.turn_id,
            expires_at=time.time() + _cfg_int("pending_ttl_seconds", 600))
        state.pending_options = [o.label for o in options]
        state.turns_since_pending = 0
    else:
        if state.pending_clarification is not None and reading.outcome in (
                NEW_TOPIC, PENDING_EXPIRED):
            state.turns_since_pending += 1
        # A REAL ANSWER is the last question -- kept masked, for "again".
        if response.get("category") not in ("CONVERSATION", "UNSUPPORTED") \
                and str(response.get("intent") or "") not in ("UNKNOWN", "GUARDRAIL_BLOCKED"):
            state.last_message = sensitivity.mask_identifiers(message)[:200]
            state.last_resolved_frame = state.last_semantic_frame
    STORE.put(state)


__all__ = ["STORE", "ConversationState", "ConversationStore", "Option",
           "PendingClarification", "Reading", "enabled", "read_turn",
           "update_from_response"]
