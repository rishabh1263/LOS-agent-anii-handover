"""
THE GENERAL LAYER (owner decisions 2026-10-08; config app/config/conversation_general.yaml).

Runs BEFORE any case logic, so a question that needs no case never meets "which case?":

    help / ok / frustration      short reply + the officer's case summary + 2-3 suggestions (or options + supervisor)
    role / stage                 from the LOGIN (JWT claims), never guessed
    "WHAT IS NAME"               one question: the applicant's / the co-applicant's / your login name
    definitions, full forms      glossary.yaml, then the knowledge base (with its source)
    process steps                numbered steps whose facts are filled from the live config (FOS gate checks, the
                                 product checklist, the required form fields, the KYC rule) -- with a source
    product documents            the product checklist (required vs optional, per party)
    unknown terms                "not in the knowledge base yet" + logged to evals/knowledge_gaps.yaml

`after()` runs on every reply: "Do you mean X or Y?" when the router's top two tools are close, and the gradual
fallback (the second miss in a row -> the closest FAQ questions + the supervisor contact line).
"""

from __future__ import annotations

import difflib
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[4]
_PATH = _ROOT / "config" / "conversation_general.yaml"
_CFG: dict[str, Any] = {"mtime": None, "data": {}}
_LOCK = threading.RLock()


def cfg() -> dict[str, Any]:
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _CFG["mtime"] != mtime:
            _CFG["data"] = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
            _CFG["mtime"] = mtime
        return _CFG["data"]


def enabled() -> bool:
    return bool(cfg().get("enabled", False))


