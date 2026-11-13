"""
QWEN CALLS THE TOOLS (owner, 2026-10-13: "tool bhi Qwen hi call karega, kuch hardcode mat karna ... real jaisa convo").

One officer message on the FOS copilot ->
    1. Qwen reads it WITH the last few exchanges of this chat and either calls tools (native tool calling) or asks one
       short follow-up question itself;
    2. every tool runs through the existing, checked paths -- a case tool is the engine's own lookup for this case
       (ownership via workspace.authorize, exactly like the bounded planner), `accepted_documents` reads the product
       checklist, `handbook` the approved FOS handbook answer;
    3. Qwen writes ONE natural opening line in the officer's language (answering/polish.py lead: no number, no id, no
       invented step), and the tools' facts follow as clean points, byte for byte.

FACTS ARE NEVER WRITTEN BY THE MODEL. It chooses tools and words one line; every figure, name and status is a tool's.
LATENCY (measured 2026-10-13, qwen3 4B instruct, CPU): a tool choice ~2.5-4.5 s (the tool list is a byte-identical
prompt prefix, so Ollama reuses it), the opener ~1.5-2.5 s. FALLBACKS, NEVER AN ERROR: off, Ollama down / slow / short
of memory, no valid call, every tool failing -> None, and the existing pipeline answers the turn.
Config: copilot_reply.yaml `agent`.
"""
from __future__ import annotations

from app.llm import turn_budget as _tb

import asyncio
import contextvars
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

STATS: dict[str, int] = {"asked": 0, "answered": 0, "followup": 0, "fallback": 0}
Lookup = Callable[[str], Awaitable[dict[str, Any]]]

_PARTIES = ("APPLICANT", "CO_APPLICANT")

#: THE AGENT HAS USED THIS TURN'S MODEL TIME (2026-10-13, measured): on one CPU with one Ollama slot, a second model
#: call with a DIFFERENT prompt (the meaning chooser, the router, polish) evicts the agent's cached tool list and costs
#: seconds. When the agent ran, the pipeline that follows answers from rules + embeddings only (meaning / router /
#: polish read this and skip the model).
SPENT: "contextvars.ContextVar[bool]" = contextvars.ContextVar("qwen_agent_spent", default=False)


def spent() -> bool:
    return SPENT.get()


#: WHAT THIS TURN READ AS HISTORY (diagnostic, 2026-10-18): (source, turns) of the last history() call -- "shared"
#: (the conversation store), "process" (this process's fallback) or "none". Counts only, never text; the turn log
#: writes it, so a restart can be shown to continue from the stored history.
HISTORY_READ: "contextvars.ContextVar[tuple[str, int] | None]" = contextvars.ContextVar("qwen_agent_history_read",
                                                                                        default=None)
_MEMORY: "OrderedDict[str, list[dict[str, str]]]" = OrderedDict()
_LOCK = threading.Lock()


def cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.answering import contract

    return contract.cfg().get("agent") or {}


