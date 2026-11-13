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

from app.llm import turn_budget as _tb

import asyncio
import contextvars
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
STATS: dict[str, int] = {"asked": 0, "embedding": 0, "model": 0, "followup": 0, "none": 0, "model_failed": 0,
                         "clarify": 0}


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


_ADAPTER: dict[str, Any] = {"mtime": None, "matrix": None}


def _adapter():
    """
    THE LOS MEANING ADAPTER (2026-10-12; scripts/train_meaning_adapter.py): a linear map learned from the catalogue's
    own examples on top of the frozen embeddings -- LOS paraphrases of one intent closer, different intents further.
    Config intent_catalogue.yaml `adapter` (enabled + path); missing / unreadable file: None (plain cosine, as before).
    """
    spec = catalogue().get("adapter") or {}
    if not spec.get("enabled"):
        return None
    path = Path(__file__).resolve().parents[4] / str(spec.get("path") or "models/meaning_adapter.npy")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _LOCK:
        if _ADAPTER["mtime"] != mtime:
            try:
                import numpy as np

                _ADAPTER.update(mtime=mtime, matrix=np.load(path))
            except Exception:  # noqa: BLE001 - an unreadable adapter is no adapter
                logger.warning("meaning: adapter %s unreadable; plain embeddings", path, exc_info=True)
                _ADAPTER.update(mtime=mtime, matrix=None)
        return _ADAPTER["matrix"]


def _adapted(v: list[float]) -> list[float]:
    matrix = _adapter()
    # ONLY FOR THE EMBEDDING IT WAS TRAINED ON: a vector of another size (another model, a test embedder) gets the
    # plain cosine -- a size mismatch once made every ranking fail and silently switched meaning off (2026-10-12)
    if matrix is None or getattr(matrix, "shape", (0, 0))[1] != len(v):
        return _norm(v)
    return _norm([float(x) for x in matrix @ _norm(v)])


_CLASSIFIER: dict[str, Any] = {"mtime": None, "model": None}


def _classifier():
    """
    THE MEANING CLASSIFIER (2026-10-14; scripts/train_meaning_classifier.py): softmax regression over the same adapted
    embeddings, trained on the catalogue + paraphrases (+ labelled logs). Config intent_catalogue.yaml `classifier`
    (enabled + path + weight); missing / unreadable / another embedding size: None (k-NN only, as before).
    """
    spec = catalogue().get("classifier") or {}
    if not spec.get("enabled"):
        return None
    path = Path(__file__).resolve().parents[4] / str(spec.get("path") or "models/meaning_classifier.npz")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _LOCK:
        if _CLASSIFIER["mtime"] != mtime:
            try:
                import numpy as np

                data = np.load(path, allow_pickle=False)
                _CLASSIFIER.update(mtime=mtime, model={
                    "w": data["w"], "b": data["b"], "classes": [str(c) for c in data["classes"]],
                    "temperature": float(data["temperature"])})
            except Exception:  # noqa: BLE001 - an unreadable classifier is no classifier
                logger.warning("meaning: classifier %s unreadable; k-NN only", path, exc_info=True)
                _CLASSIFIER.update(mtime=mtime, model=None)
        return _CLASSIFIER["model"]


