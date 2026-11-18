"""
THE STRUCTURED TASK (Phase 2, 2026-10-12) -- WHAT the officer asks for, separated from the words: intent, scope,
operation, filters, quantity, ordering, entities, references, confidence, ambiguities. It is built from the signals the
pipeline already has, never from a second model call:

    conversation signal   greeting / acknowledgement / NEGATION / CORRECTION (copilot_reply.yaml, conversation_general.yaml)
    reference             a row of the list just shown ("why is the first one pending?") -- case_list.ordinal_pick
    portfolio operation   count / rank / compare (case_insights shapes), list / page (case_list.understand)
    portfolio follow-up   "what about the stuck ones?" after a count: the PREVIOUS operation with the NEW filter
    meaning               the semantic reading (embeddings + adapter + Qwen chooser, semantics/meaning.py)

SCOPE IS NOT THE SUBJECT. "which documents are pending for this application?" (one case) and "which applications have
pending documents?" (the portfolio) share their words; a singular pointer ("this application", "iska", "here") keeps the
one case, the officer's cases in the plural with a status cue are the portfolio (decide.plural_scope).

WHAT IT IS USED FOR: (1) routing the few turns the words alone mis-route -- a portfolio follow-up is re-asked as the
deterministic question for the new filter, a correction to another row of the list opens THAT row; (2) the turn
diagnostics (codes only). Execution stays with the deterministic handlers; a Task never authorises anything -- the
case / list handlers check ownership on every row as before. Unknown values stay None; nothing is invented.

Config: intent_catalogue.yaml `task`.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

SCOPES = ("conversation", "one_case", "portfolio", "knowledge", "general", "none", "unresolved")
OPERATIONS = ("greet", "farewell", "ack", "negate", "retrieve", "which_case", "list", "count", "rank", "compare", "page",
              "explain", "clarify", "refuse", "command", "unknown")


@dataclass
class Task:
    scope: str = "unresolved"
    operation: str = "unknown"
    intent: str | None = None               # the meaning intent / the deterministic shape that decided
    subject: str | None = None              # case / cases / document / policy / workflow / conversation
    filters: list[str] = field(default_factory=list)       # case_list.yaml filter keys
    quantity: int | None = None
    ordering: str | None = None             # a case_list.yaml sort key / "loan_amount" / "blockers"
    entities: dict[str, Any] = field(default_factory=dict)  # document / party (names, never values)
    references: dict[str, Any] = field(default_factory=dict)  # {"kind": "ordinal" | "previous_task", "rows": [...]}
    correction: bool = False                # "no, I meant ..." -- replaces part of the previous request
    constraints: list[str] = field(default_factory=list)    # own_cases_only / open_case
    confidence: float = 0.0
    ambiguities: list[str] = field(default_factory=list)
    decided_by: str = "none"
    supported: bool = True                  # a capability the system has (not: authorised, not: inputs present)

    def public(self) -> dict[str, Any]:
        """Codes only, for the diagnostics: no text, no case id, no name."""
        out = asdict(self)
        out["references"] = {k: v for k, v in self.references.items() if k in ("kind", "rows")}
        return out

    def memo(self) -> dict[str, Any]:
        """What the next turn may refer back to (bounded: codes only)."""
        return {"scope": self.scope, "operation": self.operation, "filters": list(self.filters)[:3],
                "intent": self.intent}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.semantics import meaning

    return meaning.catalogue().get("task") or {}


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", str(text or "").lower()).split()


def _hit(patterns: list[str] | None, text: str) -> bool:
    said = " " + " ".join(_words(text)) + " "
    return any(re.search(p, said) for p in patterns or [])


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

def correction_rest(message: str) -> str | None:
    """'no, I meant the second one' -> 'the second one'; None when the message is not a correction."""
    words = _words(message)
    lead = {str(w).lower() for w in _cfg().get("correction_leads") or []}
    fillers = {str(w).lower() for w in _cfg().get("correction_fillers") or []}
    if len(words) < 2 or words[0] not in lead:
        return None
    rest = words[1:]
    while rest and rest[0] in fillers:
        rest = rest[1:]
    return " ".join(rest) or None


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def bare_negation(message: str) -> bool:
    """
    'nah not really' / 'nahi ji bas': a NO with only softeners -- every word a negation word (conversation_general.yaml
    phrases.negation + task.negation_words) or a softener (task.softeners), the first a negation word.
    """
    from app.agents.applicant.copilot.capabilities import general

    words = _words(message)
    no = {w for p in (general.cfg().get("phrases") or {}).get("negation") or [] for w in _words(p)}
    no |= {str(w).lower() for w in _cfg().get("negation_words") or []}
    soft = {str(w).lower() for w in _cfg().get("softeners") or []}
    return bool(words) and words[0] in no and all(w in no | soft for w in words)


def _near(a: str, b: str) -> bool:
    """
    One letter MISSING or EXTRA ("oldst", "pendng", "hellow"). Never a replaced or swapped letter: those turn real
    words into list words ("take care" -> "atke case" read as the stuck list; regression caught 2026-10-12).
    """
    if abs(len(a) - len(b)) != 1:
        return False
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _vocabulary() -> set[str]:
    """The words the deterministic readers know: the case list's phrases, the filter roots, the greeting phrases."""
    from app.agents.applicant.copilot.answering import realtime
    from app.agents.applicant.copilot.capabilities import case_list

    out: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                add(k)
                add(v)
        elif isinstance(value, list):
            for v in value:
                add(v)
        elif isinstance(value, str):
            out.update(_words(value))
    add(case_list.cfg().get("phrases") or {})
    add(_cfg().get("filter_roots") or {})
    add((realtime._cfg().get("greeting") or {}).get("phrases") or [])
    return {w for w in out if len(w) >= 4 and w.isalpha()}


