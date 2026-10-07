"""
THE LLM ROUTER (Phase 3 step 6b) -- one bounded Qwen call that PICKS A TOOL, never a fact.

FAST LANE FIRST. The rules read most questions in ~20 ms. Only a question they
cannot read (Intent.UNKNOWN) comes here -- the slot the retired frame call
(semantic_frame.llm_frame) used. Qwen gets a compact tool catalogue from config
(applicant_agent.yaml: chatbot.router) and answers with one JSON object,
{"t": tool, "a": {arg: value}}. The choice is validated against the catalogue
and turned into that tool's CANONICAL QUESTION, which the same rules then route,
so there is one engine and every downstream check (ownership, tools, templates)
is the one a typed question gets.

WHAT IT NEVER DOES. It never sees case data (only the question and last-turn
LABELS), never answers, never decides access: the input guardrail has already
run (agent.answer_question) and every tool enforces ownership itself.

LATENCY. The system prefix -- instructions + catalogue -- is built once and is
BYTE-IDENTICAL on every call (nothing dynamic in it), so Ollama reuses its
prompt cache and processes only the short state line and the question. Output
is short-keyed JSON, temperature 0, at most `num_predict` tokens. A decision is
cached in process (normalised question + labels -> tool choice; no case data).

FALLBACKS, NEVER AN ERROR. Disabled (COPILOT_LLM_ROUTER=false), free memory
under `min_free_ram_gb`, Ollama unreachable, over `timeout_seconds`, invalid
JSON or a tool / value outside the catalogue -> None, and the caller asks its
one clarifying question.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

FLAG = "COPILOT_LLM_ROUTER"
DEPRECATED_FLAG = "COPILOT_UNDERSTANDING_LLM"
_ON = {"1", "true", "yes", "on"}


@dataclass
class Routed:
    """A validated router choice."""

    tool: str
    args: dict[str, str] = field(default_factory=dict)
    #: the canonical English question the rules route (None when `intent` is set)
    question: str | None = None
    #: answered directly as this intent, nothing read (refuse / out_of_scope)
    intent: str | None = None
    #: who chose it: EMBEDDING (no model) or LLM
    source: str = "LLM"
    #: 6b-tune-2 AGREEMENT: when the bank and the model disagree, BOTH choices are
    #: offered as a one-tap question instead of answering either
    options: list["Routed"] = field(default_factory=list)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _config() -> dict[str, Any]:
    try:
        from app.agents.applicant import config

        return config.chatbot("router") or {}
    except Exception:  # noqa: BLE001
        return {}


def enabled() -> bool:
    """COPILOT_LLM_ROUTER, else the deprecated COPILOT_UNDERSTANDING_LLM, else config (default ON)."""
    for name in (FLAG, DEPRECATED_FLAG):
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip().lower() in _ON
    return bool(_config().get("enabled", True))


def deprecated_flag_in_use() -> bool:
    return bool((os.getenv(DEPRECATED_FLAG) or "").strip())


def _number(key: str, default: float) -> float:
    try:
        return float(_config().get(key, default))
    except (TypeError, ValueError):
        return default


def timeout_seconds() -> float:
    return _number("timeout_seconds", 2.5)


def min_free_ram_gb() -> float:
    return _number("min_free_ram_gb", 1.5)


def _tools() -> dict[str, dict[str, Any]]:
    tools = _config().get("tools") or {}
    return {str(k): dict(v or {}) for k, v in tools.items()}


# --------------------------------------------------------------------------
# the prompt: a byte-identical prefix, then the dynamic part
# --------------------------------------------------------------------------

_SYSTEM = ("You route messages for a loan-application assistant. Pick exactly ONE tool for the user's "
           "latest message. Use the state line to resolve follow-ups like 'aur EMI?'. Messages may be "
           "English, Hindi, Hinglish or Marathi. Never answer the question yourself.")

_PREFIX: dict[str, str] = {}
_PREFIX_LOCK = threading.Lock()


def _catalogue_line(name: str, spec: dict[str, Any]) -> str:
    args = spec.get("args") or {}
    shown = ", ".join(f"{a}=" + ("|".join(map(str, v)) if isinstance(v, list) else "text")
                      for a, v in args.items())
    return f"- {name}({shown}): {spec.get('description') or ''}".rstrip()


def _readable() -> bool:
    """Output keys: "readable" {"tool","args"} (default since 6b-tune) or "short" {"t","a"}."""
    return str(_config().get("output_keys", "readable")).lower() != "short"


def _reply_json(tool: str, args: dict | None) -> str:
    keys = ("tool", "args") if _readable() else ("t", "a")
    return json.dumps({keys[0]: tool, keys[1]: dict(args or {})}, ensure_ascii=False)


def system_prefix() -> str:
    """
    Instructions + compact catalogue + few-shot examples. Built once per
    configuration; identical bytes on every call, so Ollama's prompt cache holds
    all of it and the examples cost nothing after the first call.
    """
    cfg = _config()
    key = json.dumps({"tools": cfg.get("tools"), "examples": cfg.get("examples"),
                      "keys": _readable()}, sort_keys=True, default=str)
    with _PREFIX_LOCK:
        cached = _PREFIX.get(key)
        if cached is None:
            from app.security import guardrails

            examples = []
            for ex in cfg.get("examples") or []:
                state = f"State: {ex['state']}\n" if ex.get("state") else ""
                examples.append(f"{state}Message: {ex['message']}\n-> {_reply_json(ex['tool'], ex.get('args'))}")
            shape = '{"tool": "<tool>", "args": {<arg>: <value>}}' if _readable() \
                else '{"t": "<tool>", "a": {<arg>: <value>}}'
            cached = (_SYSTEM + " " + guardrails.UNTRUSTED_NOTICE + "\nTools:\n"
                      + "\n".join(_catalogue_line(n, s) for n, s in _tools().items())
                      + ("\nExamples:\n" + "\n".join(examples) if examples else "")
                      + f"\nReply ONLY with JSON: {shape}")
            _PREFIX.clear()
            _PREFIX[key] = cached
        return cached


def state_line(context: Any) -> str:
    """Last-turn LABELS only (followup.Context): never a case value, a name or an id."""
    parts = []
    for label, attr in (("last", "last_intent"), ("party", "last_subject"), ("document", "last_document")):
        value = getattr(context, attr, None) if context is not None else None
        if value and re.fullmatch(r"[A-Z0-9_]{1,40}", str(value)):
            parts.append(f"{label}={value}")
    line = "State: " + (" ".join(parts) if parts else "none")
    memory = getattr(context, "memory", None) if context is not None else None
    # 6c: the session summary as labels, AFTER the cached prefix (the prefix stays byte-identical)
    return line + (f"\n{memory}" if memory else "")


def user_content(message: str, context: Any) -> str:
    limit = int(_number("max_question_chars", 400))
    return state_line(context) + "\nMessage: " + str(message or "")[:limit]


# --------------------------------------------------------------------------
# validation: the model's JSON -> a canonical question, or None
# --------------------------------------------------------------------------

_TOPIC = re.compile(r"[A-Za-z][A-Za-z0-9 \-]{0,39}")


def validate(data: Any) -> Routed | None:
    """Only a catalogue tool with catalogue values survives. Anything else is None."""
    if not isinstance(data, dict):
        return None
    tools = _tools()
    tool = str(data.get("t") or data.get("tool") or "").strip()
    spec = tools.get(tool)
    if spec is None:
        return None
    raw = data.get("a") if isinstance(data.get("a"), dict) else (
        data.get("args") if isinstance(data.get("args"), dict) else {})
    allowed = spec.get("args") or {}
    args: dict[str, str] = {}
    for key, value in raw.items():
        key = str(key)
        if key not in allowed or value in (None, ""):
            continue                                        # unknown or empty argument: dropped
        values = allowed[key]
        text = str(value).strip()
        if isinstance(values, list):
            upper = text.upper().replace(" ", "_")
            if upper not in {str(v) for v in values}:
                return None                                 # a value outside the catalogue
            args[key] = upper
        else:
            if not _TOPIC.fullmatch(text) or len(text.split()) > 4:
                return None                                 # free text stays a short plain term
            args[key] = text
    if spec.get("intent"):
        return Routed(tool=tool, args=args, intent=str(spec["intent"]))
    question = None
    for arg, table in (spec.get("by_value") or {}).items():
        if arg in args and isinstance(table, dict):
            question = table.get(args[arg])
    if question is None and "CO_APPLICANT" in args.values() and spec.get("co_applicant_question"):
        question = spec["co_applicant_question"]
    question = question or spec.get("question")
    if not question:
        return None
    # "{arg}" -> its value: a catalogue value as words ("DRIVING_LICENCE" -> "driving licence"; PAN / ITR kept)
    for key, value in args.items():
        label = value if (not isinstance(allowed.get(key), list) or value in ("PAN", "ITR")) \
            else value.replace("_", " ").lower()
        question = question.replace("{" + key + "}", label)
    if re.search(r"\{[a-z_]+\}", question):
        return None                                         # a template slot the model did not fill
    return Routed(tool=tool, args=args, question=question)


# --------------------------------------------------------------------------
# replies are not questions (step 6b-tune; 6d replaces this with real reading)
# --------------------------------------------------------------------------

_OPTION = re.compile(
    r"^(pehl[aie]|pahl[aie]|pehle|doosr[aie]|dusr[aie]|teesr[aie]|tisr[aie]|aakhri|akhri|first|second|third|"
    r"last|one|two|three|[1-9]|option\s*[1-9])(\s+(wala|wali|wale|vala|vali|one|option|number))?$")
_REPLIES = frozenset({"rehne do", "rehne de", "rehne dijiye", "chhodo", "chodo", "chhod do", "theek hai",
                      "thik hai", "ok", "okay", "haan", "ha", "haa", "han", "nahi", "nahin", "na", "no", "yes",
                      "abhi nahi", "baad mein", "mat karo", "kar do", "chalega", "bilkul", "sure", "hmm ok"})


def reply_like(message: str) -> bool:
    """
    A message that is only a yes / no / acknowledgement / option pick ("haan",
    "nahi rehne do", "pehla wala", "2", "ok"). Measured 2026-10-07: the router
    turned "nahi rehne do" into NEXT_ACTION and "pehla wala" into pending
    documents. Until 6d reads them against the bot's own question, they never
    reach the model: the caller asks its one clarifying question. EXACT matches
    only -- a typo-tolerant match here could swallow a real question.
    """
    text = normalise(message)
    if not text or len(text.split()) > 4:
        return False
    if _OPTION.match(text) or text in _REPLIES:
        return True
    words = text.split()
    if all(w in _REPLIES or w in ("ji", "bhai", "please", "pls") for w in words):
        return True                                         # "haan ji", "ok bhai", "nahi please"
    try:
        from app.agents.applicant.copilot.conversation import state as _state

        vocab = _state._vocab()
        phrases = {normalise(p) for kind in ("AFFIRM", "NEGATE", "ACK", "CONFIRM_SOFT", "NEITHER")
                   for p in vocab.get(kind, [])}
    except Exception:  # noqa: BLE001
        phrases = set()
    if text in phrases:
        return True
    # a reply made of two known replies: "nahi rehne do", "haan theek hai", "ok kar do"
    return any(text[:i].strip() in (_REPLIES | phrases) and text[i:].strip() in (_REPLIES | phrases)
               for i in range(1, len(text)) if text[i] == " ")


def _json_only(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


# --------------------------------------------------------------------------
# the decision cache (in process; no case data in key or value)
# --------------------------------------------------------------------------

from app.agents.applicant.copilot.caching import TTLCache, settings_from

DECISIONS = TTLCache(settings_from("router", "decision_cache"))
STATS = {"calls": 0}


def normalise(message: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(message or "").lower())).strip()


def _cache_key(message: str, context: Any) -> tuple:
    return (normalise(message), state_line(context), hash(system_prefix()))


def clear_cache() -> None:
    DECISIONS.clear()
    STATS["calls"] = 0


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------

def _flag(name: str) -> bool:
    return bool(_config().get(name, False))


def prepare(message: str) -> str:
    """6b-tune-2 NORMALISE (config `normalise`): the app's typo pass + the router's spelling map."""
    if not _flag("normalise"):
        return message
    try:
        from app.agents.applicant import normalize

        text = normalize.normalise(message).text or message
    except Exception:  # noqa: BLE001
        text = message
    spelling = {str(k).lower(): str(v) for k, v in (_config().get("spelling") or {}).items()}
    if spelling:
        text = " ".join(spelling.get(w.lower(), w) for w in text.split())
    return text


#: document words -> catalogue value; party words (explicit wording wins over an example's args)
_DOC_WORDS = (("PAN", r"\bpan\b"), ("AADHAAR", r"\baadh?aa?r\b"), ("DRIVING_LICENCE", r"\b(dl|driving\s+licen[cs]e)\b"),
              ("VOTER_ID", r"\bvoter\b"), ("PASSPORT", r"\bpassport\b"), ("SALARY_SLIP", r"\bsalary\s+slip\b"),
              ("BANK_STATEMENT", r"\bbank\s+statement\b"), ("ITR", r"\bitr\b"))
_CO_WORDS = re.compile(r"\b(co[\s-]?applicant|coapp|wife|husband|patni|pati|saath\s+wale)\b", re.I)


def _args_from_text(tool: str, message: str, base: dict, context: Any) -> dict:
    allowed = (_tools().get(tool) or {}).get("args") or {}
    args = {k: v for k, v in (base or {}).items() if k in allowed}
    party_key = next((k for k, v in allowed.items() if isinstance(v, list) and "CO_APPLICANT" in v), None)
    doc_key = next((k for k, v in allowed.items() if isinstance(v, list) and "PAN" in v), None)
    if party_key:
        if _CO_WORDS.search(message):
            args[party_key] = "CO_APPLICANT"
        elif re.search(r"\b(mera|meri|mere|my|mine)\b", message, re.I) and "party" not in (base or {}):
            args.pop(party_key, None)
    if doc_key:
        found = next((value for value, pattern in _DOC_WORDS if re.search(pattern, message, re.I)), None)
        if found:
            args[doc_key] = found
    return args


def _routed_from_match(match: Any, message: str, context: Any) -> "Routed | None":
    keys = ("tool", "args") if _readable() else ("t", "a")
    routed = validate({keys[0]: match.tool, keys[1]: _args_from_text(match.tool, message, match.args, context)})
    if routed is not None:
        routed.source = "EMBEDDING"
    return routed


async def route(message: str, context: Any = None, *, generator: Any = None,
                case_labels: str | None = None) -> tuple[Routed | None, dict[str, Any]]:
    """
    The router. Returns (choice or None, trace). Never raises.

    Order (each step configurable in applicant_agent.yaml: chatbot.router):
      reply guard -> normalise -> EXAMPLE BANK (no LLM when close enough) -> decision
      cache -> Qwen (single-step or hierarchical, JSON or schema-constrained) ->
      AGREEMENT (bank vs Qwen disagree -> both offered as a one-tap question).

    `generator(prefix, content, timeout, **kw) -> dict` (Ollama's /api/chat body) is
    injectable for tests; the default posts to Ollama.
    """
    trace: dict[str, Any] = {"consulted": False, "status": "SKIPPED", "ms": 0.0}
    if not enabled():
        trace["status"] = "DISABLED"
        return None, trace
    if reply_like(message):
        trace["status"] = "REPLY_LIKE"                      # a yes / no / option pick: asked back, never routed
        return None, trace
    text = prepare(message)
    last_intent = getattr(context, "last_intent", None) if context is not None else None

    # THE EXAMPLE BANK: the nearest labelled examples, milliseconds, no model
    candidate = None
    from app.agents.applicant.copilot.semantics import embedding_router

    if embedding_router.enabled():
        started = time.perf_counter()
        try:
            match = embedding_router.bank().match(text, last_intent)
        except Exception as exc:  # noqa: BLE001 - the bank failing leaves the model
            logger.info("copilot_router bank skipped (%s)", type(exc).__name__)
            match = None
        trace["bank_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if match is not None:
            trace.update(bank_tool=match.tool, bank_score=match.score)
            candidate = _routed_from_match(match, text, context)
            if candidate is not None and embedding_router.confident(match):
                trace.update(status="EMBEDDING", tool=candidate.tool)
                _log(trace)
                return candidate, trace

    key = _cache_key(text, context)
    cached = DECISIONS.get(key)
    if cached is not None:
        routed = validate(cached)
        trace.update(status="CACHED" if routed else "INVALID", tool=routed.tool if routed else None)
        return routed, trace
    if generator is None:
        from app.llm import availability
        from app.llm.memory import free_gb

        free = free_gb()
        if free is not None and free < min_free_ram_gb():
            trace.update(status="LOW_MEMORY", free_gb=round(free, 2))
            logger.warning("copilot_router skipped: free memory %.2f GB < %.2f GB", free, min_free_ram_gb())
            return None, trace
        if not availability.provider_reachable():
            trace["status"] = "UNAVAILABLE"
            return None, trace
        generator = _ollama_chat

    started = time.perf_counter()
    trace["consulted"] = True
    content = user_content(text, context) + (f"\nCase: {case_labels}" if case_labels and _flag("case_labels") else "")
    if _flag("hierarchical"):
        routed, status = await _hierarchical(text, content, context, generator, trace)
    else:
        data, status = await _ask(generator, system_prefix(), content, trace,
                                  schema=_schema() if _flag("constrained") else None)
        routed = validate(data) if status == "OK" else None
        status = "OK" if routed else ("INVALID" if status == "OK" else status)
    trace["ms"] = round((time.perf_counter() - started) * 1000, 2)
    if status in ("TIMEOUT",) or status.startswith("ERROR"):
        trace["status"] = status
        _log(trace)
        return None, trace

    # AGREEMENT: the bank had a (weaker) opinion and the model chose differently -> ask, do not guess
    if _flag("agreement") and candidate is not None and routed is not None and candidate.tool != routed.tool:
        trace.update(status="DISAGREE", tool=None, options=[routed.tool, candidate.tool])
        _log(trace)
        return Routed(tool="clarify", options=[routed, candidate], source="AGREEMENT"), trace
    trace.update(status="OK" if routed else "INVALID", tool=routed.tool if routed else None)
    if routed is not None:
        DECISIONS.put(key, {"t": routed.tool, "a": routed.args})
    _log(trace)
    return routed, trace


async def _ask(generator: Any, prefix: str, content: str, trace: dict, *,
               schema: dict | None = None) -> tuple[dict | None, str]:
    """One model call. Returns (parsed JSON or None, OK | INVALID | TIMEOUT | ERROR:x)."""
    import asyncio

    limit = timeout_seconds()
    STATS["calls"] += 1
    try:
        call = generator(prefix, content, limit, schema=schema) if schema is not None \
            else generator(prefix, content, limit)
        body = await asyncio.wait_for(call, timeout=limit)
    except (asyncio.TimeoutError, TimeoutError):
        return None, "TIMEOUT"
    except Exception as exc:  # noqa: BLE001 - a failed call is a clarification, not an error
        return None, f"ERROR:{type(exc).__name__}"
    body = body if isinstance(body, dict) else {}
    if generator is _ollama_chat:
        from app.llm import keep_warm

        keep_warm.mark(True)                                # an answered call: the model is loaded
    # PROOF THE PROMPT CACHE WORKS: tokens Ollama evaluated, and how long that took
    for out_key, in_key in (("prompt_tokens", "prompt_eval_count"), ("load_ms", "load_duration"),
                            ("prompt_ms", "prompt_eval_duration"), ("gen_ms", "eval_duration")):
        if body.get(in_key) is not None:
            value = body[in_key]
            trace[out_key] = round(trace.get(out_key, 0) + (value / 1e6 if out_key.endswith("_ms") else value), 1)
    text = str((body.get("message") or {}).get("content") or "")
    try:
        return json.loads(_json_only(text)), "OK"
    except (TypeError, ValueError):
        return None, "OK"


def _schema(tools: list[str] | None = None) -> dict:
    """6b-tune-2 CONSTRAINED OUTPUT: a JSON schema whose enums are the catalogue, so invalid output cannot be produced."""
    catalogue = _tools()
    names = tools or list(catalogue)
    arg_props: dict[str, Any] = {}
    for name in names:
        for arg, values in ((catalogue.get(name) or {}).get("args") or {}).items():
            arg_props[arg] = {"type": "string", "enum": list(values)} if isinstance(values, list) \
                else {"type": "string", "maxLength": 40}
    keys = ("tool", "args") if _readable() else ("t", "a")
    return {"type": "object", "required": [keys[0]],
            "properties": {keys[0]: {"type": "string", "enum": names},
                           keys[1]: {"type": "object", "properties": arg_props}}}


#: 6b-tune-2 HIERARCHICAL: category first (config `categories`), then the tool within it
_DEFAULT_CATEGORIES = {
    "status": ["case_status", "next_action", "loan_terms", "list_cases"],
    "documents": ["documents_needing_action", "document_details"],
    "kyc": ["kyc_result"],
    "knowledge": ["knowledge"],
    "out_of_scope": ["out_of_scope", "refuse"],
}


async def _hierarchical(text: str, content: str, context: Any, generator: Any, trace: dict) -> tuple[Any, str]:
    categories = _config().get("categories") or _DEFAULT_CATEGORIES
    prefix = (_SYSTEM + "\nPick the CATEGORY of the user's latest message: "
              + "; ".join(f"{c} ({', '.join(t)})" for c, t in categories.items())
              + '\nReply ONLY with JSON: {"category": "<category>"}')
    schema = {"type": "object", "required": ["category"],
              "properties": {"category": {"type": "string", "enum": list(categories)}}}
    data, status = await _ask(generator, prefix, content, trace, schema=schema)
    if status != "OK" or not isinstance(data, dict) or data.get("category") not in categories:
        return None, "INVALID" if status == "OK" else status
    tools = list(categories[data["category"]])
    trace["category"] = data["category"]
    if len(tools) == 1:
        keys = ("tool", "args") if _readable() else ("t", "a")
        routed = validate({keys[0]: tools[0], keys[1]: _args_from_text(tools[0], text, {}, context)})
        return routed, "OK" if routed else "INVALID"
    from app.agents.applicant.copilot.semantics import embedding_router

    if embedding_router.enabled():                          # the bank picks within the category
        match = embedding_router.bank().match(text, getattr(context, "last_intent", None), tools=set(tools))
        if match is not None:
            routed = _routed_from_match(match, text, context)
            return routed, "OK" if routed else "INVALID"
    data, status = await _ask(generator, system_prefix(), content, trace, schema=_schema(tools))
    routed = validate(data) if status == "OK" else None
    return routed, ("OK" if routed else ("INVALID" if status == "OK" else status))


def _log(trace: dict[str, Any]) -> None:
    # EVERY CALL'S LATENCY, for the step 8 p50 / p95 (no message text, no case data)
    logger.info("copilot_router status=%s tool=%s ms=%s prompt_tokens=%s load_ms=%s",
                trace.get("status"), trace.get("tool"), trace.get("ms"),
                trace.get("prompt_tokens"), trace.get("load_ms"))
    try:
        from app.llm import trace as _llm_trace
        from app.llm.config import ollama_model

        _llm_trace.record({"caller": f"{__name__}.route", "model": ollama_model(), "host": "local",
                           "prompt_chars": len(system_prefix()), "max_tokens": int(_number("num_predict", 40)),
                           "outcome": "OK" if str(trace.get("status")) in ("OK", "INVALID") else str(trace.get("status")),
                           "ms": trace.get("ms"), "output_chars": 0, "used": trace.get("status") == "OK"})
    except Exception:  # noqa: BLE001 - the ledger is observability, never a failure
        pass


def router_model() -> str:
    """The router's model: config `model`, else the shared one (OLLAMA_MODEL)."""
    from app.llm.config import ollama_model

    return str(_config().get("model") or ollama_model())


async def _ollama_chat(prefix: str, content: str, timeout: float, schema: dict | None = None) -> dict:
    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, with_num_ctx

    model = router_model()
    body = {
        "model": model,
        "messages": [{"role": "system", "content": prefix}, {"role": "user", "content": content}],
        # a JSON schema makes an out-of-catalogue answer impossible (Ollama structured output)
        "stream": False, "format": schema or "json", "keep_alive": keep_alive(),
        "options": with_num_ctx({"temperature": 0.0, "num_predict": int(_number("num_predict", 40))}),
    }
    if model.startswith("qwen3"):
        body["think"] = False                               # a tool choice, not a reasoning trace
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body)
        response.raise_for_status()
        return response.json()


__all__ = ["DECISIONS", "DEPRECATED_FLAG", "FLAG", "Routed", "STATS", "clear_cache", "deprecated_flag_in_use",
           "enabled", "normalise", "prepare", "route", "router_model", "state_line", "system_prefix",
           "timeout_seconds", "user_content", "validate"]
