"""
REPLY POLISH (owner 2026-10-09: "feel like a real conversation ... if necessary send it to the LLM").

The deterministic engine writes the FACTS; the configured model (Qwen) may only REWORD a prose answer so it reads
like a person talking -- plain English, the answer first, no form-like phrasing. Config: copilot_reply.yaml `polish`.

It never decides anything and never adds a fact. A rewrite is used only when it passes every check, else the
engine's own text is shown unchanged:
  * every number, amount, id / code (CASE-..., PAN, KYC ...) and every configured document name of the original is
    still there, and no number appears that the original did not have;
  * no link, table or list is touched (an answer that has any is not polished at all);
  * not longer than the original (config max_growth), not shorter than config min_ratio of it;
  * no first-person company voice, no "as an AI", nothing the instructions forbid (config forbidden).
Slow / down / low RAM / off: the original. One model call per reply at most.
"""

from __future__ import annotations

from app.llm import turn_budget as _tb

import asyncio
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)
STATS = {"asked": 0, "used": 0, "rejected": 0, "skipped": 0, "failed": 0}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.answering import contract

    return contract.cfg().get("polish") or {}


def enabled() -> bool:
    flag = (os.getenv("COPILOT_POLISH") or "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(_cfg().get("enabled", False))


def _document_names() -> set[str]:
    from app.agents.applicant.copilot.semantics import meaning

    return {name.lower() for _, name in meaning._documents()}


def _document_aliases() -> set[str]:
    from app.agents.applicant.copilot.semantics import meaning

    return {re.sub(r"[^a-z\s]", " ", alias.lower()).strip() for alias, _ in meaning._documents() if len(alias) >= 3}


def lead() -> bool:
    """LEAD STYLE (owner, 2026-10-13: "real bot se baat kar raha hai, robot se nahi"): Qwen writes ONE natural opening
    line in the officer's own language; the engine's facts follow as clean points, untouched. Measured: a free 4B
    rewrite of the whole answer invented advice ("request confirmation from the employee"), deadlines ("within 48
    hours") and misleading emoji -- one short line is fast (~1-2 s) and checkable."""
    return str(_cfg().get("style") or "prose").strip().lower() == "lead"


def points() -> bool:
    """POINTS STYLE (owner, 2026-10-13): the answer as short bullets with at most `max_emoji` emoji (config
    `style: points`). The default `prose` style rewords a paragraph and leaves lists alone."""
    return str(_cfg().get("style") or "prose").strip().lower() in ("points", "lead", "natural")


def natural() -> bool:
    """NATURAL STYLE (owner, 2026-10-15: "pura natural banao, same time accurate"): Qwen rewrites the WHOLE answer as
    a colleague would say it; shown only when natural_accepted() proves it says the same facts -- every number and
    document kept, none added, each document with ITS status, no count / decision word the facts lack. Else the
    engine's card, unchanged."""
    return str(_cfg().get("style") or "prose").strip().lower() == "natural"


def _groups(text: str) -> set[str]:
    """The STATUS GROUPS a piece of text states (config polish.status_groups: verified / pending / rejected ...)."""
    low = " " + " ".join(re.sub(r"[^a-z\s'-]", " ", text.lower()).split()) + " "
    return {g for g, words in (_cfg().get("status_groups") or {}).items()
            if any(re.search(rf"(?<![a-z]){re.escape(str(w).lower())}(?![a-z])", low) for w in words or [])}


def _docs_in(text: str) -> set[str]:
    low = " " + " ".join(re.sub(r"[^a-z\s]", " ", text.lower()).split()) + " "
    return {n for n in _document_names() if f" {n} " in low}


def _doc_status(original: str) -> dict[str, set[str]]:
    """Document -> the status groups the ENGINE's text gives it: groups on the document's own line, else the nearest
    heading above it ("Still pending:" over "- Address Proof")."""
    out: dict[str, set[str]] = {}
    heading: set[str] = set()
    for raw in original.split("\n"):
        # sentence by sentence: "PAN is verified. Salary Slip is verified. Address Proof has not been uploaded."
        # on one line gives each document ITS status, not all three
        for line in re.split(r"(?<=[.!?])\s+", raw):
            groups, docs = _groups(line), _docs_in(line)
            if groups and (not docs or line.rstrip().endswith(":")):
                heading = groups
            for d in docs:
                out.setdefault(d, set()).update(groups or heading)
    return out


def natural_accepted(original: str, rewritten: str) -> bool:
    """The rewrite says exactly the engine's facts (see natural()). Clause by clause: a document named next to a
    status must have that status in the engine's text -- "PAN is pending" when PAN is verified is refused."""
    spec = _cfg()
    new = rewritten.strip()
    if not new or re.search(r"\]\(|^\s*\|", new, re.M):
        return False
    if len(new) > len(original) * float(spec.get("natural_max_growth", 1.4)) \
            or len(new) < len(original) * float(spec.get("natural_min_ratio", 0.3)):
        return False
    if len(_EMOJI.findall(new)) > int(spec.get("max_emoji", 2)) + len(_EMOJI.findall(original)):
        return False
    # A RECORD ID IS NOT A FACT TO REPEAT in conversation ("CASE-2903C0A2513E is stuck..." -> "This case is stuck...")
    # -- but an id the rewrite DOES write must be the engine's, and every other number / code (CPA, FOS, amounts) stays
    ids = re.compile(r"\b(?:CASE|APP|DOC|Q)-[0-9A-Z]{4,}\b")
    numbers, codes, _ = _facts(ids.sub(" ", original))
    new_numbers, new_codes, _ = _facts(ids.sub(" ", new))
    # A COUNT SAID AS A WORD is the same fact: "Pending (2)" -> "both pending documents" (config polish.count_word_values)
    values = {str(k).lower(): str(v) for k, v in (spec.get("count_word_values") or {}).items()}
    new_low_words = set(re.findall(r"[a-z]+", new.lower()))
    spoken = {values[w] for w in new_low_words if w in values}
    if spoken - numbers:
        return False                                   # a count word with no such number in the facts
    if (numbers - spoken) != new_numbers or not codes <= new_codes or not set(ids.findall(new)) <= set(ids.findall(original)):
        return False                                   # a number dropped / invented, a code dropped, an id invented
    if _docs_in(original) != _docs_in(new):
        return False                                   # a document dropped or one added
    if any(_times_named(d, new) > max(1, _times_named(d, original)) for d in _docs_in(new)):
        return False                                   # one named twice (2026-10-24 live: "Signature, and Signature")
    stem = lambda w: re.sub(r"(ed|es|s|ing)$", "", w)  # noqa: E731 - "needs" / "needed" / "need" are one word here
    said = {stem(w) for w in re.findall(r"[a-z]+", original.lower())}
    new_words = {w for w in new_low_words if stem(w) not in said}
    # "ALL" IS TRUE when every document the facts name has the same status ("PAN, Salary Slip and Bank Statement are
    # all verified", 2026-10-24); otherwise it stays a forbidden count word
    statuses = list(_doc_status(original).values())
    uniform = len(statuses) >= 2 and all(s and s == statuses[0] for s in statuses)
    # AN ORDER THE FACTS ALREADY GIVE (2026-10-24: "Upload the Address Proof first" under a Next step was refused):
    # config polish.natural_next_step_words are allowed when the facts carry a "Next step" line
    ordered = {str(w).lower() for w in spec.get("natural_next_step_words") or []} \
        if re.search(r"(?im)^\W*next(\s+step)?\s*:", original) else set()
    if any(w in new_words for w in (spec.get("no_new_words") or [])
           + [c for c in spec.get("lead_count_words") or [] if str(c).lower() not in values
              and str(c).lower() not in ordered
              and not (uniform and str(c).lower() == "all")]):
        return False                                   # a decision / count word the engine did not write
    if not _groups(new) <= _groups(original):
        return False                                   # a status the engine never said
    truth = _doc_status(original)
    for clause in re.split(r"[.;:\n]|,\s|\s[—–-]\s|\bwhile\b|\bbut\b|\bwhereas\b", new):
        groups, docs = _groups(clause), _docs_in(clause)
        if groups and any(truth.get(d) and not groups <= truth[d] for d in docs):
            return False                               # a document given a status that is not its own
    return not any(re.search(p, new, re.I) for p in spec.get("forbidden") or [])


def _times_named(doc: str, text: str) -> int:
    """How often a document (a lowercase name from _docs_in) is named in the text, as a whole phrase."""
    return len(re.findall(rf"(?<![a-z]){re.escape(doc)}(?![a-z])", text.lower()))


_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕⌚-⏿]")