def respelled(message: str) -> str:
    """
    A TYPO of a word the deterministic readers know, corrected ("show my oldst cases" -> "oldest", "helo" -> "hello"):
    a word of 4+ letters that is not known itself and is ONE edit from exactly one known word. Everything else as typed.
    The caller uses the result only when it makes a reader understand what the typed words did not.
    """
    vocab = _vocabulary()
    out = []
    for raw in str(message or "").split():
        word = re.sub(r"[^\w]", "", raw.lower())
        if len(word) >= 4 and word.isalpha() and word not in vocab:
            close = [v for v in vocab if len(v) >= 5 and _near(word, v)]       # short list words are never targets
            if len(close) == 1:
                out.append(raw.lower().replace(word, close[0]))
                continue
        out.append(raw)
    return " ".join(out)


def asks_over_cases(message: str) -> bool:
    """
    'which applications can go to CPA now?' / 'kaunsi files ready hain': a QUESTION that selects among the officer's
    cases (a selector word + a plural case noun, no case id) -- never the move / open COMMAND its verbs resemble.
    """
    from app.agents.applicant.copilot.capabilities import case_list

    words = _words(message)
    plural = case_list._case_nouns()[1] | {str(w).lower() for w in _cfg().get("people_plural") or []}
    selectors = {str(w).lower() for w in _cfg().get("selectors") or []}
    named_id = re.search(r"\b(case|app|coapp)-[0-9a-f]{4,}\b", str(message or "").lower())
    return bool(set(words) & selectors) and bool(set(words) & plural) and not named_id


def social_kind(message: str) -> str | None:
    """GREETING / THANKS / GOODBYE ... when the conversation classifier reads the WHOLE message as social, else None."""
    try:
        from app.agents.applicant import conversation

        turn = conversation.classify(message)
    except Exception:  # noqa: BLE001 - no classifier: not social
        return None
    kinds = {str(k) for k in _cfg().get("social_kinds") or []}
    return turn.kind if turn is not None and turn.kind in kinds else None


def plain_social(message: str) -> bool:
    """
    May a SMALL-TALK / ACKNOWLEDGEMENT reading stand? Only for a short message with no question in it: a question word
    (task.question_words, typos included: "wats pendng here") or "?" means the officer asks something -- never "ok".
    """
    words = _words(message)
    asks = {str(w).lower() for w in _cfg().get("question_words") or []}
    # an OBJECT to act on ("take care of THIS", "isko kar do") is a request, not an "ok"
    objects = {str(w).lower() for w in _cfg().get("action_objects") or []}
    short = bool(words) and len(words) <= int(_cfg().get("social_max_words", 5)) and "?" not in str(message)
    return short and not any(w in asks for w in words) and not any(w in objects for w in words)


def asks_role(message: str) -> bool:
    """
    "WHO reviews the file once it reaches CPA?" / "kaun check karta hai": a question about a ROLE in the workflow
    (task.role_words + task.role_verbs) -- the approved stage knowledge, never the open case's own data. "who is the
    applicant" / "who uploaded the PAN" name no role verb and stay case questions.
    """
    words = set(_words(message))
    return bool(words & {str(w).lower() for w in _cfg().get("role_words") or []}) and bool(
        words & {str(w).lower() for w in _cfg().get("role_verbs") or []})


