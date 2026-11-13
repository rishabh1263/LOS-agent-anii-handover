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

from app.llm import turn_budget as _tb

import difflib
import os
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any

import yaml

import logging

logger_general = logging.getLogger(__name__)
GENERAL_LLM_STATS: dict[str, int] = {"called": 0, "used": 0, "rejected": 0}

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
           "category": extra.pop("category", "KNOWLEDGE_ONLY"), "query_type": "CONVERSATION", "response_source": "GENERAL",
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


def _more_detail(message: str, state, case_in_scope: str | None = None, claims: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None) -> str | None:
    """
    "tell me more" / "aur batao" with a case open -> the canonical question of the more detailed topic for what was just
    answered (config more_detail.after[last meaning intent], else more_detail.default); None otherwise. When the
    ENGINE answered the last turn it elaborates its own answer (conversation state) -- this applies only after another
    layer answered (moved_on cleared the engine's last answer) or when there is no engine answer at all.
    """
    spec = cfg().get("more_detail") or {}
    if not (getattr(state, "active_case_id", None) or case_in_scope):
        return None
    said = _plain(message)
    if not any(said == _plain(p) for p in spec.get("phrases") or []):
        return None
    conversation_id = (context or {}).get("conversation_id") if isinstance(context, dict) else None
    if conversation_id and claims is not None:
        from app.agents.applicant.copilot.capabilities import workspace
        from app.agents.applicant.copilot.conversation import state as conv

        engine = conv.STORE.get(workspace._subject(claims), conversation_id)
        if engine is not None and getattr(engine, "last_answer_reference", None):
            return None                               # the engine's own follow-up reading answers it
    from app.agents.applicant.copilot.semantics import meaning

    last = ((getattr(state, "flow", None) or {}).get("last_meaning") or {}).get("intent")
    target = (spec.get("after") or {}).get(last) or spec.get("default")
    canonical = ((meaning.catalogue().get("intents") or {}).get(target) or {}).get("canonical")
    return str(canonical) if canonical and "{" not in str(canonical) else None


def moved_on(claims: dict[str, Any], context: dict[str, Any] | None) -> None:
    """
    THE OFFICER MOVED ON (owner directive 2026-10-12): a turn answered OUTSIDE the engine (this layer, case insights)
    after the engine asked "which one do you mean?" leaves that question stale -- "ok" two turns later answered the old
    co-applicant question. The chat's pending clarification is dropped; nothing else in the conversation changes.
    """
    conversation_id = (context or {}).get("conversation_id") if isinstance(context, dict) else None
    if not conversation_id:
        return
    from app.agents.applicant.copilot.capabilities import workspace
    from app.agents.applicant.copilot.conversation import state as conv

    try:
        found = conv.STORE.get(workspace._subject(claims), conversation_id)
        if found is not None and (getattr(found, "pending_clarification", None) is not None
                                  or getattr(found, "last_answer_reference", None) is not None):
            # the engine's own last answer is no longer the last thing said: "tell me more" is not about it
            found.pending_clarification = None
            found.last_answer_reference = None
            conv.STORE.put(found)
    except Exception:  # noqa: BLE001 - never fails the answer already given
        logger_general.warning("general: could not drop a stale clarification", exc_info=True)


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
    aliases = (cfg().get("policy") or {}).get("product_aliases") or {}
    for product in config.products() or []:
        name = str(product)
        if name.lower() == "default":
            continue
        if _norm(name.replace("_", " ")) in said or _norm(name) in said:
            return name
        if any(_norm(a) in said for a in aliases.get(name) or []):
            return name                          # "max tenure pl" / "interest rate for hl" (config policy.product_aliases)
    return None


