"""
THE UNIFIED VALIDATOR -- one standard for every model-written sentence.

Every place a language model's words can reach a person goes through
`validate` here, with the same checks in the same spirit:

    surface            where it is used                       caller
    applicant_answer   FOS agent answer                       validate.validate_answer
    copilot_composer   Universal Copilot composition          validate_answer + check_composed
    knowledge_phrase   handbook phrasing                      grounding.validate (applicant)
    case_summary       FOS case summary                       case_summary._validated
    los_summary        LOS pipeline summary                   los.summary.validate_llm_summary
    fraud_summary      fraud/risk summary                     fraud_risk.summary.validate_llm_summary
    document_workflow  document-verification agent reply      document_verification.agent

THE COMMON CHECKS (every surface):

    reasoning     a <think> block is removed before anything is judged
    leakage       the output guardrail: code, paths, SQL, secrets, tokens,
                  tool payloads, prompts, stack traces (guardrails.check_output)
    shape         empty / runaway / JSON-or-markup answers
    decisions     approval, sanction, disbursement, credit-score, eligibility
                  and recommendation language -- no model decides these
    numbers       every number must appear in the truth it was given
    dates         every date must appear in the truth it was given

Then the SURFACE'S OWN checks, passed in as `extra`: status vocabulary (FOS),
computed verdicts and whose-outcome (LOS), risk category and outcome (fraud),
and -- for composed answers -- `validate.check_composed`, which holds the
sentence to the deterministic answer (no dropped name / pending document /
hold, no invented stage, party, action, id or code).

TRUTH FIRST. The structured truth is computed before any model runs; this
module only decides whether the model's WORDING of that truth may be
published. A rejection is never repaired: the caller publishes its
deterministic answer instead. Qwen is never the source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable

Check = Callable[[str], "str | None"]

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: Language no model may produce on any surface: these are decisions made
#: (if at all) by an authoritative stage, never phrased into being.
DECISION_LANGUAGE = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bapproved?\b", r"\bsanction\w*\b", r"\bdisburs\w+\b",
    r"\bcredit\s*score\b", r"\bcibil\b", r"\beligib\w+\s+for\s+\w+\s+loan\b",
    r"\brecommend\w*\s+(approval|rejection)\b", r"\bcreditworth\w*\b",
))

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"), start=1)}
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),                     # 2024-03-12
    re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b"),             # 12/03/2024
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b"),
    re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"),
)


@dataclass(frozen=True)
class Surface:
    name: str
    min_chars: int
    max_chars: int
    #: How a reason names the text: "answer" or "summary" (kept per surface
    #: so existing callers' reasons read as they always have).
    noun: str = "answer"
    #: Run the guardrail before the shape checks (the summaries always did).
    guardrail_first: bool = False
    #: Count list lengths and 0 as statable numbers ("three documents").
    count_numbers: bool = True
    #: Accept "12.50" for 12.5 (the fraud summary formats money this way).
    two_decimals: bool = False
    #: Decision words are allowed when the truth itself uses them. ONLY for
    #: handbook phrasing, where the truth is a process passage ("disbursement
    #: follows HOPS") rather than a case -- a case answer never gets this.
    decisions_from_truth: bool = False


SURFACES: dict[str, Surface] = {s.name: s for s in (
    Surface("applicant_answer", 8, 700),
    Surface("copilot_composer", 8, 700),
    Surface("knowledge_phrase", 8, 700, count_numbers=False,
            decisions_from_truth=True),
    Surface("case_summary", 8, 700),
    Surface("los_summary", 20, 400, noun="summary", guardrail_first=True,
            count_numbers=False),
    Surface("fraud_summary", 15, 320, noun="summary", guardrail_first=True,
            count_numbers=False, two_decimals=True),
    Surface("document_workflow", 1, 4000),
)}


@dataclass(frozen=True)
class Result:
    accepted: bool
    #: The cleaned text when accepted; the reason when rejected.
    value: str
    #: Which check decided: "passed", or the failing check's name.
    check: str = "passed"

    def pair(self) -> tuple[bool, str]:
        return self.accepted, self.value


# -- primitives (shared; no other module keeps its own copy) ------------------

def strip_reasoning(text: str) -> str:
    return _THINK.sub("", text or "").strip().strip('"').strip()


def allowed_numbers(truth: Any, *, counts: bool = True,
                    two_decimals: bool = False) -> set[str]:
    """Every numeric token the model may reproduce from `truth`."""
    allowed: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple, set, frozenset)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            allowed.add(str(node))
            if float(node).is_integer():
                allowed.add(str(int(node)))
            if two_decimals:
                allowed.add(f"{float(node):.2f}")
        elif isinstance(node, str):
            allowed.update(NUMBER.findall(node))

    walk(truth)
    if counts:
        def lengths(node: Any) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    lengths(value)
            elif isinstance(node, list):
                allowed.add(str(len(node)))
                for value in node:
                    lengths(value)
        lengths(truth)
        allowed.add("0")
    return allowed | {v.rstrip("0").rstrip(".") for v in allowed if "." in v}


def unsupported_number(text: str, allowed: set[str]) -> str | None:
    for token in NUMBER.findall(text):
        variants = {token}
        if "." in token:
            variants.add(token.rstrip("0").rstrip("."))
        if not variants & allowed:
            return token
    return None


def decision_language(text: str) -> str | None:
    for pattern in DECISION_LANGUAGE:
        match = pattern.search(text or "")
        if match:
            return match.group(0)
    return None


def _as_date(a: str, b: str, c: str, order: str) -> date | None:
    try:
        if order == "ymd":
            return date(int(a), int(b), int(c))
        if order == "dmy":
            return date(int(c), int(b), int(a))
        if order == "d_mon_y":
            month = _MONTHS.get(b[:3].lower())
            return date(int(c), month, int(a)) if month else None
        if order == "mon_d_y":
            month = _MONTHS.get(a[:3].lower())
            return date(int(c), month, int(b)) if month else None
    except (TypeError, ValueError):
        return None
    return None


def dates_in(text: str) -> set[date]:
    found: set[date] = set()
    for pattern, order in zip(_DATE_PATTERNS, ("ymd", "dmy", "d_mon_y", "mon_d_y")):
        for groups in pattern.findall(text or ""):
            parsed = _as_date(*groups, order)
            if parsed:
                found.add(parsed)
    return found


def _strings(truth: Any) -> Iterable[str]:
    if isinstance(truth, dict):
        for value in truth.values():
            yield from _strings(value)
    elif isinstance(truth, (list, tuple, set, frozenset)):
        for value in truth:
            yield from _strings(value)
    elif truth is not None and not isinstance(truth, bool):
        yield str(truth)


def unsupported_date(text: str, truth: Any) -> str | None:
    said = dates_in(text)
    if not said:
        return None
    known: set[date] = set()
    for value in _strings(truth):
        known |= dates_in(value)
        # ISO timestamps: 2024-03-12T10:00:00
        for match in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})T", value):
            parsed = _as_date(*match, "ymd")
            if parsed:
                known.add(parsed)
    for when in sorted(said):
        if when not in known:
            return when.isoformat()
    return None


# -- the validator -------------------------------------------------------------

def validate(text: Any, *, surface: str, truth: Any,
             extra: Iterable[Check] = ()) -> Result:
    """
    Whether a model-written text may be published on `surface`.

    `truth` is the structured data the model was shown (and nothing else);
    `extra` are the surface's own checks, each returning a reason or None.
    """
    spec = SURFACES[surface]
    noun = spec.noun
    if not isinstance(text, str):
        return Result(False, f"{noun} was not a string", "type")
    cleaned = strip_reasoning(text)

    from app.security import guardrails

    def guard() -> Result | None:
        verdict = guardrails.check_output(cleaned)
        if not verdict.allowed:
            return Result(False, f"{noun} failed the output guardrail: "
                                 f"{verdict.category.value}", "leakage")
        return None

    def shape() -> Result | None:
        if len(cleaned) < spec.min_chars:
            return Result(False, f"{noun} too short", "shape")
        if len(cleaned) > spec.max_chars:
            return Result(False, f"{noun} too long", "shape")
        if cleaned.lstrip().startswith(("{", "[")):
            return Result(False, f"{noun} returned structured data", "shape")
        return None

    for step in ((guard, shape) if spec.guardrail_first else (shape, guard)):
        failed = step()
        if failed:
            return failed

    # THE TRUTH MAY BE LAZY: a surface whose payload is costly (or cannot be
    # built from a malformed input) passes a callable, resolved only once the
    # leakage and shape checks have passed.
    if callable(truth):
        truth = truth()

    said = decision_language(cleaned)
    if said and spec.decisions_from_truth and re.search(
            rf"\b{re.escape(said)}", " ".join(_strings(truth)), re.IGNORECASE):
        said = None
    if said:
        return Result(False, f"{noun} used downstream decision language: {said}",
                      "decision_language")

    # DATES BEFORE NUMBERS: a date is judged as a date (12/03/2024 is the
    # 12th of March, whatever order its digits are in), and once a date is
    # known to be on record its digits are not re-judged as loose numbers.
    when = unsupported_date(cleaned, truth)
    if when is not None:
        return Result(False, f"{noun} contained unsupported date: {when}", "date")
    undated = cleaned
    for pattern in _DATE_PATTERNS:
        undated = pattern.sub(" ", undated)

    number = unsupported_number(undated, allowed_numbers(
        truth, counts=spec.count_numbers, two_decimals=spec.two_decimals))
    if number is not None:
        return Result(False, f"{noun} contained unsupported number: {number}",
                      "number")

    for check in extra:
        reason = check(cleaned)
        if reason:
            return Result(False, reason, getattr(check, "__name__", "surface"))

    return Result(True, cleaned)


__all__ = ["DECISION_LANGUAGE", "NUMBER", "Result", "SURFACES", "Surface",
           "allowed_numbers", "dates_in", "decision_language", "strip_reasoning",
           "unsupported_date", "unsupported_number", "validate"]
