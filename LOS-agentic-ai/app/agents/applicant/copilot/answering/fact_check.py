"""
EVERY REPLY CHECKED AGAINST THE CASE'S FACTS BEFORE IT IS PUBLISHED (2026-11-06).

Whatever layer wrote a reply -- the engine, the simple pipeline, a fact line, Qwen's rewrite, a localized template --
the claims it makes about THIS case are checked against facts/case_facts.py (one source) and corrected in place:

    STAGE        "Stage: <x>" / "in the <x> stage" names the case's LOS stage   (the summary said "Application Created")
    COUNT        "Pending (N)", "N documents are pending", "N to clear"          = the records' count
    LOAN AMOUNT  "loan amount ... Rs X"                                           = the recorded amount
    READINESS    a sentence saying it IS ready for CPA when it is not, or the reverse (Qwen turned "Yes. Once ..." into
                 "No, the case can't move ..." -- a conditional is not a claim and is left alone)

Corrections are logged (which rule, never the text) so the layer that got it wrong can be found. The readiness words
are the ones polish already uses (copilot_reply.yaml polish.status_groups); stage names are the LOS stages' configured
labels. A rule that cannot decide leaves the sentence as it is.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)
STATS: dict[str, int] = {}

_NEGATION = re.compile(r"\b(not|no|never|cannot|can't|isn't|won't|nahi|nahin|nhi)\b|n't\b", re.IGNORECASE)
#: a sentence about the future or a condition ("once ...", "after ...", "as soon as ...") states no current fact
_CONDITIONAL = re.compile(r"\b(once|after|as\s+soon\s+as|when|if|jab|baad)\b", re.IGNORECASE)


def enabled() -> bool:
    """COPILOT_FACT_CHECK (default on)."""
    import os

    return os.getenv("COPILOT_FACT_CHECK", "true").strip().lower() not in {"0", "false", "no", "off"}


def _stage_names() -> dict[str, str]:
    """label (lower) -> stage code, for every LOS stage and every FOS workflow step."""
    from app.agents.applicant import config
    from app.agents.los.stages import LosStage

    names = {config.stage_label(s.value).lower(): s.value for s in LosStage}
    for step in config.workflow_states():
        names[step.replace("_", " ").lower()] = step
    return names


def _readiness_words() -> tuple[list[str], list[str]]:
    from app.agents.applicant.copilot.answering import polish

    groups = (polish._cfg().get("status_groups") or {})
    return ([str(w).lower() for w in groups.get("ready") or []],
            [str(w).lower() for w in groups.get("not_ready") or []])


def _fix(rule: str, fixes: list[str]) -> None:
    fixes.append(rule)
    STATS[rule] = STATS.get(rule, 0) + 1


def _stage(text: str, facts: Any, fixes: list[str]) -> str:
    if not facts.stage_label:
        return text
    names = _stage_names()
    right = facts.stage_label

    def stage_line(m: re.Match) -> str:
        said = m.group(2).strip().rstrip(".")
        if said.lower() == right.lower() or said.lower().startswith(right.lower()):
            return m.group(0)
        if said.lower() not in names:
            return m.group(0)                      # not a stage name we know: leave it
        _fix("stage_line", fixes)
        step = f" ({said})" if names[said.lower()] == facts.workflow_step else ""
        return f"{m.group(1)}{right}{step}"
    text = re.sub(r"(\bStage:\s*\**)([A-Za-z][A-Za-z ]{1,40}?)(?=\**\s*(?:\n|$|\.|,|\())", stage_line, text)

    def in_stage(m: re.Match) -> str:
        said = m.group(2)
        code = names.get(said.lower())
        if code is None or code == facts.stage or said.lower() == right.lower():
            return m.group(0)
        from app.agents.los.stages import LosStage

        if code not in {s.value for s in LosStage}:
            return m.group(0)                      # a workflow step "at Basic Document Verification" is a detail
        _fix("stage_phrase", fixes)
        return f"{m.group(1)}{right}{m.group(3)}"
    return re.sub(r"(\b(?:is\s+(?:in|at)|currently\s+(?:in|at))\s+the\s+)([A-Za-z][A-Za-z ]{1,25}?)(\s+stage\b)",
                  in_stage, text, flags=re.IGNORECASE)


def _counts(text: str, facts: Any, fixes: list[str]) -> str:
    docs = len(facts.pending_documents)

    def pending_paren(m: re.Match) -> str:
        if int(m.group(2)) == docs or not docs:
            return m.group(0)
        _fix("pending_count", fixes)
        return f"{m.group(1)}{docs}{m.group(3)}"
    text = re.sub(r"(\bPending\s*\()(\d+)(\))", pending_paren, text)

    def n_documents(m: re.Match) -> str:
        if int(m.group(1)) == docs or not docs:
            return m.group(0)
        _fix("pending_count", fixes)
        return f"{docs}{m.group(2)}"
    return re.sub(r"\b(\d+)(\s+documents?\s+(?:are|is)\s+(?:still\s+)?pending)\b", n_documents, text,
                  flags=re.IGNORECASE)


def _amount(text: str, facts: Any, fixes: list[str]) -> str:
    if facts.loan_amount is None:
        return text
    from app.agents.applicant.copilot.facts.document_facts import _rupees

    def amount(m: re.Match) -> str:
        said = float(m.group(3).replace(",", ""))
        if abs(said - facts.loan_amount) < 1:
            return m.group(0)
        _fix("loan_amount", fixes)
        return f"{m.group(1)}{m.group(2)}{_rupees(facts.loan_amount).lstrip('₹')}"
    return re.sub(r"(\bloan\s+amount\b[^₹\n]{0,25})(₹\s?|Rs\.?\s?|INR\s?)([\d,]+(?:\.\d+)?)", amount, text,
                  flags=re.IGNORECASE)


def _readiness(text: str, facts: Any, fixes: list[str]) -> str:
    ready_words, not_ready_words = _readiness_words()
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        low = sentence.lower()
        if _CONDITIONAL.search(sentence) or "cpa" not in low and "ready" not in low:
            out.append(sentence)
            continue
        says_ready = any(w in low for w in ready_words) and not _NEGATION.search(sentence) \
            and not any(w in low for w in not_ready_words)
        says_not = any(w in low for w in not_ready_words) or (
            _NEGATION.search(sentence) and re.search(r"\b(move|go|ready)\b[^.]{0,20}\bcpa\b", low))
        if says_ready and not facts.ready:
            _fix("readiness_claimed_ready", fixes)
            sentence = "Not ready for CPA yet."
        elif says_not and facts.ready and not re.search(r"\bnot\s+(yet\s+)?(been\s+)?(uploaded|verified)", low):
            _fix("readiness_claimed_not_ready", fixes)
            sentence = "This case is ready for CPA."
        out.append(sentence)
    return " ".join(out) if len(out) > 1 else (out[0] if out else text)


def correct(text: str, facts: Any) -> tuple[str, list[str]]:
    """(the text with every claim about the case made true to its facts, the rules that corrected something)."""
    if not text or facts is None:
        return text, []
    fixes: list[str] = []
    # links / actions are code, never claims: only the prose before the first action link is checked line by line
    lines = []
    for line in str(text).split("\n"):
        if "](action:" in line or "](ask:" in line:
            lines.append(line)
            continue
        line = _stage(line, facts, fixes)
        line = _counts(line, facts, fixes)
        line = _amount(line, facts, fixes)
        line = _readiness(line, facts, fixes)
        lines.append(line)
    if fixes:
        logger.info("fact_check corrected %s", ",".join(fixes))
    return "\n".join(lines), fixes


def _no_such_party(reply: dict[str, Any], facts: Any) -> bool:
    """
    THE QUESTION WAS ABOUT A CO-APPLICANT THE CASE DOES NOT HAVE, and the answer does not say so ("and for the
    co-applicant?" -> "No document needs any action right now" read as "the co-applicant is all clear"): the answer
    becomes the configured no-co-applicant line (conversation_general.yaml texts.no_co_applicant), in place.
    """
    if facts.has_co_applicant is not False:
        return False
    answer = str(reply.get("answer") or "")
    if re.search(r"co-?\s?applicant|co-?borrower|सह-?आवेदक|सह-?अर्जदार", answer, re.IGNORECASE):
        return False                                  # the answer already talks about the co-applicant
    try:
        from app.agents.applicant.copilot.semantics import meaning

        if meaning.party_in(str(reply.get("_question") or "")) != "CO_APPLICANT":
            return False
        from app.agents.applicant.copilot.answering import language_lock
        from app.agents.applicant.copilot.capabilities import general

        line = general._say_text((general.cfg().get("texts") or {}).get("no_co_applicant"),
                                 language_lock.current() or "en", case_id=facts.case_id)
    except Exception:  # noqa: BLE001 - cannot tell: the reply stands
        return False
    if not line:
        return False
    reply.update(answer=line, intent="NO_CO_APPLICANT")
    for key in ("tts_override", "tts_text"):
        if reply.get(key):
            reply[key] = line
    return True


def check_reply(reply: dict[str, Any], case_id: str | None) -> list[str]:
    """Correct a reply in place (answer, and the spoken text when it carries its own). The rules that fired."""
    if not isinstance(reply, dict) or not case_id or reply.get("category") == "CONVERSATION":
        return []
    from app.agents.applicant.copilot.facts import case_facts

    facts = case_facts.read(case_id)
    if facts is None:
        return []
    fired: list[str] = []
    if _no_such_party(reply, facts):
        fired.append("no_co_applicant")
        STATS["no_co_applicant"] = STATS.get("no_co_applicant", 0) + 1
        logger.info("fact_check corrected no_co_applicant")
        return fired
    for key in ("answer", "tts_override", "tts_text"):
        if isinstance(reply.get(key), str) and reply[key]:
            reply[key], fixes = correct(reply[key], facts)
            fired += fixes
    if fired:
        reply.setdefault("fact_check", sorted(set(fired)))
    return fired


__all__ = ["STATS", "check_reply", "correct", "enabled"]