def _unconfigured_product(text: str) -> bool:
    """A product this lender has no configuration for ("LAP", "car loan"): its numbers are never another product's."""
    said = _norm(text)
    return any(_norm(p) in said for p in (cfg().get("policy") or {}).get("unconfigured_products") or [])


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
            header = "".join(f"# {line}\n" for line in cfg().get("gaps_file_header") or [])
            path.write_text(header + yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
        except OSError:
            pass


def _downstream_route(message: str, case_in_scope: str | None) -> str | None:
    """
    The downstream agent this message belongs to (CREDIT_AGENT, DECISION_AGENT, KYC_AGENT, RISK_AGENT, RCU_AGENT ...),
    or None. THE SAME DECISION THE ROUTER MAKES: the deterministic classifier (semantics/intents.understand) and the
    `routing:` table of applicant_agent.yaml -- no word list of its own. A route to NONE (investment / product advice)
    stays with this layer's polite out-of-scope line. 2026-10-22: this layer answered "should we approve this loan?"
    as CLARIFICATION and "credit score?" as NOT_IN_SCOPE, BEFORE the router could route them (OUT_OF_SCOPE + route_to).
    """
    try:
        from app.agents.applicant import config as _agent_config
        from app.agents.applicant.copilot.semantics import intents as _intents

        found = _intents.understand(str(message or ""), has_case=bool(case_in_scope))
        if found.intent is not _intents.Intent.OUT_OF_SCOPE:
            return None
        # a CONCEPT / PROCESS question that names no case ("what is a sanction letter", "who approves the loan",
        # "foreclosure charges kitne hai") is general knowledge -- the handbook / policy answers it here. Only a
        # question about A case ("should we approve THIS loan?", "the applicant's credit score") goes downstream.
        # 2026-10-23: the topic word alone routed 9 of the 12 wrong general answers away from the handbook.
        if _general_question(message):
            return None
        target = str((_agent_config.routing_table().get(found.route_to or "") or {}).get("route_to") or "")
        return target if target and target.upper() != "NONE" else None
    except Exception:  # noqa: BLE001 - a classifier failure leaves this layer exactly as it was
        return None


async def answer(message: str, claims: dict[str, Any], context: dict[str, Any] | None, request_id: str,
                 lang: str, case_in_scope: str | None = None, meaning_kind: str | None = None,
                 knowledge_hint: str | None = None) -> dict[str, Any] | None:
    """
    FAST LANE FIRST (the rules below, ~20 ms). Only a message they did not understand with confidence -- nothing
    matched and it is not a clear case question, or "not in the knowledge base" -- takes the SLOW PATH: the LLM
    rewrites it into one standard English question (semantics/question_rewrite.py, validated: nothing new) and the
    same rules route THAT. The model never answers. Timeout / model down / low memory -> the fast lane's reply.
    """
    if not enabled() or not str(message or "").strip():
        return None
    if meaning_kind == "case":
        return None                     # understood as a question about the case: the case engine answers it
    if _downstream_route(message, case_in_scope):
        if not (case_in_scope or getattr(_state(claims, context), "active_case_id", None)) and _decision(message):
            # asking the bot to DECIDE, no case open ("will this loan get approved", "should I reject this customer"):
            # the decline is the same for every case -- said now, never "which case is this about?" first
            spec = cfg().get("decisions") or {}
            return _reply(request_id, "CREDIT_DECISION_DECLINED", _pick(spec.get("text"), lang),
                          block=_asks(_pick(spec.get("suggestions"), lang) or []), query_type="PROCESS_KNOWLEDGE")
        return None                     # "should we approve this loan?", "credit score?": the downstream router answers
    if (case_in_scope or getattr(_state(claims, context), "active_case_id", None)) and _one_recorded_field(message):
        return None                     # "what is the tenure" on an open case: ITS tenure (the profile), not the glossary
    if _stage_process_question(message) and not _handbook_heading_answers(message, claims, context, case_in_scope):
        return None                     # "What does CPA verify?": the stage guide answers it (STAGE_PROCESS)
    parts = _parts(message)
    if len(parts) > 1:
        # "what is FOIR and what is LTV": every part answered by the same pipeline, joined; a part that needs a case
        # sends the whole message on (the case logic reads it as before)
        replies = [await answer(p, claims, context, request_id, lang, case_in_scope) for p in parts]
        if all(r is not None for r in replies):
            joined = dict(replies[0])
            joined["answer"] = "\n\n".join(f"**{p[:1].upper() + p[1:]}**\n{r.get('answer') or ''}"
                                           for p, r in zip(parts, replies))
            joined["intent"], joined["parts"] = "MULTI_PART", [r.get("intent") for r in replies]
            joined.pop("tts_text", None)
            return joined
    reply = await _fast(message, claims, context, request_id, lang, case_in_scope, meaning_kind, knowledge_hint)
    state = _state(claims, context)
    if not _needs_slow_path(message, reply, state, claims, context, case_in_scope):
        _remember(state, message, reply)
        return reply
    from app.agents.applicant.copilot.semantics import question_rewrite

    previous = str((getattr(state, "flow", None) or {}).get("last_general_q") or "") or None
    rewritten, trace = await question_rewrite.rewrite(message, previous=previous, request_id=request_id, lang=lang)
    if rewritten and _downstream_route(rewritten, case_in_scope):
        return None                     # the rewrite names a downstream question: the router answers it, not this layer
    if rewritten:
        again = await _fast(rewritten, claims, context, request_id, lang, case_in_scope)
        understood = again is not None and str(again.get("intent")) != "UNKNOWN_TERM"
        question_rewrite.log(message, rewritten, str(again.get("intent")) if again else "PASSED_ON", trace)
        if understood:
            again["understood_as"] = rewritten
            again["answer"] = _say("understood_as", lang, question=rewritten) + "\n\n" + str(again.get("answer") or "")
            _remember(state, rewritten, again)
            return again
    elif trace.get("status") not in ("DISABLED", "SKIPPED"):
        question_rewrite.log(message, None, str(trace.get("status")), trace)
    _remember(state, message, reply)
    return reply


_SPLIT = re.compile(r"\s+(?:and|aur|also|plus|as well as|&)\s+", re.I)
_LEAD = re.compile(r"^(what is|what are|what's|whats|define|explain|how to|how do i|how does|meaning of)\s+", re.I)


def _parts(message: str) -> list[str]:
    """A two-question message split in two ("X and Y"); each part at least 2 words once the first part's lead
    ("what is") is lent to a bare second part. Anything else: [message] (one question)."""
    text = re.sub(r"[?]+", " ", str(message or "")).strip()
    pieces = [p.strip() for p in _SPLIT.split(text) if p.strip()]
    if len(pieces) != 2 or re.search(r"\b(between|difference|compare|vs|versus|both|fark|farak)\b", pieces[0], re.I):
        return [message]                # "difference between net AND gross" is one question
    lead = _LEAD.match(pieces[0])
    if lead and not _LEAD.match(pieces[1]) and not _general_question(pieces[1]):
        pieces[1] = lead.group(0) + pieces[1]
    if any(len(p.split()) < 2 for p in pieces) or not all(_general_question(p) for p in pieces):
        return [message]
    return pieces


def _needs_slow_path(message: str, reply: dict[str, Any] | None, state, claims: dict[str, Any],
                     context: dict[str, Any] | None, case_in_scope: str | None) -> bool:
    """Not understood with confidence: "not in the knowledge base", or nothing matched and not a clear case question."""
    if reply is not None:
        return str(reply.get("intent")) == "UNKNOWN_TERM"
    from app.agents.applicant.copilot.semantics import meaning

    if meaning.social_reading(message):
        return False                    # "thx a lot": small talk -- never re-worded by the model
    if case_in_scope or getattr(state, "active_case_id", None) or _pending(state, claims, context):
        return False                    # a case is in scope / a question is pending: the case logic reads it
    said = _plain(message)
    if len(said.split()) < 2 or re.fullmatch(r"[\d\s.,]+", said):
        return False                    # "1", "ok", a pick
    spec = cfg().get("general_question") or {}
    if any(re.search(p, said) for p in spec.get("case_only") or []):
        return False
    rest = " " + said + " "
    if any(" " + _plain(r) in rest for r in spec.get("case_referents") or []):
        return False
    from app.agents.applicant.copilot.capabilities import case_list, workspace

    if any(p.search(message) for p in workspace._ID.values()) or workspace._only_asks_for_the_list(message) \
            or case_list.understand(message) is not None:
        return False
    try:
        from app.agents.applicant.copilot.semantics import embedding_router

        if embedding_router.enabled():
            match = embedding_router.bank().match(message)
            if embedding_router.confident(match) and match.tool not in {"knowledge"}:
                return False            # the case tools understood it (the fast lane of the case logic)
    except Exception:  # noqa: BLE001 - no bank: the rewrite decides
        pass
    return True


def _remember(state, message: str, reply: dict[str, Any] | None) -> None:
    """The last general question this chat asked (masked, short): context for a follow-up's rewrite."""
    if reply is None or str(reply.get("intent")) in ("UNKNOWN_TERM", "ACK", "THANKS", "ACKNOWLEDGEMENT", "FRUSTRATED",
                                                       "GENERAL_HELP"):
        return
    from app.agents.applicant.copilot.capabilities import workspace
    from app.security import sensitivity

    flow = dict(getattr(state, "flow", None) or {})
    flow["last_general_q"] = sensitivity.mask_identifiers(str(message))[:160]
    state.flow = flow
    workspace._save(state)


def acceptance(wanted: str, product: str | None, lang: str) -> str | None:
    """One direct line: is document `wanted` accepted for this product (any product when None)? yes (as what) /
    only for a co-applicant (and the applicant's options) / no. None when `wanted` is itself a requirement slot."""
    from app.agents.applicant import config as agent_config
    from app.agents.applicant.copilot.answering.answer import _readable

    wanted = str(wanted or "").upper()
    products = [product] if product else [p for p in agent_config.products() if str(p).lower() != "default"]
    rows = [r for p in products for r in agent_config.checklist_for(p) or []]
    if any(str(r.get("slot") or "").upper() == wanted for r in rows):
        # A REQUIREMENT OF ITS OWN ("is the salary slip done / ok?", 2026-10-13 held-out: "Salary Slip is accepted as
        # Salary Slip"): a status question for the case logic, never an acceptability answer
        return None
    for r in rows:
        if wanted in [str(a).upper() for a in r.get("accepts") or []]:
            return _say("accepted_short", lang, document=_readable(wanted), slot=_readable(r.get("slot")))
    for r in agent_config.co_applicant_documents() or []:
        if wanted in [str(a).upper() for a in r.get("accepts") or []]:
            slot = str(r.get("slot") or "").upper()
            mine = next((x for x in rows if str(x.get("slot") or "").upper() == slot), None)
            options = ", ".join(_readable(a) for a in (mine or {}).get("accepts") or [])
            return _say("coapp_only", lang, document=_readable(wanted), slot=_readable(slot), options=options or "-")
    return _say("not_accepted", lang, document=_readable(wanted))


def _lacked(said: str, names: dict, found: list[str]) -> str | None:
    """The ONE found document the message says the customer LACKS -- the entity frame's negation reading
    (semantics/entities.py: "DL nahi hai", "doesn't have a DL", "without passport"); the older phrase list
    (phrases.acceptable_lacks) only as its fallback. Else None."""
    from app.agents.applicant.copilot.semantics import entities

    lacks = entities.extract(said).lacks
    hit = next((c for c in lacks if c in found), None)
    if hit:
        return hit
    for code in found:
        for alias in names.get(code) or []:
            a = _norm(alias).strip()
            for phrase in (cfg().get("phrases") or {}).get("acceptable_lacks") or []:
                p = _norm(str(phrase).replace("{doc}", a)).strip()
                if "{doc}" in str(phrase) and re.search(rf"\b{re.escape(p)}\b", said):
                    return code
                if "{doc}" not in str(phrase) and re.search(
                        rf"\b{re.escape(p)}\s+(?:a\s+|an\s+|the\s+|any\s+)?{re.escape(a)}\b", said):
                    return code
    return None


_ACCEPT_MEMO: dict[str, float] = {}


def _accepted(text: str, lang: str) -> str | None:
    """"Is a voter ID fine?" -- yes, when the configured checklists accept that document for a slot of its own name
    (applicant_agent.yaml documents.*.accepts). None when the message is not that question, names no known document,
    or the document is no OPTION of any slot (the case logic answers those)."""
    from app.agents.applicant import config as agent_config
    from app.agents.applicant.copilot.answering.answer import _readable

    # a LEADING "ok / so / hmm" acknowledges the last answer -- it is not "is it ok?" (config phrases.ack_lead)
    leads = "|".join(re.escape(str(w)) for w in (cfg().get("phrases") or {}).get("ack_lead") or [])
    core = re.sub(rf"^\s*(?:(?:{leads})\b[\s,.!]*)+", "", str(text), flags=re.I) if leads else str(text)
    said = _norm(core)
    names = (cfg().get("phrases") or {}).get("document_names") or {}
    determiners = "|".join(re.escape(_norm(w).strip()) for w in (cfg().get("phrases") or {}).get(
        "acceptable_not_before") or [])
    aliases = "|".join(re.escape(_norm(a).strip()) for v in names.values() for a in v or [])
    owned = bool(determiners and aliases and re.search(rf"\b({determiners})\s+({aliases})\b", said))
    if not _has(core, "acceptable") or _has(core, "case_document_state") or (
            (owned or _has(core, "acceptable_not_with")) and not _has(core, "acceptable_instead")):
        return None
    found = [code for code, aliases in names.items() if any(_norm(a) in said for a in aliases or [])]
    lacked = _lacked(said, names, found)
    if lacked:
        # "he DOESN'T HAVE a DL, what else works for address proof?": that option ruled out -> the slot's others
        for product in agent_config.products():
            for row in agent_config.checklist_for(product) or []:
                slot = str(row.get("slot") or "").upper()
                accepts = [str(a).upper() for a in row.get("accepts") or []]
                if lacked in accepts and len(accepts) > 1 and (slot in found or len(found) == 1):
                    return _say("accepted_other", lang, document=_readable(lacked), slot=_readable(slot),
                                options=", ".join(_readable(a) for a in accepts if a != lacked))
    if len(found) == 2:
        # "address proof ke liye passport chalega?" / "is a passport ok for address proof?" (2026-10-14 Hinglish
        # dev): a requirement slot AND one of its options -- that pair, answered from the checklist
        for product in agent_config.products():
            for row in agent_config.checklist_for(product) or []:
                slot = str(row.get("slot") or "").upper()
                accepts = [str(a).upper() for a in row.get("accepts") or []]
                other = next((c for c in found if c != slot), None)
                if slot in found and other in accepts and len(accepts) > 1:
                    return _say("accepted", lang, document=_readable(other), slot=_readable(slot),
                                options=", ".join(_readable(a) for a in accepts))
        return None
    if len(found) != 1:
        return None                                                     # none, or two documents: not this question
    wanted = found[0]
    rows: dict[str, list[str]] = {}
    for product in agent_config.products():
        for row in agent_config.checklist_for(product) or []:
            accepts = [str(a).upper() for a in row.get("accepts") or []]
            slot = str(row.get("slot") or "").upper()
            if wanted in accepts and slot != wanted and len(accepts) > 1:
                rows.setdefault(slot, accepts)
    if not rows:
        return acceptance(wanted, None, lang)                           # "electricity bill instead?" -- no / co-app
    if len(rows) != 1:
        return None                                                     # ambiguous: the case logic
    slot, accepts = next(iter(rows.items()))
    return _say("accepted", lang, document=_readable(wanted), slot=_readable(slot),
                options=", ".join(_readable(a) for a in accepts))


def _one_recorded_field(text: str) -> bool:
    """Whether the message asks for ONE recorded application / applicant field (answering/profile.py detect)."""
    from app.agents.applicant.copilot.answering import profile

    try:
        asked = profile.detect(text)
    except Exception:  # noqa: BLE001
        return False
    return asked is not None and str(getattr(asked, "field", "") or "") not in ("", profile.ALL)


def _direct_reply(text: str, case_id: str | None, request_id: str, lang: str) -> dict[str, Any] | None:
    """The general layer's DIRECT answers, needing no model and no case read: confusion and "can you handle it" on an
    open case (one question back about it), "is X accepted?" (the checklist). None for anything else."""
    if case_id and _has(text, "frustrated") and not _has(text, "one_document") \
            and not _has(text, "case_document_state"):
        # CONFUSED ON AN OPEN CASE (2026-10-13 English eval: "I don't understand anything" got "outside what I can
        # help with"): one question back about this case, with its two commonest asks
        return _reply(request_id, "FRUSTRATED", _say("frustrated_case", lang),
                      block=_asks(list(cfg().get("case_asks") or [])), case_id=case_id)
    if case_id and _has(text, "delegate") and not _has(text, "one_document") \
            and not _has(text, "case_document_state"):
        # "can you handle it" names nothing to do (2026-10-13 English eval: a document guess, or "outside what I
        # can help with"): one question back about the open case
        return _reply(request_id, "WHICH_ACTION", _say("delegate_case", lang),
                      block=_asks(list(cfg().get("case_asks") or [])), case_id=case_id)
    accepted = _accepted(text, lang)
    if accepted:
        return _reply(request_id, "DOCUMENT_ACCEPTED", accepted, case_id=case_id, category="KNOWLEDGE")
    from app.agents.applicant.copilot.capabilities import product_flow as _oos_first

    if _oos_first.out_of_scope(text):
        # CLEARLY OFF-TOPIC, BEFORE ANY MODEL STEP (2026-10-24: "what is the capital of france" waited 4 s on the
        # definition / meaning steps): the same line and next option _fast gives, in well under a second
        line, option = _oos_first.out_of_scope_reply(lang, len(text))
        return _reply(request_id, "NOT_IN_SCOPE", line, case_id=case_id, block=option, category="UNSUPPORTED",
                      query_type="CONVERSATION")
    after = _after_stage(text, lang)
    if after:
        return _reply(request_id, "PROCESS_STAGES", after, case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    return None


def _after_stage(text: str, lang: str) -> str | None:
    """"what comes after credit?" / "and after cpa?": the next stages, from config stage_sequence (never a guess)."""
    sequence = [s for s in cfg().get("stage_sequence") or [] if isinstance(s, dict)]
    if not sequence or _has(text, "case_document_state") or _has(text, "after_stage_not"):
        return None
    for pattern in (cfg().get("phrases") or {}).get("after_stage") or []:
        found = re.search(pattern, str(text or ""), re.I)
        if not found:
            continue
        code = found.group("stage").upper()
        at = next((i for i, s in enumerate(sequence) if str(s.get("code")).upper() == code), None)
        if at is None:
            return None
        if at == len(sequence) - 1:
            return _say("after_last_stage", lang, stage=str(sequence[at].get("code")).title())
        _NEXT_STAGE.code = str(sequence[at + 1].get("code"))      # "and after that?" steps on from here
        rest = [str(s.get("say")) for s in sequence[at + 1:]]
        following = rest[0] + (", then " + ", ".join(rest[1:]) if len(rest) > 1 else "")
        return _say("after_stage", lang, stage=str(sequence[at].get("code")).title() if len(code) > 4
                    else code, next=following)
    return None


def eligibility_question(text: str) -> str | None:
    """Eligibility wording ("is rahul eligible for the loan?") -> the standard eligibility question, which the engine
    answers with its redirect line (localized like every answer). None for "eligible to move to CPA" / a question over
    the officer's cases (phrases.eligibility_not) -- those are about CPA readiness."""
    from app.agents.applicant.copilot.semantics import task as _task

    if not _has(text, "eligibility") or _has(text, "eligibility_not") or _task.asks_over_cases(text):
        return None
    return str(((cfg().get("texts") or {}).get("eligibility_question") or {}).get("en") or "") or None


#: the stage the last "after <stage>" answer named first, per chat: "and after that?" steps on from it
_NEXT_STAGE = threading.local()
_STAGE_MEMO: dict[str, tuple[float, str]] = {}


def _chat(claims: dict[str, Any], context: dict[str, Any] | None) -> str:
    from app.agents.applicant.copilot.capabilities import workspace

    workspace_id = (context or {}).get("workspace_id") if isinstance(context, dict) else None
    return f"{workspace._subject(claims)}|{workspace_id or ''}"


def direct(message: str, claims: dict[str, Any], context: dict[str, Any] | None, request_id: str, lang: str,
           case_in_scope: str | None = None) -> dict[str, Any] | None:
    """_direct_reply for a request, BEFORE any model step (fos_api): these answers never wait on Qwen."""
    import time as _time

    state = _state(claims, context)
    case_id = getattr(state, "active_case_id", None) or case_in_scope
    chat = _chat(claims, context)
    text = str(message)
    with _LOCK:
        memo = _STAGE_MEMO.pop(chat, None)
    if memo and memo[0] > _time.time() and _has(text, "after_that") and not _has(text, "after_that_not"):
        # "ok and after that?" RIGHT AFTER AN "after <stage>" ANSWER (2026-10-14): the stage after the one it named
        text = f"what comes after {memo[1].lower()}?"
    with _LOCK:
        accept_memo = _ACCEPT_MEMO.pop(chat, 0.0)
    names = (cfg().get("phrases") or {}).get("document_names") or {}
    bare = _norm(text).strip(" ?.!")
    if accept_memo > _time.time() and len(bare.split()) <= 4 and any(
            re.fullmatch(rf"(?:and\s+|what\s+about\s+|how\s+about\s+|or\s+)?(?:a\s+|an\s+|the\s+)?"
                         rf"{re.escape(_norm(a).strip())}", bare) for v in names.values() for a in v or []):
        # "rent agreement?" RIGHT AFTER AN ACCEPTABILITY ANSWER (2026-10-14 officer set): is THAT one accepted
        text = f"is {bare} acceptable?"
    _NEXT_STAGE.code = None
    reply = _direct_reply(text, case_id, request_id, lang)
    if reply is not None and reply.get("intent") == "DOCUMENT_ACCEPTED":
        with _LOCK:
            _ACCEPT_MEMO[chat] = _time.time() + 600
            if len(_ACCEPT_MEMO) > 5000:
                _ACCEPT_MEMO.pop(next(iter(_ACCEPT_MEMO)))
    if reply is not None and reply.get("intent") == "PROCESS_STAGES" and _NEXT_STAGE.code:
        with _LOCK:
            _STAGE_MEMO[chat] = (_time.time() + 600, _NEXT_STAGE.code)
            if len(_STAGE_MEMO) > 5000:
                _STAGE_MEMO.pop(next(iter(_STAGE_MEMO)))
    return reply


async def _fast(message: str, claims: dict[str, Any], context: dict[str, Any] | None, request_id: str,
                lang: str, case_in_scope: str | None = None, meaning_kind: str | None = None,
                knowledge_hint: str | None = None) -> dict[str, Any] | None:
    """The reply when this message needs no case (see the module doc), else None (the message goes on).

    `case_in_scope`: a case the REQUEST names (case_id). With a case in scope -- named or opened -- help, frustration
    and "my stage" belong to that case (the case logic answers them); definitions, calculators, policy numbers,
    credit-decision declines and knowledge questions about no case data are still answered here."""
    from app.security.auth import get_subject

    state = _state(claims, context)
    seed = int(getattr(state, "turn_id", 0) or 0)
    text = str(message)
    case_id = getattr(state, "active_case_id", None) or case_in_scope

    reply = _direct_reply(text, case_id, request_id, lang)
    if reply is not None:
        return reply
    if _has(text, "frustrated") and not case_id:
        options = ["My pending cases", "What is pending?" if case_id else "Show my cases"]
        block = _asks(options) + "\n" + _pick((cfg().get("fallback") or {}).get("supervisor"), lang)
        return _reply(request_id, "FRUSTRATED", _say("frustrated", lang) + "\n" + _summary(claims, lang),
                      block=block, case_id=case_id)
    if (_has(text, "help") or _only(text, "help")) and not case_id:
        return _reply(request_id, "GENERAL_HELP", _say("help", lang) + "\n\n" + _summary(claims, lang),
                      block=_asks(_suggestions(state, lang, 3)), case_id=case_id,
                      tts=_say("help", lang) + " " + _summary(claims, lang))
    if _only(text, "ack") and (_only(text, "thanks") or not _pending(state, claims, context)):
        # the small-talk contract: THANKS / ACKNOWLEDGEMENT, a CONVERSATION answer that read nothing
        # (config phrases.thanks names the thank-yous among the ack words)
        kind = "THANKS" if _only(text, "thanks") else "ACKNOWLEDGEMENT"
        outcome = "NEGATION" if _only(text, "negation") else "ACKNOWLEDGEMENT"
        said = _say("thanks_reply", lang, seed) if kind == "THANKS" and (cfg().get("texts") or {}).get("thanks_reply") \
            else _say("ack", lang, seed)
        return _reply(request_id, kind, said + " " + _say("ack_next", lang),
                      block=_asks(_suggestions(state, lang, 2)), case_id=case_id, category="CONVERSATION",
                      answer_basis={"composition": {"called": False}, "source": "SMALL_TALK"},
                      # the turn's conversation outcome, as the conversation-state layer names it: an acknowledgement
                      # of the previous answer -- nothing read, nothing resolved (conversation eval o3)
                      # a bare "no" / "nahi" keeps the conversation-state contract's NEGATION (evals h5)
                      understanding={"conversation": {"outcome": outcome, "turn_type": outcome}})
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
            if spec.get("then_stage"):
                # "who reviews it after that?" right after this process answer: the stage after it (stage_sequence)
                import time as _time

                with _LOCK:
                    _STAGE_MEMO[_chat(claims, context)] = (_time.time() + 600, str(spec["then_stage"]))
            return _reply(request_id, "PROCESS_ANSWER", md, case_id=case_id, tts=spoken,
                          query_type="PROCESS_KNOWLEDGE")
    if _has(text, "product_documents") and _has(text, "co_applicant") and not _product_of(text)             and not _has(text, "case_document_state"):
        # "which documents of the co-applicant are PENDING" asks about the case's co-applicant, not the product list
        md, spoken = _coapp_table(lang)
        return _reply(request_id, "PRODUCT_CHECKLIST", md, case_id=case_id, tts=spoken, query_type="PROCESS_KNOWLEDGE")
    # "which documents are accepted as ADDRESS PROOF" asks about one document: the knowledge path answers it
    # "kaunse documents BAAKI hai" asks about a case's documents (pending / uploaded / verified): never the product list
    # A PARAPHRASE of the checklist question ("what does the customer need to submit for a personal loan?"): meaning read
    # a knowledge question whose nearest catalogue example IS the checklist shape, and the words name the product
    # (the example may be a reviewed paraphrase in other words: "...what documents are accepted for a personal loan")
    paraphrased = (meaning_kind == "knowledge" and bool(knowledge_hint) and _product_of(text) is not None
                   and (_has(str(knowledge_hint), "product_documents")
                        or bool(re.search(r"\bdocuments\b", str(knowledge_hint).lower()))))
    if (_has(text, "product_documents") or paraphrased) and not _has(text, "one_document") \
            and not _has(text, "case_document_state"):
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
    from app.agents.applicant.copilot.capabilities import product_flow as _oos_flow

    if _oos_flow.out_of_scope(text):
        # CLEARLY OFF-TOPIC (product_flow.yaml out_of_scope words), case open or not: the one polite line and the next
        # option -- never a company "policy value" (2026-10-18: "best mutual fund to invest in?" got POLICY_NOT_CONFIGURED)
        line, option = _oos_flow.out_of_scope_reply(lang, len(text))
        return _reply(request_id, "NOT_IN_SCOPE", line, case_id=case_id, block=option, category="UNSUPPORTED",
                      query_type="CONVERSATION")
    if _decision(text) or meaning_kind == "decision":
        spec = cfg().get("decisions") or {}
        return _reply(request_id, "CREDIT_DECISION_DECLINED", _pick(spec.get("text"), lang), case_id=case_id,
                      block=_asks(_pick(spec.get("suggestions"), lang) or []), query_type="PROCESS_KNOWLEDGE")
    calculated = _calculate(text, lang)
    if calculated is not None:
        return _reply(request_id, "CALCULATOR", calculated, case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    # with a case in scope, "interest rate" / "tenure" means THAT case's value (the case logic); the policy number only
    # when a product is named ("maximum tenure for home loan")
    unconfigured = _unconfigured_product(text) and not _product_of(text)
    # ...and a LIMIT asked by name ("what is the MAX LTV?", "minimum income?") is the policy number, case open or not
    # (2026-10-12) -- "what is the interest rate?" with a case open stays that case's own value
    limit_asked = bool(re.search(r"\b(max|maximum|min|minimum|limit|limits|cut\s*-?\s*off|upto|up\s+to|allowed|"
                                 r"zyada\s+se\s+zyada|kam\s+se\s+kam)\b", _plain(text)))
    policy_answer = (_policy(text, state, lang)
                     if not case_id or _product_of(text) or unconfigured or limit_asked else None)
    if policy_answer is not None and unconfigured:
        policy_answer = _say_text((cfg().get("policy") or {}).get("texts", {}).get("generic_not_configured"), lang)
    if policy_answer is not None:
        return _reply(request_id, "POLICY_ANSWER", policy_answer, case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    if meaning_kind == "policy":
        # understood as a company VALUE but no configured topic matched: said plainly, never guessed
        return _reply(request_id, "POLICY_NOT_CONFIGURED", _say_text((cfg().get("policy") or {}).get("texts", {})
                                                                     .get("generic_not_configured"), lang),
                      case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    # with a case in scope, "what is THE cibil score" asks for that case's VALUE (the case logic routes it), never the
    # glossary's definition of CIBIL; "what is CIBIL" stays a definition (config general_question.case_value_patterns)
    values = (cfg().get("general_question") or {}).get("case_value_patterns") or []
    if case_id and any(re.search(p, _plain(text)) for p in values):
        return None
    # "address proof mein kya de sakte hai" = WHICH DOCUMENTS ARE ACCEPTED: the configured list, case open or not
    accepted = _english_shape(text).startswith("which documents are accepted")
    if accepted:
        return await _knowledge(_english_shape(text), request_id, lang, None)
    # "is signature mandatory" / "do i need bank statement" / "aadhaar instead of pan": the product checklist answers
    # (config general_question.requirement_shapes / substitute_shapes), case open or not -- never a glossary passage
    rule = _requirement_answer(text, lang)
    if rule is not None:
        return _reply(request_id, "DOCUMENT_REQUIREMENT", rule, case_id=case_id, query_type="PROCESS_KNOWLEDGE")
    in_case = case_id or case_in_scope
    terms = (os.getenv("COPILOT_TERMS_KNOWLEDGE") or "").strip().lower()
    if in_case and terms in {"0", "false", "no", "off"}:
        return None                      # definitions with a case open are off (COPILOT_TERMS_KNOWLEDGE=false)
    if in_case and _case_question_not_definition(text, meaning_kind):
        return None                      # "is any document still outstanding" -- that case's data, not the glossary
    if in_case and _one_recorded_field(text):
        return None                      # "what is the tenure" on an open case: its tenure, not the glossary
    compared = await _difference(text, request_id, lang, case_id)
    if compared is not None:
        return compared
    defined = await _definition(text, request_id, lang, case_id)
    if defined is not None:
        return defined
    # understood as GENERAL KNOWLEDGE by meaning (semantics/meaning.py): the knowledge base answers it, case open or not
    if meaning_kind == "knowledge" or (_general_question(text) and not (case_id and _names_case_data(text)))             or (not case_id and _handbook_answers_without_a_case(text)):
        # with a case OPEN, "is PAN mandatory?" / "documents required?" is about THAT case: the case logic answers
        # (unless MEANING said general knowledge: then a miss is "not in the knowledge base", never a case answer)
        return await _knowledge(text, request_id, lang, None if meaning_kind == "knowledge" else case_id,
                                hint=knowledge_hint if meaning_kind == "knowledge" else None)
    return None


def _document_slots(text: str) -> list[tuple[int, str]]:
    """(position, slot) for every configured document the message names, by its readable name or an accepted type."""
    from app.agents.applicant import config

    said = " " + _plain(text) + " "
    names: dict[str, str] = {}
    entries = [e for product in config.products() or [] for e in config.checklist_for(product) or []]
    for e in entries + list(config.co_applicant_documents() or []):
        names[_plain(_readable(e.get("slot")))] = str(e.get("slot"))
        for a in e.get("accepts") or []:
            names.setdefault(_plain(_readable(a)), str(a))
    for alias, slot in ((cfg().get("general_question") or {}).get("document_aliases") or {}).items():
        names[_plain(alias)] = str(slot)
    found = []
    for name, slot in sorted(names.items(), key=lambda kv: -len(kv[0])):
        at = said.find(" " + name + " ") if name else -1
        if at >= 0 and not any(s == slot for _, s in found):
            found.append((at, slot))
            said = said[:at] + " " + "#" * len(name) + said[at + len(name) + 1:]   # "pan" never again inside "pan card"
    return sorted(found)


def _requirement_answer(text: str, lang: str) -> str | None:
    """'Is X mandatory?' -> required / optional per configured product; 'X instead of Y?' -> what Y's slot accepts."""
    from app.agents.applicant import config

    spec = cfg().get("general_question") or {}
    plain = _plain(text)
    slots = _document_slots(text)
    if not slots:
        return None
    products = [p for p in config.products() or [] if str(p).lower() != "default"]
    texts = cfg().get("texts") or {}
    if len(slots) >= 2 and any(re.search(p, plain) for p in spec.get("substitute_shapes") or []):
        offered, wanted = slots[0][1], slots[1][1]
        if any(re.search(p, plain) for p in spec.get("substitute_shapes_reversed") or []):
            offered, wanted = wanted, offered            # "aadhaar ki jagah voter id": voter id is the one offered
        entries = [e for product in products for e in config.checklist_for(product) or []]
        for e in entries + list(config.co_applicant_documents() or []):
            accepts = [str(a) for a in e.get("accepts") or []]
            if wanted in accepts or str(e.get("slot")) == wanted:
                key = "substitute_yes" if offered in accepts else "substitute_no"
                return product_flow_say(texts.get(key), lang, document=_readable(offered),
                                        slot=_readable(e.get("slot")),
                                        accepted=", ".join(_readable(a) for a in accepts))
        return None
    if len(slots) != 1 or not any(re.search(p, plain) for p in spec.get("requirement_shapes") or []):
        return None
    if any(re.search(p, plain) for p in spec.get("requirement_not_shapes") or []):
        return None
    slot = slots[0][1]
    coapp = list(config.co_applicant_documents() or [])
    required, optional = [], []
    for product in products:
        for e in config.checklist_for(product) or []:
            accepts = [str(a) for a in e.get("accepts") or []]
            # THE SLOT ITSELF, or a document that is the slot's ONLY accepted type (PAN) -- never one option among
            # several: "Do I need a passport?" is not "Passport is mandatory" when a driving licence or a voter ID
            # satisfies the same slot (2026-10-10, family eval req_option_of_slot)
            if slot == str(e.get("slot")) or accepts == [slot]:
                (required if e.get("mandatory", True) else optional).append(_readable(product))
                break
    if not required and not optional:
        # an ACCEPTED TYPE, not a slot ("is aadhaar mandatory"): never mandatory by itself, one option for a slot
        options = sorted({_readable(e.get("slot")) + (" (co-applicant)" if e in coapp else "")
                          for e in [x for p in products for x in config.checklist_for(p) or []] + coapp
                          if slot in [str(a) for a in e.get("accepts") or []]})
        if options:
            return product_flow_say(texts.get("requirement_accepted"), lang, document=_readable(slot),
                                    slots=", ".join(options))
        return product_flow_say(texts.get("requirement_none"), lang, document=_readable(slot))
    if required and not optional and len(required) == len(products):
        return product_flow_say(texts.get("requirement_all"), lang, document=_readable(slot))
    parts = []
    if required:
        parts.append(product_flow_say(texts.get("requirement_some"), lang, document=_readable(slot),
                                      products=", ".join(required)))
    if optional:
        parts.append(product_flow_say(texts.get("requirement_optional"), lang, document=_readable(slot),
                                      products=", ".join(optional)))
    return " ".join(parts)


def product_flow_say(value: Any, lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say(value, lang, 0, **values)


def _case_question_not_definition(text: str, meaning_kind: str | None) -> bool:
    """
    With a case OPEN: a question that points at the case ("this file", "the customer") is never a glossary answer;
    otherwise a glossary / knowledge answer only for a DEFINITION-shaped question ("what is FOIR", "meaning of LTV")
    or one meaning read as knowledge (config general_question.case_referents / definition_shapes).
    """
    spec = cfg().get("general_question") or {}
    said = " " + _plain(text) + " "
    if any(" " + _plain(r) + " " in said for r in spec.get("case_referents") or []):
        return True
    if meaning_kind == "knowledge":
        return False
    return not any(re.search(p, _plain(text)) for p in spec.get("definition_shapes") or [])


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


def _handbook_answers_without_a_case(text: str) -> bool:
    """
    ANY PHRASING, NOT A SHAPE LIST (2026-10-12, held-out evaluation): with no case open, a question that points at
    no case (no case referent, no case id, not a list request, not case-only) and that the handbook answers WITH
    CONFIDENCE is a general question -- "what comes after CPA in the process?" was sent to "which case?" because it
    did not start with "what is". A case question ("kitna loan maanga hai") has no confident handbook answer and
    still goes to the case logic.
    """
    spec = cfg().get("general_question") or {}
    said = _plain(text)
    if not said or any(re.search(p, said) for p in spec.get("case_only") or []):
        return False
    # A PICK OR A FRAGMENT ("1", "yes", "docs") answers the conversation, never the handbook: BM25 matched "1" to a
    # numbered handbook list and the "which case?" pick was answered with it (test_chat_raise_query, 2026-10-12)
    if re.fullmatch(r"[\d\s.,]+", said) or len(re.findall(r"[a-z]{2,}", said)) < 3:
        return False
    # ABOUT THE OFFICER'S OWN CASES ("show top 2 cases", "show me the next two", "why is it pending"): the workspace
    # and the case logic answer it -- never the handbook (Run 9, 2026-10-12). "what comes after CPA in the process?"
    # names no case and stays a handbook question.
    from app.agents.applicant.copilot.capabilities import case_list as _own
    from app.agents.applicant.copilot.capabilities import workspace as _ws

    if (any(re.search(p, said) for p in (spec.get("case_value_patterns") or []) + (spec.get("case_pointers") or []))
            or _own.understand(text) is not None or _ws._says("more", text)
            or _own._has(text, (_own.cfg().get("phrases") or {}).get("next"))):
        return False
    if any(" " + _plain(r) in " " + said + " " for r in spec.get("case_referents") or []):
        return False
    from app.agents.applicant.copilot.capabilities import workspace

    if workspace._only_asks_for_the_list(text) or any(p.search(text) for p in workspace._ID.values()):
        return False
    # A REQUEST TO DO SOMETHING is never a general question ("query daalo - bank statement 6 mahine ka chahiye",
    # "raise deviation ..."): the workflow's own phrases (case actions, stage flow) send it to its case step
    from app.agents.applicant.copilot.capabilities import case_actions as _acts
    from app.agents.applicant.copilot.capabilities import stage_flow as _flow

    if any(_acts.asks(k, text) for k in ("raise_query", "list_queries", "new_case")) or any(
            _flow._says(k, text) for k in ((_flow._cfg().get("phrases") or {}).keys())):
        return False
    # "which CASES have a KYC mismatch?" asks for the caller's cases filtered -- the case list, not the handbook
    # ("what is a KYC mismatch?" names no cases and stays a definition)
    from app.agents.applicant.copilot.capabilities import case_list as _cl

    if re.search(r"\b(cases|files|applications|applicants|customers)\b", said):
        wanted = _cl.understand(text)
        if wanted is not None and (getattr(wanted, "filter", None) or getattr(wanted, "status", None)
                                   or getattr(wanted, "group", None) or getattr(wanted, "sort", None)):
            return False
    from app.agents.applicant import knowledge_answer

    result = knowledge_answer.retrieve(text)
    if result and result.confident and result.hits:
        return True
    # ...or a COMPANY VALUE the knowledge base deliberately does not state ("what's the processing fee you charge?":
    # the best passage is a general-lending definition, knowledge.yaml no_company_values_sources + value_shapes) --
    # answered honestly as not in the knowledge base, never "which case?"
    from app.knowledge import _config as _knowledge_cfg

    kcfg = _knowledge_cfg()
    general = {str(s) for s in kcfg.get("no_company_values_sources") or []}
    return bool(result and result.hits and result.hits[0].chunk.source in general
                and any(re.search(p, said) for p in kcfg.get("value_shapes") or []))


def _general_question(text: str) -> bool:
    """Shaped like general knowledge and pointing at no case (conversation_general.yaml general_question)."""
    spec = cfg().get("general_question") or {}
    said = _plain(text)
    if not said or any(re.search(p, said) for p in spec.get("case_only") or []):
        return False
    from app.agents.applicant.copilot.semantics import intents as _credit

    if _credit.names_credit_record(text):
        return False                                    # "what does the credit memo say": the credit team's route
    # a GENERAL topic named outright ("another lender ... take over his loan" = balance transfer) wins over a referent
    # word ("his"): config general_question.general_override_shapes
    if any(re.search(p, said) for p in spec.get("general_override_shapes") or []):
        from app.agents.applicant.copilot.capabilities import workspace

        return not any(pattern.search(text) for pattern in workspace._ID.values())
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


async def _knowledge(text: str, request_id: str, lang: str, case_id: str | None,
                     hint: str | None = None) -> dict[str, Any] | None:
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
    # ...and, for a paraphrase the handbook does not match as worded, the closest catalogue example of the knowledge
    # intent meaning chose (audit root cause #3) -- a retrieval wording only; the answer is the passage it finds
    for query in dict.fromkeys([q for q in (text, _english_shape(text), hint) if q]):
        answer = await _knowledge_candidate(query, request_id, case_id, allow)
        if answer is not None:
            found.append(answer)
            if answer[0]:
                break
    if found:
        return next((reply for authoritative, reply in found if authoritative), found[0][1])
    if case_id:
        return None                 # a case is in scope: nothing general found -- the case logic answers it
    # THE HANDBOOK COVERS THE TOPIC (2026-11-02 audit): retrieval was confident but the passage failed the relevance
    # guard -- the general model must never answer a question the approved source holds (it can contradict it)
    general = None if _handbook_confident(text) else await _general_llm(text, request_id)
    if general:
        # NEVER PASSED OFF AS THE HANDBOOK (2026-10-11, directive section 9): a general answer carries no citation, so
        # it says what it is (config general_llm.provenance_note) and is marked as such on the reply
        note = _pick((cfg().get("general_llm") or {}).get("provenance_note"), lang)
        reply = _reply(request_id, "GENERAL_LLM", f"{general}\n\n{note}" if note else general, case_id=case_id,
                       query_type="PROCESS_KNOWLEDGE")
        reply.update(response_source="LLM_GENERAL")
        return reply
    _log_gap(_plain(text), kind="questions")
    return _reply(request_id, "UNKNOWN_TERM", _pick(cfg().get("general_unknown"), lang), case_id=case_id)


def _poor_general_answer(answer: str, question: str, spec: dict[str, Any]) -> bool:
    """Too short, or mostly the question's own words said back ("Policy mein kya requirement hai, depend ke liye
    lender."): never shown (config general_llm.min_words / max_echo)."""
    words = re.findall(r"[a-z]+", answer.lower())
    asked = set(re.findall(r"[a-z]+", question.lower()))
    if len(words) < int(spec.get("min_words", 8)):
        return True
    echo = sum(1 for w in words if w in asked) / max(1, len(words))
    return echo > float(spec.get("max_echo", 0.4))


async def _general_llm(question: str, request_id: str) -> str | None:
    """
    A GENERAL banking / lending question the knowledge base does not hold (Smart Bot plan section 5, type "general"):
    the model answers from general knowledge in at most `max_sentences` sentences -- never a company number (limits,
    rates, fees, TAT: it must say "depends on the lender's policy"), never case data. VALIDATED: no digit, ₹ or % the
    question did not carry, no first-person company voice, not "unknown" -> else None ("not in the knowledge base").
    An unknown ALL-CAPS term (FNR, FTNR) is company jargon: never explained by the model.
    """
    spec = cfg().get("general_llm") or {}
    flag = (os.getenv("COPILOT_GENERAL_LLM") or "").strip().lower()
    if (flag and flag not in {"1", "true", "yes", "on"}) or (not flag and not spec.get("enabled", False)):
        return None
    terms = re.findall(r"\b[A-Z]{2,6}\b", str(question))
    from app.agents.applicant.copilot.answering import style

    if terms and not any(style.defined_term(f"what is {t}") for t in terms):
        return None                                      # an unknown acronym: "not in the knowledge base", logged
    from app.llm import availability
    from app.llm.memory import free_gb_for_model as free_gb

    free = free_gb()
    if (free is not None and free < float(spec.get("min_free_ram_gb", 2.0))) or not availability.provider_reachable():
        return None
    from app.agents.applicant.copilot.answering import realtime

    realtime.hint(request_id, realtime.say_status("understanding", "en"))
    import asyncio

    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    body = {"model": str(spec.get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "messages": [{"role": "system", "content": str(spec.get("instructions") or "")},
                         {"role": "user", "content": str(question)[:300]}],
            "options": with_num_ctx({"temperature": 0.0, "num_predict": int(spec.get("num_predict", 90))})}
    timeout = float(spec.get("timeout_seconds", 8.0))
    GENERAL_LLM_STATS["called"] += 1
    try:
        _tb.check("general")      # an earlier model call of this turn failed: answer without it (app/llm/turn_budget.py)
        async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
            response = await asyncio.wait_for(_tb.guarded(client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body), "general"), timeout)
            text = str(((response.json() or {}).get("message") or {}).get("content") or "").strip()
    except Exception:  # noqa: BLE001 - slow / down: "not in the knowledge base"
        return None
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()][:int(spec.get("max_sentences", 3))]
    answer = " ".join(sentences).strip()
    said = str(question)
    if (not answer or re.search(r"(?i)\bunknown\b|i('m| am) not sure|i don't know|as an ai", answer)
            or any(d not in said for d in re.findall(r"\d+", answer)) or re.search(r"[₹%]", answer)
            or re.search(r"(?i)\b(our|we offer|we charge|our bank)\b", answer)
            or _poor_general_answer(answer, said, spec)):
        logger_general.info("general_llm rejected: %s", answer[:120])
        GENERAL_LLM_STATS["rejected"] += 1
        return None
    GENERAL_LLM_STATS["used"] += 1
    logger_general.info("general_llm used (provenance llm_general): %s", _plain(question)[:120])
    return answer


# --------------------------------------------------------------------------
# GENERAL QUESTIONS AFTER LOS (Phase 4 directive 2C): conversation_general.yaml general_chat
# --------------------------------------------------------------------------
_LOS_VOCABULARY: dict[str, Any] = {"words": None, "phrases": None}


def _los_vocabulary() -> tuple[set[str], list[str]]:
    """
    Every word / phrase that names something of the LOS, from the SAME sources the Copilot already answers from
    (never a new list): the case words (product_flow which_case.case_words), the glossary terms, the approved
    handbook glossary's headings (knowledge/fos/los_glossary.md), the stage names, the document types and their
    aliases, the case list's nouns and the product aliases. Cached for the process.
    """
    if _LOS_VOCABULARY["words"] is not None:
        return _LOS_VOCABULARY["words"], _LOS_VOCABULARY["phrases"]
    terms: set[str] = set()
    try:
        from app.agents.applicant.copilot.capabilities import product_flow

        terms |= {str(w) for w in (product_flow.cfg().get("which_case") or {}).get("case_words") or []}
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.agents.applicant.copilot.answering import style

        terms |= {str(t) for t in style.terms()}
    except Exception:  # noqa: BLE001
        pass
    try:
        from pathlib import Path

        glossary = Path(__file__).resolve().parents[5] / "knowledge" / "fos" / "los_glossary.md"
        for line in glossary.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                head = line[3:].strip()
                terms.add(head.split("(")[0].strip())
                inner = re.search(r"\(([^)]+)\)", head)
                if inner:
                    terms.add(inner.group(1).strip())
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.agents.applicant import config as _agent_config

        for stage in getattr(_agent_config, "stage_codes", lambda: [])() or []:
            terms |= {str(stage), str(_agent_config.stage_label(stage))}
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.agents.applicant.copilot.capabilities import case_list

        singular, plural = case_list._case_nouns()
        terms |= set(singular) | set(plural)
    except Exception:  # noqa: BLE001
        pass
    try:
        spec = cfg().get("policy") or {}
        for aliases in (spec.get("product_aliases") or {}).values():
            terms |= {str(a) for a in aliases or []}
        terms |= {str(p) for p in spec.get("unconfigured_products") or []}
    except Exception:  # noqa: BLE001
        pass
    words, phrases = set(), []
    for term in terms:
        plain = _plain(term)
        if not plain or len(plain) < 2:
            continue                                  # a one-letter heading never names the LOS on its own
        if " " in plain:
            phrases.append(plain)
        else:
            words.add(plain)
    _LOS_VOCABULARY.update(words=words, phrases=phrases)
    return words, phrases


def names_los(text: str) -> bool:
    """Whether the message names anything of the LOS (a word / phrase of `_los_vocabulary`, a document, a product,
    a stage, a case / applicant id)."""
    plain = _plain(text)
    words, phrases = _los_vocabulary()
    said = set(plain.split())
    if said & words or any(f" {p} " in f" {plain} " for p in phrases):
        return True
    if re.search(r"(?i)\b(case|app|coapp)-[0-9a-f]{4,}\b", str(text)):
        return True
    try:
        from app.agents.applicant.copilot.semantics import intents, meaning

        if meaning.document_in(text) or intents.stage_in(text):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(_product_of(text))


def is_general_question(text: str) -> bool:
    """
    A question that names NOTHING of the LOS and that no Copilot reader recognises as an LOS turn: answered as an
    ordinary question (general_chat). Never a fragment ("why", "docs" -- general_chat.min_words), a social turn, an
    action request ("sort this out", "approve this"), or anything the task layer / the engine's rules read as LOS.
    """
    spec = cfg().get("general_chat") or {}
    plain = _plain(text)
    if not plain or len(plain.split()) < int(spec.get("min_words", 3)) or names_los(text):
        return False
    # "give ME a tip" / "tell ME about Paris": the asker as the object of the request verb, not a pointer back into
    # the conversation ("what still needs me?" is one)
    asked = re.sub(r"^(can you |could you |please )?(give|tell|show|teach|help|remind|explain to) me\b", "", plain)
    if set(asked.split()) & {str(w).lower() for w in spec.get("context_words") or []}:
        return False                         # it points back into the conversation: its own follow-up
    try:
        from app.agents.applicant import language as _language

        detected = _language.detect(text)
        if getattr(detected, "code", None) not in set(spec.get("languages") or ["en"]) \
                or getattr(detected, "code_mixed", False):
            return False                     # Hinglish / another language: the LOS pipeline reads it
    except Exception:  # noqa: BLE001
        return False
    try:
        from app.agents.applicant import conversation
        from app.agents.applicant.copilot.semantics import intents
        from app.agents.applicant.copilot.semantics import task as _task

        turn = conversation.classify(text)
        if turn is not None and turn.kind != conversation.OFF_TOPIC:
            return False                     # hello / thanks / bye / help / who are you: the conversation's own
        if _task.unnamed_action(text) or _task.asks_over_cases(text) or _task.portfolio_followup(text, None):
            return False
        from app.agents.applicant.copilot.capabilities import vague as _vague

        if _vague.is_command(text, case_open=True):
            return False                     # "open Rohan Bye": a workspace / form command
        if intents.classify(text).intent not in (intents.Intent.UNKNOWN, intents.Intent.FOS_KNOWLEDGE,
                                                 intents.Intent.OUT_OF_SCOPE):
            return False
        if intents.classify(text).intent is intents.Intent.OUT_OF_SCOPE and turn is None:
            return False                     # "approve this" / "reject the file": the LOS refusal, not a chat
    except Exception:  # noqa: BLE001 - unsure: the LOS pipeline answers as before
        return False
    return True


def general_chat_available() -> bool:
    """General chat is on AND the model can take a question now -- checked FIRST, so with no model nothing (no name
    lookup, no retrieval) runs for a general question and the LOS pipeline answers it as before."""
    spec = cfg().get("general_chat") or {}
    flag = (os.getenv("COPILOT_GENERAL_CHAT") or "").strip().lower()
    if (flag and flag not in {"1", "true", "yes", "on"}) or (not flag and not spec.get("enabled", False)):
        return False
    return _general_chat_ready(spec)


def _general_chat_ready(spec: dict[str, Any]) -> bool:
    """The local model can take a general question now (enough free memory, the provider reachable)."""
    try:
        from app.llm import availability
        from app.llm.memory import free_gb_for_model as free_gb

        free = free_gb()
        return not (free is not None and free < float(spec.get("min_free_ram_gb", 1.5))) \
            and bool(availability.provider_reachable())
    except Exception:  # noqa: BLE001
        return False


async def _chat_model(messages: list[dict[str, str]], spec: dict[str, Any]) -> str | None:
    """One bounded call to the configured local model (the same Ollama chat call as `_general_llm`)."""
    import asyncio

    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    body = {"model": str(spec.get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "messages": messages,
            "options": with_num_ctx({"temperature": 0.0, "num_predict": int(spec.get("num_predict", 180))})}
    timeout = float(spec.get("timeout_seconds", 12.0))
    try:
        _tb.check("general")      # an earlier model call of this turn failed: answer without it (app/llm/turn_budget.py)
        async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
            response = await asyncio.wait_for(_tb.guarded(client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body), "general"), timeout)
            return str(((response.json() or {}).get("message") or {}).get("content") or "").strip()
    except Exception:  # noqa: BLE001 - slow / down: said as unavailable, never guessed
        return None


def _unsafe_general(answer: str) -> bool:
    """Anything internal in a general answer (a host, a URL, a source file, a key, a stack trace) or what the
    output guardrail refuses."""
    if re.search(r"(?i)https?://|\b127\.0\.0\.1\b|\blocalhost\b|\b11434\b|[\w-]+\.py\b|\bapp/\w+|\.env\b|"
                 r"\bTraceback\b|eyJ[A-Za-z0-9_-]{8,}\.|-----BEGIN|\bsk-[A-Za-z0-9]{10,}|\bsystem prompt\b", answer):
        return True
    try:
        from app.security import guardrails

        return not guardrails.check_output(answer).allowed
    except Exception:  # noqa: BLE001
        return True


async def general_chat(text: str, request_id: str, lang: str, case_id: str | None = None,
                       chat_ref: Any = None) -> dict[str, Any] | None:
    """
    The answer to an ordinary (non-LOS) question, or None when general chat is off. The approved handbook is asked
    first and wins; then the local model, with ONLY the question (no case, no customer, no history). A live-info
    question is said to need a live source; an unsure / unsafe / empty answer is never shown.
    """
    spec = cfg().get("general_chat") or {}
    flag = (os.getenv("COPILOT_GENERAL_CHAT") or "").strip().lower()
    if (flag and flag not in {"1", "true", "yes", "on"}) or (not flag and not spec.get("enabled", False)):
        return None
    texts = spec.get("texts") or {}
    if not _general_chat_ready(spec):
        return None                              # no model now: the LOS pipeline answers as it always did
    handbook = await _knowledge_candidate(text, request_id, case_id, False)
    if handbook is not None:
        return handbook[1]                       # the approved source answers: never the general model
    if _handbook_confident(text):
        return None                              # the handbook holds the topic: the LOS pipeline answers, not the model
    else:
        from app.agents.applicant.copilot.answering import realtime

        realtime.hint(request_id, realtime.say_status("understanding", "en"))
        # the chat's last exchanges (natural_chat: "and why is that?" follows the last answer); no case data in them
        from app.agents.applicant.copilot.capabilities import natural_chat

        said = await _chat_model([{"role": "system", "content": str(spec.get("instructions") or "")}]
                                 + (natural_chat.history_for(chat_ref) if natural_chat.enabled() else [])
                                 + [{"role": "user", "content": str(text)[:400]}], spec)
        said = str(said or "").strip()
        if said.upper().strip(" .") == str(spec.get("live_marker", "LIVE_INFO")).upper():
            answer = _pick(texts.get("live"), lang)
        elif not said or said.upper().strip(" .") == str(spec.get("unknown_marker", "UNKNOWN")).upper():
            return None                          # the model does not know: the LOS pipeline's own clarification
        elif _unsafe_general(said):
            answer = _pick(texts.get("unknown"), lang)
        else:
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", said) if s.strip()][:int(spec.get("max_sentences", 4))]
            if len(sentences) > 1 and not re.search(r"[.!?)\]\"']$", sentences[-1].strip()):
                sentences = sentences[:-1]       # cut by num_predict mid-sentence: never shown half-written
            answer = " ".join(sentences).strip()
            note = _pick(texts.get("note"), lang)
            answer = f"{answer}\n\n{note}" if note else answer
    reply = _reply(request_id, "GENERAL_CHAT", answer, case_id=case_id, query_type="CONVERSATION")
    reply.update(response_source="LLM_GENERAL", tools_invoked=[])
    return reply


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
                                 query_type="PROCESS_KNOWLEDGE", citations=[tied.chunk.citation()])
    except Exception:  # noqa: BLE001 - knowledge unavailable: said as not known, never guessed
        return None
    heading = _heading_of((detail.get("citations") or [""])[0])
    # a CONFIGURED fact is matched by its own rules (the slot / product it names): the relevance guard is for passages
    if detail.get("confident") and str(reply_text or "").strip() and (
            detail.get("authoritative") or _about(query, f"{reply_text} {heading}")):
        return bool(detail.get("authoritative")), _reply(
            request_id, "GENERAL_KNOWLEDGE", reply_text, case_id=case_id, query_type="PROCESS_KNOWLEDGE",
            citations=detail.get("citations") or [], answered_from=detail.get("answered_from"))
    return None


_HEADINGS: dict[str, Any] = {"key": None, "plain": ()}


def _question_headings() -> tuple[str, ...]:
    """The handbook's question headings ("...?"), normalised as heading_answer compares them; cached per repository
    load (the repository object changes when the handbook is reloaded)."""
    try:
        from app.knowledge import get_repository

        repo = get_repository()
        key = (id(repo), tuple(repo.stages()))
        if _HEADINGS["key"] != key:
            plain = {_plain(c.heading) for stage in repo.stages() for c in repo.chunks(stage)
                     if str(c.heading or "").strip().endswith("?")}
            _HEADINGS.update(key=key, plain=tuple(p for p in plain if p))
        return _HEADINGS["plain"]
    except Exception:  # noqa: BLE001 - no handbook list: let the retrieval path decide, as before
        return ()


def _near_a_heading(text: str, min_ratio: float) -> bool:
    headings = _question_headings()
    if not headings:
        return True                       # unknown: never block the layer, the retrieval below decides
    said = _plain(text)
    # quick_ratio is an upper bound of ratio: cheap rejection first, the exact ratio only for survivors
    for heading in headings:
        matcher = difflib.SequenceMatcher(None, said, heading)
        if matcher.quick_ratio() >= min_ratio and matcher.ratio() >= min_ratio:
            return True
    return False


async def heading_answer(text: str, request_id: str, case_id: str | None) -> dict[str, Any] | None:
    """
    The message IS one of the handbook's question headings (config handbook_heading): that section's answer, through
    the ordinary knowledge path (relevance guard, masking, citation). None for anything else.
    """
    spec = cfg().get("handbook_heading") or {}
    flag = (os.getenv("COPILOT_HANDBOOK_HEADING") or "").strip().lower()      # rollback / A-B switch
    on = flag in {"1", "true", "yes", "on"} if flag else bool(spec.get("enabled"))
    if not on or any(re.search(p, str(text).lower()) for p in spec.get("not_with") or []):
        return None
    from app.agents.applicant.copilot.answering import style

    if style.defined_term(text):
        return None                     # "What is FOIR?": the glossary's definition layer answers, as before
    # NO HEADING CLOSE ENOUGH, NO RETRIEVAL (2026-11-05): this layer runs before every other one, and it retrieved
    # the handbook for EVERY message -- "what is pending" on a case too (the conversation eval failed 119 / 124 on
    # "retrieval on a case conversation", and each turn paid a retrieval). The same ratio is checked first against
    # the handbook's own question headings, in memory; only a message that can match one is retrieved, as before.
    if not _near_a_heading(text, float(spec.get("min_ratio", 0.9))):
        return None
    try:
        from app.agents.applicant import knowledge_answer

        result = knowledge_answer.retrieve(text)
    except Exception:  # noqa: BLE001 - retrieval unavailable: the ordinary layers answer
        return None
    if result is None or not result.confident or not result.hits:
        return None
    said = _plain(text)
    for hit in result.hits[:3]:
        heading = str(hit.chunk.heading or "")
        if not heading.strip().endswith("?"):
            continue
        if difflib.SequenceMatcher(None, said, _plain(heading)).ratio() >= float(spec.get("min_ratio", 0.9)):
            found = await _knowledge_candidate(heading, request_id, case_id, False)
            return found[1] if found is not None else None
    return None


def _handbook_confident(question: str) -> bool:
    """Whether handbook retrieval is confident for this question (whatever the relevance guard then decided)."""
    try:
        from app.agents.applicant import knowledge_answer

        result = knowledge_answer.retrieve(question)
        if not (result is not None and result.confident and result.hits):
            return False
        # ...ABOUT THE QUESTION'S SUBJECT, not only its frame: "What is the difference between RAM and storage?" was
        # "confident" on the heading "What is the difference between a co-applicant and a guarantor?" (2026-11-02)
        words = _subject_words(question)
        if not words:
            return True
        top = result.hits[0].chunk
        # most of the subject, as the answer layer's own relevance rule (_about): one shared word is not the topic
        # ("cost-based query PLANNER" vs "Approved building PLAN", 2026-11-02)
        return _about(question, f"{top.heading or ''} {top.text}")
    except Exception:  # noqa: BLE001 - retrieval unavailable: not known to be covered
        return False


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
    # a SHORT question names its subject in every word: "late payment fee" is not answered by a passage on the
    # processing fee (2 of 3). Longer questions carry filler: 75 %, rounded up.
    return hits == len(words) if len(words) <= 3 else hits >= -(-len(words) * 3 // 4)


_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)?\s*(\d+(?:[.,]\d+)*)\s*(crore|cr|lakh|lakhs|lac|lacs|l|k|thousand|hazar|hazaar)?\b",
                     re.I)
_RATE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|(?:percent|pct|pc)\b)", re.I)
# units with their common typos ("24 monts", "3 yeras"): a calculator must not fail on a slip of the thumb
_YEARS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:years?|yeras?|yaers?|yrs?|yr|saal|sal|varsh)\b", re.I)
_MONTHS = re.compile(r"(\d+)\s*(?:months?|monts?|mnths?|monhts?|mahine|mahina|mahinon|mths?|m)\b", re.I)
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
    # ELIGIBILITY IS NOT THE CHATBOT'S (owner, 2026-10-13): no eligibility limit is read here -- a policy-number
    # question gets the configured redirect (conversation_general.yaml policy.texts.unavailable)
    return []


def _ltv_grid_text(policy: Any) -> str:
    """'80% (residential)' -- the product's one LTV limit and the property types it accepts."""
    if policy.ltv_maximum_percent is None:
        return ""
    kinds = ", ".join(str(k).replace("_", " ").lower() for k in policy.property_types)
    return f"{float(policy.ltv_maximum_percent):g}%" + (f" ({kinds})" if kinds else "")


def _policy_values(kind: str) -> dict[str, Any] | None:
    """The configured limit a calculator compares with (the first product that sets one)."""
    for p in _policies():
        if kind == "foir" and p.foir_maximum_percent is not None:
            return {"limit": f"{p.foir_maximum_percent:g}", "policy_id": p.policy_id}
        if kind == "ltv" and p.ltv_enabled and p.ltv_maximum_percent is not None:
            return {"limit": f"{p.ltv_maximum_percent:g}", "policy_id": p.policy_id, "product": _readable(p.product)}
    return None


def _policy(text: str, state, lang: str, only: str | None = None) -> str | None:
    """A policy number asked ("maximum tenure for home loan"): read from the eligibility policy now, never typed.
    `only`: answer this one subject (one line of a several-values question)."""
    spec = cfg().get("policy") or {}
    said = _norm(text)
    subjects = [only] if only else [s for s, words in (spec.get("subjects") or {}).items()
                                    if any(_norm(w) in said for w in words)]
    if not subjects and not only and _product_of(text):
        # "loan amount and tenure for personal loan": a PRODUCT named -> its policy, not a case's own data
        subjects = [s for s, words in (spec.get("bare_words") or {}).items() if any(_norm(w) in said for w in words)]
    if not subjects and not only and any(_norm(w) in said for w in spec.get("limit_words") or []):
        # "what FOIR is ALLOWED?": a topic word + a limit word is the configured limit -- never for ONE case
        from app.agents.applicant.copilot.semantics import task as _limit_task

        if not _limit_task.singular_pointer(text):
            subjects = [s for s, words in (spec.get("bare_words") or {}).items()
                        if any(_norm(w) in said for w in words)]
    if subjects and not only:
        # several values asked at once ("tenure and interest rate for home loan"): once one policy topic is named,
        # a bare topic word counts too (policy.bare_words) -- every value answered, never only the first
        subjects += [s for s, words in (spec.get("bare_words") or {}).items()
                     if s not in subjects and any(_norm(w) in said for w in words)]
    if len(subjects) > 1:
        lines = [_policy(text, state, lang, only=s) for s in dict.fromkeys(subjects)]
        lines = [x for x in lines if x]
        return "\n\n".join(lines) if lines else None
    subject = subjects[0] if subjects else None
    if subject is None and any(re.search(p, _plain(text)) for p in spec.get("fee_amount") or []):
        subject = "fees"                # any fee / charge / penalty AMOUNT: never configured here, never guessed
    if subject is None:
        return None
    # a policy NUMBER is the answer only to a question that asks for a value ("maximum tenure", "rate kitna") or a
    # short one ("home loan tenure"): "a lower interest rate from another bank -- what do we offer?" is not one
    if len(_plain(text).split()) > 7 and not any(_norm(w) in said for w in spec.get("value_words") or []):
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
            if p.ltv_enabled and getattr(p, "ltv_grid", None):
                # THE LTV GRID (2026-10-13): every row, as configured -- property / customer type and loan slab
                lines.append(_say_text(texts.get("ltv_grid"), lang, product=name, grid=_ltv_grid_text(p)))
            elif p.ltv_enabled and p.ltv_maximum_percent is not None:
                lines.append(_say_text(texts.get("ltv"), lang, product=name, maximum=f"{p.ltv_maximum_percent:g}"))
            else:
                lines.append(_say_text(texts.get("ltv_off"), lang, product=name))
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


def _stage_process_question(text: str) -> bool:
    """
    HOW A STAGE WORKS, not what a word means (2026-10-10, test_copilot_process_questions): "What does CPA verify?",
    "How does the CPA stage work?" are read by the classifier as STAGE_PROCESS and answered from the stage guide.
    This layer took them first as "what does <ACRONYM>" and, with no confident handbook hit, said the term was
    unknown. A DEFINITION of the stage name ("what is CPA", "CPA kya hai" -- config phrases.definition) stays here:
    the glossary answers that.
    """
    from app.agents.applicant.copilot.semantics import intents

    if intents.classify(text).intent is not intents.Intent.STAGE_PROCESS:
        return False
    return not any(re.match(p, str(text or "").strip(), re.I)
                   for p in (cfg().get("phrases") or {}).get("definition") or [])


def _handbook_heading_answers(text: str, claims: dict[str, Any], context: dict[str, Any] | None,
                              case_in_scope: str | None) -> bool:
    """
    A PROCESS QUESTION THE HANDBOOK ANSWERS WORD FOR WORD, with no case open: "what happens after CPA" has its own
    handbook section -- the stage guide needs a case, and diverting there asked "which case?" for a question about no
    case (2026-10-11, tests/integration/test_general_coverage; passed at HEAD before the 2026-10-10 diversion).
    """
    if case_in_scope or getattr(_state(claims, context), "active_case_id", None):
        return False
    from app.agents.applicant import knowledge_answer

    result = knowledge_answer.retrieve(text)
    plain = lambda s: " ".join(re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower()).split())   # noqa: E731
    return bool(result and result.confident and result.hits and plain(result.hits[0].chunk.heading) == plain(text))


