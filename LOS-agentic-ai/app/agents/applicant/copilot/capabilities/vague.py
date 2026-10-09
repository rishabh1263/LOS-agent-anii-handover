"""
VAGUE / ONE-WORD MESSAGES (owner decision A, 2026-10-08; config app/config/conversation_general.yaml `vague`).

"docs", "kyc", "status", "top 2", "case" ... -> ONE short follow-up question with 2-4 options (ask: links) built from the
STATE and the MEANING ranking (semantics/meaning.py), never from a keyword table:
  * a case open: the most likely CASE intents for the word, their canonical questions (with the document it names);
  * no case open: a number with a list word -> the list orders (top N needing action / latest N / oldest N); else the
    configured starting points (my cases, a new case, help).
The pick answers at once (a number, or the option's words); a SECOND vague message in a row -> the most likely answer
directly, never the same question again. Nothing here reads case data or writes anything.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

_NUMBER = re.compile(r"\b(\d{1,2})\b")


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import general

    return general.cfg().get("vague") or {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def _words(message: str) -> list[str]:
    return re.sub(r"[^\w\s-]", " ", str(message or "").lower()).split()


def is_command(message: str, case_open: bool = False) -> bool:
    """Open / close / switch / more / a case or applicant id / a new case / a query / a case list ("show the top 2 cases
    needing action"): the workspace and the forms act on these words -- never vague, never re-understood."""
    from app.agents.applicant.copilot.capabilities import case_actions, case_list, workspace

    text = str(message or "")
    if any(p.search(text) for p in workspace._ID.values()):
        return True
    if any(workspace._says(k, text) for k in ("open", "exit", "switch", "previous", "more", "list", "this_applicant")):
        return True
    if any(case_actions.asks(k, text) for k in ("raise_query", "list_queries", "new_case")):
        return True
    wanted = case_list.understand(text)
    if wanted is None:
        return False
    nouns = (case_list.cfg().get("phrases") or {}).get("case_nouns") or {}
    plural = {str(w).lower() for w in nouns.get("plural") or []}
    said = set(_words(text))
    verbs = {str(w).lower() for w in (case_list.cfg().get("phrases") or {}).get("list_verbs") or []}
    singular = {str(w).lower() for w in nouns.get("singular") or []}
    if said & singular and not said & plural and not said & verbs and not wanted.size and not wanted.sort:
        return False                       # "is my case ready?": ONE case -- a question, not the list
    return bool(said & plural) or bool(said & verbs) or (not case_open and bool(wanted.filter or wanted.sort or wanted.size))


_KNOWN: dict[str, Any] = {}


def _known_words() -> set[str]:
    """Every word of the intent examples / paraphrases, the document aliases and the vague config (cached)."""
    from app.agents.applicant.copilot.semantics import meaning

    cat, para = meaning.catalogue(), meaning._load("intent_paraphrases.yaml")
    key = (id(cat), id(para))
    if _KNOWN.get("key") != key:
        texts = [str(e) for spec in (cat.get("intents") or {}).values() for e in spec.get("examples") or []]
        texts += [str(e) for items in (para.get("paraphrases") or {}).values() for e in items or []]
        texts += [alias for alias, _ in meaning._documents()]
        texts += [str(w) for w in _cfg().get("known_words") or []]
        _KNOWN.update(key=key, words={w for t in texts for w in _words(t)})
    return _KNOWN["words"]


def is_vague(message: str) -> bool:
    """A fragment: at most `max_words` words, not a pick / yes / no / greeting / thanks (config `not_vague`)."""
    words = _words(message)
    if not words or len(words) > int(_cfg().get("max_words", 2)):
        return False
    said = " ".join(words)
    if re.fullmatch(r"\d+", said):
        return False                                 # a pick, never vague
    not_vague = {str(w).lower() for w in _cfg().get("not_vague") or []}
    if said in not_vague:
        return False
    from app.agents.applicant.copilot.capabilities import case_list, workspace

    if any(p.search(str(message)) for p in workspace._ID.values()) or any(
            workspace._says(k, str(message)) for k in ("open", "exit", "switch", "previous", "more")):
        return False                                 # "open CASE-1A2B", "close", "more": a command

    wanted = case_list.understand(message)
    first_n = {str(w).lower() for w in (case_list.cfg().get("phrases") or {}).get("first_n") or []}
    if wanted is not None and (wanted.filter or wanted.status or wanted.group or wanted.sort not in (None, "", "recent")
                               or (wanted.sort == "recent" and _NUMBER.search(said) and not first_n & set(words))):
        return False                                 # "last 3" / "kyc cases" / "oldest 2": the list order is clear
    if wanted is not None and not wanted.sort and _NUMBER.search(said):
        return True                                  # "top 2": top by what? -- needing action / latest / oldest
    if len(words) == 1:
        # a ONE word the system knows (an intent example's word, a document alias, a configured phrase); gibberish
        # ("asdfghjkl") is not vague -- the "didn't understand" clarification answers it
        return words[0] in _known_words()
    from app.agents.applicant.copilot.semantics import meaning

    asked = {str(w).lower() for w in _cfg().get("document_question_words") or []}
    if meaning.document_in(message) and asked & set(words):
        return False                                 # "dl verified?": that document's status, asked clearly
    # two words: vague only when MEANING is not confident ("kyc status" is clear; "hmm status" is not)
    try:
        ranked = meaning.rank(message)
    except Exception:  # noqa: BLE001 - no embedding model: a two-word message goes on as typed
        return False
    accept = float((meaning.catalogue().get("decide") or {})["accept"])
    return bool(ranked) and ranked[0][1] < accept


def names_a_case(message: str, claims) -> bool:
    """ "Priya Verma" / "Rahul": the applicant name of one of the caller's OWN cases -- the workspace selects it."""
    from app.agents.applicant.copilot.capabilities import workspace

    try:
        found = workspace._by_name(message, workspace.my_cases(claims))
    except Exception:  # noqa: BLE001 - no case list: not a name
        return False
    return bool(found.application or found.candidates)


def waits_for(message: str, state, claims, context) -> bool:
    """Whether a question the chat is waiting on could take THIS word as its answer: a draft / write proposal or a
    "which case?" pick always; the agent's clarification only when the word is one of its options' words. A yes / no
    question never takes "docs" / "pan" (a vague word is never yes / no), so it never blocks the follow-up options."""
    flow = dict(getattr(state, "flow", None) or {})
    if flow.get("write") or flow.get("after_pick") or (flow.get("portfolio") or {}).get("pending"):
        return True          # the list's Yes / No / Pending / Done question survives only when this message answers it
    from app.agents.applicant.copilot.capabilities import workspace
    from app.agents.applicant.copilot.conversation import state as conv

    held = [state]
    conversation_id = (context or {}).get("conversation_id") if isinstance(context, dict) else None
    if conversation_id and claims is not None:
        held.append(conv.STORE.get(workspace._subject(claims), conversation_id))
    said = set(_words(message))
    for record in held:
        pending = getattr(record, "pending_clarification", None)
        labels = [o.label for o in getattr(pending, "options", None) or []] or list(
            getattr(record, "pending_options", None) or [])
        if pending is not None and getattr(pending, "question_type", None) == conv.YES_NO and not labels:
            continue
        if any(said & set(_words(label)) for label in labels):
            return True
    return False


def _ask_link(text: str) -> str:
    return f"[{text}](ask:{quote(text)})"


def _document_code(message: str) -> str | None:
    """The configured document code a word names ("pan" -> PAN), for the Upload link."""
    from app.agents.applicant.copilot.semantics import meaning

    said = " " + re.sub(r"[^\w\s]", " ", meaning._normalised(message).lower()) + " "
    docs = meaning._load("documents.yaml").get("documents") or {}
    for code, spec in (docs.items() if isinstance(docs, dict) else []):
        for alias in [*(spec or {}).get("aliases", []), str(code).replace("_", " ")]:
            if alias and f" {str(alias).lower()} " in said:
                return str(code)
    return None


def upload_option(message: str, code: str) -> str:
    """upload:<code>:<party>:<label> for the document a message names; the party it names ("co-applicant pan"), as
    the upload link writes it (config upload_party)."""
    from app.agents.applicant.copilot.semantics import meaning

    parties = _cfg().get("upload_party") or {}
    party = parties.get(meaning.party_in(message) or "") or parties.get("default")
    return f"upload:{code}:{party}:{meaning.document_in(message) or code}"


def is_upload(option: str) -> bool:
    return str(option or "").startswith("upload:")


def _lang() -> str:
    from app.agents.applicant.copilot.answering import language_lock

    return language_lock.current() or "en"


def _parts(option: str) -> tuple[str, str, str]:
    """upload:<code>:<party>:<label> -> (code, party, label)."""
    _, code, party, label = (str(option).split(":", 3) + ["", "", ""])[:4]
    return code, party, label


def _upload_link(option: str) -> str:
    from app.agents.applicant.copilot.answering import contract

    code, party, label = _parts(option)
    return contract.link("upload", _lang(), doc=code, party=party, doc_label=label or code)


def upload_reply(option: str, request_id: str, context, claims) -> dict[str, Any]:
    """The Upload option picked by its number / words: the upload link for the open case."""
    from app.agents.applicant.copilot.capabilities import general

    code, _party, label = _parts(option)
    state = general._state(claims, context)
    text = general._say_text(_cfg().get("upload_reply"), _lang(), document=label or code)
    link = _upload_link(option)
    return general._reply(request_id, "VAGUE_UPLOAD", f"{text}\n\n{link}",
                          case_id=getattr(state, "active_case_id", None), query_type="ACTION_REQUEST")


def _render(option: str) -> str:
    return _upload_link(option) if is_upload(option) else _ask_link(option)


def _list_options(message: str) -> list[str]:
    """ "top 2" / "2 cases": a count with no order -- the configured list orders for that number."""
    number = _NUMBER.search(str(message or ""))
    if not number:
        return []
    return [str(t).format(n=number.group(1)) for t in _cfg().get("list_with_number") or []][:4]


def _case_options(message: str) -> list[str]:
    """The canonical questions of the most likely CASE intents for this word (meaning ranking), document filled."""
    from app.agents.applicant.copilot.semantics import meaning

    listed = _list_options(message)
    if listed:
        return listed

    try:
        ranked = meaning.rank(message)
    except Exception:  # noqa: BLE001 - no embedding model: the configured case defaults
        ranked = []
    intents = meaning.catalogue().get("intents") or {}
    document = meaning.document_in(message)
    if not document:
        # "docs" / "kyc": the TOPIC's case questions (config `case_topics`), never a loose nearest intent
        for name, _score in ranked[:3]:
            for topic in _cfg().get("case_topics") or []:
                if name in (topic.get("intents") or []):
                    return [str(o) for o in topic.get("options") or []][:4]
    out: list[str] = []
    for name, _score in ranked:
        spec = intents.get(name) or {}
        if spec.get("kind") != "case":
            continue
        if document and spec.get("needs") != "document" and name not in (_cfg().get("document_intents_also") or []):
            continue                                  # "pan" -> that document's questions first
        text = meaning._canonical(spec, None, document)
        if text and text not in out:
            out.append(text)
        if len(out) >= int(_cfg().get("max_options", 3)):
            break
    code = _document_code(message) if document else None
    if code and len(out) < 4:
        out.append(upload_option(message, code))
    for fallback in _cfg().get("case_defaults") or []:
        if len(out) >= 2:
            break
        if fallback not in out:
            out.append(str(fallback))
    return out[:4]


def _no_case_options(message: str) -> list[str]:
    """No case open: a count -> the list orders; else the TOPIC the word is about (meaning ranking -> config
    `no_case_topics`: documents, KYC, CPA, ...) as list / how-to questions; else the configured starting points."""
    listed = _list_options(message)
    if listed:
        return listed
    from app.agents.applicant.copilot.semantics import meaning

    try:
        ranked = [name for name, _ in meaning.rank(message)[:3]]
    except Exception:  # noqa: BLE001 - no embedding model: the starting points
        ranked = []
    if meaning.document_in(message) and _cfg().get("document_intent"):
        ranked.insert(0, str(_cfg()["document_intent"]))     # a named document: the documents topic first
    for name in ranked:
        for topic in _cfg().get("no_case_topics") or []:
            if name in (topic.get("intents") or []):
                return [str(o) for o in topic.get("options") or []][:4]
    return [str(t) for t in _cfg().get("no_case_defaults") or []][:4]


def contextual(message: str, state, case_open: bool) -> str | None:
    """
    A FRAGMENT READ IN CONTEXT (owner 2026-10-09; config `vague.contextual`): the one interpretation the conversation
    supports, as the question to answer for the OPEN case (its data is read fresh by the engine, never taken from the
    conversation), or None -> the follow-up question with options.
      1. the previous question of this chat (meaning's last_meaning) is one of the fragment's readings -- in the
         fragment's topic (config case_topics) or among its top-ranked intents: that question again, with its
         document / party ("docs" after "what is pending" -> pending; after "is PAN verified" -> the PAN's status;
         after a switch -> the same question for the applicant now open);
      2. "upload" with a document in context -> that document's upload option;
      3. the fragment alone has ONE clear reading (meaning's accept_with_lead) -> that question.
    No case open: None (what to do is asked).
    """
    spec = _cfg().get("contextual") or {}
    if not spec.get("enabled") or not case_open:
        return None
    from app.agents.applicant.copilot.semantics import meaning

    try:
        ranked = meaning.rank(message)
    except Exception:  # noqa: BLE001 - no embedding model: no reading -> the options
        return None
    if not ranked:
        return None
    intents = meaning.catalogue().get("intents") or {}
    case_like = [(n, sc) for n, sc in ranked if (intents.get(n) or {}).get("kind") in ("case", "upload")]
    top_names = [n for n, _ in case_like[:int(spec.get("previous_in_top", 3))]]
    previous = (getattr(state, "flow", None) or {}).get("last_meaning") or {}
    prev_intent = previous.get("intent")
    if "upload_document" in top_names and previous.get("document"):
        code = _document_code(previous.get("document") or "")
        if code:
            return upload_option(previous.get("document") or "", code)
    topic = next((t.get("intents") or [] for t in _cfg().get("case_topics") or []
                  if top_names and top_names[0] in (t.get("intents") or [])), [])
    if prev_intent and (prev_intent in topic or prev_intent in top_names):
        return meaning._canonical(intents.get(prev_intent) or {}, previous.get("party"), previous.get("document"))
    lead = (meaning.catalogue().get("decide") or {}).get("accept_with_lead") or {}
    in_wide_topic = any(case_like and case_like[0][0] in (t.get("intents") or []) and len(t.get("intents") or []) > 1
                        for t in _cfg().get("case_topics") or [])
    if lead and case_like and not in_wide_topic:
        # ONE reading on its own ("kyc", "next"); a topic word with several readings ("docs": pending / uploaded /
        # verified) is asked unless the conversation chose one above
        best, score = case_like[0]
        second = case_like[1][1] if len(case_like) > 1 else 0.0
        if score >= float(lead["score"]) and score - second >= float(lead["margin"]):
            return meaning._canonical(intents.get(best) or {}, meaning.party_in(message), meaning.document_in(message))
    return None


def ask(message: str, state, request_id: str, case_open: bool) -> dict[str, Any] | None:
    """The follow-up question with its options (and the options remembered on the chat), or None (< 2 options)."""
    options = _case_options(message) if case_open else _no_case_options(message)
    if len(options) < 2:
        return None
    from app.agents.applicant.copilot.capabilities import general, workspace

    topic = " ".join(_words(message))
    question = general._say_text(_cfg().get("question"), _lang(), topic=topic)
    flow = dict(getattr(state, "flow", None) or {})
    flow["vague"] = {"options": options, "message": topic}
    state.flow = flow
    workspace._save(state)
    lines = [question, ""] + [f"{n}. {_render(o)}" for n, o in enumerate(options, 1)]
    return general._reply(request_id, "VAGUE_OPTIONS", "\n".join(lines), case_id=getattr(state, "active_case_id", None),
                          query_type="CLARIFICATION")


def resolve(message: str, state, case_open: bool = False) -> str | None:
    """While options are pending: a number / an option's words -> that option; a second vague message -> the most
    likely option (option 1) directly. Anything else clears the pending options and goes on as typed."""
    flow = dict(getattr(state, "flow", None) or {})
    pending = flow.get("vague")
    if not pending:
        return None
    from app.agents.applicant.copilot.capabilities import workspace

    flow.pop("vague", None)
    state.flow = flow
    workspace._save(state)
    options = list(pending.get("options") or [])
    said = " ".join(_words(message))
    if re.fullmatch(r"\d{1,2}", said) and 1 <= int(said) <= len(options):
        return options[int(said) - 1]
    for option in options:
        shown = _parts(option)[2] if is_upload(option) else option
        if said and (said == " ".join(_words(option)) or said in (" ".join(_words(f"upload the {shown}")),
                                                               " ".join(_words(f"upload {shown}")))):
            return option
    if is_vague(message) and options:
        # vague again: the most likely answer, never a second question -- for THIS word ("docs" then "kyc" answers
        # the KYC option, not the documents one)
        if said != pending.get("message"):
            fresh = _case_options(message) if case_open else _no_case_options(message)
            if fresh:
                return fresh[0]
        return options[0]
    return None


__all__ = ["ask", "contextual", "enabled", "is_command", "is_upload", "is_vague", "resolve", "upload_option", "upload_reply",
           "waits_for"]