def unnamed_action(message: str) -> bool:
    """
    "sort this out for me" / "ye sambhal lo": an ACTION asked on something (task.action_objects) that names no action
    -- short, no question, not social, and nothing the system's own readers recognise: the engine's rule classifier
    says UNKNOWN (so "approve this" / "reject this file" keep their refusal, "explain this" its answer), no case word,
    no workspace / form command. The caller asks "which action?"; an action is never guessed.
    """
    words = _words(message)
    if not words or len(words) > int(_cfg().get("unnamed_action_max_words", 6)) or "?" in str(message):
        return False
    # ONLY PLAIN WORDS: an encoded / pasted payload ("translate and act on this: cmV2ZWFs...") is never a request to
    # clarify -- the guardrails and the engine judge it as they always did
    tokens = [t.strip(".,!;:'\"") for t in str(message).split()]
    if any(not t.isalpha() or len(t) > 15 for t in tokens if t):
        return False
    objects ={str(w).lower() for w in _cfg().get("action_objects") or []}
    asks = {str(w).lower() for w in _cfg().get("question_words") or []}
    if not set(words) & objects or set(words) & asks:
        return False
    try:
        from app.agents.applicant import conversation
        from app.agents.applicant.copilot.capabilities import case_list, product_flow, vague
        from app.agents.applicant.copilot.semantics import intents

        if conversation.classify(message) is not None or product_flow.names_case_words(message):
            return False
        if set(words) & set(conversation._vocabulary()):
            return False                    # "thanks for this" / "bye for now": a social word, never an action
        if vague.is_command(message, case_open=True) or case_list.understand(message) is not None:
            return False
        return intents.classify(message).intent is intents.Intent.UNKNOWN
    except Exception:  # noqa: BLE001 - not sure: the message goes on as typed
        return False


def filter_in(message: str) -> str | None:
    """
    The portfolio filter the message names (a case_list.yaml filter key) or None: the case list's own phrases first,
    then the filter's ROOT words (task.filter_roots: a closed set of filter values, like document aliases -- "the KYC
    ones", "the ready ones"), each matched at a word start.
    """
    from app.agents.applicant import normalize
    from app.agents.applicant.copilot.capabilities import case_list

    plain = normalize.normalise(str(message or "")).text
    found = [key for key, phrases in ((case_list.cfg().get("phrases") or {}).get("filter") or {}).items()
             if case_list._has(message, phrases) or case_list._has(plain, phrases)]
    said = " " + " ".join(_words(plain)) + " "
    found += [key for key, roots in (_cfg().get("filter_roots") or {}).items()
              if any(re.search(r"\s" + re.escape(str(r).lower()), said) for r in roots or [])]
    if not found:
        return None
    # SEVERAL NAMED ("kitne case KYC mein ATKE hain"): the specific REASON before the generic state (task.filter_priority)
    order = [str(k) for k in _cfg().get("filter_priority") or []]
    return min(dict.fromkeys(found), key=lambda k: order.index(k) if k in order else len(order))


def singular_pointer(message: str) -> bool:
    """'this application' / 'is case' / 'iska' / 'here': the request points at ONE case (task.singular_pointer)."""
    return _hit(_cfg().get("singular_pointer"), message)


def _count_word(message: str) -> bool:
    from app.agents.applicant.copilot.semantics import meaning

    return _hit(((meaning._decide_cfg().get("plural_scope") or {}).get("count_words")), message)


