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


__all__ = ["validate_answer", "validate_response_shape"]
