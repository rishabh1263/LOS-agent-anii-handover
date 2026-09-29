"""
Answering from the FOS knowledge base, and refusing to.

THE RULE: NOTHING HERE MAY STATE A FACT ABOUT A CASE. Retrieval returns
policy — what the product requires, what satisfies a slot, what a verdict
means. It never returns whether THIS applicant's PAN passed. Case facts come
from the store, and the two are kept apart deliberately: a knowledge base
that appears to answer case questions is a system that will one day answer
one wrongly, fluently, with a citation.

THE OTHER RULE: A RETRIEVAL THAT IS NOT CONFIDENT PRODUCES A REFUSAL. Not a
hedged answer, not the best paragraph available with a disclaimer -- a plain
statement that the knowledge base does not cover it. A grounded system's
worst failure is a confident sourced answer to a question its sources never
addressed, because it is indistinguishable from a correct one.

The model, when it runs at all, is given the retrieved passages and asked to
phrase them. It is not given the question alone, it cannot reach the store,
and if it is unavailable or slow the retrieved text is returned directly.
Losing the model costs the prose and nothing else.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app import knowledge as knowledge_layer
from app.agents.applicant import config, counters

logger = logging.getLogger(__name__)

#: The stage this agent retrieves from. Never widened at runtime: a FOS
#: question answered from credit knowledge would be authoritative and out of
#: scope, which is the failure the stage scoping exists to prevent.
STAGE = "FOS"

#: What is said when retrieval is not confident. Deliberately plain, and
#: deliberately not an apology with a guess attached.
NO_ANSWER = (
    "I don't have enough information in the FOS knowledge base to answer "
    "that."
)

_SYSTEM = (
    "You are a field officer's assistant for the FOS stage of a loan "
    "application.\n"
    "Answer ONLY from the reference material supplied below.\n"
    "If the material does not answer the question, say you do not know.\n"
    "Never invent a document type, a rule, a threshold or a status.\n"
    "Never state anything about a specific applicant, case or document -- "
    "you have no access to case data and must not appear to.\n"
    "Be brief: two or three sentences, plain prose, no preamble, no "
    "markdown headings."
)


def retrieve(question: str, *, limit: int = 3):
    """
    Retrieve FOS knowledge for a question. Never raises.

    ONE PASS WHEN IT IS CLEAR: a confident retrieval is returned as the
    retriever produced it. OTHERWISE A SECOND LOOK, bounded and deterministic
    -- the question is also retrieved in its canonical English form
    (Hinglish / Hindi / Marathi rewritten by the language layer), the hits are
    merged, and each passage is RERANKED by how much of the question's
    DISTINCTIVE vocabulary it covers. A passage is accepted below the score
    threshold only when it covers most of what was asked; a weakly confident
    hit that covers almost none of it is declined. A question the handbook
    does not cover ("prepayment penalty") shares no distinctive term with any
    passage and is still answered honestly: not enough information.
    """
    if not knowledge_layer.enabled():
        return None
    try:
        retriever = knowledge_layer.get_retriever()
        first = retriever.retrieve(question, STAGE, limit=max(limit, 5))
    except Exception:
        # The knowledge base failing must not take a case question with it.
        logger.exception("FOS knowledge retrieval failed")
        return None
    try:
        return _second_look(retriever, question, first, limit)
    except Exception:  # noqa: BLE001 - the second look never costs the first
        logger.exception("FOS knowledge rerank failed")
        return _trimmed(first, limit)


#: Words that say nothing about WHICH passage answers ("loan", "customer").
_GENERIC_TERMS = frozenset("""
loan loans customer customers applicant applicants borrower case cases application applications
document documents doc docs file files system personal home business can could use used using need
needed needs get give tell show know want please will would should does did done make made take
taken good fine okay yes no also still just like one two first second any some thing things way
else ones clear stop sitting turned several instead less haven entered yet include depend always
regardless got shows details didn over handing through process usually live left right really about
there their them they then than these those this that which what when where while who whom why how
liye chalega maana jata tha raha rahi hoga hogi toh uske uska uski nahi rahe karta karte karna aane
baad kiya kiye phir dobara karne kitne kitna lagte lagta hota hoti hote jaata jaati jayega jayegi
chahiye bhejne bhej kab kaise kya hai hain mein par wala wali beech chal agar jab bhi sirf sab kuch
""".split())
#: Coverage a below-threshold passage needs to be accepted, the floor under
#: which a weakly confident one is declined, and what "weakly" means.
_ACCEPT_COVERAGE, _DECLINE_COVERAGE, _WEAK_CONFIDENT = 0.5, 0.25, 0.4
#: ... and a passage must cover at least this many distinct terms of it.
_MIN_MATCHED = 2


def _trimmed(result, limit: int):
    from dataclasses import replace

    return replace(result, hits=list(result.hits[:limit]))


def _distinctive(text: str) -> list[str]:
    from app.knowledge.retriever import content_terms

    return list(dict.fromkeys(t for t in content_terms(text)
                              if len(t) >= 3 and t not in _GENERIC_TERMS and not t.isdigit()))


def _matched(terms: list[str], hit) -> int:
    from app.knowledge.embeddings import tokenize

    words = set(tokenize(f"{hit.chunk.heading} {hit.chunk.text}"))
    stems = {w[:5] for w in words if len(w) >= 5}
    return sum(1 for t in terms if t in words or (len(t) >= 5 and t[:5] in stems))


def _coverage(terms: list[str], hit) -> float:
    return _matched(terms, hit) / len(terms) if terms else 0.0


def _vocabulary() -> list[dict[str, list[str]]]:
    """app/config/knowledge_vocabulary.yaml: how people ask -> handbook words."""
    global _VOCABULARY
    if _VOCABULARY is None:
        try:
            import pathlib
            import yaml

            path = pathlib.Path(__file__).resolve().parents[2] / "config" / "knowledge_vocabulary.yaml"
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _VOCABULARY = [{"asks": [str(a).lower() for a in c.get("asks") or []],
                            "handbook": [str(h).lower() for h in c.get("handbook") or []]}
                           for c in loaded.get("concepts") or [] if c.get("handbook")]
        except Exception:  # noqa: BLE001 - no vocabulary: the plain terms only
            logger.exception("knowledge vocabulary unavailable")
            _VOCABULARY = []
    return _VOCABULARY


_VOCABULARY: list[dict[str, list[str]]] | None = None
_CORPUS_WORDS: dict[str, set[str]] = {}


def _corpus_words() -> set[str]:
    """Every word the FOS handbook uses (headings and text), for the stage."""
    if STAGE not in _CORPUS_WORDS:
        from app.knowledge.embeddings import tokenize

        words: set[str] = set()
        try:
            for chunk in knowledge_layer.get_repository().chunks(STAGE):
                words.update(tokenize(f"{chunk.heading} {chunk.text}"))
        except Exception:  # noqa: BLE001 - unknown corpus: no guard from it
            logger.exception("FOS corpus vocabulary unavailable")
        _CORPUS_WORDS[STAGE] = words
    return _CORPUS_WORDS[STAGE]


def _unknown_subject(terms: list[str]) -> bool:
    """
    A THING THE HANDBOOK NEVER MENTIONS ("NRI", "prepayment"): a distinctive
    word of the question that no passage uses, in any form. A passage that
    covers the rest of the question does not answer a question about it.
    """
    words = _corpus_words()
    if not words:
        return False
    stems = {w[:5] for w in words if len(w) >= 5}
    plain = [t for t in terms if len(t) >= 3 and t.isalpha()]
    unknown = [t for t in plain if t not in words and not (len(t) >= 5 and t[:5] in stems)]
    # an incidental word ("marked", "borrowed") is not the subject; when the
    # unknown words are half of what was asked, the subject is one of them
    return bool(unknown) and len(unknown) * 2 >= len(plain)


#: A question asking for a THRESHOLD is answered only by a passage stating one.
_THRESHOLD = None


def _asks_threshold(text: str) -> bool:
    import re as _re

    return bool(_re.search(r"\b(minimum|maximum|min|max|threshold|cut-?off|at\s+least|at\s+most|"
                           r"upper\s+limit|lower\s+limit)\b", text, _re.I))


def _concepts(*texts: str) -> tuple[list[list[str]], set[str]]:
    """
    THE ASKED CONCEPTS, each as the handbook words that would answer it, and
    the question words they came from. One concept is one group: a passage
    covering any of its handbook words covers it ONCE.
    """
    import re as _re

    said = " ".join(t.lower() for t in texts if t)
    groups: list[list[str]] = []
    sources: set[str] = set()
    for concept in _vocabulary():
        hit = [a for a in concept["asks"]
               if _re.search(r"(?<![\w-])" + _re.escape(a) + r"(?![\w-])", said)]
        if hit:
            groups.append(list(concept["handbook"]))
            for a in hit:
                sources.update(a.split())
    return groups, sources


def _stem(term: str) -> str:
    """A light stem: "means"/"meaning" -> "mean", "documents" -> "document"."""
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(term) - len(suffix) >= 4 and term.endswith(suffix):
            return term[: -len(suffix)]
    return term


def _group_matched(group: list[str], hit, words: set[str], text: str) -> bool:
    """Any of the group's terms at the START of a word of the text (so
    "established" is found in NOT_ESTABLISHED and "mean" in "means")."""
    import re as _re

    for term in group:
        stem = _stem(term.lower())
        if _re.search(r"(?<![a-z0-9])" + _re.escape(stem), text):
            return True
    return False


def _group_coverage(groups: list[list[str]], hit) -> tuple[float, int, int]:
    """(share of groups covered, groups covered, groups covered by the heading)."""
    from app.knowledge.embeddings import tokenize

    body = f"{hit.chunk.heading} {hit.chunk.text}"
    words, text = set(tokenize(body)), " ".join(body.lower().split())
    head_words = set(tokenize(hit.chunk.heading))
    head_text = hit.chunk.heading.lower()
    covered = sum(1 for g in groups if _group_matched(g, hit, words, text))
    in_heading = sum(1 for g in groups if _group_matched(g, hit, head_words, head_text))
    return (covered / len(groups) if groups else 0.0), covered, in_heading


def _group_weight(group: list[str]) -> float:
    """How RARE the group is in the handbook (log inverse passage frequency):
    "lifecycle" says more about which passage answers than "stage" does."""
    import math

    key = "\x1f".join(group)
    cached = _WEIGHTS.get(key)
    if cached is not None:
        return cached
    try:
        chunks = list(knowledge_layer.get_repository().chunks(STAGE))
    except Exception:  # noqa: BLE001
        chunks = []
    if not chunks:
        return 1.0
    df = sum(1 for c in chunks
             if _group_matched(group, None, set(), " ".join(f"{c.heading} {c.text}".lower().split())))
    weight = math.log((len(chunks) + 1) / (df + 1)) + 0.1
    _WEIGHTS[key] = weight
    return weight


_WEIGHTS: dict[str, float] = {}


def _weighted_coverage(groups: list[list[str]], hit) -> float:
    body = " ".join(f"{hit.chunk.heading} {hit.chunk.text}".lower().split())
    total = sum(_group_weight(g) for g in groups)
    got = sum(_group_weight(g) for g in groups if _group_matched(g, hit, set(), body))
    return got / total if total else 0.0


def _rank(scored: tuple[float, int, int], groups: int, hit=None, codes: tuple[str, ...] = (),
          weights_for: list[list[str]] | None = None) -> float:
    """
    RANKING ONLY (acceptance uses the plain coverage): body coverage, plus
    what the HEADING covers -- a section titled by the question's subject is
    the one written for it -- plus the CODES the question names in capitals
    ("SKIPPED", "CONDITIONAL"), found written the same way in the passage:
    the handbook spells a status exactly as the screen shows it.
    """
    coverage, _covered, in_heading = scored
    if weights_for is not None and hit is not None:
        coverage = _weighted_coverage(weights_for, hit)
    rank = coverage + 0.5 * (in_heading / groups if groups else 0.0)
    if codes and hit is not None:
        import re as _re

        body = f"{hit.chunk.heading} {hit.chunk.text}"
        found = sum(1 for c in codes if _re.search(r"(?<![A-Za-z0-9_])" + _re.escape(c) + r"(?![A-Za-z0-9_])", body))
        in_head = sum(1 for c in codes if c in hit.chunk.heading)
        rank += (found + in_head) / len(codes)
    return round(rank, 4)


def _codes(question: str) -> tuple[str, ...]:
    """Status / field codes the question writes in capitals (not the stage)."""
    import re as _re

    return tuple(dict.fromkeys(c for c in _re.findall(r"\b[A-Z][A-Z_]{2,}\b", question)
                               if c not in (STAGE, "CPA", "KYC", "PAN", "ID", "FAQ")))


def _common_terms() -> set[str]:
    """Words most of the handbook uses ("fos", "stage"): they say nothing
    about WHICH passage answers."""
    if "_common" not in _CORPUS_WORDS:
        from app.knowledge.embeddings import tokenize

        counts: dict[str, int] = {}
        total = 0
        try:
            for chunk in knowledge_layer.get_repository().chunks(STAGE):
                total += 1
                for w in set(tokenize(f"{chunk.heading} {chunk.text}")):
                    counts[w] = counts.get(w, 0) + 1
        except Exception:  # noqa: BLE001
            total = 0
        _CORPUS_WORDS["_common"] = {w for w, n in counts.items() if total and n / total >= 0.4}
    return _CORPUS_WORDS["_common"]


def _second_look(retriever, question: str, first, limit: int):
    from dataclasses import replace
    from app.agents.applicant import normalize

    canonical = normalize.normalise(question).text or question
    concepts, asked_words = _concepts(question, canonical)
    terms = [t for t in (_distinctive(canonical) or _distinctive(question))
             if t not in asked_words]
    # ONE GROUP PER THING ASKED: a plain distinctive term, or a concept with
    # the handbook words that answer it. The thresholds apply to the groups.
    groups = [[t] for t in terms] + concepts
    # for RANKING, the words every passage uses are left out
    common = _common_terms()
    rank_groups = [g for g in groups if not all(t in common for t in g)] or groups
    codes = _codes(question)
    if first.confident:
        # A WEAK "confident" hit that answers almost nothing asked is declined.
        if first.hits and first.hits[0].score < _WEAK_CONFIDENT and len(groups) >= 2 \
                and not any(_group_coverage(groups, h)[0] >= _ACCEPT_COVERAGE
                            and _group_coverage(groups, h)[1] >= _MIN_MATCHED
                            for h in first.hits[:3]):
            return replace(first, hits=list(first.hits[:limit]), confident=False)
        if len(groups) < 2:
            return _trimmed(first, limit)
        pool = {h.chunk.chunk_id: h for h in first.hits}
        if concepts:
            rewrite = " ".join([canonical] + [w for g in concepts for w in g])
            for h in retriever.retrieve(rewrite, STAGE, limit=10).hits:
                pool.setdefault(h.chunk.chunk_id, h)
        scored_first = {cid: _group_coverage(rank_groups, h) for cid, h in pool.items()}
        reranked = sorted(pool.values(),
                          key=lambda h: (_rank(scored_first[h.chunk.chunk_id], len(rank_groups), h, codes,
                                               rank_groups), h.score),
                          reverse=True)
        return replace(first, hits=reranked[:limit])
    merged = {h.chunk.chunk_id: h for h in first.hits}
    rewrites = []
    if canonical.strip().lower() != question.strip().lower():
        rewrites.append(canonical)
    if concepts:
        # THE QUESTION IN THE HANDBOOK'S WORDS, retrieved as well
        rewrites.append(" ".join([canonical] + [w for g in concepts for w in g]))
    for rewrite in rewrites:
        for h in retriever.retrieve(rewrite, STAGE, limit=10).hits:
            known = merged.get(h.chunk.chunk_id)
            if known is None or h.score > known.score:
                merged[h.chunk.chunk_id] = h
    if not merged or len(groups) < 2:
        return _trimmed(first, limit)
    scored = {cid: _group_coverage(groups, h) for cid, h in merged.items()}
    for_rank = {cid: _group_coverage(rank_groups, h) for cid, h in merged.items()}

    def _acceptable(h) -> bool:
        cov, cov_n, _ = scored[h.chunk.chunk_id]
        return cov >= _ACCEPT_COVERAGE and cov_n >= _MIN_MATCHED and h.score >= 0.1

    # a passage that meets the (unchanged) acceptance rule comes before one
    # that only ranks well; among each, the ranking decides
    ranked = sorted(merged.values(),
                    key=lambda h: (_acceptable(h),
                                   _rank(for_rank[h.chunk.chunk_id], len(rank_groups), h, codes,
                                         rank_groups), h.score),
                    reverse=True)
    best, covered, _heading = scored[ranked[0].chunk.chunk_id]
    confident = (best >= _ACCEPT_COVERAGE and covered >= _MIN_MATCHED
                 and ranked[0].score >= 0.1)
    # GROUNDING GUARDS on a rescued retrieval: the question's own subject
    # must exist in the handbook, and a threshold needs a stated number.
    if confident and _unknown_subject(terms):
        confident = False
    if confident and _asks_threshold(canonical) and not any(
            ch.isdigit() for ch in ranked[0].chunk.text):
        confident = False
    return replace(first, hits=ranked[:limit], confident=confident)


def _question_groups(question: str) -> list[list[str]]:
    """The things a question asks, as term groups (see _second_look)."""
    from app.agents.applicant import normalize

    canonical = normalize.normalise(question).text or question
    concepts, asked_words = _concepts(question, canonical)
    terms = [t for t in (_distinctive(canonical) or _distinctive(question))
             if t not in asked_words]
    return [[t] for t in terms] + concepts


def _evidence(body: str, question: str, *, keep: int = 2) -> str:
    """
    ANSWER MORE, SAY LESS: the sentences of the passage that answer THIS
    question -- those covering most of what it asks -- in the passage's own
    order and words. Nothing is rephrased or added; a passage whose opening
    already answers keeps its opening.
    """
    import re as _re
    from app.knowledge.embeddings import tokenize

    groups = _question_groups(question)
    # NO IMPLEMENTATION DETAIL reaches a user: a file path in the handbook
    # ("`app/config/policies/x.yaml`") is said as what it is
    body = _re.sub(r"`?[\w.-]*(?:/[\w.-]+)+\.(?:ya?ml|json|py|md|txt)`?", "a configured policy file", body)
    body = _re.sub(r"\|(\s*:?-{3,}:?\s*\|)+", " ", body)          # table rules
    body = _re.sub(r"\s*\|\s*\|\s*", " || ", body)                  # row boundaries
    # a heading, a table row, a list item is its own piece
    pieces = [p.strip(" |") for p in _re.split(
        r"(?<=[.!?])(?<!\b\d\.)\s+(?=[A-Z0-9*`(-])|(?<=:)\s+(?=-\s|\d+\.\s)|\s+(?=\d+\.\s)|\s+(?=-\s+\*\*)"
        r"|\s*#{2,}\s+|\s*\|\|\s*",
        body) if p.strip(" |")]
    pieces = [_re.sub(r"\s*\|\s*", " -- ", p) for p in pieces]
    # a list item keeps its own continuation sentences ("- **CONDITIONAL** --
    # needed because ... It is just as binding as REQUIRED ...")
    kept: list[str] = []
    merged_once = False
    for p in pieces:
        # ONE continuation sentence, and only after an item that is itself a
        # sentence -- a bare label ("- **EMPLOYMENT_PROOF**") has none
        if kept and not merged_once and _re.match(r"(-\s+|\d+\.\s)", kept[-1]) \
                and kept[-1].endswith(".") and not _re.match(r"(-\s+|\d+\.\s)", p) \
                and not p.endswith(":") and len(p.split()) > 2:
            kept[-1] = f"{kept[-1]} {p}"
            merged_once = True
        else:
            kept.append(p)
            merged_once = False
    pieces = kept
    # a heading on its own says nothing: joined to the line it heads
    joined: list[str] = []
    for p in pieces:
        if joined and len(joined[-1].split()) <= 4 and not joined[-1].endswith((".", ":")):
            joined[-1] = f"{joined[-1]}: {p}"
        else:
            joined.append(p)
    pieces = joined
    if not groups or len(pieces) <= keep:
        return body
    scored = []
    for index, piece in enumerate(pieces):
        words, text = set(tokenize(piece)), " ".join(piece.lower().split())
        covered = sum(1 for g in groups if _group_matched(g, None, words, text))
        scored.append((covered, -index, index))
    best = max(scored)
    if best[0] == 0:
        return " ".join(pieces[:keep])
    chosen = sorted(i for _c, _neg, i in sorted(scored, reverse=True)[:keep] if _c > 0)
    asks_list = _re.search(r"\b(which|what\s+are|list|steps|states|stages|kaun-?kaun|kin-?kin|kya\s+kya)\b|"
                           r"कौन-कौन|किन-किन", question, _re.I)
    if asks_list and chosen and _re.match(r"\d+\.\s", pieces[chosen[0]]):
        start = chosen[0]
        while start > 0 and _re.match(r"\d+\.\s", pieces[start - 1]):
            start -= 1
        end = start
        while end + 1 < len(pieces) and _re.match(r"\d+\.\s", pieces[end + 1]) and end - start < 7:
            end += 1
        items = [_re.sub(r"\s+--?\s+.*$|\s+—\s+.*$", "", pieces[i]) for i in range(start, end + 1)]
        return "; ".join(i.rstrip(".") for i in items) + "."
    # a sentence introducing a list ("Concretely:") brings its first item
    first = chosen[0]
    if pieces[first].endswith(":") and first + 1 < len(pieces) and first + 1 not in chosen:
        chosen = sorted(set(chosen[:keep - 1]) | {first, first + 1})
    return " ".join(pieces[i] for i in chosen)


def deterministic_answer(result, *, max_sentences: int | None = None,
                         question: str | None = None) -> str:
    """
    The retrieved passage, returned as written.

    Used when the model is off, unavailable, too slow or not wanted. Wordier
    than a phrased answer and entirely correct, which is the right trade when
    the alternative is nothing.

    `max_sentences` trims it. A chat reply carrying a whole handbook section
    -- numbered list, table and all -- is not an answer anybody reads.
    """
    if result is None or not result.confident or not result.hits:
        return NO_ANSWER

    top = result.hits[0].chunk
    body = " ".join(top.text.split())

    if question:
        body = _evidence(body, question, keep=max(2, max_sentences or 2))
    elif max_sentences:
        import re as _re

        sentences = _re.split(r"(?<=[.!?])\s+", body)
        body = " ".join(sentences[:max_sentences]).strip()

    if len(body) > 700:
        body = body[:700].rsplit(" ", 1)[0] + "…"
    return body


async def answer(
    question: str,
    *,
    limit: int = 3,
    allow_model: bool = True,
    max_sentences: int | None = None,
) -> tuple[str, str, dict]:
    """
    Answer a FOS knowledge question.

    Returns (answer, response_source, detail). `detail` carries the retrieval
    evidence -- citations and the top score -- so a caller can show where an
    answer came from, and so a reviewer can tell a refusal caused by a thin
    corpus from one caused by an off-topic question.

    `allow_model=False` skips phrasing entirely. The MIXED path uses it: that
    answer is already being composed from a computed half and a retrieved
    half, so phrasing one of them buys nothing and costs the model budget.
    Measured on a reachable-but-slow provider, phrasing the knowledge half of
    a mixed answer took the request from 3 ms to 2773 ms and then timed out
    and fell back to the retrieved text anyway.

    `max_sentences` trims the retrieved passage. A chat answer must not carry
    a handbook section.
    """
    started = time.perf_counter()
    result = retrieve(question, limit=limit)

    detail: dict[str, Any] = {
        "stage": STAGE,
        "retrieved": len(result.hits) if result else 0,
        "top_score": round(result.top_score, 4) if result else 0.0,
        "threshold": result.threshold if result else None,
        "confident": bool(result and result.confident),
        "citations": result.citations() if result else [],
        "versions": result.versions() if result else [],
    }

    if result is None or not result.confident:
        detail["refused"] = True
        return NO_ANSWER, "deterministic", detail

    if not allow_model or not config.llm_enabled():
        counters.record(called=False)
        return (deterministic_answer(result, max_sentences=max_sentences, question=question),
                "deterministic", detail)

    # GUARDED AT THE CALL SITE AS WELL, not only inside `_phrase`.
    #
    # `_phrase` promises to return None on any failure, and that promise was
    # broken once already -- an import above its try block raised straight
    # through and turned an answerable question into a 500. Phrasing is the
    # optional half of this path, so the caller does not rely on the callee
    # keeping its word about something this cheap to guarantee here.
    # WHAT THE MODEL IS SHOWN: the top passages, cut to whole sentences within
    # `chatbot.compose.knowledge_context_chars`. On this CPU every prompt token
    # costs ~8.5 ms on a new question, so the passage size IS the latency.
    context = _model_context(result, limit)
    try:
        phrased = await _phrase(question, context)
    except Exception:
        logger.exception("FOS knowledge phrasing raised; using retrieved text")
        phrased = None

    if phrased is None:
        return (deterministic_answer(result, max_sentences=max_sentences, question=question),
                "deterministic", detail)

    detail["processing_ms"] = round((time.perf_counter() - started) * 1000, 2)
    # WHAT THE MODEL WAS SHOWN, for the caller's unified validation (numbers,
    # dates, decision words must come from the passage). Internal: the
    # caller removes it before anything is published.
    detail["_passage"] = context
    return phrased, "llm", detail


def _model_context(result, limit: int) -> str:
    """The passages a model phrases from: bounded, whole sentences."""
    import re

    try:
        chars = int(config.chatbot("compose").get("knowledge_context_chars", 700))
        passages = int(config.chatbot("compose").get("knowledge_passages", 2))
    except (TypeError, ValueError):
        chars, passages = 700, 2
    text = result.context(limit=max(1, min(limit, passages)))
    if len(text) <= chars:
        return text
    kept = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(kept) + len(sentence) + 1 > chars:
            break
        kept = f"{kept} {sentence}".strip()
    return kept or text[:chars]


async def _phrase(question: str, context: str) -> str | None:
    """
    Ask the model to phrase the retrieved material. None on any failure.

    Follows the same shape as every other model call here -- reachability
    checked first, one bounded wait, availability marked on failure -- so a
    slow knowledge answer trips the same cooldown as a slow summary and stops
    being attempted for a while, rather than making every later request pay
    the timeout.

    A GENERATED ANSWER IS ONLY EVER PHRASING. It is built from the retrieved
    passages, never from the question alone, and returning None simply falls
    back to those passages as written.
    """
    # EVERY import is inside the try, including the framework's own.
    #
    # They were above it, and an import that failed raised straight out of a
    # function whose entire contract is "returns None on any failure" -- a
    # 500 on a question the service could answer perfectly well from the
    # retrieved text. Phrasing is the optional half of this path; nothing in
    # it may take down the answer.
    try:
        import asyncio

        from agent_framework import Message

        from app.llm import availability
        from app.llm.provider import create_ollama_client
        from app.security import guardrails

        # A RETRIEVED PASSAGE IS DATA, however it is worded.
        context = guardrails.untrusted(context)

        budget = config.llm_timeout_seconds()

        if not availability.provider_reachable():
            raise ConnectionError("model provider is not reachable")

        client = create_ollama_client()
        response = await asyncio.wait_for(
            client.get_response(
                [
                    Message(role="system", contents=[
                        _SYSTEM + " " + guardrails.UNTRUSTED_NOTICE]),
                    Message(role="user", contents=[
                        f"Reference material:\n\n{context}\n\n"
                        f"Question: {question}"
                    ]),
                ],
                stream=False,
                options={
                    "max_tokens": 160,
                    "temperature": config.temperature(),
                    "keep_alive": _keep_alive(),
                },
            ),
            timeout=budget,
        )
        counters.record(called=True)
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            return None
        return text.strip()

    except Exception as exc:
        from app.llm import availability as _availability

        _availability.mark_slow("FOS knowledge phrasing failed")
        logger.info(
            "FOS knowledge answer fell back to the retrieved text (%s: %s)",
            type(exc).__name__, exc,
        )
        return None


def _keep_alive() -> str:
    from app.agents.los.summary import keep_alive

    return keep_alive()


__all__ = ["NO_ANSWER", "STAGE", "answer", "deterministic_answer", "retrieve"]


def retrieved_text_for(question: str) -> str:
    """
    The retrieved passage for a question, as written.

    Used when a generated answer was rejected by grounding: the fallback must
    be the source material, never the rejected sentence with a note attached.
    """
    result = retrieve(question, limit=1)
    if result is not None and result.confident and result.hits:
        return deterministic_answer(result)
    return NO_ANSWER