def enabled() -> bool:
    import os

    flag = (os.getenv("COPILOT_QWEN_AGENT") or "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


# --------------------------------------------------------------------------
# the conversation this chat has had (the officer's words + the bot's opener; never case data)
# --------------------------------------------------------------------------

#: SHARED ACROSS WORKERS (2026-10-18, audit P1-A): the history is a row of the conversation store's own table
#: (conversation_state, the same Postgres the conversation state lives in), conversation id "agent:" + the chat id,
#: under the caller's subject -- every worker and a restart read the same turns; one caller never reads another's.
#: The row is MASKED then ENCRYPTED (crypto.seal_value, as chat history) and expires / is pruned with the
#: conversation (state.ttl_seconds). The process dict stays only as the fallback when the store cannot be read.
_ROW = "agent:"
_SHARED_LOCK = threading.Lock()


def _shared_key(chat_key: Any) -> tuple[str, str] | None:
    """(subject, chat id) of a chat ref (subject, chat_id); None for a legacy string key (process memory only)."""
    if isinstance(chat_key, (tuple, list)) and len(chat_key) == 2 and chat_key[0]:
        return str(chat_key[0]), str(chat_key[1] or "")
    return None


def _local_key(chat_key: Any) -> str | None:
    if isinstance(chat_key, (tuple, list)):
        return "|".join(str(k) for k in chat_key) if chat_key and chat_key[0] else None
    return chat_key or None


def _shared_on() -> bool:
    return bool(cfg().get("shared_history", True))


def _read_shared(chat_key: Any) -> list[dict[str, Any]] | None:
    """The stored turns (oldest first); [] when none / expired; None when not shared or the store cannot be read."""
    key = _shared_key(chat_key)
    if key is None or not _shared_on():
        return None
    try:
        from app.agents.applicant.copilot.conversation import state as conv
        from app.store import crypto, get_repository

        found = get_repository().get_conversation(key[0], _ROW + key[1])
        if not found:
            return []
        text, at = found
        if time.time() - float(at) > conv.ttl_seconds():
            return []
        data = json.loads(crypto.open_value(text) or "{}")
        turns = data.get("turns") if isinstance(data, dict) else None
        return [t for t in turns if isinstance(t, dict) and t.get("role") in ("user", "assistant")
                and isinstance(t.get("content"), str)] if isinstance(turns, list) else []
    except Exception:  # noqa: BLE001 - no shared history this turn: the process memory answers, never a failed turn
        logger.debug("qwen agent: shared history not read", exc_info=True)
        return None


def _write_shared(chat_key: Any, pair: list[dict[str, Any]], keep: int, request_id: str | None) -> None:
    key = _shared_key(chat_key)
    if key is None or not _shared_on():
        return
    try:
        from app.store import crypto, get_repository

        with _SHARED_LOCK:
            current = _read_shared(chat_key)
            if current is None:
                return                                   # the store is down: the process memory keeps this turn
            if request_id and any(t.get("rid") == request_id for t in current):
                return                                   # a retried request: the exchange is stored once
            turns = (current + pair)[-keep:]
            get_repository().put_conversation(key[0], _ROW + key[1],
                                              crypto.seal_value(json.dumps({"v": 1, "turns": turns})), time.time())
    except Exception:  # noqa: BLE001 - the history is a convenience, never the answer
        logger.debug("qwen agent: shared history not stored", exc_info=True)


def history(chat_key: Any) -> list[dict[str, str]]:
    """The last exchanges of this chat for Qwen: the SHARED row when the chat ref is (subject, chat_id) and the store
    answers, else this process's memory. Only role + content reach the model."""
    local = _local_key(chat_key)
    if not local:
        HISTORY_READ.set(("none", 0))
        return []
    shared = _read_shared(chat_key)
    source = "shared"
    if shared is None:
        source = "process"
        with _LOCK:
            shared = list(_MEMORY.get(local) or [])
    HISTORY_READ.set((source, len(shared)))
    return [{"role": t["role"], "content": t["content"]} for t in shared]


def remember(chat_key: Any, question: str, said: str, request_id: str | None = None) -> None:
    local = _local_key(chat_key)
    if not local:
        return
    from app.agents.applicant.copilot.semantics import meaning

    question, said = meaning._masked(str(question or "")), meaning._masked(str(said or ""))
    keep = int(cfg().get("history_exchanges", 3)) * 2
    chars = int(cfg().get("history_chars", 300))     # each new history token is read on the next turn: keep it short
    pair = [{"role": "user", "content": question[:chars], "rid": request_id},
            {"role": "assistant", "content": said[:chars], "rid": request_id}]
    with _LOCK:
        turns = _MEMORY.pop(local, None) or []
        if not (request_id and any(t.get("rid") == request_id for t in turns)):
            turns = turns + pair
        _MEMORY[local] = turns[-keep:]
        while len(_MEMORY) > int(cfg().get("max_chats", 500)):
            _MEMORY.popitem(last=False)
    _write_shared(chat_key, pair, keep, request_id)


# --------------------------------------------------------------------------
# the tools Qwen may call (names + short words from config; byte-identical every call)
# --------------------------------------------------------------------------

def _case_tools() -> dict[str, dict[str, Any]]:
    """The READ-ONLY case tools: the bounded planner's allow-list (catalogue case intents with a canonical question)."""
    from app.agents.applicant.copilot.capabilities import fallback_planner

    tools = fallback_planner._tools()
    keep = [str(t) for t in cfg().get("case_tools") or []]
    return {name: tools[name] for name in keep if name in tools} if keep else tools


def tool_schemas(case_open: bool) -> list[dict[str, Any]]:
    from app.agents.applicant.copilot.capabilities import fallback_planner

    hints = fallback_planner.cfg().get("hints") or {}
    party = {"party": {"type": "string", "enum": list(_PARTIES), "description": "whose: applicant or co-applicant"}}
    out = []
    if case_open:
        for name, spec in _case_tools().items():
            props = dict(party)
            required: list[str] = []
            if spec.get("needs") == "document":
                props["document"] = {"type": "string", "description": "the document, e.g. PAN, Salary Slip"}
                required.append("document")
            out.append({"type": "function", "function": {
                "name": name, "description": str(hints.get(name) or spec.get("description") or name),
                "parameters": {"type": "object", "properties": props, "required": required}}})
    for name, spec in (cfg().get("extra_tools") or {}).items():
        props = {k: {"type": "string", "description": str(v)} for k, v in (spec.get("args") or {}).items()}
        out.append({"type": "function", "function": {
            "name": name, "description": str(spec.get("description") or name),
            "parameters": {"type": "object", "properties": props, "required": list(spec.get("required") or [])}}})
    return out


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

def compact() -> bool:
    """THE COMPACT CHOICE (2026-10-14, measured interleaved on the real prompt + history: native tool calling 3.5-5.2 s,
    ~1410 prompt / 20-38 output tokens; compact 1.8-2.6 s, ~510 / 15): the tools are listed in the system text and
    Qwen answers one short JSON object -- the same choice, a third of the prompt, half the generated tokens."""
    return str(cfg().get("choice") or "native").lower() == "compact"


def _system(case_open: bool) -> str:
    system = str(cfg().get("instructions") or "").strip()
    if case_open:
        system += "\n" + str(cfg().get("case_open_note") or "")
    if compact():
        listed = "\n".join(f"- {t['function']['name']}: {t['function']['description']}"
                           for t in tool_schemas(case_open))
        system += "\n\nTOOLS:\n" + listed + "\n\n" + str(cfg().get("compact_reply") or "").strip()
    return system


def _product_names() -> dict[str, list[str]]:
    """PRODUCT CODE -> the ways it is written ("Home Loan", "home loan", "HL"), from the configured products."""
    from app.agents.applicant import config as agent_config
    from app.agents.applicant.copilot.answering.answer import _readable

    out: dict[str, list[str]] = {}
    for code in agent_config.products() or []:
        if str(code).lower() == "default":
            continue
        readable = _readable(code)
        initials = "".join(w[0] for w in readable.split() if w).upper()
        out[str(code).upper()] = [readable.lower(), str(code).replace("_", " ").lower()] + (
            [initials.lower()] if len(initials) >= 2 else [])
    return out


def _other_product(text: str, product: str | None) -> bool:
    """A clarifying question that names a product this case is NOT (2026-10-14: "the home loan application" on a
    Personal Loan case) -- an invented option: not used (the case's own options question goes out instead)."""
    if not product:
        return False                     # no case open: a question about products is a fair question
    said = " " + re.sub(r"[^\w\s]", " ", str(text or "").lower()) + " "
    mine = str(product or "").upper()
    return any(code != mine and any(f" {name} " in said for name in names)
               for code, names in _product_names().items())


def _compact_calls(content: str, case_open: bool, message: str) -> tuple[list[dict[str, Any]], str]:
    """The compact JSON -> (calls in the native shape, a follow-up question or ""). Anything else: ([], "")."""
    try:
        data = json.loads(content or "{}")
    except ValueError:
        return [], ""
    if not isinstance(data, dict):
        return [], ""
    if data.get("q") and not data.get("t"):
        return [], str(data["q"])
    if data.get("s") and not data.get("t"):
        return [], "\x00" + str(data["s"])            # small talk: Qwen's own one line (marked, checked by answer)
    schemas = {t["function"]["name"]: t["function"]["parameters"].get("properties") or {}
               for t in tool_schemas(case_open)}
    calls = []
    # "t" / "d", and "t2" / "d2" for a second question in the same message ("is pan verified? and bank statement?")
    for tool_key, detail_key in (("t", "d"), ("t2", "d2")):
        name = str(data.get(tool_key) or "").strip()
        if name not in schemas:
            continue
        call = _one_call(name, schemas[name], str(data.get(detail_key) or "").replace("_", " ").strip(), message)
        if call not in calls:
            calls.append(call)
    return calls, ""


def _one_call(name: str, props: dict[str, Any], detail: str, message: str) -> dict[str, Any]:
    from app.agents.applicant.copilot.semantics import meaning

    args: dict[str, Any] = {}
    if "document" in props:
        args["document"] = detail
    if "requirement" in props:
        args["requirement"] = detail
    if "question" in props:
        args["question"] = message                      # the handbook reads the officer's own words
    if "party" in props:
        # the co-applicant only when the officer NAMED one: "the other one" is another item, never another person
        # (the party is read from the message itself: Qwen generates no party key -- ~6 tokens, ~0.45 s less)
        args["party"] = "CO_APPLICANT" if meaning.party_in(message) == "CO_APPLICANT" else "APPLICANT"
    if detail and not ({"document", "requirement", "question"} & set(props)):
        args["field"] = detail                          # "how much" -> loan amount: that one recorded field
    return {"function": {"name": name, "arguments": args}}


async def _chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]], timeout: float, *,
                num_predict: int | None = None, temperature: float = 0.0) -> dict[str, Any]:
    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    body = {"model": str(cfg().get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "messages": messages,
            "options": with_num_ctx({"temperature": temperature,
                                     "num_predict": int(num_predict or cfg().get("num_predict", 64))})}
    if compact():
        body["format"] = "json"
    else:
        body["tools"] = tools
    if cfg().get("think") is False:
        body["think"] = False                   # a thinking model (qwen3 1.7b) answers at once, no reasoning tokens
    _tb.check("agent")      # an earlier model call of this turn failed: answer without it (app/llm/turn_budget.py)
    import time as _time

    from app.llm import trace as _llm_trace

    # THE TURN'S MODEL LEDGER (2026-11-02 audit): this call bypassed app/llm/provider.py, so observability.model_ledger
    # said 0 calls while the agent timed out -- every call is recorded with its outcome, success or not
    entry = {"caller": f"{__name__}._chat", "model": body["model"], "host": "local",
             "prompt_chars": sum(len(str(m.get("content") or "")) for m in messages),
             "max_tokens": body["options"].get("num_predict")}
    started = _time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
            response = await asyncio.wait_for(_tb.guarded(client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body), "agent"), timeout)
            response.raise_for_status()
            message = (response.json() or {}).get("message") or {}
        entry.update(outcome="OK", output_chars=len(str(message.get("content") or "")))
        return message
    except (asyncio.TimeoutError, TimeoutError):
        entry["outcome"] = "TIMEOUT"
        raise
    except Exception:
        entry["outcome"] = "ERROR"
        raise
    finally:
        entry["ms"] = round((_time.perf_counter() - started) * 1000, 1)
        _llm_trace.record(entry)