def portfolio_followup(message: str, previous: dict[str, Any] | None) -> tuple[str, str] | None:
    """
    'what about the stuck ones?' / 'and how many are ready for CPA?' / 'no, I meant the KYC ones' right after a
    portfolio COUNT or LIST -> (operation, filter): the previous operation with the new filter (a count word makes it a
    count). Only a SHORT message with no case / applicant named and no other question in it (task.followup_max_words).
    """
    if not previous or previous.get("scope") != "portfolio" or previous.get("operation") not in ("count", "list"):
        return None
    words = _words(message)
    if not words or len(words) > int(_cfg().get("followup_max_words", 9)):
        return None
    if re.search(r"\b(case|app|coapp)-[0-9a-f]{4,}\b", " ".join(words)) or singular_pointer(message):
        return None
    lead = {str(w).lower() for w in _cfg().get("followup_leads") or []}
    if not (words[0] in lead or correction_rest(message) is not None or _count_word(message)):
        return None
    # IT MUST REFER TO THE CASES: "the stuck ONES", "KYC WALE", "how MANY", or nothing but the filter ("what about
    # ready for CPA?") -- "what documents are mandatory?" / "what is KYC?" after a list are NEW questions (regression
    # 2026-10-12: conversation evals d4 / g1 / k_define)
    refers = {str(w).lower() for w in _cfg().get("followup_refers") or []}
    vocab = {str(w).lower() for w in _cfg().get("followup_vocabulary") or []}
    from app.agents.applicant.copilot.capabilities import case_list

    for phrases in [*((case_list.cfg().get("phrases") or {}).get("filter") or {}).values(),
                    *(_cfg().get("filter_roots") or {}).values()]:
        for p in phrases or []:
            vocab |= set(_words(p))
    if not (any(w in refers for w in words) or _count_word(message) or all(w in vocab | lead for w in words)):
        return None
    key = filter_in(message)
    if key is None:
        return None
    return ("count" if _count_word(message) else str(previous["operation"])), key


def canonical_for(operation: str, key: str) -> str | None:
    """The deterministic question for (operation, filter): task.canonicals with the filter's phrase."""
    template = (_cfg().get("canonicals") or {}).get(operation)
    phrase = (_cfg().get("filter_phrase") or {}).get(key)
    return str(template).replace("{filter}", str(phrase)) if template and phrase else None


# --------------------------------------------------------------------------
# the interpretation
# --------------------------------------------------------------------------

_KIND = {"ack": ("conversation", "ack"), "case": ("one_case", "retrieve"), "process": ("knowledge", "explain"), "knowledge": ("knowledge", "explain"),
         "policy": ("knowledge", "explain"), "calculator": ("knowledge", "explain"), "decision": ("none", "refuse"),
         "out_of_scope": ("none", "refuse"), "control": ("none", "clarify"), "clarify_action": ("none", "clarify"),
         "upload": ("one_case", "command"), "command": ("unresolved", "command")}


