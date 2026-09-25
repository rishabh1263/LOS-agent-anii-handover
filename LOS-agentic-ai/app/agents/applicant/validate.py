"""
Checking a generated answer before it is allowed anywhere near a FOS screen.

THE RULE: every fact in the answer must already be in the data the model was
shown. Not "probably derived from" -- present. A summary that invents a
document, a status, a name or a number is discarded whole and the
deterministic answer is used in its place. Nothing is salvaged from a rejected
answer, because a sentence that is half invented is still wrong.

What is checked:

    length          an empty or runaway answer is not an answer
    structure       JSON or markup means the model ignored its instructions
    numbers         every numeric token must appear in the facts
    statuses        every status word must be one the facts carry
    verdicts        approval, rejection and scoring language is refused
                    outright -- the FOS stage does not decide those, so no
                    phrasing of them can be grounded

This is the same discipline as app/agents/los/summary.py, applied to a larger
answer with more fields to get wrong.
"""

from __future__ import annotations

import re
from typing import Any

MIN_ANSWER_CHARS = 8
MAX_ANSWER_CHARS = 700

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

#: Status vocabulary the FOS stage uses. A status word in the answer must be
#: one the facts actually contain.
_STATUS_WORDS = {
    "MISSING", "UPLOADED", "PROCESSING", "VERIFIED", "REVIEW", "REJECTED",
    "READY_FOR_CPA", "NOT_READY", "APPLICATION_CREATED", "DOCUMENT_COLLECTION",
    "BASIC_DOCUMENT_VERIFICATION", "PASS", "FAIL", "SKIPPED",
}

#: Language this agent must never produce, whatever the data says. These are
#: downstream decisions; there is no grounded way to phrase one here.
_FORBIDDEN = (
    r"\bapproved?\b", r"\bsanction\w*\b", r"\bdisburs\w+\b",
    r"\bcredit\s*score\b", r"\bcibil\b", r"\beligib\w+\s+for\s+\w+\s+loan\b",
    r"\brecommend\w*\s+(approval|rejection)\b", r"\bcreditworth\w*\b",
)
_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN]