def _say(key: str, lang: str, seed: int = 0, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say((cfg().get("texts") or {}).get(key), lang, seed, **values)


def _pick(value: Any, lang: str) -> Any:
    from app.agents.applicant.copilot.answering import language_lock

    return language_lock.pick(value, lang) if isinstance(value, dict) else value


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ'-]", " ", str(text or "").lower())).strip() + " "


def _has(text: str, kind: str) -> bool:
    said = _norm(text)
    return any(_norm(p) in said for p in (cfg().get("phrases") or {}).get(kind) or [])


def _only(text: str, kind: str) -> bool:
    return _norm(text).strip() in {_norm(p).strip() for p in (cfg().get("phrases") or {}).get(kind) or []}


def _reply(request_id: str, intent: str, answer: str, *, block: str = "", case_id: str | None = None,
           tts: str | None = None, **extra: Any) -> dict[str, Any]:
    # THE LAST CHECK, as on the agent's deterministic answers: an internal file name / path or an identifier in a
    # handbook passage is dropped or masked before it is shown (security/guardrails.published)
    from app.security import guardrails

    answer, _verdict = guardrails.published(answer)
    out = {"request_id": request_id, "intent": intent, "answer": answer, "case_id": case_id,
           "category": "KNOWLEDGE_ONLY", "query_type": "CONVERSATION", "response_source": "GENERAL",
           "documents": [], "actions": [], "errors": [], "tools_invoked": [], "suggested_questions": [],
           "no_case_links": True, **extra}
    if block:
        out["faq_block"] = block
    if tts:
        out["tts_text"] = tts
    return out


def _asks(questions: list[str]) -> str:
    from app.agents.applicant.copilot.answering import contract

    return " · ".join(contract.ask(q) for q in questions if q)


def _state(claims: dict[str, Any], context: dict[str, Any] | None):
    from app.agents.applicant.copilot.capabilities import workspace

    workspace_id = (context or {}).get("workspace_id") if isinstance(context, dict) else None
    return workspace._state(workspace._subject(claims), workspace_id)


def _asked(state) -> bool:
    return bool(state is not None and (getattr(state, "pending_clarification", None)
                                       or getattr(state, "pending_options", None)))


def _pending(state, claims: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> bool:
    """A question this chat is waiting on -- "ok" / "haan" / "2" answer it, never small talk.

    Three places hold one: the workspace's product flow (portfolio Yes/No, "which case?" pick, a write proposal),
    the workspace record's own clarification, and the agent's conversation (context.conversation_id).
    """
    flow = dict(getattr(state, "flow", None) or {})
    if (flow.get("portfolio") or {}).get("pending") or flow.get("after_pick") or flow.get("write") or _asked(state):
        return True
    conversation_id = (context or {}).get("conversation_id") if isinstance(context, dict) else None
    if not conversation_id or claims is None:
        return False
    from app.agents.applicant.copilot.capabilities import workspace
    from app.agents.applicant.copilot.conversation import state as conv

    return _asked(conv.STORE.get(workspace._subject(claims), conversation_id))


# --------------------------------------------------------------------------
# the answers
# --------------------------------------------------------------------------

def _summary(claims: dict[str, Any], lang: str) -> str:
    """The officer's case summary line (the case list's own counts)."""
    from app.agents.applicant.copilot.capabilities import case_list

    page = case_list.run(claims, case_list.ListQuery(size=1, with_counts=True))
    if not page.all_total:
        return case_list._say("none", lang)
    order = ((case_list.cfg().get("sorts") or {}).get("needs_action") or {}).get("order") or list(page.counts)
    parts = [case_list._name_of("part", k, lang, n=page.counts[k]) for k in order if page.counts.get(k)]
    return case_list._say("summary", lang, total=page.all_total, parts=", ".join(parts))


def _suggestions(state, lang: str, n: int = 3) -> list[str]:
    from app.agents.applicant.copilot.capabilities import faq

    return [i["send"] for i in faq.items(getattr(state, "active_case_id", None), lang, n)]


def _fos_checks() -> list[str]:
    """What the FOS gate checks now: the readiness report's own groups (its labels, the same rules switch them on),
    then any other configured gate check. The gate's catch-all "FOS requirements" is never shown by itself."""
    from app.agents.applicant import workflow
    from app.agents.applicant.copilot.answering import readiness_report
    from app.agents.applicant.copilot.capabilities import gates
    from app.agents.los import kyc_gate
    from app.agents.signature import presence

    groups = ["group_application", "group_applicant_documents"]
    groups += ["group_co_applicant_documents"] if workflow.coapp_mandatory_enabled() else []
    groups += ["group_signature"] if presence.enabled() else []
    groups += ["group_kyc"] if kyc_gate.enabled() else []
    out = [readiness_report._label(g) for g in groups]
    covered = {"readiness", "kyc_checks"}           # the sources the groups above already stand for
    for check in ((gates.config().get("gates") or {}).get("FOS") or {}).get("checks") or []:
        if gates._flag_on(check.get("when_flag")) and check.get("source") not in covered:
            out.append(str(check.get("label") or check.get("id")))
    return out


def _product_of(text: str) -> str | None:
    from app.agents.applicant import config

    said = _norm(text)
    for product in config.products() or []:
        name = str(product)
        if name.lower() == "default":
            continue
        if _norm(name.replace("_", " ")) in said or _norm(name) in said:
            return name
    return None


def _readable(value: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(value)


def _process(topic: str, state, lang: str) -> tuple[str, str]:
    """(markdown, tts) of one process answer, its facts read from the live config now."""
    from app.agents.applicant import config
    from app.agents.applicant.copilot.capabilities import case_form

    texts = cfg().get("texts") or {}
    source = _say("source", lang, source=(texts.get("sources") or {}).get(
        "complete_case" if topic == "complete_case" else topic, ""))
    if topic == "what_is_cpa":
        from app.agents.applicant.copilot.answering import professional, style

        head = professional.strip_emojis(style.definition("CPA", lang) or "").strip()
        tail = _pick(texts.get("what_is_cpa_tail"), lang).format(checks=", ".join(_fos_checks()))
        return f"{head}\n\n{tail}\n\n{source}", f"{professional.plain(head)} {tail}"
    if topic == "verify_documents":
        steps = _pick(texts.get("verify_documents"), lang) or []
    else:
        products = [p for p in config.products() or [] if str(p).lower() != "default"]
        product = None
        if getattr(state, "active_case_id", None):
            from app.store import get_repository

            application = get_repository().get_application(state.active_case_id)
            product = getattr(application, "product", None)
        product = product or (products[0] if products else "")
        mandatory = [_readable(e.get("slot")) for e in config.checklist_for(product) or [] if e.get("mandatory", True)]
        from app.agents.los import kyc_gate

        kyc_step = _pick(texts.get("kyc_step"), lang) if kyc_gate.enabled() else ""
        steps = [str(s).format(fields=", ".join(case_form.label(f, lang) for f in case_form._cfg().get("required") or []),
                               product=_readable(product), documents=", ".join(mandatory), kyc_step=kyc_step,
                               checks=", ".join(_fos_checks()))
                 for s in (_pick(texts.get("move_to_cpa"), lang) or [])]
    steps = [s for s in steps if str(s).strip()]
    md = "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1)) + f"\n\n{source}"
    return md, "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1))


def _products_table(product: str, lang: str) -> tuple[str, str]:
    from app.agents.applicant import config, workflow

    texts = cfg().get("texts") or {}
    cols = _pick(texts.get("products_columns"), lang) or ["Document", "Required", "Accepted as"]
    rows = []
    for e in config.checklist_for(product) or []:
        rows.append(f"| {_readable(e.get('slot'))} | {_say('required' if e.get('mandatory', True) else 'optional', lang)} | "
                    + ", ".join(_readable(a) for a in e.get("accepts") or []) + " |")
    lines = [_say("products_heading", lang, product=_readable(product)),
             "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols), *rows]
    coapp = workflow.coapp_mandatory_enabled()
    if coapp:
        lines += ["", _say("coapp_heading", lang), "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        lines += [f"| {_readable(e['slot'])} | {_say('required', lang)} | " + ", ".join(_readable(a) for a in e.get("accepts") or []) + " |"
                  for e in config.co_applicant_documents() or []]
    lines += ["", _say("products_note", lang),
              _say("source", lang, source=str((texts.get("sources") or {}).get("products", "")).format(
                  coapp=", co-applicant documents" if coapp else ""))]
    required = [_readable(e.get("slot")) for e in config.checklist_for(product) or [] if e.get("mandatory", True)]
    return "\n".join(lines), f"{_readable(product)}: " + ", ".join(required) + "."


def _coapp_table(lang: str) -> tuple[str, str]:
    from app.agents.applicant import config, workflow

    texts = cfg().get("texts") or {}
    cols = _pick(texts.get("products_columns"), lang) or ["Document", "Required", "Accepted as"]
    rows = [f"| {_readable(e['slot'])} | {_say('required', lang)} | " + ", ".join(_readable(a) for a in e.get("accepts") or []) + " |"
            for e in config.co_applicant_documents() or []]
    note = [] if workflow.coapp_mandatory_enabled() else ["", _say("coapp_optional", lang)]
    lines = [_say("coapp_heading", lang), "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols), *rows, *note,
             "", _say("source", lang, source=_pick((cfg().get("texts") or {}).get("coapp_source"), lang) or "")]
    return "\n".join(lines), ", ".join(_readable(e["slot"]) for e in config.co_applicant_documents() or []) + "."


def _log_gap(term: str, kind: str = "terms") -> None:
    """The unknown term (or general question) for the owner (text, count, last seen -- never case data)."""
    path = _ROOT.parent / str(cfg().get("gaps_file") or "evals/knowledge_gaps.yaml")
    with _LOCK:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
        except Exception:  # noqa: BLE001 - a broken file is rewritten, never fails the reply
            data = None
        data = data if isinstance(data, dict) else {}
        terms = data.setdefault(kind, {})
        entry = terms.setdefault(term.upper() if kind == "terms" else term[:200], {"count": 0})
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_seen"] = date.today().isoformat()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Terms the chatbot was asked and could not find (general.py). Add them to\n"
                            "# app/config/glossary.yaml or knowledge/ -- then remove them here.\n"
                            + yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
        except OSError:
            pass


async def answer(message: str, claims: dict[str, Any], context: dict[str, Any] | None, request_id: str,
                 lang: str, case_in_scope: str | None = None) -> dict[str, Any] | None:
    """The reply when this message needs no case (see the module doc), else None (the message goes on).

    `case_in_scope`: a case the REQUEST names (case_id). With a case in scope -- named or opened -- help, frustration
    and "my stage" belong to that case (the case logic answers them); definitions, calculators, policy numbers,
    credit-decision declines and knowledge questions about no case data are still answered here."""
    if not enabled() or not str(message or "").strip():
        return None
    from app.security.auth import get_subject

    state = _state(claims, context)
    seed = int(getattr(state, "turn_id", 0) or 0)
    text = str(message)
    case_id = getattr(state, "active_case_id", None) or case_in_scope

    if _has(text, "frustrated") and not case_id:
        options = ["My pending cases", "What is pending?" if case_id else "Show my cases"]
        block = _asks(options) + "\n" + _pick((cfg().get("fallback") or {}).get("supervisor"), lang)
        return _reply(request_id, "FRUSTRATED", _say("frustrated", lang) + "\n" + _summary(claims, lang),
                      block=block, case_id=case_id)
    if (_has(text, "help") or _only(text, "help")) and not case_id:
        return _reply(request_id, "GENERAL_HELP", _say("help", lang) + "\n\n" + _summary(claims, lang),
                      block=_asks(_suggestions(state, lang, 3)), case_id=case_id,
                      tts=_say("help", lang) + " " + _summary(claims, lang))
    if _only(text, "ack") and not _pending(state, claims, context):
        return _reply(request_id, "ACK", _say("ack", lang, seed) + " " + _say("ack_next", lang),
                      block=_asks(_suggestions(state, lang, 2)), case_id=case_id)
    if _has(text, "role"):
        role = str(claims.get("role") or " ".join(claims.get("roles") or []) or "--")
        return _reply(request_id, "LOGIN_ROLE", _say("role", lang, user=get_subject(claims), role=role),
                      case_id=case_id)
    if _has(text, "login_stage") and not case_id:
        stage = str(claims.get("stage") or "").upper()
        lines = [_say("login_stage", lang, stage=stage) if stage else _say("no_login_stage", lang)]
        if case_id:
            from app.agents.applicant.copilot.capabilities import workspace

            lines.append(_say("case_stage", lang, case_id=case_id, stage=workspace._stage(case_id) or "--"))
        return _reply(request_id, "LOGIN_STAGE", " ".join(lines), case_id=case_id)
    if _only(text, "name"):
        options = _pick(_say_list("name_options"), lang)
        return _reply(request_id, "NAME_CLARIFY", _say("name_question", lang), block=_asks(options),
                      case_id=case_id, query_type="CLARIFICATION")
    login_option = (_pick((cfg().get("texts") or {}).get("name_options"), lang) or [None] * 3)[-1]
    if login_option and _norm(text).strip() == _norm(login_option).strip():
        return _reply(request_id, "LOGIN_NAME", _say("login_name", lang, user=get_subject(claims)), case_id=case_id)
    for topic, spec in (cfg().get("processes") or {}).items():
        if any(_norm(p) in _norm(text) for p in spec.get("phrases") or []):
            md, spoken = _process(topic, state, lang)
            return _reply(request_id, "PROCESS_ANSWER", md, case_id=case_id, tts=spoken,
                          query_type="PROCESS_KNOWLEDGE")
    if _has(text, "product_documents") and _has(text, "co_applicant") and not _product_of(text):
        md, spoken = _coapp_table(lang)
        return _reply(request_id, "PRODUCT_CHECKLIST", md, case_id=case_id, tts=spoken, query_type="PROCESS_KNOWLEDGE")
    # "which documents are accepted as ADDRESS PROOF" asks about one document: the knowledge path answers it
    if _has(text, "product_documents") and not _has(text, "one_document"):
        product = _product_of(text)
        if product is None and not case_id:
            from app.agents.applicant import config

            choices = [f"mandatory documents for {_readable(p).lower()}" for p in config.products() or []
                       if str(p).lower() != "default"]
            return _reply(request_id, "PRODUCT_CLARIFY", _say("which_product", lang), block=_asks(choices),
                          query_type="CLARIFICATION")
        if product is not None:
            md, spoken = _products_table(product, lang)
            return _reply(request_id, "PRODUCT_CHECKLIST", md, case_id=case_id, tts=spoken,
                          query_type="PROCESS_KNOWLEDGE")
    if _decision(text):
        spec = cfg().get("decisions") or {}
        return _reply(request_id, "CREDIT_DECISION_DECLINED", _pick(spec.get("text"), lang), case_id=case_id,
                      block=_asks(_pick(spec.get("suggestions"), lang) or []), query_type="PROCESS_KNOWLEDGE")
    calculated = _calculate(text, lang)
    if calculated is not None:
        return _reply(request_id, "CALCULATOR", calculated, case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    policy_answer = _policy(text, state, lang)
    if policy_answer is not None:
        return _reply(request_id, "POLICY_ANSWER", policy_answer, case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    defined = await _definition(text, request_id, lang, case_id)
    if defined is not None:
        return defined
    if _general_question(text) and not (case_id and _names_case_data(text)):
        # with a case OPEN, "is PAN mandatory?" / "documents required?" is about THAT case: the case logic answers
        return await _knowledge(text, request_id, lang, case_id)
    return None


def _names_case_data(text: str) -> bool:
    """The message names something a case holds (product_flow.yaml which_case.case_words: documents, KYC, PAN ...)."""
    from app.agents.applicant.copilot.capabilities import product_flow

    words = {_plain(w) for w in (product_flow.cfg().get("which_case") or {}).get("case_words") or []}
    return bool(words & set(_plain(text).split()))


# --------------------------------------------------------------------------
# general questions with no case: credit decisions, calculators, policy numbers, the knowledge base
# --------------------------------------------------------------------------

def _plain(text: str) -> str:
    return _norm(text).strip()


def _decision(text: str) -> bool:
    said = _norm(text)
    return any(_norm(p) in said for p in (cfg().get("decisions") or {}).get("phrases") or [])


def _general_question(text: str) -> bool:
    """Shaped like general knowledge and pointing at no case (conversation_general.yaml general_question)."""
    spec = cfg().get("general_question") or {}
    said = _plain(text)
    if not said or any(re.search(p, said) for p in spec.get("case_only") or []):
        return False
    # the English question words are dropped first: "what IS LOAN against property" is not Hindi "is loan" (this loan)
    rest = " " + re.sub(r"^(what|whats|what s|how|why|where|which) (is|are|s)\b", "", said).strip() + " "
    # a referent is matched as a word PREFIX: "my case" also covers "my cases"
    if any(" " + _plain(r) in rest for r in spec.get("case_referents") or []):
        return False
    from app.agents.applicant.copilot.capabilities import workspace

    if workspace._only_asks_for_the_list(text):
        return False                                    # "what are my cases": the case list answers
    if any(pattern.search(text) for pattern in workspace._ID.values()):
        return False                                    # a case / applicant id named: the case logic answers
    return any(re.search(p, said) for p in spec.get("shapes") or [])


async def _knowledge(text: str, request_id: str, lang: str, case_id: str | None) -> dict[str, Any] | None:
    """The knowledge pipeline: a configured FAQ answer, then configured facts + the handbook (a model only
    phrases, checked)."""
    from app.agents.applicant.copilot.capabilities import faq

    configured = faq.answer_for(text, lang) if faq.enabled() else None
    if configured:
        return _reply(request_id, "GENERAL_KNOWLEDGE", configured, case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    allow = bool((cfg().get("general_question") or {}).get("allow_model", True))
    # as typed, then the English shape (when a rewrite applies). A CONFIGURED FACT from either wins; otherwise the
    # first accepted handbook answer ("address proof mein kya chalega" -> the configured accepted list)
    found: list[tuple[bool, dict[str, Any]]] = []
    for query in dict.fromkeys([text, _english_shape(text)]):
        answer = await _knowledge_candidate(query, request_id, case_id, allow)
        if answer is not None:
            found.append(answer)
            if answer[0]:
                break
    if found:
        return next((reply for authoritative, reply in found if authoritative), found[0][1])
    if case_id:
        return None                 # a case is in scope: nothing general found -- the case logic answers it
    _log_gap(_plain(text), kind="questions")
    return _reply(request_id, "UNKNOWN_TERM", _pick(cfg().get("general_unknown"), lang), case_id=case_id)


async def _knowledge_candidate(query: str, request_id: str, case_id: str | None,
                               allow: bool) -> tuple[bool, dict[str, Any]] | None:
    """(authoritative, reply) for one wording of the question, or None when nothing relevant was found."""
    try:
        from app.agents.applicant.copilot.agent import _knowledge_reply

        reply_text, _source, detail = await _knowledge_reply(query, allow_model=allow)
        tied = None if detail.get("authoritative") else _tie_break(query)     # a configured fact always wins
        if tied is not None:
            # two sections scored alike: the one whose HEADING is the question wins, as written in the handbook
            # ("what happens after CPA" -> "What happens after CPA?", not "CPA readiness")
            return False, _reply(request_id, "GENERAL_KNOWLEDGE", tied.chunk.text.strip(), case_id=case_id,
                                 query_type="PROCESS_KNOWLEDGE", citations=[tied.chunk.citation])
    except Exception:  # noqa: BLE001 - knowledge unavailable: said as not known, never guessed
        return None
    heading = _heading_of((detail.get("citations") or [""])[0])
    if detail.get("confident") and str(reply_text or "").strip() and _about(query, f"{reply_text} {heading}"):
        return bool(detail.get("authoritative")), _reply(
            request_id, "GENERAL_KNOWLEDGE", reply_text, case_id=case_id, query_type="PROCESS_KNOWLEDGE",
            citations=detail.get("citations") or [])
    return None


def _heading_of(citation: str) -> str:
    return str(citation).split("#", 1)[1] if "#" in str(citation) else ""


def _tie_break(question: str):
    """The hit to answer from when the top hits TIE and another one's heading matches the question clearly better
    than the first one's; else None (the ordinary knowledge answer stands)."""
    from app.agents.applicant import knowledge_answer

    result = knowledge_answer.retrieve(question)
    if result is None or not result.confident or len(result.hits) < 2:
        return None
    top = result.hits[0].score
    tied = [h for h in result.hits if top - h.score <= 0.02]
    if len(tied) < 2:
        return None
    said = _plain(question)
    score = lambda h: difflib.SequenceMatcher(None, said, _plain(h.chunk.heading or "")).ratio()  # noqa: E731
    best = max(tied[1:], key=score)
    return best if score(best) - score(tied[0]) > 0.15 else None


def _english_shape(text: str) -> str:
    """A Hinglish question in the handbook's English shape, for the knowledge search only (config rewrites)."""
    said = _plain(text)
    for pattern, replacement in (cfg().get("general_question") or {}).get("retrieval_rewrites") or []:
        said = re.sub(pattern, replacement, said)
    return said


def _flat(text: str) -> str:
    return " " + re.sub(r"[-_\s]+", " ", _plain(text)) + " "


def _subject_words(text: str) -> list[str]:
    """What a general question is ABOUT: its words minus the question words and the generic ones (config)."""
    filler = {_plain(w) for w in (cfg().get("general_question") or {}).get("filler") or []}
    return list(dict.fromkeys(w for w in _flat(text).split() if w not in filler and len(w) > 2))


def _about(question: str, answer: str) -> bool:
    """A retrieved answer is used only when it is ABOUT what was asked: most of the subject words appear in it.
    A confident-but-unrelated passage ("top up loan" -> a paragraph on filenames) is never shown as the answer."""
    words = _subject_words(question)
    if not words:
        return True
    said = _flat(answer)
    hits = sum(1 for w in words if f" {w}" in said or f" {w.rstrip('s')}" in said)
    return hits >= max(1, -(-len(words) * 3 // 5))           # at least 60 %, rounded up


_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)?\s*(\d+(?:[.,]\d+)*)\s*(crore|cr|lakh|lakhs|lac|lacs|l|k|thousand|hazar|hazaar)?\b",
                     re.I)
_RATE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|(?:percent|pct|pc)\b)", re.I)
_YEARS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:years?|yrs?|saal)\b", re.I)
_MONTHS = re.compile(r"(\d+)\s*(?:months?|mahine|mahina|mths?|m)\b", re.I)
_SCALE = {"crore": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5, "k": 1e3,
          "thousand": 1e3, "hazar": 1e3, "hazaar": 1e3}


def _amounts(text: str) -> list[tuple[int, float]]:
    """(position, rupees) of every amount typed -- rates and tenures excluded."""
    taken = [m.span() for p in (_RATE, _YEARS, _MONTHS) for m in p.finditer(text)]
    out = []
    for m in _AMOUNT.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        value = float(m.group(1).replace(",", ""))
        out.append((m.start(1), value * _SCALE.get((m.group(2) or "").lower(), 1)))
    return out


def _nearest(amounts: list[tuple[int, float]], text: str, words: list[str]) -> float | None:
    spots = [m.end() for w in words for m in re.finditer(rf"\b{re.escape(w)}\b", text, re.I)]
    if not spots or not amounts:
        return None
    return min(amounts, key=lambda a: min(abs(a[0] - s) for s in spots))[1]


def _inr(value: float) -> str:
    """Rs. in the Indian grouping (12,34,567), no paise for whole rupees."""
    whole, paise = divmod(round(value * 100), 100)
    s = str(int(whole))
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail, head = head[-2:] + "," + tail, head[:-2]
    grouped = (head + "," + tail) if head else tail
    return "₹" + grouped + (f".{paise:02d}" if paise else "")


def _calculate(text: str, lang: str) -> str | None:
    spec = cfg().get("calculators") or {}
    texts, said = spec.get("texts") or {}, _norm(text)
    has = lambda key: any(_norm(w) in said for w in spec.get(key) or [])  # noqa: E731
    amounts = _amounts(text)
    note = _say_text(texts.get("note"), lang)
    if has("ltv_words") and amounts:
        loan = _nearest(amounts, text, ["loan"])
        value = _nearest(amounts, text, ["property", "value", "ghar", "house", "makaan"])
        if (loan is None or value is None or loan == value) and len(amounts) >= 2:
            loan, value = sorted(a[1] for a in amounts)[:2]
        if not loan or not value or loan == value:
            return _missing("LTV", "the loan amount and the property value", spec, "ltv", lang)
        lines = [_say_text(texts.get("ltv"), lang, loan=_inr(loan), property_value=_inr(value), ltv=f"{loan / value * 100:.1f}")]
        limit = _policy_values("ltv")
        if limit:
            lines.append(_say_text(texts.get("ltv_limit"), lang, **limit))
        return "\n\n".join(lines + [note])
    if has("foir_words") and amounts:
        income = _nearest(amounts, text, ["income", "salary", "kamai", "earning", "earnings"])
        obligations = _nearest(amounts, text, ["emi", "emis", "obligation", "obligations"])
        if (income is None or obligations is None or income == obligations) and len(amounts) >= 2:
            obligations, income = sorted(a[1] for a in amounts)[:2]
        if not income or obligations is None or income == obligations:
            return _missing("FOIR", "the monthly income and the monthly EMIs", spec, "foir", lang)
        lines = [_say_text(texts.get("foir"), lang, obligations=_inr(obligations), income=_inr(income),
                           foir=f"{obligations / income * 100:.1f}")]
        limit = _policy_values("foir")
        if limit:
            lines.append(_say_text(texts.get("foir_limit"), lang, **limit))
        return "\n\n".join(lines + [note])
    if has("emi_words") and (amounts or _RATE.search(text)):
        rate = _RATE.search(text)
        years, months = _YEARS.search(text), _MONTHS.search(text)
        n = int(months.group(1)) if months else (round(float(years.group(1)) * 12) if years else 0)
        principal = max((a[1] for a in amounts), default=0)
        if not rate or not n or not principal:
            return _missing("the EMI", "the loan amount, the yearly rate and the tenure", spec, "emi", lang)
        r = float(rate.group(1)) / 1200
        emi = principal / n if r == 0 else principal * r * (1 + r) ** n / ((1 + r) ** n - 1)
        return "\n\n".join([
            _say_text(texts.get("emi"), lang, principal=_inr(principal), rate=rate.group(1), months=n,
                      emi=_inr(round(emi)), interest=_inr(round(emi * n - principal)), total=_inr(round(emi * n))),
            _say_text(texts.get("emi_formula"), lang), note])
    return None


def _missing(what: str, needs: str, spec: dict[str, Any], key: str, lang: str) -> str:
    return _say_text((spec.get("texts") or {}).get("missing"), lang, what=what, needs=needs,
                     example=(spec.get("examples") or {}).get(key, ""))


def _say_text(template: Any, lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say(template, lang, 0, **values)


def _policies(product: str | None = None) -> list[Any]:
    """The live eligibility policies (one product, else every configured one); [] when unavailable."""
    from app.agents.applicant import config
    from app.agents.eligibility import policy as eligibility_policy

    names = [product] if product else [p for p in config.products() or [] if str(p).lower() != "default"]
    out = []
    for name in names:
        try:
            out.append(eligibility_policy.get_policy(name))
        except Exception:  # noqa: BLE001 - PolicyUnavailable (production refuses a demo policy) or no policy
            continue
    return out


def _policy_values(kind: str) -> dict[str, Any] | None:
    """The configured limit a calculator compares with (the first product that sets one)."""
    for p in _policies():
        if kind == "foir" and p.foir_maximum_percent is not None:
            return {"limit": f"{p.foir_maximum_percent:g}", "policy_id": p.policy_id}
        if kind == "ltv" and p.ltv_enabled and p.ltv_maximum_percent is not None:
            return {"limit": f"{p.ltv_maximum_percent:g}", "policy_id": p.policy_id, "product": _readable(p.product)}
    return None


def _policy(text: str, state, lang: str) -> str | None:
    """A policy number asked ("maximum tenure for home loan"): read from the eligibility policy now, never typed."""
    spec = cfg().get("policy") or {}
    said = _norm(text)
    subject = next((s for s, words in (spec.get("subjects") or {}).items()
                    if any(_norm(w) in said for w in words)), None)
    if subject is None:
        return None
    texts = spec.get("texts") or {}
    source = _say("source", lang, source=spec.get("source", ""))
    if subject in {"cibil", "turnaround", "fees"}:
        return _say_text(texts.get(subject), lang)
    if subject == "signature":
        from app.agents.applicant import config
        from app.agents.signature import presence

        if not presence.enabled():
            return _say_text(texts.get("signature_off"), lang)
        activation, problem = config.signature_activation()
        if activation is None:
            return _say_text(texts.get("signature_on_undated"), lang, problem=problem)
        return _say_text(texts.get("signature_on"), lang, date=activation.date().isoformat())
    product = _product_of(text)
    if product is None and getattr(state, "active_case_id", None):
        from app.store import get_repository

        product = getattr(get_repository().get_application(state.active_case_id), "product", None)
    policies = _policies(product)
    if not policies:
        return _say_text(texts.get("unavailable"), lang)
    lines = []
    for p in policies:
        name = _readable(p.product)
        if subject == "loan_amount" and p.loan_maximum_amount is not None:
            lines.append(_say_text(texts.get("loan_amount"), lang, product=name,
                                   minimum=_inr(p.loan_minimum_amount or 0), maximum=_inr(p.loan_maximum_amount)))
        elif subject == "tenure" and p.tenure_maximum_months is not None:
            lines.append(_say_text(texts.get("tenure"), lang, product=name, minimum=p.tenure_minimum_months or 0,
                                   maximum=p.tenure_maximum_months))
        elif subject == "interest" and p.annual_rate_percent is not None:
            lines.append(_say_text(texts.get("interest"), lang, product=name, rate=f"{p.annual_rate_percent:g}"))
        elif subject == "age" and p.age_maximum_years is not None:
            lines.append(_say_text(texts.get("age"), lang, product=name, minimum=p.age_minimum_years or 0,
                                   maximum=p.age_maximum_years))
        elif subject == "ltv":
            lines.append(_say_text(texts.get("ltv"), lang, product=name, maximum=f"{p.ltv_maximum_percent:g}")
                         if p.ltv_enabled and p.ltv_maximum_percent is not None
                         else _say_text(texts.get("ltv_off"), lang, product=name))
        elif subject == "foir" and p.foir_maximum_percent is not None:
            lines.append(_say_text(texts.get("foir"), lang, product=name, maximum=f"{p.foir_maximum_percent:g}"))
        elif subject == "income" and p.minimum_monthly_income is not None:
            lines.append(_say_text(texts.get("income"), lang, product=name, minimum=_inr(p.minimum_monthly_income),
                                   basis=_readable(p.income_basis).lower()))
        elif subject == "employment" and p.employment_allowed:
            lines.append(_say_text(texts.get("employment"), lang, product=name,
                                   allowed=", ".join(_readable(e) for e in p.employment_allowed)))
        else:
            lines.append(_say_text(texts.get("not_set"), lang, product=name))
    demo = [p.policy_id for p in policies if not p.is_production]
    tail = [_say_text(texts.get("demo"), lang, policy_ids=", ".join(demo))] if demo else []
    return "\n".join(f"- {line}" for line in lines) + "\n\n" + "\n".join(tail + [source])


def _say_list(key: str) -> Any:
    return (cfg().get("texts") or {}).get(key)


async def _definition(text: str, request_id: str, lang: str, case_id: str | None) -> dict[str, Any] | None:
    """A definition / full form: the glossary, then the knowledge base; an unknown ACRONYM is logged."""
    term = None
    for pattern in (cfg().get("phrases") or {}).get("definition") or []:
        found = re.match(pattern, text.strip(), re.I)
        if found:
            term = found.group("term")
            break
    if not term:
        return None
    from app.agents.applicant.copilot.answering import professional, style

    known = style.defined_term(text) or style.defined_term(f"{term} kya hai")
    if known:
        said = professional.strip_emojis(style.definition(known, lang) or "").strip()
        source = _say("source", lang, source=_pick((cfg().get("texts") or {}).get("glossary_source"), lang) or "")
        return _reply(request_id, "DEFINITION", f"{said}\n\n{source}", case_id=case_id,
                      tts=professional.plain(said), query_type="PROCESS_KNOWLEDGE")
    # an ACRONYM: typed in capitals, or a short token with no vowel ("fnr", "ftnr") -- never an ordinary word
    acronym = term.isupper() or (2 <= len(term) <= 5 and not re.search(r"[aeiou]", term.lower()))
    if not acronym:
        return None                     # "what is pending" is a case question: the ordinary pipeline answers
    try:
        from app.agents.applicant.copilot.agent import _knowledge_reply

        reply_text, source, detail = await _knowledge_reply(text, allow_model=False)
    except Exception:  # noqa: BLE001 - knowledge unavailable: said as unknown, never guessed
        reply_text, detail = "", {"confident": False}
    if detail.get("confident") and reply_text and _about(text, reply_text):
        return _reply(request_id, "DEFINITION", reply_text, case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    if acronym:
        _log_gap(term)
        return _reply(request_id, "UNKNOWN_TERM", _say("unknown_term", lang, term=term.upper()), case_id=case_id)
    return None


# --------------------------------------------------------------------------
# after every reply: "Do you mean X or Y?" and the gradual fallback
# --------------------------------------------------------------------------

def after(published: Any, message: str, claims: dict[str, Any], context: dict[str, Any] | None, lang: str) -> Any:
    from app.agents.applicant.copilot.capabilities import workspace

    if not enabled() or not isinstance(published, dict) or published.get("atomic") or not message:
        return published
    fb = cfg().get("fallback") or {}
    missed = str(published.get("intent") or "").upper() in {str(i).upper() for i in fb.get("miss_intents") or []}
    # "Do you mean: <case tool> or <case tool>?" only for a CASE question the router half-understood -- a general
    # question the knowledge base lacks gets the plain "not in the knowledge base yet" (never case-tool guesses)
    knowledge_miss = str(published.get("intent") or "").upper() == "UNKNOWN_TERM" or _general_question(message)
    if missed and not knowledge_miss:
        clarified = _did_you_mean(message, lang)
        if clarified:
            published["answer"], published["faq_block"] = clarified
            published["intent"], published["no_case_links"] = "DID_YOU_MEAN", True
            published.pop("answer_markdown", None)
    state = _state(claims, context)
    flow = dict(getattr(state, "flow", None) or {})
    misses = int(flow.get("misses", 0)) + 1 if missed else 0
    flow["misses"] = misses
    state.flow = flow
    workspace._save(state)
    if missed and misses >= int(fb.get("misses_before_help", 2)):
        closest = _closest_faq(message, getattr(state, "active_case_id", None), lang)
        block = "\n".join(x for x in [_pick(fb.get("heading"), lang), _asks(closest),
                                      _pick(fb.get("supervisor"), lang)] if x)
        published["faq_block"] = (str(published.get("faq_block") or "") + "\n\n" + block).strip()
        published["no_case_links"] = True
    return published


def _did_you_mean(message: str, lang: str) -> tuple[str, str] | None:
    """When the router's best two tools are close: ("Do you mean:", the two questions as links)."""
    try:
        from app.agents.applicant.copilot.semantics import embedding_router

        if not embedding_router.enabled():
            return None
        match = embedding_router.bank().match(message)
    except Exception:  # noqa: BLE001 - no router: no guess
        return None
    spec = cfg().get("clarify") or {}
    if match is None or not match.runner_up or match.score < float(spec.get("min_score", 0.35)) \
            or match.score - match.runner_up_score > float(spec.get("max_gap", 0.05)):
        return None
    questions = spec.get("tool_questions") or {}
    first, second = questions.get(match.tool), questions.get(match.runner_up)
    if not first or not second:
        return None
    return _pick(spec.get("question"), lang), _asks([_pick(first, lang), _pick(second, lang)])


def _closest_faq(message: str, case_id: str | None, lang: str, n: int = 3) -> list[str]:
    from app.agents.applicant.copilot.capabilities import faq

    pool = [i["send"] for i in faq.items(case_id, lang)]
    for item in faq.cfg().get("answers") or []:
        pool += [str(m) for m in (item.get("match") or [])[:1]]
    said = _norm(message).strip()
    ranked = sorted(dict.fromkeys(pool), key=lambda q: -difflib.SequenceMatcher(None, said, _norm(q).strip()).ratio())
    return ranked[:n]


__all__ = ["after", "answer", "cfg", "enabled"]
