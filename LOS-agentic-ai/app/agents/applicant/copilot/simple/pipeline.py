"""
THE SIMPLE PIPELINE (owner 2026-10-24: "GPU nahi hai, current setup pe simple pipeline banao"; config simple_chat.yaml).

On this CPU a Qwen call costs 3-6 s, so a question the chat UNDERSTANDS never waits on one:

    understand  the meaning reader (embeddings, ~0.1 s) + the named document / recorded field / the last answer
    ask         the intent's canonical question to the engine (answer_question: the same records, the same checks)
    say         the natural line built from the same records (polish.fact_lines) -- no model, so no wrong fact

A message it is not sure about returns None and the old pipeline (with Qwen) answers it exactly as before.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Lookup = Callable[[str], Awaitable[dict[str, Any]]]
#: the route's own answer (social / definition): the message -> the reply, or None (the rest of the chat answers)
Handler = Callable[["Frame"], Awaitable[dict[str, Any] | None]]

#: the last intent this pipeline answered, per chat (one process serves 8010) -- for "why?" / "which one first"
_LAST: "OrderedDict[str, str]" = OrderedDict()
_LOCK = threading.Lock()
_KEEP = 5000


@dataclass(frozen=True)
class Frame:
    intent: str                 # a catalogue intent, or "field"
    canonical: str              # the question the engine answers
    score: float = 0.0
    by: str = "meaning"         # meaning / named_document / field / followup / social / definition
    route: str = "engine"       # engine (the canonical question) / social / definition (the handler's own answer)


def cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.semantics import meaning

    return meaning._load("simple_chat.yaml")


def enabled() -> bool:
    return bool(cfg().get("enabled", False))


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "").lower()).split())


def _has_any(plain: str, words: list[Any]) -> bool:
    said = f" {plain} "
    return any(f" {_plain(w)} " in said for w in words or [] if _plain(w))


def last_intent(chat: str | None) -> str | None:
    with _LOCK:
        return _LAST.get(chat) if chat else None


def remember(chat: str | None, intent: str) -> None:
    if not chat:
        return
    with _LOCK:
        _LAST[chat] = intent
        _LAST.move_to_end(chat)
        while len(_LAST) > _KEEP:
            _LAST.popitem(last=False)


def forget(chat: str | None) -> None:
    """Another layer answered: "why?" next is not about this pipeline's last answer."""
    if chat:
        with _LOCK:
            _LAST.pop(chat, None)


def _canonical(intent: str, message: str) -> str | None:
    from app.agents.applicant.copilot.semantics import meaning

    spec = (meaning.catalogue().get("intents") or {}).get(intent) or {}
    return meaning._canonical(spec, meaning.party_in(message), meaning.document_in(message))


def _is_slot(document: str) -> bool:
    """A readable document name that is a checklist slot of some product (PAN, Address Proof, Salary Slip ...)."""
    from app.agents.applicant import config

    want = _plain(document)
    return any(_plain(str(row.get("slot") or "").replace("_", " ")) == want
               for product in config.products() or [] for row in config.checklist_for(product) or [])


def social_kind(message: str) -> str | None:
    """GREETING / THANKS / GOODBYE / ACKNOWLEDGEMENT when the WHOLE message is that (conversation.classify; config
    social.kinds), else None."""
    kinds = {str(k).upper() for k in (cfg().get("social") or {}).get("kinds") or []}
    if not kinds:
        return None
    from app.agents.applicant import conversation

    turn = conversation.classify(message)
    return turn.kind if turn is not None and turn.kind in kinds else None


def clear(message: str) -> bool:
    """
    The meaning reader is SURE what this message asks (config clear: top score >= accept and ahead of the next
    DIFFERENT question by margin), even when this pipeline does not answer it itself ("when was this case created",
    "will this loan get approved", "is voter id ok"): the deterministic layers answer it, so it never waits on the
    Qwen follow-up reader / agent first (measured 2026-10-25: 4-18 s, then the same layers answered).
    """
    spec = cfg().get("clear") or {}
    if not spec.get("enabled", False):
        return False
    from app.agents.applicant.copilot.semantics import meaning

    ranked = meaning.rank(message)
    if not ranked:
        return False
    top, score = ranked[0]
    if top in set(spec.get("never") or []):
        return False
    mine = _canonical(top, message)
    nxt = next((s for n, s in ranked[1:] if n != top and (mine is None or _canonical(n, message) != mine)), 0.0)
    return score >= float(spec.get("accept", 0.9)) and score - nxt >= float(spec.get("margin", 0.04))