def _allowed_numbers(facts: Any) -> set[str]:
    """Every numeric token the model is permitted to reproduce."""
    allowed: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            allowed.add(str(node))
            if isinstance(node, float) and node.is_integer():
                allowed.add(str(int(node)))
        elif isinstance(node, str):
            for token in _NUMBER.findall(node):
                allowed.add(token)

    walk(facts)

    # Counts the model may legitimately state about what it was shown: "three
    # documents", "2 items pending". Derived from the data's own shape, so
    # they are facts rather than invention.
    def counts(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                counts(value)
        elif isinstance(node, list):
            allowed.add(str(len(node)))
            for value in node:
                counts(value)

    counts(facts)
    allowed.add("0")
    return allowed


def _allowed_statuses(facts: Any) -> set[str]:
    """Status words present anywhere in the facts."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            upper = node.upper()
            for word in _STATUS_WORDS:
                if word in upper:
                    found.add(word)

    walk(facts)
    return found


def validate_answer(text: str, facts: dict[str, Any]) -> tuple[bool, str]:
    """
    Check a generated answer against the facts it was given.

    Returns (accepted, cleaned_text_or_reason).
    """
    if not isinstance(text, str):
        return False, "answer was not a string"

    cleaned = _THINK.sub("", text).strip().strip('"').strip()

    if len(cleaned) < MIN_ANSWER_CHARS:
        return False, "answer too short"
    if len(cleaned) > MAX_ANSWER_CHARS:
        return False, "answer too long"
    if cleaned.lstrip().startswith(("{", "[")):
        return False, "answer returned structured data"

    for pattern in _FORBIDDEN_RE:
        match = pattern.search(cleaned)
        if match:
            return False, f"answer used downstream decision language: {match.group(0)}"

    allowed_numbers = _allowed_numbers(facts)
    for token in _NUMBER.findall(cleaned):
        variants = {token}
        if "." in token:
            variants.add(token.rstrip("0").rstrip("."))
        if not (variants & allowed_numbers):
            return False, f"answer contained unsupported number: {token}"

    # STATUS TOKENS, not ordinary English.
    #
    # Checked against the answer as written rather than upper-cased, because
    # almost every status word here is also a normal word: "the address is
    # missing" and "documents under review" are prose, while "PAN is MISSING"
    # is the model quoting a system status. Upper-casing the answer first made
    # the first two indistinguishable from the third, and rejected correct
    # sentences for using English.
    #
    # The narrower rule still catches what matters: a model claiming a
    # document is VERIFIED or REJECTED when the data says otherwise. A
    # lowercase paraphrase of a status it was not given is not caught here --
    # it is bounded instead by the model only ever being shown true facts, by
    # the number check above, and by the forbidden-language check before it.
    allowed_statuses = _allowed_statuses(facts)
    for word in _STATUS_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", cleaned) and word not in allowed_statuses:
            return False, f"answer asserted an unsupported status: {word}"

    return True, cleaned


# ==========================================================================
# A COMPOSED ANSWER, CHECKED AGAINST THE ANSWER THE RECORDS GIVE
# ==========================================================================
#
# `validate_answer` asks "did the model state anything it was not shown?".
# That is necessary and not sufficient. A model shown a case under review
# because two names differ can answer "your application is being
# processed" -- every word true, no fact invented, and the one thing the
# officer needed left out. It can print the case id it saw in the
# evidence. It can say REVIEW where the record says a document is
# pending.
#
# `check_composed` holds a generated sentence to the DETERMINISTIC answer
# the records produced -- the `structured` answer, which is always
# computed first -- and rejects it when it:
#
#   leaks          an internal id or an internal reason code
#   drops          a name, a pending document or a hold the record states
#   invents        a name, or a hold the record does not state
#   runs long      beyond `chatbot.response` limits
#
# Every check is switched by `chatbot.validation` in applicant_agent.yaml.
# A rejected answer is never repaired: the caller publishes the structured
# answer instead.

#: An internal identifier, whatever case it belongs to.
_ID_PATTERNS = re.compile(
    r"\bcase_[0-9a-f]{8,}\b|\b(CASE|APP|DEMO-APP|DEMO-CASE)-[A-Z0-9-]{4,}\b"
    r"|\b(cp|aa|los|r)_[0-9a-f]{16,}\b",
    re.IGNORECASE,
)

#: An internal reason or status code: UPPER_SNAKE with at least one join.
_CODE = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")

#: Two or more consecutive ALL-CAPS words: a recorded name or value.
_NAME = re.compile(r"\b[A-Z][A-Z.'-]+(?:\s+[A-Z][A-Z.'-]+)+\b")

#: Acronyms that are ordinary words in an answer, not recorded values.
_ACRONYMS = frozenset({
    "PAN", "KYC", "FOS", "CPA", "RCU", "BOPS", "HOPS", "ITR", "FOIR", "LTV",
    "EMI", "DOB", "ID", "OK", "LOS", "NA", "UPI", "IFSC", "ATM", "PDF",
})

#: "Address Proof is still pending", "X and Y are pending" -- the documents
#: a deterministic status answer names as outstanding.
_PENDING = re.compile(
    r"([A-Z][\w ,]*?)\s+(?:is|are)\s+(?:still\s+)?pending\b")

#: Holds a record can state, and the words that carry each one.
_HOLDS = {
    "under review": re.compile(r"\breview", re.IGNORECASE),
    "was declined": re.compile(r"\b(declin|reject)", re.IGNORECASE),
}


#: A single ALL-CAPS token long enough to be a recorded value, not a word
#: of emphasis: "PRIYANKAROHANMORE" as a bank records an account holder.
_SINGLE = re.compile(r"\b[A-Z][A-Z'-]{5,}\b")


def _names(text: str) -> set[str]:
    found = set()
    for match in _NAME.findall(text or ""):
        words = [w for w in match.split() if w.upper() not in _ACRONYMS]
        if len(words) >= 2:
            found.add(" ".join(words).upper())
    # A one-token value, unless it is a status word -- statuses are checked
    # by validate_answer, and an acronym is not a value.
    for token in _SINGLE.findall(text or ""):
        if (token not in _ACRONYMS and token not in _STATUS_WORDS
                and not any(token in name for name in found)):
            found.add(token)
    return found


def _pending(text: str) -> set[str]:
    found: set[str] = set()
    for match in _PENDING.findall(text or ""):
        tail = match.split(".")[-1]
        for part in re.split(r",|\band\b", tail):
            part = re.sub(r"\b\d+\s+more\b", "", part).strip()
            if part and part[0].isupper():
                found.add(part.lower())
    return found


def required_facts(structured: str) -> dict[str, set[str]]:
    """What a composed answer must keep from the deterministic one."""
    return {
        "names": _names(structured),
        "pending": _pending(structured),
        "holds": {h for h in _HOLDS if h in (structured or "")},
    }


def check_composed(
    text: str,
    *,
    structured: str,
    identifiers: tuple[str | None, ...] = (),
    evidence: str = "",
    stage: str | None = None,
) -> tuple[bool, str]:
    """
    Whether a generated answer may replace the structured one.

    Returns (accepted, cleaned_text_or_reason). `evidence` is any further
    text the model was shown (retrieved case evidence), which widens what
    it may mention -- never what it may leave out.
    """
    from app.agents.applicant import config

    if not isinstance(text, str):
        return False, "answer was not a string"
    cleaned = " ".join(_THINK.sub("", text).split()).strip().strip('"')
    if not config.validation("enabled"):
        return True, cleaned
    if not cleaned:
        return False, "answer was empty"

    # -- length ---------------------------------------------------------
    if len(cleaned) > config.max_characters():
        return False, "answer too long"
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if len(sentences) > config.max_sentences():
        return False, "answer has too many sentences"

    # -- leaks ----------------------------------------------------------
    if config.validation("reject_internal_id_leak") and not config.expose_internal_ids():
        lowered = cleaned.lower()
        if any(i and str(i).lower() in lowered for i in identifiers):
            return False, "answer exposed an internal identifier"
        if _ID_PATTERNS.search(cleaned):
            return False, "answer exposed an internal identifier"
    if not config.expose_internal_reason_codes() and _CODE.search(cleaned):
        return False, f"answer exposed an internal code: {_CODE.search(cleaned).group(0)}"
    # A RAW STATUS TOKEN -- "verification status PASS" -- that the structured
    # answer does not itself use is system vocabulary, not an answer.
    if not config.expose_internal_reason_codes():
        for word in _STATUS_WORDS:
            if (re.search(rf"\b{re.escape(word)}\b", cleaned)
                    and not re.search(rf"\b{re.escape(word)}\b", structured or "")):
                return False, f"answer exposed an internal status: {word}"

    known = f"{structured or ''} {evidence or ''}"
    required = required_facts(structured)

    # -- nothing the record states is dropped ---------------------------
    if config.validation("reject_missing_required_facts"):
        upper = cleaned.upper()
        for name in required["names"]:
            if name not in upper:
                return False, f"answer dropped a recorded value: {name}"
        lowered = cleaned.lower()
        for document in required["pending"]:
            if document not in lowered:
                return False, f"answer dropped a pending document: {document}"

    # -- no hold is dropped, and none is invented -----------------------
    if config.validation("reject_status_change"):
        for hold in required["holds"]:
            if not _HOLDS[hold].search(cleaned):
                return False, f"answer dropped the recorded hold: {hold}"
        for hold, words in _HOLDS.items():
            if words.search(cleaned) and not words.search(known):
                return False, f"answer asserted a hold not on record: {hold}"

    # -- no stage but the one the case is in ---------------------------
    #
    # THE MODEL NEVER DECIDES THE STAGE. It is told the current stage; an
    # answer placing the case at any stage that neither the current stage
    # nor the structured answer names is a stage the model inferred.
    if stage and config.validation("reject_status_change"):
        from app.agents.applicant.intents import _STAGE_WORDS

        for name, words in _STAGE_WORDS.items():
            if name == str(stage).upper():
                continue
            said = any(re.search(rf"\b{re.escape(w)}\b", cleaned, re.I)
                       for w in words)
            recorded = any(re.search(rf"\b{re.escape(w)}\b", known, re.I)
                           for w in words)
            if said and not recorded:
                return False, f"answer named a stage not on record: {name}"

    # -- no name that the records do not carry --------------------------
    if config.validation("reject_hallucination"):
        allowed = _names(known)
        for name in _names(cleaned):
            if name not in allowed and not any(name in a or a in name
                                               for a in allowed):
                return False, f"answer named a value not on record: {name}"

    return True, cleaned


def validate_response_shape(payload: dict[str, Any]) -> list[str]:
    """
    Check the outgoing envelope carries no internals.

    A second belt on top of the response model: the model cannot add keys, but
    a future edit to the assembly code could, and this is cheap.
    """
    forbidden_keys = {
        "prompt", "prompts", "system_prompt", "raw", "raw_response",
        "chain_of_thought", "reasoning", "traceback", "stack", "sql",
        "query", "connection", "token", "jwt", "secret", "password",
        "ocr_tokens", "bbox", "candidates",
    }
    problems: list[str] = []

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in forbidden_keys:
                    problems.append(f"{trail}.{key}")
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{trail}[{index}]")

    walk(payload, "response")
    return problems


__all__ = ["check_composed", "required_facts", "validate_answer",
           "validate_response_shape"]