def _blended(query: list[float], ranked: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """k-NN ORDER refined by the classifier: sorted by cosine + weight * log p(intent). The SCORES stay the cosines, so
    the accept / margin thresholds mean what they meant -- a reorder that puts a lower cosine on top yields a negative
    margin, i.e. never a confident accept (it goes to the chooser / clarification)."""
    model = _classifier()
    if model is None or model["w"].shape[0] != len(query) or not ranked:
        return ranked
    import numpy as np

    logits = (np.asarray(query) @ model["w"] + model["b"]) / model["temperature"]
    logits -= logits.max()
    p = np.exp(logits)
    p /= p.sum()
    prob = dict(zip(model["classes"], p))
    weight = float((catalogue().get("classifier") or {}).get("weight", 0.1))
    return sorted(ranked, key=lambda kv: kv[1] + weight * float(np.log(prob.get(kv[0], 0.0) + 1e-9)), reverse=True)


def _bank(embedder) -> tuple[list[str], list[list[float]], list[str]]:
    intents = catalogue().get("intents") or {}
    pairs = [(name, str(e)) for name, spec in intents.items() for e in spec.get("examples") or []]
    # every reviewed paraphrase file the catalogue names (config paraphrase_files; default the generated bank)
    for file in catalogue().get("paraphrase_files") or ["intent_paraphrases.yaml"]:
        extra = (_load(str(file)).get("paraphrases") or {})
        pairs += [(name, str(e)) for name, items in extra.items() if name in intents for e in items or []]
    _adapter()
    key = hash((tuple(pairs), _ADAPTER["mtime"] if (catalogue().get("adapter") or {}).get("enabled") else None))
    with _LOCK:
        if _BANK["key"] == key:
            return _BANK["intents"], _BANK["vectors"], _BANK["texts"]
    from app.knowledge.retriever import _cached_embed_all

    vectors = [_adapted(v) for v in _cached_embed_all(embedder, [t for _, t in pairs])]
    with _LOCK:
        _BANK.update(key=key, intents=[n for n, _ in pairs], vectors=vectors, texts=[t for _, t in pairs])
    return _BANK["intents"], _BANK["vectors"], _BANK["texts"]


#: the last few rankings (message -> ranked): the hybrid gate and the reading after it rank the same text once
_RANKED: "dict[str, list[tuple[str, float]]]" = {}


def _named_document_state(message: str, top: str) -> bool:
    """A NAMED DOCUMENT'S STATE ("address proof uploaded or not", "is the PAN verified"): the status reading is clear --
    never the "upload a document" command it can tie with (0.857 vs 0.856, 2026-10-14)."""
    return top == "document_status" and bool(document_in(message)) and bool(re.search(
        r"\b(uploaded|verified|submitted|received|approved|rejected|pending|or\s+not|has\s+been|have\s+been|"
        r"is\s+it|status|done)\b", str(message or ""), re.IGNORECASE))


#: the catalogue kinds that are conversation, not a question (thanks / ok / hello / bye)
_SOCIAL_KINDS = {"ack", "small_talk"}


def social_reading(message: str) -> bool:
    """
    SMALL TALK BY MEANING (2026-10-14 live: "thx a lot" took 6.7 s -- two Qwen calls and a rewrite, then THANKS
    anyway): a short message whose top reading is a social kind (catalogue `kind` ack / small_talk) and either the
    runner-up is social too, or the lead is clear. Read from the embeddings, no phrase list, no model.
    """
    if len(str(message or "").split()) > int((_decide_cfg().get("social") or {}).get("max_words", 5)):
        return False
    ranked = rank(message)
    if not ranked:
        return False
    kinds = catalogue().get("intents") or {}
    kind = lambda name: str((kinds.get(name) or {}).get("kind") or "")            # noqa: E731
    top, score = ranked[0]
    spec = _decide_cfg().get("social") or {}
    if kind(top) not in _SOCIAL_KINDS or score < float(spec.get("min_score", 0.6)):
        return False
    if len(ranked) < 2:
        return True
    second, runner = ranked[1]
    return kind(second) in _SOCIAL_KINDS or score - runner >= float(spec.get("margin", 0.05))


def clear_reading(message: str, case_open: bool) -> bool:
    """Whether the fast matcher reads this message CLEARLY on its own (decide.accept + margin, or the clear lead) --
    no model. The Qwen agent's hybrid gate (copilot_reply.yaml agent.only_when_unclear): clear -> the tools answer
    directly, unclear -> Qwen chooses. A follow-up of the previous turn, a clarification case or no embedder: False."""
    if not enabled() or not _clean(message):
        return False
    embedder = _embedder()
    if embedder is None:
        return False
    try:
        ranked = rank(message, embedder)
    except Exception:  # noqa: BLE001
        return False
    if not ranked:
        return False
    intents = catalogue().get("intents") or {}
    if case_open and str((intents.get(ranked[0][0]) or {}).get("kind")) == "case" and not _names_many_cases(message):
        # A CASE IS OPEN AND NO PLURAL CASE NOUN ("which ones are still missing?", 2026-10-14): a list over the
        # officer's cases is no rival reading -- the runner-up is the next one-case / process reading
        ranked = [r for r in ranked if str((intents.get(r[0]) or {}).get("kind")) != "list"]
    cfg = _decide_cfg()
    if (intents.get(ranked[0][0]) or {}).get("one_document_intent") and document_in(message):
        return True     # a list question naming ONE document: its status, read deterministically (never Qwen's list)
    if _named_document_state(message, ranked[0][0]):
        return True     # a named document's STATE ("address proof uploaded or not"), never the upload command
    score, second = ranked[0][1], (ranked[1][1] if len(ranked) > 1 else 0.0)
    lead = cfg.get("accept_with_lead") or {}
    if lead and score >= float(lead["score"]) and score - second >= float(lead["margin"]):
        return True
    return score >= float(cfg["accept"]) and score - second >= float(cfg["margin"])


def _names_many_cases(message: str) -> bool:
    """Whether the message names cases in the plural ("cases", "files", "applications") -- case_list's nouns."""
    try:
        from app.agents.applicant.copilot.capabilities import case_list

        plural = {str(w).lower() for w in case_list._case_nouns()[1]}
    except Exception:  # noqa: BLE001 - unknown: treat as naming many (the strict reading stands)
        return True
    return bool(set(re.findall(r"[a-z]+", str(message or "").lower())) & plural)


def rank(message: str, embedder=None) -> list[tuple[str, float]]:
    """Every intent with its score: the best cosine of its examples (k-NN max), highest first. [] without a model."""
    embedder = embedder or _embedder()
    if embedder is None:
        return []
    key = _clean(message)
    if key in _RANKED:
        return list(_RANKED[key])
    try:
        names, vectors, _ = _bank(embedder)
        query = _adapted(embedder.embed(key))
    except Exception as exc:  # noqa: BLE001 - the embedding model down: no ranking, the rules answer (2026-11-02:
        # an uncaught EmbeddingError here failed EVERY chat turn with HTTP 500 while Ollama was down)
        logger.info("meaning rank unavailable (%s)", type(exc).__name__)
        return []
    best: dict[str, float] = {}
    for name, vec in zip(names, vectors):
        score = sum(a * b for a, b in zip(query, vec))
        if score > best.get(name, -1.0):
            best[name] = score
    ranked = _blended(query, sorted(best.items(), key=lambda kv: kv[1], reverse=True))
    if len(_RANKED) > 256:
        _RANKED.clear()
    _RANKED[key] = ranked
    return list(ranked)


def plural_scope(message: str, decision: "Decision | None") -> "Decision | None":
    """
    PLURAL SCOPE (decide.plural_scope; live audit 2026-10-12): a case reading -- or no reading -- of a message whose
    subject is the officer's CASES (a plural noun) with a status cue is the PORTFOLIO question: the list, or the count
    when a count word is said; KYC words pick the KYC variants. None: the reading stands. Never with a case / applicant
    id in the message (that names one case). The answer is the deterministic list / insights over the caller's own rows.
    """
    spec = _decide_cfg().get("plural_scope") or {}
    if not spec:
        return None
    from app.agents.applicant.copilot.semantics import task as _task

    said = " " + re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", _normalised(message).lower())).strip() + " "
    # THE CO-APPLICANT IS ONE PERSON ON THIS CASE: "the co-applicant's KYC" (-> "co applicant s") is never the plural
    # noun "applicants" over the officer's cases (party_kyc co_kyc_about, 2026-10-14)
    said = re.sub(r"\bco\s?applicant(s|\s+s)?\b", " party ", said)
    # ...and "both applicants" / "both of them" are this case's two parties, never the officer's cases
    said = re.sub(r"\bboth\s+(the\s+)?(applicants|borrowers|of\s+them)\b", " parties ", said)
    hit = lambda key: any(re.search(p, said) for p in spec.get(key) or [])          # noqa: E731
    # A SELECTION QUESTION OVER THE OFFICER'S OWN CASES ("which of MY customers still owe me documents?"): the
    # portfolio whatever the reading -- a knowledge / process reading of it asks about THEIR cases (task.selectors +
    # plural_scope.owned_words), never the handbook
    owned = _task.asks_over_cases(message) and hit("owned_words")
    if decision is not None and decision.kind != "case" and not (owned and decision.kind in ("knowledge", "process")):
        return None
    # THE STATUS CUE comes from the words OR from the semantic reading itself (Phase 2): a case-STATUS reading
    # (pending / stuck / KYC / CPA readiness: plural_scope.status_intents) of a message about the CASES is that status
    # asked over the cases -- "show my pendng cases", "kaunsi files CPA ke liye ready hain" carry no cue word -- OR a
    # filter the selection question names ("which customers ... DOCUMENTS?": documents pending)
    from_meaning = decision is not None and decision.intent in set(spec.get("status_intents") or [])
    selection = _task.asks_over_cases(message)
    from_filter = selection and _task.filter_in(message) is not None
    # the CASES are the subject: a plural case noun -- or the people a selection question picks among ("which
    # BORROWERS of mine ...": task.people_plural, the same vocabulary asks_over_cases reads)
    if not ((hit("nouns") or selection) and (hit("status_cues") or from_meaning or from_filter)) \
            or re.search(r"\b(case|app|coapp)-[0-9a-f]{4,}\b", said):
        return None

    if _task.singular_pointer(message):
        return None                     # "this application's pending documents": ONE case, whatever the plural
    kyc = hit("kyc_words") or (from_meaning and decision.intent == "kyc_status")
    count = hit("count_words")
    which = ("kyc_" if kyc else "") + ("count" if count else "list")
    name = (spec.get("intents") or {}).get(which)
    intent = (catalogue().get("intents") or {}).get(name) or {}
    if intent.get("kind") != "list" or not intent.get("canonical"):
        return None
    canonical = str(intent["canonical"])
    # THE FILTER the message names ("ready", "stuck", "KYC" -- task.filter_roots, closed set) makes the question that
    # filter's deterministic one; none named: the intent's own canonical
    key = _task.filter_in(message)
    if key is None and from_meaning:
        # no filter word ("which applications are ELIGIBLE to move to CPA?"): the status the reading names is the
        # filter (plural_scope.status_filters) -- never the generic pending list
        key = (spec.get("status_filters") or {}).get(decision.intent)
    if key and not (kyc and key != "kyc_issue"):
        canonical = _task.canonical_for("count" if count else "list", key) or canonical
    return Decision(kind="list", intent=str(name), canonical=canonical, decided_by="plural_scope",
                    score=getattr(decision, "score", 0.0) or 0.0)


def nearest_example(message: str, intent: str, embedder=None) -> str | None:
    """
    The catalogue example of `intent` closest to the message (the same embeddings + adapter as `rank`), or None. Used
    ONLY as an extra RETRIEVAL wording for a knowledge question ("how long a statement history do we need" -> "how many
    months of bank statement are needed"): the answer is still the handbook passage it finds, with its citation.
    """
    embedder = embedder or _embedder()
    if embedder is None or not _clean(message):
        return None
    try:
        names, vectors, texts = _bank(embedder)
        query = _adapted(embedder.embed(_clean(message)))
    except Exception:  # noqa: BLE001 - the embedding model down: no extra wording
        return None
    best, best_text = -1.0, None
    for name, vec, text in zip(names, vectors, texts):
        if name != intent:
            continue
        score = sum(a * b for a, b in zip(query, vec))
        if score > best:
            best, best_text = score, text
    return best_text


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
    intents = catalogue().get("intents") or {}
    spec = intents.get(name) or {}
    party, document = party_in(message), document_in(message)
    if document and spec.get("one_document_intent") in intents and not re.search(
            r"\b(documents|docs|ones|papers|which|list|all|other\s+than|except|apart|besides|baaki|sab)\b",
            str(message or ""), re.IGNORECASE):
        # A LIST QUESTION ABOUT ONE NAMED DOCUMENT ("address proof uploaded or not"): that document's status --
        # the list alone never says whether THIS one is in it (config intents.*.one_document_intent, 2026-10-14)
        name = str(spec["one_document_intent"])
        spec = intents[name]
    nxt = next(((n, s) for n, s in ranked if n != name), (None, 0.0))
    return Decision(kind=str(spec.get("kind") or "knowledge"), intent=name,
                    canonical=_canonical(spec, party, document), score=round(score, 3), runner_up=nxt[0],
                    runner_up_score=round(nxt[1], 3), decided_by=by, party=party, document=document,
                    candidates=[(n, round(s, 3)) for n, s in ranked[:5]])


def _clarify(message: str, ranked: list[tuple[str, float]], case_open: bool,
             previous: dict[str, Any] | None = None) -> Decision | None:
    """
    CLOSE CALL, DECIDED INTERNALLY (2026-10-12; config decide.clarify). The held-out misses are mostly two CLOSE
    topics read the wrong way round (status vs stage, KYC vs document status). When the two best readings are a
    configured confusable pair and neither leads by `margin`, nothing is guessed and nothing is asked back:
      1. the CONVERSATION decides -- the reading the previous turn was about continues ("what is pending?" ... "and
         what's left?" stays on pending);
      2. otherwise BOTH readings are answered, one compound question to the same engine ("What is the status of
         this case and which stage is this case in?"), each part answered exactly as if asked alone.
    Never for a clear lead, never outside the configured pairs, never with no case open for a case reading.
    """
    spec = _decide_cfg().get("clarify") or {}
    if not spec.get("enabled") or len(ranked) < 2:
        return None
    (first, s1), (second, s2) = ranked[0], ranked[1]
    if s1 < float(spec.get("floor", 0.6)) or s1 - s2 >= float(spec.get("margin", 0.03)):
        return None
    if not any({first, second} == {str(a), str(b)} for a, b in (spec.get("pairs") or [])):
        return None
    intents = catalogue().get("intents") or {}
    if any((intents.get(n) or {}).get("kind") != "case" for n in (first, second)) or not case_open:
        return None                                # no case open: the case logic asks "which case?" first
    earlier = (previous or {}).get("intent")
    if earlier in (first, second):
        return _decision(earlier, dict(ranked)[earlier], ranked, "context", message)
    d = _decision(first, s1, ranked, "both", message)
    questions = [_canonical(intents.get(n) or {}, d.party, d.document) for n in (first, second)]
    if not all(questions) or questions[0] == questions[1]:
        return None
    a, b = (str(q).strip().rstrip("?") for q in questions)
    d.canonical = f"{a} and {b[:1].lower() + b[1:]}?"
    # only when the engine really answers it as two questions (its own compound split); else the usual path
    from app.agents.applicant.copilot.semantics import intents as _intents

    return d if _intents.compound_parts(d.canonical) else None


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
    """The Decision for this message, or None (not understood with confidence / meaning unavailable: the rules answer).

    TWO READERS MUST AGREE (2026-11-06, config simple_chat.yaml agreement): an embedding / model reading whose question
    the structural reader reads as a different intent family than the message itself is dropped -- the rules answer
    the message ("how many are pending" was answered as the time in stage)."""
    d = await _understand(message, case_open=case_open, previous=previous, request_id=request_id, generator=generator)
    if d is not None and case_open and d.decided_by in ("embedding", "model") and d.canonical:
        from app.agents.applicant.copilot.simple import pipeline as _agree

        if await asyncio.to_thread(_agree.disagrees, message, d.canonical):
            STATS["disagreed"] = STATS.get("disagreed", 0) + 1
            return None
    return d


async def _understand(message: str, *, case_open: bool, previous: dict[str, Any] | None = None,
                      request_id: str | None = None, generator=None) -> Decision | None:
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
    if case_open and kind(ranked[0][0]) == "list" and not _names_many_cases(message):
        # A CASE IS OPEN AND NO PLURAL CASE NOUN: a list over the officer's cases is not this question
        # ("What about the co-applicant's KYC?" was read as "show kyc issue cases", answered for the primary
        # applicant -- party_kyc, 2026-10-14); the best one-case reading wins instead
        one_case = [(n, s_) for n, s_ in ranked if kind(n) != "list"]
        if one_case and one_case[0][1] >= float(cfg["floor"]):
            ranked = one_case + [r for r in ranked if kind(r[0]) == "list"]
    top, score = ranked[0]
    rivals = ranked
    if case_open and kind(top) == "case" and not _names_many_cases(message):
        # the same rule as clear_reading: with a case open and no plural case noun, a list over the officer's cases
        # is no rival reading of "which ones are still missing?" (2026-10-14)
        rivals = [r for r in ranked if kind(r[0]) != "list"]
    second = rivals[1][1] if len(rivals) > 1 else 0.0
    unsure = _clarify(message, ranked, case_open, previous)
    if unsure is not None:
        STATS["clarify"] += 1
        _log(message, unsure)
        return unsure
    lead = cfg.get("accept_with_lead") or {}
    clear_lead = bool(lead) and score >= float(lead["score"]) and score - second >= float(lead["margin"])
    clear = clear_lead or (score >= float(cfg["accept"]) and score - second >= float(cfg["margin"]))
    clear = clear or _named_document_state(message, top)
    # QWEN DECIDES (owner, 2026-10-13: "question samjhne ka kaam Qwen karega", config decide.model_always): the
    # embedding only NARROWS the choice to the top candidates; Qwen picks one (or "none"). Qwen unavailable / slow
    # -> a clear embedding winner still answers, so the officer is never left without one.
    always = bool(cfg.get("model_always")) and generator is not False
    if clear and not always:
        STATS["embedding"] += 1
        d = _decision(top, score, ranked, "embedding", message)
        _log(message, d)
        return d
    if score < float(cfg["floor"]):
        STATS["none"] += 1
        return None
    chosen = await _model_choice(message, ranked[:int(cfg.get("model_candidates", 3))], case_open, previous,
                                 request_id, generator)
    if chosen is None and clear and always and _MODEL_FAILED.get():
        chosen = top                                # the model could not run: the clear embedding reading
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


def full_catalogue() -> bool:
    """QWEN CHOOSES FROM EVERY INTENT (config model.full_catalogue, 2026-10-13): measured, "FOIR kitna aaya?" never
    reached the model as eligibility -- the embedding's top candidates were loan_amount / case_stage -- so a choice
    limited to them could not be right. The whole catalogue sits in the system prompt (byte-identical every call,
    so Ollama reuses it); the embedding's best guesses are only HINTS."""
    return bool((catalogue().get("model") or {}).get("full_catalogue"))


_FULL_PREFIX: dict[str, Any] = {"key": None, "text": ""}


def prime() -> float | None:
    """One real chooser call with the system prompt, so the weights are paged in and the prompt is cached before the
    first officer asks (blocking; run it in a background thread). Milliseconds, or None when it could not run."""
    import urllib.request

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    started = time.perf_counter()
    spec = catalogue().get("model") or {}
    body = {"model": str(spec.get("name") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "format": {"type": "object", "properties": {"i": {"type": "string"}}, "required": ["i"]},
            "messages": [{"role": "system", "content": _full_prefix() if full_catalogue() else _prefix()},
                         {"role": "user", "content": str(spec.get("prime_message") or "")}],
            "options": with_num_ctx({"temperature": 0.0, "num_predict": 8})}
    try:
        request = urllib.request.Request(f"{ollama_host().rstrip('/')}/api/chat", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=180).read()
    except Exception:  # noqa: BLE001 - priming is a convenience; the first turn is just slower
        logger.info("meaning: prime skipped", exc_info=True)
        return None
    ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info("meaning: chooser primed in %s ms", ms)
    return ms


def _full_prefix() -> str:
    intents = catalogue().get("intents") or {}
    key = id(intents)
    if _FULL_PREFIX["key"] != key:
        lines = "\n".join(f"- {name}: {str((spec or {}).get('description') or '').strip()}"
                          for name, spec in sorted(intents.items()))
        _FULL_PREFIX.update(key=key, text=f"{_prefix().replace('CANDIDATES', 'INTENTS')}\n\nINTENTS:\n{lines}")
    return str(_FULL_PREFIX["text"])


#: whether THIS turn's model choice failed to run (down / slow / low memory / invalid), as opposed to choosing "none"
_MODEL_FAILED: "contextvars.ContextVar[bool]" = contextvars.ContextVar("meaning_model_failed", default=False)


async def _model_choice(message: str, candidates: list[tuple[str, float]], case_open: bool,
                        previous: dict[str, Any] | None, request_id: str | None, generator=None) -> str | None:
    _MODEL_FAILED.set(True)
    from app.agents.applicant.copilot.capabilities import qwen_agent as _qa

    if _qa.spent():
        return None                                 # the agent used this turn's model time: embeddings only
    intents = catalogue().get("intents") or {}
    spec = catalogue().get("model") or {}
    if generator is None:
        from app.llm import availability
        from app.llm.memory import free_gb_for_model as free_gb

        free = free_gb()
        if free is not None and free < float(spec.get("min_free_ram_gb", 2.0)):
            STATS["model_failed"] += 1
            return None
        if not availability.provider_reachable():
            STATS["model_failed"] += 1
            return None
        name = str(spec.get("name") or "")
        if name and availability.model_loaded(name) is False:
            # THE MODEL IS NOT IN MEMORY: a 4 s call would only start a load it cannot wait for (and abort it). One
            # background load instead; this turn answers from the embeddings at once (2026-10-28)
            availability.warm_in_background(name)
            STATS["model_failed"] += 1
            return None
        generator = _ollama
    if request_id:
        from app.agents.applicant.copilot.answering import realtime

        realtime.hint(request_id, realtime.say_status("understanding", "en"))
    full = full_catalogue()
    lines = "\n".join(f"- {n}: {intents.get(n, {}).get('description', '')}" for n, _ in candidates)
    hints = ", ".join(n for n, _ in candidates)
    content = (f"Case open: {'yes' if case_open else 'no'}. Previous question type: {(previous or {}).get('intent') or '-'}.\n"
               + (f"MESSAGE: {_masked(message)}\n{spec.get('likely_label') or ''} {hints}" if full
                  else f"MESSAGE: {_masked(message)}\nCANDIDATES:\n{lines}"))
    timeout = float(spec.get("timeout_seconds", 4.0))
    try:
        body = await asyncio.wait_for(generator(_full_prefix() if full else _prefix(), content, timeout), timeout)
        raw = ((body or {}).get("message") or {}).get("content") or ""
        chosen = str((json.loads(raw) if raw.strip().startswith("{") else {}).get("i") or "").strip()
    except Exception:  # noqa: BLE001 - slow / down / invalid: the rules answer
        STATS["model_failed"] += 1
        if generator is _ollama:
            from app.llm import availability as _avail

            _avail.warm_in_background(str(spec.get("name") or "") or None)   # a cold model: ready for the next turn
        return None
    _MODEL_FAILED.set(False)                        # it ran: "none" / an id outside the list is its answer
    allowed = set(intents) if full else {n for n, _ in candidates}
    return chosen if chosen in allowed else None


_MOBILE_IN_TEXT = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{4})[\s-]?(\d{5})(?!\d)")


def _masked(message: str) -> str:
    """What a MODEL may read of the officer's message: PAN / Aadhaar / account masked (sensitivity policy) and, for a
    model, an Indian mobile too -- last 4 kept (2026-10-23: a full mobile reached the planner's prompt)."""
    from app.security import sensitivity

    out = sensitivity.mask_identifiers(_clean(message))
    return _MOBILE_IN_TEXT.sub(lambda m: "XXXXXX" + m.group(2)[-4:], out)


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
    _tb.check("meaning")      # an earlier model call of this turn failed: answer without it (app/llm/turn_budget.py)
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await _tb.guarded(client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body), "meaning")
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