def structural_sure(message: str) -> bool:
    """
    The DETERMINISTIC readers are sure of this message (2026-11-06): the structural reader names an intent by a strong
    match (config agreement.weak_* are not strong), or the conversation classifier reads a conversational act ("ek
    sawal hai" -> PREFACE). Such a message is answered by the deterministic layers, never handed to the model planner
    (live eval: the planner answered "quick question" with the case summary, "no, I meant the tenure" with the glossary).
    """
    spec = cfg().get("agreement") or {}
    try:
        from app.agents.applicant import conversation
        from app.agents.applicant.copilot.semantics import intents

        turn = conversation.classify(message)
        if turn is not None:
            # off-topic is the natural layer's polite refusal, never "about the case"
            return turn.kind != getattr(conversation, "OFF_TOPIC", "OFF_TOPIC")
        said = intents.understand(message, has_case=True)
        name, matched = getattr(said.intent, "name", str(said.intent)), said.matched_on
        if not (name in ("UNKNOWN", "MIXED") or matched in set(spec.get("weak_matches") or [])
                or any(str(matched).startswith(p) for p in spec.get("weak_prefixes") or [])):
            return True
        # A POLICY QUESTION THE HANDBOOK ANSWERS ("is a utility bill accepted as address proof?"): retrieval is
        # confident AND about the question's subject -- the handbook layer answers it, not the model planner
        if spec.get("handbook_is_sure", True) and name == "FOS_KNOWLEDGE":
            from app.agents.applicant.copilot.capabilities import general

            return general._handbook_confident(message)
        return False
    except Exception:  # noqa: BLE001 - not known to be sure
        return False


def sure(message: str, chat: str | None = None) -> bool:
    """This pipeline answers it, the meaning reader is sure of it, or the deterministic readers are."""
    return read(message, chat) is not None or clear(message) or structural_sure(message)


def _asks(message: str) -> bool:
    """The message ASKS something: a "?" or a question word (intent_catalogue.yaml task.question_words) -- "No, I meant
    the previous case" names the case id but asks nothing about it."""
    from app.agents.applicant.copilot.semantics import task

    asks = {str(w).lower() for w in task._cfg().get("question_words") or []}
    return "?" in str(message) or any(w in asks for w in _plain(message).split())


def is_edit(message: str) -> bool:
    """A CHANGE to the case, never a question about it: an edit word (product_flow.yaml case_form.edit_words: "change",
    "badlo") or a value with a field named ("loan amount 600000 karo") -- the case form's turn."""
    from app.agents.applicant.copilot.capabilities import case_form

    plain = _plain(message)
    if _has_any(plain, case_form._words("edit_words")):
        return True
    return bool(re.search(r"\d", message)) and case_form._edit_target(message, "en") is not None


def _family(intent: str, families: list) -> str:
    for group in families:
        if intent in {str(i) for i in group}:
            return "|".join(sorted(str(i) for i in group))
    return intent


def disagrees(message: str, canonical: str) -> bool:
    """
    TWO READERS MUST AGREE (config agreement): the structural reader's reading of the MESSAGE vs its reading of the
    canonical question this pipeline would ask. True only when the message reading is confident and names a different
    intent family -- then the engine answers the message itself.
    """
    spec = cfg().get("agreement") or {}
    if not spec.get("enabled", True) or not canonical:
        return False
    try:
        from app.agents.applicant.copilot.semantics import intents

        said = intents.understand(message, has_case=True)
        asked = intents.understand(canonical, has_case=True)
        # a DOCUMENT the message names is lost from the question asked ("बैंक स्टेटमेंट अपलोड हुआ क्या?" -> "What is
        # pending?"): the engine answers about that document
        if said.document_type is not None and asked.document_type != said.document_type:
            return True
        name = getattr(said.intent, "name", str(said.intent))
        matched = said.matched_on
        if name in ("UNKNOWN", "MIXED") or matched in set(spec.get("weak_matches") or []) \
                or any(str(matched).startswith(p) for p in spec.get("weak_prefixes") or []):
            return False
        families = list(spec.get("families") or [])
        return _family(name, families) != _family(getattr(asked.intent, "name", str(asked.intent)), families)
    except Exception:  # noqa: BLE001 - no second reading: the first stands, as before
        return False