async def _difference(text: str, request_id: str, lang: str, case_id: str | None) -> dict[str, Any] | None:
    """
    "What's the difference between CPA and Credit?" (2026-10-12): when the handbook has no section comparing the two,
    each term's OWN definition, side by side -- only when BOTH are defined (a stage name also as "<term> stage");
    otherwise None and the ordinary pipeline answers. Nothing is composed beyond the two sourced definitions.
    """
    found = None
    for pattern in (cfg().get("phrases") or {}).get("difference") or []:
        found = re.match(pattern, text.strip(), re.I)
        if found:
            break
    if not found:
        return None
    from app.agents.applicant import knowledge_answer

    whole = knowledge_answer.retrieve(text)
    terms = [found.group("a"), found.group("b")]
    if whole and whole.confident and whole.hits and all(
            t.lower() in f"{whole.hits[0].chunk.heading} {whole.hits[0].chunk.text}".lower() for t in terms):
        return None                       # a section compares them already: the knowledge pipeline answers it
    parts = []
    for term in terms:
        body = await _stage_description(term)        # a LOS stage: the configured stage guide describes it
        if body is None:
            defined = await _definition(f"what is {term}", request_id, lang, case_id)
            if defined is None or str(defined.get("intent")) != "DEFINITION":
                return None               # one of them is not defined: never half an answer
            body = str(defined.get("answer") or "").split("\n\n")[0].strip()
        parts.append(body if body.lower().lstrip("*").startswith(term.lower()) else f"**{term}**: {body}")
    return _reply(request_id, "DEFINITION", "\n\n".join(parts), case_id=case_id, query_type="PROCESS_KNOWLEDGE")