def _ready() -> bool:
    from app.llm import availability
    from app.llm.memory import free_gb_for_model as free_gb

    free = free_gb()
    return (free is None or free >= float(cfg().get("min_free_ram_gb", 1.5))) and availability.provider_reachable()


# --------------------------------------------------------------------------
# running a call (every one validated; a bad call is dropped, never guessed)
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    """A tool's answer without its buttons / suggestion links."""
    lines = [line for line in str(text or "").split("\n") if not re.search(r"\]\((action|ask):", line)]
    return "\n".join(lines).strip()


def _pretty(code: str) -> str:
    """PAN / VOTER_ID / ADDRESS_PROOF -> PAN / Voter ID / Address Proof."""
    words = str(code or "").replace("_", " ").title().split()
    return " ".join({"Pan": "PAN", "Id": "ID", "Itr": "ITR", "Gst": "GST"}.get(w, w) for w in words)


def accepted_documents(requirement: str, product: str | None) -> str | None:
    """What the product's checklist accepts for a requirement ("address proof", or a document named in it); every row
    when the requirement matches none -- the facts the opener answers "chalega?" from."""
    from app.agents.applicant import config
    from app.agents.applicant.copilot.semantics import meaning

    words = set(re.findall(r"[a-z]+", str(requirement or "").lower())) - {"proof", "document", "documents", "ke", "liye"}
    named = (meaning.document_in(requirement) or "").upper().replace(" ", "_")
    rows, matched = [], []
    for row in config.checklist_for(product):
        slot = str(row.get("slot") or "")
        accepts = [str(k).upper() for k in row.get("accepts") or []]
        line = f"{_pretty(slot)}: " + ", ".join(_pretty(k) for k in accepts)
        rows.append(line)
        if (set(slot.lower().split("_")) - {"proof"}) & words or (named and named in accepts):
            matched.append(line)
    chosen = matched or rows
    if not chosen:
        return None
    label = str(product or "this product").replace("_", " ").title()
    facts = f"Accepted documents ({label}):\n" + "\n".join(f"- {r}" for r in chosen)
    verdict = _verdict(requirement, product)
    return f"{verdict}\n\n{facts}" if verdict else facts