def read(message: str, chat: str | None = None, previous_intent: str | None = None) -> Frame | None:
    """What the message asks, as the engine's canonical question -- or None (not sure: the old pipeline answers)."""
    frame = _read(message, chat, previous_intent)
    # follow-ups / small talk / definitions are read from the chat, not the words: never re-checked
    if frame is not None and frame.route == "engine" and frame.by in ("meaning", "named_document", "field") \
            and disagrees(message, frame.canonical):
        return None
    return frame


def _read(message: str, chat: str | None = None, previous_intent: str | None = None) -> Frame | None:
    from app.agents.applicant.copilot.answering import profile
    from app.agents.applicant.copilot.semantics import meaning

    spec = cfg()
    plain = _plain(message)
    if not plain:
        return None
    # 0. SMALL TALK AND DEFINITIONS (no case needed): the whole message is a greeting / thanks / goodbye, or a
    # glossary acronym asked bare ("what is cpa") -- their fixed answers, at once (measured 2026-10-25: 4-7 s waiting
    # on Qwen's small talk / follow-up reader, then the same line)
    social = social_kind(message)
    if social:
        return Frame(social, message, 1.0, "social", "social")
    if (spec.get("definitions") or {}).get("enabled", True):
        from app.agents.applicant.copilot.capabilities import general

        if general.glossary_acronym_asked(message):
            return Frame("definition", message, 1.0, "definition", "definition")
    # 1. A FOLLOW-UP that needs the last answer ("why?", "which one first")
    # THE LAST ANSWER, whoever gave it (2026-11-06 live eval: "which one first?" after the ENGINE answered "what is
    # pending" was out of scope -- this pipeline only remembered its own answers). The engine's intent comes back in
    # the chat's context; config engine_intents maps it to this pipeline's topic.
    last = last_intent(chat) or (spec.get("engine_intents") or {}).get(str(previous_intent or "").upper())
    for rule in (spec.get("followups") or {}).values():
        if plain in {_plain(p) for p in rule.get("phrases") or []}:
            target = (rule.get("after") or {}).get(last) if last else None
            canonical = _canonical(str(target), message) if target else None
            return Frame(str(target), canonical, 1.0, "followup") if canonical else None
    # THE SAME QUESTION FOR THE OTHER PARTY ("and for the co-applicant?" right after "what is pending"): the last
    # topic, asked for the party the message names (config party_followup; 2026-11-06 live eval: the model planner
    # answered it for the applicant)
    party_rule = spec.get("party_followup") or {}
    if last and party_rule.get("enabled", True) and len(plain.split()) <= int(party_rule.get("max_words", 6)) \
            and last in set(party_rule.get("topics") or []):
        party = meaning.party_in(message)
        if party == "CO_APPLICANT" and not meaning.document_in(message):
            canonical = _canonical(last, message)
            if canonical and "co-applicant" in canonical.lower():
                return Frame(last, canonical, 1.0, "followup")
    if (spec.get("edits") or {}).get("guard", True) and is_edit(message):
        return None                     # the case form answers a change ("confirm?"), never this pipeline
    # WHAT A DOCUMENT SAYS ("what does the salary slip say", "show me the pan details"): its recorded fields
    # (conversation_general.yaml phrases.document_contents -> texts.document_contents_question)
    if (spec.get("document_contents") or {}).get("enabled", True):
        from app.agents.applicant.copilot.capabilities import bare_document

        contents = bare_document.contents(message)
        if contents:
            return Frame("document_details", contents, 1.0, "document_contents")
    document = meaning.document_in(message)
    ranked = meaning.rank(message)
    top, score = ranked[0] if ranked else ("", 0.0)
    # the runner-up that would be a DIFFERENT question ("upload_pending" asks "What is pending?" too)
    mine = _canonical(top, message) if top else None
    nxt = next((s for n, s in ranked[1:] if _canonical(n, message) != mine), 0.0)
    # 2. ONE RECORDED FIELD ("what is his date of birth") -- unless the meaning reader is SURE of another question
    # ("how long has it been at this stage" is the time in stage, not the tenure)
    field_spec = spec.get("field_question") or {}
    sure = top in set(field_spec.get("unless_sure_of") or []) and score >= float(field_spec.get("sure", 0.95))
    # a VALUE in the message ("loan amount 600000 karo") is an edit / a case id, never the field's question
    # and the message must ASK it ("No, I meant the previous case" names the case id, asks nothing)
    if field_spec.get("enabled") and not document and not sure and not re.search(r"\d", message) \
            and (not field_spec.get("needs_question", True) or _asks(message)):
        asked = profile.detect(message)
        fields = list(getattr(asked, "fields", []) or []) if asked is not None else []
        if len(fields) == 1 and fields[0] not in (profile.ALL, profile.ALL_APPLICANT, profile.ALL_APPLICATION,
                                                  profile.ALL_FINANCIAL, profile.FIELDS_AVAILABLE):
            # a field whose NAME is also a glossary term ("tenure"): the conversation layer's own recorded-field
            # question (conversation_general.yaml texts.field_question), never the glossary's "what is tenure"
            from app.agents.applicant.copilot.capabilities import general

            texts = general.cfg().get("texts") or {}
            template = str(field_spec.get("canonical") or "what is the {field}")
            if fields[0] in set(texts.get("field_question_fields") or []):
                template = str((texts.get("field_question") or {}).get("en") or template)
            canonical = template.format(field=profile.label(fields[0]).lower())
            return Frame("field", canonical, 1.0, "field")
    if not ranked:
        return None
    # 3. A NAMED DOCUMENT ASKED ABOUT IN A FEW WORDS: its status on the open case -- only a document the case HAS a
    # place for (a checklist slot: "Voter ID" is one way to give the Address Proof, asked as "is it accepted")
    named = spec.get("named_document") or {}
    if document and _is_slot(document) and top in set(named.get("from_intents") or []) \
            and len(plain.split()) <= int(named.get("max_words", 5)) \
            and not _has_any(plain, (named.get("acceptance_words") or []) + (named.get("detail_words") or [])):
        intent = str(named.get("intent") or "document_status")
        canonical = _canonical(intent, message)
        if canonical:
            return Frame(intent, canonical, round(score, 3), "named_document")
    # 4. THE MEANING READER, when it is sure
    if top not in set(spec.get("intents") or []):
        return None
    if score < float(spec.get("accept", 0.8)) or score - nxt < float(spec.get("margin", 0.04)):
        return None
    decision = meaning._decision(top, score, ranked, "simple", message)
    if not decision.canonical or "{" in decision.canonical:
        return None
    return Frame(decision.intent, decision.canonical, round(score, 3), "meaning")