def _polishable(text: str) -> bool:
    """Prose only (points style: lists too): no link, table, code or quoted message; long enough to be worth it."""
    floor = _cfg().get("natural_min_chars") if natural() else None
    if len(text) < int(floor if floor is not None else _cfg().get("min_chars", 80)):
        return False
    if points():
        return not re.search(r"\]\(|^\s*\||^>|`", text, re.M)
    return not re.search(r"\]\(|^\s*\||^\s*[-*•]\s|^\s*\d+\.\s|^>|`", text, re.M)


def _facts(text: str) -> tuple[set[str], set[str], set[str]]:
    # a number ends on a digit: "₹5,00,000." at a sentence end is the number 5,00,000 (2026-10-24)
    numbers = set(re.findall(r"\d(?:[\d,.]*\d)?", text))
    codes = set(re.findall(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)*\b", text))
    lowered = text.lower()
    documents = {d for d in _document_names() if d in lowered}
    return numbers, codes, documents


def accepted(original: str, rewritten: str) -> bool:
    """Every check of the module docstring; True only when the rewrite says the same facts, shorter or equal."""
    spec = _cfg()
    new = rewritten.strip()
    if not new or re.search(r"\]\(|^\s*\|", new, re.M):
        return False
    growth = float(spec.get("max_growth_points", 1.6) if points() else spec.get("max_growth", 1.05))
    if len(new) > len(original) * growth or len(new) < len(original) * float(spec.get("min_ratio", 0.4)):
        return False
    if len(_EMOJI.findall(new)) > int(spec.get("max_emoji", 2)) + len(_EMOJI.findall(original)):
        return False                                   # emoji are seasoning: at most `max_emoji` added
    numbers, codes, documents = _facts(original)
    new_numbers, new_codes, new_documents = _facts(new)
    if not numbers <= new_numbers or not new_numbers <= numbers:
        return False                                   # a number dropped, or one invented
    if not codes <= new_codes or not documents <= new_documents:
        return False                                   # an id / code / document name dropped
    said = set(re.findall(r"[a-z]+", original.lower()))
    if any(w in set(re.findall(r"[a-z]+", new.lower())) - said for w in spec.get("no_new_words") or []):
        return False                                   # a decision word the engine did not write
    return not any(re.search(p, new, re.I) for p in spec.get("forbidden") or [])


async def _ask(text: str, timeout: float, question: str | None = None) -> str | None:
    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    spec = _cfg()
    if natural():
        system = str(spec.get("instructions_natural") or "")
        # the facts WITHOUT the card's markup (bold, ticks, bullets), so the model writes speech, not a copy of the card
        plain = re.sub(r"\*\*|[✓⏳⚠✅❌]\s?", "", text)
        content = f"Officer asked: {str(question or '').strip()[:300]}\nFacts:\n{plain}"
        predict = int(spec.get("num_predict_natural", 110))
    else:
        system = str((spec.get("instructions_points") if points() else None) or spec.get("instructions") or "")
        content = text
        predict = int(spec.get("num_predict_points", 220) if points() else spec.get("num_predict", 120))
    body = {"model": str(spec.get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "options": with_num_ctx({"temperature": 0.0, "num_predict": predict})}
    _tb.check("polish", need=_tb.wording_need())   # only with time left for wording (app/llm/turn_budget.py)
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await asyncio.wait_for(_tb.guarded(client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body), "polish"), timeout)
        response.raise_for_status()
        return str(((response.json() or {}).get("message") or {}).get("content") or "").strip()


def lead_accepted(body: str, line: str) -> bool:
    """One line, short, no number / id at all, at most one emoji, no decision word or forbidden word the facts lack."""
    spec = _cfg()
    line = line.strip()
    if not line or "\n" in line or len(line) > int(spec.get("lead_max_chars", 140)):
        return False
    if re.search(r"\d", line) or len(_EMOJI.findall(line)) > int(spec.get("lead_max_emoji", 1)):
        return False                                   # a figure belongs to the facts below, never to the opener
    said = set(re.findall(r"[a-z]+", body.lower()))
    words = set(re.findall(r"[a-z]+", line.lower()))
    # THE LINE CARRIES THE GIST, NEVER A SPECIFIC (2026-10-14: "two signature uploads still pending" for one Address
    # Proof + one Signature): no document name and no count word -- the facts below name and count them
    if words & {str(w).lower() for w in spec.get("lead_count_words") or []}:
        return False
    mode = str(spec.get("lead_documents") or ("none" if spec.get("lead_no_documents", True) else "any")).lower()
    if mode in ("none", "in_facts"):
        low = " " + " ".join(re.sub(r"[^a-z\s]", " ", line.lower()).split()) + " "
        facts_low = " " + " ".join(re.sub(r"[^a-z\s]", " ", body.lower()).split()) + " "
        named = [n for n in _document_names() | _document_aliases() if f" {n} " in low]
        # "none": never a document; "in_facts" (2026-10-14: "none" dropped most lines and the officer waited for
        # nothing): only a document the facts below also name
        if named and (mode == "none" or any(f" {n} " not in facts_low for n in named)):
            return False
    if any(w in words - said for w in (spec.get("no_new_words") or []) + (spec.get("lead_forbidden_words") or [])):
        return False
    return not any(re.search(p, line, re.I) for p in spec.get("forbidden") or [])


def as_points(body: str) -> str:
    """The engine's facts as clean points: a paragraph becomes one point per sentence; lists / `Next step:` kept."""
    out = []
    for line in body.split("\n"):
        text = line.strip()
        if not text:
            continue
        if re.match(r"^[-*•]\s|^\d+\.\s|^\*\*", text) or text.endswith(":"):
            out.append(text)
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z*])", text) if s.strip()]
        out.extend(f"- {s}" for s in sentences) if len(sentences) > 1 else out.append(f"- {text}")
    return "\n".join(out)


