"""
Document validation rules applied AFTER extraction.

WHY THIS EXISTS. Structural verification in basic.py answers "is this a
legible document of the claimed class, carrying an identifier of the right
shape?" That is necessary and it is not sufficient. Measured against the
sample corpus before this module existed, EVERY readable PAN card reached
PASS -- including a photograph of a computer screen and a black-and-white
photocopy -- because nothing after the identifier regex was ever checked.

What is checked here, and could not be checked earlier:

    required fields    a PAN with no name and no date of birth is not a
                       verified PAN, however clean the number is
    structure          the identifier's own internal rules, beyond its shape
    consistency        fields that must agree with each other
    plausibility       dates that cannot be true

These need the EXTRACTED fields, which by design do not exist until
verification has already passed. So this runs after extraction and may only
ever DOWNGRADE a verdict -- PASS to REVIEW, REVIEW to FAIL. It can never
promote one, so no rule here can turn a failed structural check into a pass.

AUTHENTICITY IS NOT CHECKED HERE, AND CANNOT BE. Nothing in this repository
can establish that a document was issued by the authority it names. There is
no government lookup, no issuer API and no signature-of-issuer to verify. See
`authenticity_policy()` for what the service does about that, and what a PASS
therefore does and does not mean.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Verdict ordering, so a rule can only ever make things worse.
#:
#: ADVISORY sits below PASS deliberately: an advisory finding is REPORTED but
#: never changes a verdict. It is how a check that is meaningful but not
#: reliable enough to refuse a document on earns its place -- surfaced for a
#: reviewer, powerless over the outcome.
_SEVERITY = {"ADVISORY": -1, "PASS": 0, "REVIEW": 1, "FAIL": 2}


def _worst(current: str, proposed: str) -> str:
    return proposed if _SEVERITY.get(proposed, 0) > _SEVERITY.get(current, 0) else current


def _severity(document_class: str, name: str, default: str) -> str:
    """
    How hard a named check bites: `advisory`, `review` or `fail`.

    Per-check and configurable, because "is this signal trustworthy enough to
    refuse a customer's document?" is a policy question with a different
    answer per lender and per check.
    """
    section = _rules_section().get("documents", {}) or {}
    entry = section.get(document_class.upper(), {}) or {}
    configured = str(
        (entry.get("severity", {}) or {}).get(name, default)
    ).strip().lower()
    return {"advisory": "ADVISORY", "review": "REVIEW",
            "fail": "FAIL", "blocking": "REVIEW"}.get(configured, "ADVISORY")


# ==========================================================================
# CONFIGURATION
# ==========================================================================

def _rules_section() -> dict[str, Any]:
    from app.services.verification_config import _load

    return (_load().get("verification", {}) or {}).get("rules", {}) or {}


def rules_enabled() -> bool:
    """
    Whether post-extraction validation runs at all.

    Switchable because it tightens existing verdicts, and an operator
    upgrading mid-flight may want to land the change deliberately. Off means
    the older, weaker behaviour -- which is why it defaults ON.
    """
    import os

    override = (os.getenv("VERIFICATION_RULES_ENABLED") or "").strip().lower()
    if override in {"true", "1", "yes", "on"}:
        return True
    if override in {"false", "0", "no", "off"}:
        return False
    return bool(_rules_section().get("enabled", True))


def authenticity_policy() -> str:
    """
    What a PASS is allowed to mean.

    STRUCTURAL_PASS (default)
        A document that satisfies every structural and field rule may reach
        PASS. This is NOT a claim that the document is genuine -- only that
        nothing checkable about it is wrong. Every response carries
        `authenticity: NOT_ESTABLISHED` so the distinction survives into the
        caller's data rather than living only in this docstring.

    REQUIRE_EXTERNAL
        Authenticity must be confirmed by an authoritative external source.
        No such source is wired up in this build, so identity documents are
        capped at REVIEW with AUTHENTICITY_NOT_ESTABLISHED. Correct for a
        lender that will not treat an unverified card as verified; it means
        no case reaches READY_FOR_CPA on documents alone until an issuer
        check exists.

    Set VERIFICATION_AUTHENTICITY_POLICY to override.
    """
    import os

    override = (os.getenv("VERIFICATION_AUTHENTICITY_POLICY") or "").strip().upper()
    if override in {"STRUCTURAL_PASS", "REQUIRE_EXTERNAL"}:
        return override
    configured = str(
        (_rules_section().get("authenticity", {}) or {}).get("policy", "")
    ).strip().upper()
    return configured if configured in {"STRUCTURAL_PASS", "REQUIRE_EXTERNAL"} \
        else "STRUCTURAL_PASS"


def _required_fields(document_class: str) -> list[str]:
    section = _rules_section().get("documents", {}) or {}
    entry = section.get(document_class.upper(), {}) or {}
    return [str(f) for f in (entry.get("required_fields") or [])]


def _field_aliases(document_class: str) -> dict[str, list[str]]:
    """
    Other names a required field may arrive under.

    WHY THIS EXISTS, and what it cost to not have it.

    A required field is named in configuration; the value is produced by an
    extractor that names its own keys. Nothing checked that the two agreed,
    and for VOTER_ID they did not: configuration asked for `voter_id`, the
    extractor emitted `epic_number`. The number was read correctly, at 0.95
    confidence, and every voter ID in the service still came back
    REQUIRED_FIELD_MISSING -> REVIEW. The defect was invisible because each
    side was individually right.

    THE ALIAS IS NOT THE REAL FIX. It makes the two vocabularies
    reconcilable, which is worth having -- a lender's configuration should
    not have to know an extractor's internal key names. What stops the
    class of bug recurring is the contract test in
    tests/agents/test_required_field_contract.py, which asserts that every
    configured required field is producible by that type's extractor spec,
    through aliases or directly. Adding a required field nobody extracts
    now fails there instead of silently reviewing every document.

    Read per type, then merged with a global map, so a name shared across
    types is declared once.
    """
    rules = _rules_section()
    merged: dict[str, list[str]] = {}

    for source in (rules.get("field_aliases") or {},
                   (rules.get("documents", {}) or {})
                   .get(document_class.upper(), {}).get("field_aliases") or {}):
        if not isinstance(source, dict):
            continue
        for canonical, alternatives in source.items():
            if isinstance(alternatives, str):
                alternatives = [alternatives]
            merged.setdefault(str(canonical), [])
            merged[str(canonical)].extend(
                str(a) for a in (alternatives or [])
            )

    return merged


def _resolve_field(
    name: str,
    fields: dict[str, Any],
    aliases: dict[str, list[str]],
) -> Any:
    """
    The value for a required field, under its own name or any alias.

    The canonical name is tried FIRST. An alias is a fallback for a
    vocabulary difference, never a way to shadow the real field: a document
    carrying both must be read as carrying the one configuration asked for.
    """
    value = fields.get(name)
    if str(value or "").strip():
        return value

    for alternative in aliases.get(name, ()):
        value = fields.get(alternative)
        if str(value or "").strip():
            return value

    return None


def _rule_on(document_class: str, name: str, default: bool = True) -> bool:
    section = _rules_section().get("documents", {}) or {}
    entry = section.get(document_class.upper(), {}) or {}
    checks = entry.get("checks", {}) or {}
    return bool(checks.get(name, default))


# ==========================================================================
# PAN
# ==========================================================================

#: The fourth character of a PAN is a holder-type code from a fixed set.
#: Anything outside it is not a PAN however well the rest matches the shape.
#: Same set the extractor and the classifier already use.
PAN_HOLDER_TYPES = "ABCFGHLJPTKE"

PAN_STRUCTURE = re.compile(rf"^[A-Z]{{3}}[{PAN_HOLDER_TYPES}][A-Z]\d{{4}}[A-Z]$")


def _check_pan(fields: dict[str, Any]) -> list[tuple[str, str, str]]:
    """
    PAN-specific rules. Returns (verdict, code, detail) per finding.

    Every rule here is offline and deterministic. None of them establishes
    that the card is genuine.
    """
    findings: list[tuple[str, str, str]] = []
    number = str(fields.get("pan_number") or "").strip().upper()

    if number and _rule_on("PAN", "structure"):
        if not PAN_STRUCTURE.match(number):
            # The shape may be right while the holder-type character is not,
            # which no AAAAA9999A regex would catch.
            findings.append((
                "FAIL", "PAN_STRUCTURE_INVALID",
                "The PAN does not satisfy the AAAAA9999A structure with a "
                "valid holder-type character in position four.",
            ))

    # The fifth character is the first letter of the holder's surname -- a
    # real consistency check between two independently read fields, and one
    # of the few meaningful things checkable without an issuer.
    #
    # ADVISORY BY DEFAULT, and that is a measured decision rather than a
    # timid one. Run against the real sample corpus as a blocking rule it
    # rejected a GENUINE PAN: a black-and-white card whose printed name OCR
    # read without the surname the fifth character refers to. The check is
    # sound; the name field it depends on is not read reliably enough to
    # refuse a customer's document on.
    #
    # So it is reported and not acted on. Set severity to `blocking` in
    # documents.yaml where a reviewer would rather see false positives than
    # miss the signal.
    name = str(fields.get("name") or "").strip().upper()
    if number and name and len(number) >= 5 and _rule_on("PAN", "name_initial"):
        initial = number[4]
        words = [w for w in re.split(r"\s+", name) if w]
        # Card name order varies, so a match on ANY word's initial is
        # accepted; only a complete absence is worth reporting.
        if words and not any(w[0] == initial for w in words):
            findings.append((
                _severity("PAN", "name_initial", "advisory"),
                "PAN_NAME_INITIAL_MISMATCH",
                f"The PAN's fifth character ({initial}) matches no initial in "
                f"the printed name.",
            ))

    return findings


# ==========================================================================
# SHARED
# ==========================================================================

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _check_dates(document_class: str, fields: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Dates that cannot be true say something about the read or the card."""
    findings: list[tuple[str, str, str]] = []
    if not _rule_on(document_class, "date_plausible"):
        return findings

    today = date.today()

    dob = _parse_date(fields.get("date_of_birth"))
    if dob:
        if dob > today:
            findings.append((
                "REVIEW", "DOB_IN_FUTURE",
                "The date of birth is in the future.",
            ))
        else:
            age = (today - dob).days / 365.25
            if age > 120:
                findings.append((
                    "REVIEW", "DOB_IMPLAUSIBLE",
                    "The date of birth implies an implausible age.",
                ))
            elif age < 18:
                findings.append((
                    "REVIEW", "APPLICANT_UNDER_18",
                    "The date of birth implies the holder is under 18.",
                ))

    valid_till = _parse_date(fields.get("valid_till"))
    if valid_till and valid_till < today:
        findings.append((
            "FAIL", "DOCUMENT_EXPIRED",
            f"The document expired on {valid_till.isoformat()}.",
        ))

    return findings