async def answer(message: str, *, chat: str | None, lookup: Lookup, question: str | None = None,
                 handlers: dict[str, Handler] | None = None, case_open: bool = True,
                 previous_intent: str | None = None) -> dict[str, Any] | None:
    """The reply for a message this pipeline understands, else None. `lookup(canonical)` runs the engine; `handlers`
    answer the social / definition routes. With no case open only those routes run (config case_open_only)."""
    if not enabled():
        return None
    frame = read(message, chat, previous_intent)
    if frame is None:
        return None
    if frame.route != "engine":
        handler = (handlers or {}).get(frame.route)
        out = await handler(frame) if handler is not None else None
        if not isinstance(out, dict) or not str(out.get("answer") or "").strip():
            return None
        out = dict(out)
        out["polished"] = True          # a fixed line: no model rewrite after it
        out["simple"] = {"intent": frame.intent, "by": frame.by, "score": frame.score}
        forget(chat)                    # "why?" after a hello is not about an earlier answer
        return out
    if not case_open and cfg().get("case_open_only", True):
        return None
    reply = await lookup(frame.canonical)
    intent = str((reply or {}).get("intent") or "")
    if not isinstance(reply, dict) or not str(reply.get("answer") or "").strip() \
            or intent in ("UNKNOWN", "GUARDRAIL_BLOCKED", "CLARIFICATION", ""):
        return None                     # the engine could not answer its own question: the old pipeline tries
    from app.agents.applicant.copilot.answering import polish

    # THE NATURAL LINE FROM THE SAME RECORDS, no model (polish.fact_lines + the short facts below it)
    out = await polish.apply(reply, question=question or message, use_model=False)
    out = dict(out)
    out["polished"] = True              # the old pipeline's model rewrite never runs on it again
    out["simple"] = {"intent": frame.intent, "by": frame.by, "score": frame.score}
    remember(chat, frame.intent if frame.intent != "field" else "case_status")
    return out


__all__ = ["Frame", "Handler", "answer", "cfg", "disagrees", "enabled", "forget", "last_intent", "read", "remember", "structural_sure", "sure",
           "social_kind"]
