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
NO_STATE = "NO_STATE"

#: Questions that can be asked about ONE PARTY on the case (subjects.py).
PER_PARTY_INTENTS = frozenset({"DOCUMENT_VERIFICATION", "DOCUMENTS_PENDING", "DOCUMENTS_MISSING",
                               "PENDING_ITEMS", "READINESS", "DOCUMENTS_UPLOADED",
                               "DOCUMENT_DETAILS", "CASE_HISTORY"})

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
    last_slot: str | None = None
    last_application_context: dict[str, Any] | None = None  # labels: stage, query type
    last_tool_result_reference: list[str] = field(default_factory=list)
    last_message: str | None = None           # the last QUESTION, masked, for "again"
    language: str | None = None
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
    from app.agents.applicant import semantic_frame

    raw = semantic_frame._config().get("conversation") or {}
    out: dict[str, list[str]] = {}
    for key, entries in raw.items():
        if isinstance(entries, list):
            words = []
            for e in entries:
                if isinstance(e, bool):        # YAML reads a bare yes / no as a boolean
                    e = "yes" if e else "no"
                if str(e).strip():
                    words.append(str(e).strip().lower())
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
    from app.agents.applicant import intents

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
    from app.agents.applicant import short_query

    c = _frame_of(text)
    from app.agents.applicant import profile

    asked = profile.detect(text)
    if asked is not None and not asked.field.startswith("ALL"):
        # "mobile" after "which field: mobile, email or address?" names it.
        by_field = [i for i, o in enumerate(options)
                    if (profile.detect(o.label) or profile.Question("")).field == asked.field]
        if len(by_field) == 1:
            return by_field, c
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
        from app.agents.applicant import profile

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

    # 2. CORRECTION -- "no, I mean ...", "actually ...", "mera matlab ..."
    rest = _leading("CORRECTION", text)
    if rest:
        state.pending_clarification = None
        return Reading(CORRECTION, _as_question(rest), note="the previous reading was corrected")

    # A TURN THAT CONTINUES THE CONVERSATION ("and what should I do next?")
    # is the question after the conjunction.
    continued = re.sub(r"^\s*(and|aur|also|then|so|or|और|आणि)\s+(?=\S+\s+\S)", "", text,
                       flags=re.IGNORECASE)
    if continued != text:
        text = continued

    # 3. "AGAIN" / "SAME" / "MORE" -- the last question, asked once more
    #    (optionally for the other party).
    if state.last_message:
        last_intent = str((state.last_answer_reference or {}).get("intent") or "")
        again = _leading("AGAIN", text)
        if again is None and _has("AGAIN", text):
            again = ""
        if again is not None and not pending:
            replay = state.last_message
            if again:
                party = _leading("FOR_PARTY", again)
                if party is not None and last_intent in PER_PARTY_INTENTS:
                    replay = f"{replay.rstrip('?')} for the co-applicant?"
                else:
                    return Reading(NEW_TOPIC, text)
            return Reading(REPLAY, replay, note="the previous question, asked again")
        if _has("FOR_SELF", text) and not pending and last_intent in PER_PARTY_INTENTS \
                and state.active_party == "CO_APPLICANT":
            replay = re.sub(r"\b(for|of|about)\s+(my|the|our)\s+co-?\s?applicant('s)?\b", "for me",
                            state.last_message, flags=re.IGNORECASE)
            return Reading(REPLAY, replay, note="the previous question, for the primary applicant")
        # "what about the other one?" -- the same question for the co-applicant,
        # when that question is one a party can be asked about; otherwise the
        # frame's own co-applicant clarification decides.
        if (_has("OTHER_PARTY", text) or _has("FOR_PARTY", text)) and not pending \
                and last_intent in PER_PARTY_INTENTS \
                and (state.active_party or "SELF") != "CO_APPLICANT":
            return Reading(REPLAY, f"{state.last_message.rstrip('?')} for the co-applicant?",
                           note="the previous question, for the co-applicant")

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
        from app.agents.applicant import short_query

        confident = (bool(frame is not None and frame.is_confident())
                     or classified.intent.value not in ("UNKNOWN",)) \
            and short_query.short_head(text) is None       # a bare word settles nothing
        if confident:
            state.pending_clarification = None
            return Reading(NEW_TOPIC, text, note="a new question supersedes the clarification")
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
    STILL_AMBIGUOUS: AMBIGUOUS, INVALID_OPTION: AMBIGUOUS, NEGATION: AMBIGUOUS,
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
    context = response.get("context") or {}
    if isinstance(context, dict):
        state.last_slot = context.get("last_slot") or None
        listed = context.get("last_documents")
        state.last_documents = [str(d) for d in listed] if isinstance(listed, list) else []
        state.last_document = context.get("last_document") or None
        state.active_subject = context.get("last_subject") or state.active_subject
    stage = understanding.get("case_stage") if isinstance(understanding, dict) else None
    if stage:
        state.active_stage = str(stage)
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