_NEXT = re.compile(r"^[-*\u2022\s]*(?:\*\*)?(?:\U0001F449\s*)?(?:\*\*)?next(?: step)?\s*:?(?:\*\*)?\s*:?", re.I)
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+\.)\s+")


def brief(line: str, facts: str, intent: str = "") -> str:
    """
    TO THE POINT (owner, 2026-10-23: "2-3 lines but to point"; config polish.brief): under the natural line, only
    what it does not already say -- the configured housekeeping lines go, a point naming only documents the line
    already names goes, a list header left with no items goes; at most `max_points` points, then the Next line.
    Never a new word: lines are only kept or dropped.
    """
    spec = _cfg().get("brief") or {}
    if not spec.get("enabled") or intent in {str(i) for i in spec.get("skip_intents") or []}:
        return facts                                   # a SUMMARY was asked for: the whole card
    said = _docs_in(line) if line else set()           # no line: the trim only (natural mode's input)
    rows = [r for r in facts.split("\n") if r.strip()
            and not any(re.search(p, r.strip(), re.I) for p in spec.get("hide") or [])]
    nxt = next((r for r in rows if _NEXT.match(r.strip())), None)
    kept: list[str] = []
    for r in rows:
        if r is nxt:
            continue
        named = _docs_in(r)
        if named and named <= said and not r.rstrip().endswith(":"):
            continue                                   # the line already names every document this point does
        kept.append(r)
    # a header ("Still pending:") with none of its items left
    kept = [r for i, r in enumerate(kept)
            if not (r.rstrip().endswith(":") and not (i + 1 < len(kept) and _BULLET.match(kept[i + 1])))]
    # NO OPENER (natural mode's input): nothing is said yet, so nothing is cut -- a list that IS the answer keeps
    # every item (2026-10-24: "which documents are uploaded" lost Bank Statement); only housekeeping went above
    limit, out, points = (int(spec.get("max_points", 2)) if line else len(kept)), [], 0
    for r in kept:
        header = r.rstrip().endswith(":")
        if not header:
            if points >= limit:
                break
            points += 1
        out.append(r)
    while out and out[-1].rstrip().endswith(":"):
        out.pop()                                      # a header cut off from its items
    return "\n".join(out + ([nxt] if nxt else []))