async def _stage_description(term: str) -> str | None:
    """A LOS stage named by the term ("CPA", "credit", "RCU"): its description from the configured stage guide."""
    from app.agents.applicant import config as _agent_config
    from app.agents.applicant.copilot.semantics import intents as _stage_intents
    from app.knowledge import process_knowledge

    stage = _stage_intents.stage_in(f"{term} stage")
    if not stage:
        return None
    described = process_knowledge.describe(stage, _agent_config.stage_label(stage))
    return f"**{_agent_config.stage_label(stage)}**: {described}" if described else None


def glossary_acronym_asked(text: str) -> bool:
    """"what is cpa" / "foir kya hai": a definition question whose whole term is an ACRONYM the glossary defines
    (glossary.yaml keys, upper case) -- with a case open these are still definitions, never a case question."""
    for pattern in (cfg().get("phrases") or {}).get("definition") or []:
        found = re.match(pattern, text.strip(), re.I)
        if found:
            term = str(found.group("term") or "").strip(" ?.!")
            if not term or " " in term:
                return False
            from app.agents.applicant.copilot.answering import style

            known = style.defined_term(f"{term} kya hai")
            return bool(known) and str(known).upper() == term.upper() and str(known).isupper()
    return False


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
    if re.match(r"(?i)^\s*(my|mine|our|your|his|her|their|this|that|the\s+customer'?s?|mera|meri|mere)\b", term):
        return None                     # "what is MY stage" asks about something specific, never a definition
    from app.agents.applicant.copilot.answering import professional, style

    known = style.defined_term(text) or style.defined_term(f"{term} kya hai")
    if known:
        said = professional.strip_emojis(style.definition(known, lang) or "").strip()
        source = _say("source", lang, source=_pick((cfg().get("texts") or {}).get("glossary_source"), lang) or "")
        # texts.source is "" (no sources in replies, Smart Bot plan section 8): then no empty trailing lines either
        return _reply(request_id, "DEFINITION", f"{said}\n\n{source}" if source.strip() else said, case_id=case_id,
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
        return _reply(request_id, "DEFINITION", reply_text, case_id=case_id, query_type="PROCESS_KNOWLEDGE",
                      citations=detail.get("citations") or [], answered_from=detail.get("answered_from"))
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
    # A DELIBERATE ROUTE IS AN ANSWER, NOT A MISS (2026-10-12): "what is the risk on this case?" was routed by the
    # engine ("Risk scoring is handled by the downstream Risk process") and then overwritten by "Do you mean: ..."
    if str(published.get("response_type") or "").upper() == "ROUTED" or published.get("route_to"):
        missed = False
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
