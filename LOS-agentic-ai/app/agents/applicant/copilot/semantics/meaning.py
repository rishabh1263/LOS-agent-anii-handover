"""
UNDERSTANDING BY MEANING (Smart Bot plan sections 2-5, owner 2026-10-08) -- replaces "first phrase that matches wins".

    message -> embed (nomic-embed-text, ~0.1 s on CPU) -> nearest intents (catalogue examples + reviewed paraphrases)
            -> entities (the document / party named, from documents.yaml aliases and the checklist)
            -> follow-up ("and the co-applicant?", "what about PAN?", "docs"): the previous intent with the new slot
            -> DECIDE: a clear winner is taken at once (no model); an ambiguous one is decided by Qwen among the top
               candidates (small prompt, JSON, ~2-3 s, cached prefix); nothing close -> None (the old rules answer)
            -> a Decision the pipeline executes: case / process intents run their CANONICAL question through the existing
               deterministic engine (answers are never written by the model); knowledge / policy / calculator / decision
               / small talk go to the general layer; commands pass on unchanged.

WHY NOT ONE BIG MODEL CALL. Measured on this machine (2026-10-08): qwen2.5:3b reads ~100 tokens/s and writes ~14 tokens/s on
CPU -- a prompt with the case facts and passages (~2 400 tokens) takes ~25 s. So the model only CHOOSES, from a short list,
when the embedding cannot.

FALLBACKS, NEVER AN ERROR: the embedding model unreachable, the model slow / down / low memory, an invalid choice -> None
(today's rules path). Provenance is logged (masked), never shown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FLAG = "COPILOT_MEANING"
_ROOT = Path(__file__).resolve().parents[5]
_LOCK = threading.RLock()
_BANK: dict[str, Any] = {"key": None, "intents": [], "vectors": [], "texts": []}
STATS: dict[str, int] = {"asked": 0, "embedding": 0, "model": 0, "followup": 0, "none": 0, "model_failed": 0}


@dataclass
class Decision:
    kind: str                                   # case / process / knowledge / policy / decision / calculator / small_talk /
    intent: str                                 # command / out_of_scope
    canonical: str | None = None                # the question the deterministic engine answers (None: as typed)
    score: float = 0.0
    runner_up: str | None = None
    runner_up_score: float = 0.0
    decided_by: str = "embedding"               # embedding / model / followup
    party: str | None = None                    # CO_APPLICANT when named
    document: str | None = None                 # a readable document name when named
    candidates: list[tuple[str, float]] = field(default_factory=list)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

_LOADED: dict[str, tuple[float, dict[str, Any]]] = {}


def _load(name: str) -> dict[str, Any]:
    """A config file, parsed once per change (its mtime): the paraphrase bank is ~0.2 s of YAML per parse."""
    import yaml

    path = _ROOT / "app" / "config" / name
    try:
        mtime = path.stat().st_mtime
        cached = _LOADED.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _LOADED[name] = (mtime, data)
        return data
    except (OSError, ValueError):
        return {}


def catalogue() -> dict[str, Any]:
    return _load("intent_catalogue.yaml")


def enabled() -> bool:
    raw = os.getenv(FLAG)
    if raw is not None and raw.strip():
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(catalogue().get("enabled", True))


def _decide_cfg() -> dict[str, Any]:
    return catalogue().get("decide") or {}


# --------------------------------------------------------------------------
# the example bank (seed examples + reviewed paraphrases), embedded once and cached on disk
# --------------------------------------------------------------------------

def _embedder():
    """The real embedding model, or None (hashing / unreachable): meaning is then off and the rules answer."""
    from app.knowledge.embeddings import get_query_embedder, provider_name

    if provider_name() != "ollama":
        return None
    try:
        return get_query_embedder()
    except Exception:  # noqa: BLE001 - no embedder: the rules answer
        return None


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _bank(embedder) -> tuple[list[str], list[list[float]], list[str]]:
    intents = catalogue().get("intents") or {}
    extra = (_load("intent_paraphrases.yaml").get("paraphrases") or {})
    pairs = [(name, str(e)) for name, spec in intents.items() for e in spec.get("examples") or []]
    pairs += [(name, str(e)) for name, items in extra.items() if name in intents for e in items or []]
    key = hash(tuple(pairs))
    with _LOCK:
        if _BANK["key"] == key:
            return _BANK["intents"], _BANK["vectors"], _BANK["texts"]
    from app.knowledge.retriever import _cached_embed_all

    vectors = [_norm(v) for v in _cached_embed_all(embedder, [t for _, t in pairs])]
    with _LOCK:
        _BANK.update(key=key, intents=[n for n, _ in pairs], vectors=vectors, texts=[t for _, t in pairs])
    return _BANK["intents"], _BANK["vectors"], _BANK["texts"]


def rank(message: str, embedder=None) -> list[tuple[str, float]]:
    """Every intent with its score: the best cosine of its examples (k-NN max), highest first. [] without a model."""
    embedder = embedder or _embedder()
    if embedder is None:
        return []
    names, vectors, _ = _bank(embedder)
    query = _norm(embedder.embed(_clean(message)))
    best: dict[str, float] = {}
    for name, vec in zip(names, vectors):
        score = sum(a * b for a, b in zip(query, vec))
        if score > best.get(name, -1.0):
            best[name] = score
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


def _clean(message: str) -> str:
    return re.sub(r"\s+", " ", str(message or "")).strip()[:300]


# --------------------------------------------------------------------------
# entities: the document and the party named (from config, never a typed list here)
# --------------------------------------------------------------------------

def _documents() -> list[tuple[str, str]]:
    """(alias, readable name) for every configured document type and checklist slot, longest alias first."""
    from app.agents.applicant import config
    from app.agents.applicant.copilot.answering.answer import _readable

    out: list[tuple[str, str]] = []
    docs = (_load("documents.yaml").get("documents") or {})
    items = docs.items() if isinstance(docs, dict) else []
    for code, spec in items:
        name = _readable(code)
        for alias in [*(spec or {}).get("aliases", []), str(code).replace("_", " ")]:
            out.append((str(alias).lower(), name))
    for product in config.products() or []:
        for entry in config.checklist_for(product) or []:
            slot = str(entry.get("slot") or "")
            out.append((slot.replace("_", " ").lower(), _readable(slot)))
    return sorted(set(out), key=lambda p: len(p[0]), reverse=True)


def _normalised(message: str) -> str:
    """The message with the configured short forms expanded ("bank stmt" -> "bank statement", "dl" -> ...)."""
    from app.agents.applicant import normalize

    try:
        return normalize.normalise(str(message or "")).text
    except Exception:  # noqa: BLE001 - the normaliser is a convenience
        return str(message or "")


def document_in(message: str) -> str | None:
    said = " " + re.sub(r"[^\w\s]", " ", _normalised(message).lower()) + " "
    for alias, name in _documents():
        if alias and f" {alias} " in said:
            return name
    return None


def party_in(message: str) -> str | None:
    said = str(message or "").lower()
    words = (catalogue().get("entities") or {}).get("co_applicant_words") or ["co-applicant", "co applicant",
                                                                              "coapplicant", "co-borrower"]
    return "CO_APPLICANT" if any(w in said for w in words) else None


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def _canonical(spec: dict[str, Any], party: str | None, document: str | None) -> str | None:
    if spec.get("needs") == "document" and not document:
        return spec.get("canonical_without_slot") or spec.get("canonical")
    text = spec.get("canonical_party") if party == "CO_APPLICANT" and spec.get("canonical_party") else spec.get("canonical")
    if not text:
        return None
    text = str(text).replace("{document}", document or "")
    if party == "CO_APPLICANT" and "co-applicant" not in text.lower() and spec.get("party_aware"):
        text = text.rstrip("?") + " for the co-applicant?"
    return text


def _decision(name: str, score: float, ranked: list[tuple[str, float]], by: str, message: str) -> Decision:
    spec = (catalogue().get("intents") or {}).get(name) or {}
    party, document = party_in(message), document_in(message)
    nxt = next(((n, s) for n, s in ranked if n != name), (None, 0.0))
    return Decision(kind=str(spec.get("kind") or "knowledge"), intent=name,
                    canonical=_canonical(spec, party, document), score=round(score, 3), runner_up=nxt[0],
                    runner_up_score=round(nxt[1], 3), decided_by=by, party=party, document=document,
                    candidates=[(n, round(s, 3)) for n, s in ranked[:5]])


def _followup(message: str, previous: dict[str, Any] | None) -> Decision | None:
    """'and the co-applicant?' / 'what about PAN?' / 'and bank statement?': the previous case intent, the new slot."""
    if not previous or not previous.get("intent"):
        return None
    words = re.sub(r"[^\w\s-]", " ", str(message or "").lower()).split()
    fillers = set((catalogue().get("entities") or {}).get("followup_fillers") or [])
    party, document = party_in(message), document_in(message)
    if not (party or document) or len(words) > 7:
        return None
    # ONLY the new slot ("and the co-applicant?", "what about PAN?"): every word left after the fillers must name the
    # document or the party -- "upload new bank statement" / "dl verified?" / "is pan ok" ask something new
    slot_words = set()
    for alias, name in _documents():
        if name == document:
            slot_words |= set(alias.split())
    party_words = (catalogue().get("entities") or {}).get("co_applicant_words") or []
    slot_words |= {w for p in party_words for w in re.sub(r"[^\w\s-]", " ", str(p).lower()).split()}
    plain = re.sub(r"[^\w\s-]", " ", _normalised(message).lower()).split()
    if [w for w in plain if w not in fillers and w not in slot_words]:
        return None                              # more than a slot: a new question
    spec = (catalogue().get("intents") or {}).get(previous["intent"]) or {}
    if spec.get("kind") != "case":
        return None
    intent = previous["intent"]
    if document and spec.get("needs") != "document":
        intent = "document_status"               # "what about PAN?" after "what is pending" -> that document's status
        spec = (catalogue().get("intents") or {}).get(intent) or {}
    d = Decision(kind="case", intent=intent, canonical=_canonical(spec, party or previous.get("party"), document),
                 decided_by="followup", party=party or previous.get("party"), document=document, score=1.0)
    return d


async def understand(message: str, *, case_open: bool, previous: dict[str, Any] | None = None,
                     request_id: str | None = None, generator=None) -> Decision | None:
    """The Decision for this message, or None (not understood with confidence / meaning unavailable: the rules answer)."""
    if not enabled() or not _clean(message):
        return None
    STATS["asked"] += 1
    follow = _followup(message, previous)
    if follow is not None:
        STATS["followup"] += 1
        _log(message, follow)
        return follow
    embedder = _embedder()
    if embedder is None:
        return None
    try:
        ranked = await asyncio.to_thread(rank, message, embedder)
    except Exception:  # noqa: BLE001 - the embedding model down: the rules answer
        logger.warning("meaning: ranking failed; the rules answer", exc_info=True)
        return None
    if not ranked:
        return None
    cfg = _decide_cfg()
    intents = catalogue().get("intents") or {}
    kind = lambda name: (intents.get(name) or {}).get("kind")          # noqa: E731
    if not case_open and cfg.get("prefer_general_without_case"):
        general_kinds = set(cfg.get("general_kinds") or [])
        general = [(n, s_) for n, s_ in ranked if kind(n) in general_kinds]
        if general and ranked[0][1] - general[0][1] < float(cfg["margin"]) and general[0][1] >= float(cfg["floor"]):
            # no case open and a general reading as close as the case one: the general reading, without the model
            STATS["embedding"] += 1
            d = _decision(general[0][0], general[0][1], ranked, "embedding", message)
            _log(message, d)
            return d
    top, score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    lead = cfg.get("accept_with_lead") or {}
    clear_lead = bool(lead) and score >= float(lead["score"]) and score - second >= float(lead["margin"])
    if clear_lead or (score >= float(cfg["accept"]) and score - second >= float(cfg["margin"])):
        STATS["embedding"] += 1
        d = _decision(top, score, ranked, "embedding", message)
        _log(message, d)
        return d
    if score < float(cfg["floor"]):
        STATS["none"] += 1
        return None
    chosen = await _model_choice(message, ranked[:int(cfg.get("model_candidates", 3))], case_open, previous,
                                 request_id, generator)
    fallback = cfg.get("fallback_accept")
    if chosen is None and fallback is not None and case_open and kind(top) in ("case", "process")             and score >= float(fallback):
        # the model could not choose (or is down) but the best CASE reading is strong: that reading, never the
        # generic "which of these?" menu with a case open (config decide.fallback_accept)
        chosen = top
    if chosen is None:
        STATS["none"] += 1
        return None
    STATS["model"] += 1
    d = _decision(chosen, dict(ranked).get(chosen, 0.0), ranked, "model", message)
    _log(message, d)
    return d


# --------------------------------------------------------------------------
# the model: CHOOSES among the candidates (never answers)
# --------------------------------------------------------------------------

def _prefix() -> str:
    return str((catalogue().get("model") or {}).get("instructions") or "").strip()   # intent_catalogue.yaml model


async def _model_choice(message: str, candidates: list[tuple[str, float]], case_open: bool,
                        previous: dict[str, Any] | None, request_id: str | None, generator=None) -> str | None:
    intents = catalogue().get("intents") or {}
    spec = catalogue().get("model") or {}
    if generator is None:
        from app.llm import availability
        from app.llm.memory import free_gb

        free = free_gb()
        if free is not None and free < float(spec.get("min_free_ram_gb", 2.0)):
            STATS["model_failed"] += 1
            return None
        if not availability.provider_reachable():
            STATS["model_failed"] += 1
            return None
        generator = _ollama
    if request_id:
        from app.agents.applicant.copilot.answering import realtime

        realtime.hint(request_id, realtime.say_status("understanding", "en"))
    lines = "\n".join(f"- {n}: {intents.get(n, {}).get('description', '')}" for n, _ in candidates)
    content = (f"Case open: {'yes' if case_open else 'no'}. Previous question type: {(previous or {}).get('intent') or '-'}.\n"
               f"MESSAGE: {_masked(message)}\nCANDIDATES:\n{lines}")
    timeout = float(spec.get("timeout_seconds", 4.0))
    try:
        body = await asyncio.wait_for(generator(_prefix(), content, timeout), timeout)
        raw = ((body or {}).get("message") or {}).get("content") or ""
        chosen = str((json.loads(raw) if raw.strip().startswith("{") else {}).get("i") or "").strip()
    except Exception:  # noqa: BLE001 - slow / down / invalid: the rules answer
        STATS["model_failed"] += 1
        if generator is _ollama:
            from app.llm import availability as _avail

            _avail.warm_in_background(str(spec.get("name") or "") or None)   # a cold model: ready for the next turn
        return None
    return chosen if chosen in {n for n, _ in candidates} else None


def _masked(message: str) -> str:
    from app.security import sensitivity

    return sensitivity.mask_identifiers(_clean(message))


async def _ollama(prefix: str, content: str, timeout: float) -> dict:
    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    model = str((catalogue().get("model") or {}).get("name") or ollama_model())
    body = {"model": model, "stream": False, "keep_alive": keep_alive(),
            "format": {"type": "object", "properties": {"i": {"type": "string"}}, "required": ["i"]},
            "messages": [{"role": "system", "content": prefix}, {"role": "user", "content": content}],
            "options": with_num_ctx({"temperature": 0.0, "num_predict": int((catalogue().get("model") or {})
                                                                               .get("num_predict", 20))})}
    if model.startswith("qwen3"):
        body["think"] = False
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body)
        response.raise_for_status()
        return response.json()


# --------------------------------------------------------------------------
# provenance (internal only, masked)
# --------------------------------------------------------------------------

def _log(message: str, d: Decision) -> None:
    logger.info("meaning: %s", json.dumps({"message": _masked(message)[:160], "intent": d.intent, "kind": d.kind,
                                           "by": d.decided_by, "score": d.score, "runner_up": d.runner_up,
                                           "runner_up_score": d.runner_up_score, "party": d.party,
                                           "document": d.document, "canonical": d.canonical}, ensure_ascii=False))


def remember(state, d: Decision | None) -> None:
    """The last CASE intent of this chat (for follow-ups), on the workspace record."""
    if d is None or d.kind != "case":
        return
    flow = dict(getattr(state, "flow", None) or {})
    flow["last_meaning"] = {"intent": d.intent, "party": d.party, "document": d.document}
    state.flow = flow


__all__ = ["Decision", "FLAG", "STATS", "catalogue", "document_in", "enabled", "party_in", "rank", "remember",
           "understand"]