def interpret(message: str, decision: Any = None, *, case_open: bool = False, listed: list[str] | None = None,
              previous: dict[str, Any] | None = None, list_query: dict[str, Any] | None = None) -> Task:
    """The structured task of this message in this conversation. Deterministic over the given signals; no model."""
    from app.agents.applicant.copilot.answering import realtime
    from app.agents.applicant.copilot.capabilities import case_insights, case_list, general

    text = " ".join(str(message or "").split())
    constraints = ["own_cases_only"] + (["open_case"] if case_open else [])
    t = Task(constraints=constraints)
    rest = correction_rest(text)
    t.correction = rest is not None
    # 1. the conversation signal (a bare greeting / ok / no answers the conversation, reads no case)
    if realtime.is_greeting(text):
        return _set(t, "conversation", "greet", "greeting", 1.0, subject="conversation")
    social = social_kind(text)
    if social == "GREETING":
        return _set(t, "conversation", "greet", "conversation_classifier", 1.0, subject="conversation")
    if social == "GOODBYE":
        return _set(t, "conversation", "farewell", "conversation_classifier", 1.0, subject="conversation")
    if general._only(text, "negation") or bare_negation(text):
        return _set(t, "conversation", "negate", "negation", 1.0, subject="conversation")
    if general._only(text, "ack") or general._only(text, "thanks"):
        return _set(t, "conversation", "ack", "acknowledgement", 1.0, subject="conversation")
    body = rest or text
    # 2. a row of the list just shown ("why is the first one pending?", "no, the third one")
    pick = case_list.ordinal_pick(body, listed or [])
    insight = case_insights.kind_of(body)
    if pick and insight != case_insights.COMPARE:
        index = (listed or []).index(pick[0]) + 1 if pick[0] in (listed or []) else None
        t.references = {"kind": "ordinal", "rows": [index] if index else [], "case_id": pick[0]}
        return _set(t, "one_case", "retrieve", "ordinal_reference", 1.0, subject="case")
    # 3. a portfolio follow-up: the previous count / list with the new filter
    # the list ON SCREEN is the previous portfolio task when nothing newer was asked ("show my cases" -> "only the
    # ready ones"); a one-case question since then ends it
    on_screen = {"scope": "portfolio", "operation": "list"} if listed and list_query and not previous else None
    # A CASE IS OPEN AND THE FOLLOW-UP POINTS AT A PERSON ("and my documents?", "pending for them?"): that party on
    # this case -- never a filter on the cases list still on screen (party_kyc q_pending_*, f_switch, 2026-10-14)
    personal = case_open and re.search(r"\b(my|mine|their|theirs|them|his|her|hers|co[\s-]?applicant'?s?)\b",
                                       text, re.IGNORECASE)
    follow = None if personal else portfolio_followup(text, previous or on_screen)
    if follow:
        t.filters, t.references = [follow[1]], {"kind": "previous_task"}
        return _set(t, "portfolio", follow[0], "portfolio_followup", 0.9, subject="cases")
    # 4. the deterministic portfolio shapes (count / rank / compare / list / page)
    if insight:
        op = {"COUNT": "count", "COMPARE": "compare"}.get(insight, "rank")
        t.ordering = {"CLOSEST": "blockers", "PENDING": "pending_documents", "AMOUNT": "loan_amount"}.get(insight)
        key = filter_in(body) if insight == "COUNT" else None
        t.filters = [key] if key else []
        if insight == "COMPARE" and listed:
            t.references = {"kind": "ordinal"}
        return _set(t, "portfolio", op, f"insight_{insight.lower()}", 1.0, subject="cases")
    # A BARE "more" PAGES THE LIST ONLY RIGHT AFTER THE LIST (2026-11-06 live eval: "more" after the loan tenure
    # opened page 2 of a list shown in another conversation -- `listed` is the workspace's, it outlives the chat). The
    # previous turn's task must itself be the portfolio for "more" / "aur dikhao" to continue it.
    after_list = bool(listed) and (previous or {}).get("scope") == "portfolio"
    query = case_list.understand(body, (list_query or None) if after_list else None)
    if query is not None and not singular_pointer(body):
        t.filters = [query.filter] if query.filter else []
        t.quantity, t.ordering = query.size, query.sort
        paged = after_list and case_list.only_pages_the_list(body)
        return _set(t, "portfolio", "page" if paged else "list", "case_list", 1.0, subject="cases")
    if after_list and case_list.only_pages_the_list(body):
        return _set(t, "portfolio", "page", "case_list", 1.0, subject="cases")
    from app.agents.applicant.copilot.capabilities import workspace

    if workspace._says("list", body) and not singular_pointer(body):
        return _set(t, "portfolio", "list", "list_command", 1.0, subject="cases")     # "show my cases"
    # 5. the semantic reading
    if decision is None:
        t.ambiguities.append("not_understood")
        return _set(t, "unresolved", "unknown", "none", 0.0)
    t.intent = getattr(decision, "intent", None)
    t.entities = {k: v for k, v in (("document", getattr(decision, "document", None)),
                                    ("party", getattr(decision, "party", None))) if v}
    kind = str(getattr(decision, "kind", "") or "")
    score = float(getattr(decision, "score", 0.0) or 0.0)
    if kind == "small_talk":
        return _set(t, "conversation", "greet", getattr(decision, "decided_by", "meaning"), score,
                    subject="conversation")
    if kind == "list":
        t.filters = [k for k in [filter_in(getattr(decision, "canonical", "") or "")] if k]
        op = "count" if _count_word(getattr(decision, "canonical", "") or "") else "list"
        return _set(t, "portfolio", op, getattr(decision, "decided_by", "meaning"), score, subject="cases")
    scope, op = _KIND.get(kind, ("unresolved", "unknown"))
    if scope == "one_case" and op == "retrieve" and not case_open:
        op = "which_case"
        t.ambiguities.append("which_case")
    subject = {"one_case": "document" if t.entities.get("document") else "case", "knowledge": "policy"}.get(scope)
    return _set(t, scope, op, getattr(decision, "decided_by", "meaning"), score, subject=subject)


def _set(t: Task, scope: str, op: str, by: str, confidence: float, subject: str | None = None) -> Task:
    t.scope, t.operation, t.decided_by, t.confidence = scope, op, by, round(float(confidence), 3)
    t.subject = subject or t.subject
    t.intent = t.intent or by
    return t


def remember(state: Any, task: Task) -> None:
    """The last task of this chat (codes only), on the workspace record -- for the next turn's follow-up."""
    if task.scope not in ("portfolio", "one_case", "knowledge"):
        return
    flow = dict(getattr(state, "flow", None) or {})
    flow["last_task"] = task.memo()
    state.flow = flow


__all__ = ["OPERATIONS", "SCOPES", "Task", "canonical_for", "correction_rest", "filter_in", "interpret",
           "portfolio_followup", "remember", "singular_pointer"]