def without_echo(line: str, facts: str) -> str:
    """The facts without a first sentence that only repeats the opener (2026-10-23: "Not ready for CPA yet; address
    and signature proofs missing." over "Not ready for CPA yet. Still to do: ..."). Only when EVERY word of that
    sentence is in the opener -- a sentence that adds anything stays."""
    lines = facts.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None or not line:
        return facts
    bullet = re.match(r"^\s*(?:[-*\u2022]\s+)?", lines[first]).group(0)
    text = lines[first][len(bullet):]
    sentence = re.match(r"^(.+?[.!?;])(?:\s+|$)", text)
    if not sentence:
        return facts
    words = re.findall(r"[a-z]+", sentence.group(1).lower())
    if len(words) < 2 or not set(words) <= set(re.findall(r"[a-z]+", line.lower())):
        return facts
    rest = text[sentence.end():].strip()
    lines[first] = (bullet + rest) if rest else None
    return "\n".join(ln for ln in lines if ln is not None).strip("\n")


async def _lead(body: str, question: str, timeout: float) -> str | None:
    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx

    spec = _cfg()
    content = f"Officer asked: {question.strip()[:300]}\nAnswer (facts):\n{body[:1200]}"
    payload = {"model": str(spec.get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
               "messages": [{"role": "system", "content": str(spec.get("instructions_lead") or "")},
                            {"role": "user", "content": content}],
               "options": with_num_ctx({"temperature": 0.3, "num_predict": int(spec.get("num_predict_lead", 40))})}
    _tb.check("polish", need=_tb.wording_need())   # only with time left for wording (app/llm/turn_budget.py)
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await asyncio.wait_for(_tb.guarded(client.post(f"{ollama_host().rstrip('/')}/api/chat", json=payload), "polish"),
                                          timeout)
        response.raise_for_status()
        return str(((response.json() or {}).get("message") or {}).get("content") or "").strip().strip('"')


def prime() -> None:
    """One real lead call at startup, so the first officer's line is not the one that reads the instructions cold
    (~6-11 s on CPU -> over the 4 s budget -> dropped). Best effort, never raises."""
    if not enabled() or not lead():
        return
    started = time.perf_counter()
    try:
        asyncio.run(_lead("Rahul Sharma -- Personal Loan\n- Pending: Address Proof\nNext: upload it.",
                          "summary of this case", 120.0))
        logger.info("polish lead: primed in %s ms", round((time.perf_counter() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 - a convenience: the first line is just dropped as before
        logger.info("polish lead: prime skipped (%s)", type(exc).__name__)


async def _natural(rest: str, question: str | None, intent: str) -> str | None:
    """THE ANSWER IN QWEN'S OWN WORDS (polish.style natural): it rewrites the SHORT facts (brief trim) and the rewrite
    is used only when natural_accepted proves it says exactly those facts. None: model busy / slow / down / rejected."""
    from app.agents.applicant.copilot.capabilities import qwen_agent as _qa_n
    from app.llm import availability
    from app.llm.memory import free_gb_for_model as free_gb

    free = free_gb()
    if (free is not None and free < float(_cfg().get("min_free_ram_gb", 2.0))) or not availability.provider_reachable():
        return None
    facts = brief("", as_points(rest), intent)
    STATS["asked"] += 1
    try:
        if _qa_n.spent() and not _cfg().get("lead_after_agent"):
            raise TimeoutError("the agent used this turn's model time")
        new = await _ask(facts, float(_cfg().get("natural_timeout_seconds", 5.0)), question=question)
    except Exception as exc:  # noqa: BLE001 - slow / down: the instant answer
        STATS["failed"] += 1
        logger.info("polish natural failed for intent %s (%s)", intent, type(exc).__name__)
        availability.warm_in_background()
        return None
    if not new or not natural_accepted(facts, new):
        STATS["rejected"] += 1
        logger.info("polish natural rejected for intent %s", intent)
        return None
    STATS["used"] += 1
    return new


#: the answer acts whose shape the deterministic line keeps (see apply)
_SHAPED_ACTS = frozenset({"CONDITIONAL", "YES_NO", "WHY", "WHY_NOT", "WHEN", "COUNT"})


async def apply(reply: Any, question: str | None = None, use_model: bool = True) -> Any:
    """The reply with its prose answer reworded when every check passes; else unchanged (never raises).
    use_model=False (the simple pipeline): only the instant natural line from the records, never a model call."""
    if not enabled() or not isinstance(reply, dict) or reply.get("polished"):
        return reply
    text = str(reply.get("answer") or "")
    # THE BUTTONS STAY THE ENGINE'S (points style): the action / ask links and [Close] at the end of an answer are
    # set aside, only the text above them is reworded, and they are put back unchanged
    chrome = ""
    if points():
        lines = text.rstrip().split("\n")
        cut = len(lines)
        while cut > 0 and (not lines[cut - 1].strip() or re.search(r"\]\((action|ask):", lines[cut - 1])):
            cut -= 1
        text, chrome = "\n".join(lines[:cut]).rstrip(), "\n".join(lines[cut:]).strip()
    head, sep, rest = text.partition("\n") if re.match(r"^CASE-[0-9A-F]+\s*$", text.split("\n")[0]) else ("", "", text)
    only = {str(i) for i in _cfg().get("only_intents") or []}
    bullet = re.fullmatch(r"\s*-\s+([^\n]+)\s*", rest)
    if bullet:
        rest = bullet.group(1)                         # ONE bullet line is a sentence, not a list
    intent = str(reply.get("intent") or "")
    never = {str(i) for i in _cfg().get("never_intents") or []}
    if (only and intent not in only) or intent in never or not _polishable(rest):
        STATS["skipped"] += 1
        return reply
    # A QUESTION WITH A SHAPE (2026-11-06, live eval): a yes / no, a why, a when, an "after this, can it ...?", a count
    # is answered by the deterministic line built for that shape (fact_line, the learned answer act) -- Qwen's rewrite
    # turned "Yes. Once PAN ... are uploaded" into "No, the case can't move to CPA yet" and dropped the reasons of
    # "why are they pending?". Those turns keep the engine's words; every other answer is still Qwen's.
    from app.agents.applicant.copilot.answering import fact_line as _shape_line

    shaped = _shape_line.applies(intent) and _shape_line.act_of(question or "") in _SHAPED_ACTS
    if natural() and use_model and not shaped:
        # QWEN'S OWN WORDS FIRST (owner 2026-10-24); slow / rejected -> the instant answer below
        said = await _natural(rest, question, intent)
        if said:
            out = dict(reply)
            out["answer"] = ((head + sep + said) if head else said) + (f"\n\n{chrome}" if chrome else "")
            out.pop("answer_markdown", None)
            out["polished"] = True
            return out
    # A DATA ANSWER: the natural line from its own records -- instant, no model (answering/fact_line.py)
    from app.agents.applicant.copilot.answering import fact_line as _fact_line

    if _fact_line.applies(intent):
        line = _fact_line.line(reply, question or "")
        if line:
            STATS["fact_line"] = STATS.get("fact_line", 0) + 1
            out = dict(reply)
            body = f"{line}\n\n{brief(line, without_echo(line, as_points(rest)), intent)}".rstrip()
            out["answer"] = ((head + sep + body) if head else body) + (f"\n\n{chrome}" if chrome else "")
            out.pop("answer_markdown", None)
            out["polished"] = True
            return out
        return reply                                   # no clear situation: the card alone, still instant
    if not use_model:
        return reply
    from app.llm import availability
    from app.llm.memory import free_gb_for_model as free_gb

    free = free_gb()
    if (free is not None and free < float(_cfg().get("min_free_ram_gb", 2.0))) or not availability.provider_reachable():
        STATS["skipped"] += 1
        return reply
    STATS["asked"] += 1
    if natural():
        return reply                                   # Qwen already tried above: the engine's card
    if lead():
        # ONE natural line from Qwen + the facts as points (the facts are the engine's, byte for byte)
        facts = as_points(rest)
        from app.agents.applicant.copilot.capabilities import qwen_agent as _qa

        try:
            if _qa.spent() and not _cfg().get("lead_after_agent"):
                raise TimeoutError("the agent used this turn's model time")
            line = await _lead(rest, question or "", float(_cfg().get("lead_timeout_seconds", 6.0)))
        except Exception as exc:  # noqa: BLE001 - slow / down: the facts alone, still as points
            STATS["failed"] += 1
            logger.info("polish lead failed for intent %s (%s)", intent, type(exc).__name__)
            availability.warm_in_background()
            line = None
        if line and not lead_accepted(rest, line):
            STATS["rejected"] += 1
            logger.info("polish lead rejected for intent %s", intent)
            line = None
        STATS["used"] += 1 if line else 0
        if not line:
            # NO LINE (slow / rejected): the engine's answer EXACTLY as built -- a case card keeps its own layout,
            # never re-bulleted ("- Rahul Sharma — ..." on the name line, 2026-10-15)
            return reply
        out = dict(reply)
        body = (f"{line}\n\n{brief(line, without_echo(line, facts), intent)}".rstrip() if line else facts)
        out["answer"] = ((head + sep + body) if head else body) + (f"\n\n{chrome}" if chrome else "")
        out.pop("answer_markdown", None)
        out["polished"] = bool(line)
        return out
    try:
        new = await _ask(rest, float(_cfg().get("timeout_seconds", 4.0)))
    except Exception:  # noqa: BLE001 - slow / down: the engine's own words
        STATS["failed"] += 1
        availability.warm_in_background()
        return reply
    if not new or not accepted(rest, new):
        STATS["rejected"] += 1
        logger.info("polish rejected for intent %s", reply.get("intent"))
        return reply
    STATS["used"] += 1
    out = dict(reply)
    out["answer"] = ((head + sep + new) if head else new) + (f"\n\n{chrome}" if chrome else "")
    out.pop("answer_markdown", None)                   # the bold version described the old wording
    out["polished"] = True
    return out


__all__ = ["STATS", "accepted", "apply", "enabled"]