def _verdict(requirement: str, product: str | None) -> str | None:
    """"Can he give an electricity bill instead?" -- one DIRECT line from the checklist: yes (and as what) / no.
    None when the question names no single known document (config phrases.document_names)."""
    from app.agents.applicant import config
    from app.agents.applicant.copilot.answering import language_lock
    from app.agents.applicant.copilot.capabilities import general

    said = general._norm(requirement)
    names = (general.cfg().get("phrases") or {}).get("document_names") or {}
    found = [code for code, aliases in names.items() if any(general._norm(a) in said for a in aliases or [])]
    if len(found) != 1:
        return None
    return general.acceptance(str(found[0]), product, language_lock.current() or "en")


async def _run(call: dict[str, Any], *, case_id: str | None, product: str | None, claims: dict[str, Any],
               lookup: Lookup, handbook: Callable[[str], Awaitable[str | None]]) -> tuple[str, str] | None:
    """(tool name, its facts) for one call, or None when it is not allowed / answers nothing."""
    from app.agents.applicant.copilot.capabilities import workspace
    from app.agents.applicant.copilot.semantics import meaning
    from app.security import access

    fn = (call or {}).get("function") or {}
    name = str(fn.get("name") or "").strip()
    args = fn.get("arguments") if isinstance(fn.get("arguments"), dict) else {}
    tools = _case_tools()
    if name in tools:
        if not case_id:
            return None
        try:
            workspace.authorize(claims, case_id)
        except access.AccessDenied:
            return None
        party = str(args.get("party") or "").strip().upper() or None
        party = party if party in _PARTIES and party != "APPLICANT" else None
        document = None
        if tools[name].get("needs") == "document":
            document = meaning.document_in(str(args.get("document") or ""))
            if not document:
                return None
        canonical = meaning._canonical(tools[name], party, document)
        if not canonical:
            return None
        field = re.sub(r"[^\w\s]", " ", str(args.get("field") or "")).strip()
        if field and not party and name == str(cfg().get("field_tool") or "application_details"):
            # ONE RECORDED FIELD Qwen named ("how much" -> loan amount): the profile's single-field answer when the
            # engine reads that field; else the whole record as before
            from app.agents.applicant.copilot.answering import profile

            asked = profile.detect(f"what is the {field}")
            if asked is not None and str(getattr(asked, "field", "") or "") not in ("", profile.ALL):
                canonical = f"what is the {field}"
        result = await lookup(canonical)
        if str((result or {}).get("intent") or "") in ("UNKNOWN", "GUARDRAIL_BLOCKED", ""):
            return None
        text = _clean(str((result or {}).get("answer") or ""))
        return (name, text) if text else None
    if name == "case_record":
        # ASK ANYTHING ABOUT THE CASE: the masked fact sheet, the model's answer FACT-CHECKED (snapshot_qa); owner first
        if not case_id:
            return None
        from app.agents.applicant.copilot.capabilities import snapshot_qa

        try:
            workspace.authorize(claims, case_id)
        except access.AccessDenied:
            return None
        found = await snapshot_qa.answer(case_id, str(args.get("question") or ""))
        text = str((found or {}).get("answer") or "").strip()
        return (name, text) if text and (found or {}).get("status") == "ANSWERED" else None
    if name == "accepted_documents":
        text = accepted_documents(str(args.get("requirement") or ""), product)
        return (name, text) if text else None
    if name == "handbook":
        text = await handbook(str(args.get("question") or "").strip() or "")
        return (name, _clean(text)) if text else None
    return None