def _check_required_fields(
    document_class: str,
    fields: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """
    Fields the document must actually carry.

    A PAN card with a clean number, no name and no date of birth was reaching
    PASS. It is a readable card; it is not a verified identity document.
    """
    required = _required_fields(document_class)
    if not required:
        return []

    aliases = _field_aliases(document_class)
    missing = [
        name for name in required
        if _resolve_field(name, fields, aliases) is None
    ]
    if not missing:
        return []

    return [(
        "REVIEW", "REQUIRED_FIELD_MISSING",
        f"Not read from the document: {', '.join(missing)}.",
    )]


#: document class -> its own rule function.
_CLASS_RULES = {
    "PAN": _check_pan,
}


# ==========================================================================
# ENTRY POINT
# ==========================================================================

def apply(
    *,
    document_class: str,
    status: str,
    fields: dict[str, Any] | None,
    reason_codes: list[str] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    """
    Apply post-extraction validation.

    Returns (status, reason_codes, detail). The status may only be the same
    or worse than the one passed in.

    `detail` carries the authenticity position, which is reported on every
    identity document regardless of the verdict -- a caller has to be able to
    tell "nothing checkable is wrong" from "confirmed genuine", and only one
    of those is something this service can offer.
    """
    codes = list(reason_codes or [])
    detail: dict[str, Any] = {}
    document_class = (document_class or "").upper()

    is_identity = document_class in {"PAN", "DRIVING_LICENCE", "VOTER_ID",
                                     "PASSPORT", "AADHAAR"}

    if is_identity:
        # Stated on every identity document, at every verdict. Under
        # STRUCTURAL_PASS a PASS means "structurally valid and internally
        # consistent" -- never "issued by the authority it names".
        detail["authenticity"] = "NOT_ESTABLISHED"
        detail["authenticity_policy"] = authenticity_policy()

    if not rules_enabled():
        detail["rules_applied"] = False
        return status, codes, detail

    detail["rules_applied"] = True

    # FAIL is terminal. Extraction was withheld, so there are no fields to
    # validate and nothing a rule here could add.
    if status == "FAIL":
        return status, codes, detail

    findings: list[tuple[str, str, str]] = []
    safe_fields = fields or {}

    findings += _check_required_fields(document_class, safe_fields)
    findings += _check_dates(document_class, safe_fields)

    rule = _CLASS_RULES.get(document_class)
    if rule is not None:
        findings += rule(safe_fields)

    for verdict, code, message in findings:
        if verdict == "ADVISORY":
            # Reported beside the verdict, never folded into it, and never
            # added to the public reason codes -- a note on a clean document
            # reads as a problem with it.
            detail.setdefault("advisories", []).append(
                {"code": code, "detail": message}
            )
            continue

        status = _worst(status, verdict)
        if code not in codes:
            codes.append(code)
        detail.setdefault("findings", []).append({"code": code, "detail": message})

    # Authenticity, last, so it is applied to whatever the rules concluded.
    if is_identity and authenticity_policy() == "REQUIRE_EXTERNAL":
        status = _worst(status, "REVIEW")
        if "AUTHENTICITY_NOT_ESTABLISHED" not in codes:
            codes.append("AUTHENTICITY_NOT_ESTABLISHED")
        detail.setdefault("findings", []).append({
            "code": "AUTHENTICITY_NOT_ESTABLISHED",
            "detail": ("No authoritative external source is configured, so "
                       "this document cannot be confirmed genuine."),
        })

    return status, codes, detail


__all__ = [
    "PAN_HOLDER_TYPES", "PAN_STRUCTURE", "apply", "authenticity_policy",
    "rules_enabled",
]