# --------------------------------------------------------------------------
# one turn
# --------------------------------------------------------------------------

async def answer(message: str, *, chat_key: Any, case_id: str | None, product: str | None,
                 claims: dict[str, Any], lookup: Lookup,
                 handbook: Callable[[str], Awaitable[str | None]],
                 timeout: float | None = None, must_ask: bool = False,
                 request_id: str | None = None) -> dict[str, Any] | None:
    """{"answer", "tools", "kind", "timings"} for this turn, or None (the existing pipeline answers)."""
    from app.agents.applicant.copilot.answering import polish
    from app.agents.applicant.copilot.semantics import meaning

    if not enabled() or not str(message or "").strip() or not _ready():
        return None
    from app.agents.applicant.copilot.capabilities import general as _general

    if _general._downstream_route(message, case_id):
        # A DOWNSTREAM QUESTION ("should we approve this loan?", "run an RCU check") is the router's: OUT_OF_SCOPE +
        # route_to, before any model call -- never a question back (2026-10-22: "Do you mean should we approve ...")
        return None
    STATS["asked"] += 1
    SPENT.set(True)
    started = time.perf_counter()
    timings: dict[str, float] = {}
    system = _system(bool(case_id))
    said = meaning._masked(message)
    if cfg().get("entities_hint", True):
        # THE ENTITY FRAME (semantics/entities.py): what the message names, read deterministically -- documents in
        # order, what the customer lacks, a negated status, the party, a stage -- so Qwen's choice starts from them
        from app.agents.applicant.copilot.semantics import entities

        frame = entities.extract(message).line()
        if frame:
            said += f"\n(entities: {frame})"
    if case_id and product:
        # THE OPEN CASE'S PRODUCT, so a question back never offers another one ("the home loan application" on a
        # Personal Loan case, 2026-10-14) -- after the history: the cached prompt prefix stays the same
        from app.agents.applicant.copilot.answering.answer import _readable

        said += f"\n(this case: {_readable(product)})"
    if must_ask:
        # nothing before it to resolve this short message from: Qwen asks (config agent.ask_first_note)
        said += "\n" + str(cfg().get("ask_first_note") or "")
    if cfg().get("small_talk"):
        # the part of the day, so a greeting is answered "Good evening" at 7 pm (after the history: the cached prefix
        # stays the same)
        from app.agents.applicant.copilot.answering import realtime

        salute = realtime._salutation(((realtime._cfg().get("greeting") or {}).get("time_of_day") or {}), "en")
        if salute:
            said += f"\n(now: {salute})"
    messages = [{"role": "system", "content": system}] + history(chat_key) + [{"role": "user", "content": said}]
    try:
        reply = await _chat(messages, tool_schemas(bool(case_id)),
                            float(timeout or cfg().get("choose_timeout_seconds", 8.0)))
    except Exception as exc:  # noqa: BLE001 - slow / down: the existing pipeline answers
        logger.info("qwen agent: choice failed (%s)", type(exc).__name__)
        STATS["fallback"] += 1
        return None
    timings["choose_ms"] = round((time.perf_counter() - started) * 1000, 1)
    asked = ""
    if compact():
        calls, asked = _compact_calls(str(reply.get("content") or ""), bool(case_id), message)
        reply = {"content": "", "tool_calls": calls}
    calls = list(reply.get("tool_calls") or [])[:int(cfg().get("max_calls", 2))]
    if not calls and asked.startswith("\x00"):
        # SMALL TALK IN QWEN'S OWN WORDS (owner, 2026-10-14): one short friendly line -- no figure, no id, no case word
        # (config agent.small_talk_never); anything else is not used and the pipeline answers
        line = " ".join(asked[1:].split()).strip().strip('"')
        never = [str(w).lower() for w in cfg().get("small_talk_never") or []]
        from app.agents.applicant.copilot.semantics import task as _st_task

        # ONLY WHEN THE MESSAGE IS SMALL TALK (measured 2026-10-14: Qwen once greeted "what's the status of this
        # application?"): the embeddings / the social reader must agree, else the pipeline answers the question
        social = meaning.social_reading(message) or (_st_task.enabled() and bool(_st_task.social_kind(message)))
        if (cfg().get("small_talk") and social and line and len(line) <= int(cfg().get("small_talk_max_chars", 160))
                and not re.search(r"\d", line) and not any(re.search(rf"\b{re.escape(w)}\b", line.lower())
                                                            for w in never)):
            remember(chat_key, message, line, request_id)
            return {"answer": line[:1].upper() + line[1:], "tools": [], "kind": "chat", "timings": timings}
        STATS["fallback"] += 1
        return None
    if not calls:
        # NO TOOL: Qwen asks ONE follow-up question itself -- a question with no figure in it, else not used
        text = (asked or str(reply.get("content") or "")).strip()
        text = text[:1].upper() + text[1:]
        if text and text.endswith("?") and not re.search(r"\d", text) and len(text) <= 300 \
                and not _other_product(text, product):
            STATS["followup"] += 1
            remember(chat_key, message, text, request_id)
            return {"answer": text, "tools": [], "kind": "followup", "timings": timings}
        STATS["fallback"] += 1
        if text and text.endswith("?"):
            # Qwen DID want to ask, but its question was not usable (another product, a figure): the case's own
            # options question asks instead (fos_api._agent_reply) -- never "outside what I can help with"
            return {"answer": "", "tools": [], "kind": "ask_rejected", "timings": timings}
        return None

    async def handbook_or_none(question: str) -> str | None:
        return await handbook(question or message)

    t = time.perf_counter()
    ran = await asyncio.gather(*[_run(c, case_id=case_id, product=product, claims=claims, lookup=lookup,
                                      handbook=handbook_or_none) for c in calls], return_exceptions=True)
    done = [r for r in ran if isinstance(r, tuple)]
    timings["tools_ms"] = round((time.perf_counter() - t) * 1000, 1)
    if not done:
        STATS["fallback"] += 1
        return None
    facts = "\n\n".join(polish.as_points(text) for _, text in done)
    t = time.perf_counter()
    line = None
    try:
        if not cfg().get("opener", True):
            raise LookupError("opener off")             # config agent.opener: the facts alone, one model call less
        # THE SAME CONVERSATION, CONTINUED: system + tools + history + the question are the prefix Ollama already
        # holds; only the tool results and one instruction are new -- the opener costs its own few words, not a re-read
        follow = (messages + [{"role": "assistant", "content": str(reply.get("content") or ""),
                               "tool_calls": reply.get("tool_calls")}]
                  + [{"role": "tool", "content": text[:1500]} for _, text in done]
                  + [{"role": "user", "content": str(cfg().get("opener_instruction") or "")}])
        said = await _chat(follow, tool_schemas(bool(case_id)), float(cfg().get("lead_timeout_seconds", 6.0)),
                           num_predict=int(cfg().get("opener_num_predict", 40)), temperature=0.3)
        line = str(said.get("content") or "").strip().strip('"') or None
    except LookupError:
        pass
    except Exception as exc:  # noqa: BLE001 - no opener: the facts alone
        logger.info("qwen agent: opener failed (%s)", type(exc).__name__)
    if line and not polish.lead_accepted(facts, line):
        line = None
    timings["lead_ms"] = round((time.perf_counter() - t) * 1000, 1)
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    STATS["answered"] += 1
    remember(chat_key, message, line or facts.split("\n")[0], request_id)
    return {"answer": f"{line}\n\n{facts}" if line else facts, "tools": [name for name, _ in done],
            "kind": "answer", "timings": timings, "opener": bool(line)}


def prime(case_open: bool = True) -> float | None:
    """One real tool-choice call at startup: the weights paged in and the agent's prefix (system + tools) cached."""
    import urllib.request

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    if not enabled():
        return None
    started = time.perf_counter()
    body = {"model": str(cfg().get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "messages": [{"role": "system", "content": _system(case_open)}, {"role": "user", "content": "hello"}],
            "options": with_num_ctx({"temperature": 0.0, "num_predict": 8})}
    if compact():
        body["format"] = "json"
    else:
        body["tools"] = tool_schemas(case_open)
    try:
        request = urllib.request.Request(f"{ollama_host().rstrip('/')}/api/chat", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=180).read()
    except Exception:  # noqa: BLE001 - a convenience: the first turn is just slower
        logger.info("qwen agent: prime skipped", exc_info=True)
        return None
    ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info("qwen agent: primed in %s ms", ms)
    return ms


__all__ = ["STATS", "accepted_documents", "answer", "enabled", "history", "remember", "tool_schemas"]
