"""
Fast document verification.

Answers one question cheaply: is this a readable document of the class the
caller claims it is?

WHAT THIS IS NOT
    It is not an authenticity check. It cannot tell a well-made forgery from a
    genuine card, because that needs photo-level analysis of substrate,
    microprint and security features. Calling this "real or fake" would
    overstate it, and a credit officer relying on that framing would be
    misled.

    What it does catch is the common case: the wrong document uploaded, an
    unreadable scan, a blank page, and an identifier that does not match the
    published format for its type.

There are two verification entry points:

    quick_verify()
        Legacy/public-compatible path. Owns its OCR pass.

    quick_verify_from_tokens()
        Shared-evidence path for the unified Document Agent. The caller owns
        OCR and supplies the already-recognised OCR tokens and optional image.
        This function NEVER invokes OCR.

The shared-evidence path exists so the unified Document Agent can perform:

    upload -> OCR -> classification -> extraction -> verification

without running OCR twice.
"""

from __future__ import annotations

import logging
import os
import re
import time
from enum import Enum

from pydantic import BaseModel, Field

from app.core.text_match import FUZZY_WINDOW, build_index, caption_matches

logger = logging.getLogger(__name__)


from app.agents.verification import slots as _slots


class DocumentClass(str, Enum):
    PAN = "PAN"
    DRIVING_LICENCE = "DRIVING_LICENCE"
    VOTER_ID = "VOTER_ID"
    PASSPORT = "PASSPORT"
    AADHAAR = "AADHAAR"
    MARK_SHEET = "MARK_SHEET"
    SALE_DEED = "SALE_DEED"
    SALARY_SLIP = "SALARY_SLIP"
    BANK_STATEMENT = "BANK_STATEMENT"
    ITR = "ITR"
    UNKNOWN = "UNKNOWN"


# Captions that identify each class. Checked against compact OCR text and
# scored rather than first-match because document vocabularies overlap.
_MARKERS: dict[DocumentClass, tuple[tuple[str, float], ...]] = {
    DocumentClass.PAN: (
        ("INCOMETAXDEPARTMENT", 0.35),
        ("PERMANENTACCOUNTNUMBER", 0.35),
        ("INCOMETAX", 0.25),
        ("ACCOUNTNUMBER", 0.25),
        ("GOVTOFINDIA", 0.05),
    ),
    DocumentClass.DRIVING_LICENCE: (
        ("DRIVINGLICENCE", 0.40),
        ("DRIVINGLICENSE", 0.40),
        ("AUTHORISATIONTODRIVE", 0.25),
        ("VALIDTILL", 0.10),
        ("DLNO", 0.20),
    ),
    DocumentClass.VOTER_ID: (
        ("ELECTIONCOMMISSIONOFINDIA", 0.35),
        ("ELECTORPHOTOIDENTITYCARD", 0.35),
        # Real cards print the possessive ("ELECTOR'S PHOTO IDENTITY CARD"),
        # which compacts to an extra S mid-caption. The identity classifier in
        # document_agent/classify.py already carries both spellings; this
        # table did not, so the same card scored differently depending on
        # which classifier looked at it.
        ("ELECTORSPHOTOIDENTITYCARD", 0.35),
        ("ELECTIONCOMMISSION", 0.25),
        ("ELECTORSNAME", 0.15),
    ),
    DocumentClass.PASSPORT: (
        ("PASSPORT", 0.30),
        ("REPUBLICOFINDIA", 0.20),
        ("PLACEOFISSUE", 0.15),
    ),
    DocumentClass.AADHAAR: (
        ("UNIQUEIDENTIFICATIONAUTHORITY", 0.35),
        ("AADHAAR", 0.30),
        ("GOVERNMENTOFINDIA", 0.10),
        ("MERAAADHAARMERIPEHCHAN", 0.20),
    ),
    DocumentClass.MARK_SHEET: (
        ("STATEMENTOFMARKS", 0.30),
        ("MARKSHEET", 0.30),
        ("MARKSSTATEMENT", 0.30),
        ("BOARDOFSECONDARYEDUCATION", 0.25),
        ("SECONDARYSCHOOLEXAMINATION", 0.25),
        ("CENTRALBOARDOFSECONDARY", 0.25),
        ("ROLLNO", 0.10),
        ("TOTALMARKS", 0.15),
        ("GRADE", 0.05),
        ("SUBJECT", 0.05),
    ),
    DocumentClass.SALE_DEED: (
        ("SALEDEED", 0.35),
        ("DEEDOFSALE", 0.35),
        ("CONVEYANCEDEED", 0.30),
        ("SUBREGISTRAR", 0.20),
        ("STAMPDUTY", 0.15),
        ("VENDOR", 0.10),
        ("PURCHASER", 0.10),
        ("SCHEDULEOFPROPERTY", 0.20),
    ),
    DocumentClass.SALARY_SLIP: (
        ("SALARYSLIP", 0.35),
        ("PAYSLIP", 0.35),
        ("SALARYSTATEMENT", 0.30),
        ("NETPAY", 0.20),
        ("GROSSSALARY", 0.20),
        ("EARNINGS", 0.10),
        ("DEDUCTIONS", 0.10),
        ("DATEOFJOINING", 0.15),
        # Statutory contractor wage slips (Contract Labour Act FORM XIX).
        # Common for contract-labour applicants and worded nothing like a
        # corporate payslip: "Wage Slip", "Name of Workman", "Basic Wages",
        # "Gross Earning", "Net paid amount". A real sample scored 0.15 on
        # the captions above, fell through to UNKNOWN, and that cost a full
        # OCR pass on a PDF whose text layer was already readable.
        ("WAGESLIP", 0.35),
        ("FORMXIX", 0.30),
        ("NAMEOFWORKMAN", 0.30),
        ("BASICWAGES", 0.25),
        ("NETPAIDAMOUNT", 0.25),
        ("RATEOFDAILYWAGES", 0.25),
        ("GROSSEARNING", 0.20),
    ),
    DocumentClass.BANK_STATEMENT: (
        ("STATEMENTOFACCOUNT", 0.30),
        ("ACCOUNTSTATEMENT", 0.30),
        ("TXNDATE", 0.20),
        ("TRANSACTIONDATE", 0.20),
        ("VALUEDATE", 0.15),
        ("CLOSINGBALANCE", 0.20),
        ("OPENINGBALANCE", 0.15),
        ("WITHDRAWAL", 0.15),
        ("DEPOSIT", 0.10),
        ("NARRATION", 0.15),
        ("DEBIT", 0.10),
        ("CREDIT", 0.10),
        ("BALANCE", 0.10),
        ("ACCOUNTNUMBER", 0.10),
        ("IFSC", 0.15),
        ("BRANCH", 0.05),
    ),
    DocumentClass.ITR: (
        ("INCOMETAXRETURNACKNOWLEDGEMENT", 0.40),
        ("ACKNOWLEDGEMENTNUMBER", 0.25),
        ("ASSESSMENTYEAR", 0.20),
        ("FILEDU", 0.10),
    ),
}


# Identifier formats.
_IDENTIFIERS: dict[DocumentClass, tuple[re.Pattern, str]] = {
    DocumentClass.PAN: (
        re.compile(
            r"\b[A-Z]{3}[ABCFGHLJPTKE][A-Z]\d{4}[A-Z]\b"
        ),
        "five letters, four digits, one letter",
    ),
    DocumentClass.VOTER_ID: (
        re.compile(r"\b[A-Z]{3}\d{7}\b"),
        "three letters and seven digits",
    ),
    DocumentClass.AADHAAR: (
        re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),
        "twelve digits",
    ),
    DocumentClass.PASSPORT: (
        re.compile(r"\b[A-Z]\d{7}\b"),
        "one letter and seven digits",
    ),
    DocumentClass.DRIVING_LICENCE: (
        re.compile(
            r"\b[A-Z]{2}[-\s]?\d{2}[-\s]?\d{4,11}\b"
        ),
        "state code, RTO code and serial",
    ),
}


MIN_TOKENS = 4
MIN_CLASS_SCORE = 0.30


_SIGNATURE_CAPTIONS = (
    "SIGNATURE",
    "SIGN",
    "HOLDERSSIGNATURE",
    "SIGNATUREOFHOLDER",
    "SIGNATURETHUMB",
    "THUMBIMPRESSION",
    "HASTAKSHAR",
)


_SIGNATURE_EXPECTED = {
    DocumentClass.PAN,
    DocumentClass.DRIVING_LICENCE,
    DocumentClass.PASSPORT,
    DocumentClass.VOTER_ID,
}


BLANK_PAGE_TOKENS = 2

POOR_SCAN_CONFIDENCE = 0.55


_EXPIRY_LABELS = {
    DocumentClass.DRIVING_LICENCE: (
        "VALIDTILL",
        "VALIDUPTO",
        "VALIDTO",
        "EXPIRY",
    ),
    DocumentClass.PASSPORT: (
        "DATEOFEXPIRY",
        "VALIDUNTIL",
        "EXPIRY",
    ),
}


_DATE_IN_TEXT = re.compile(
    r"\b(\d{1,2})[-/.](\d{1,2}|[A-Za-z]{3})[-/.](\d{2,4})\b"
)


def _latest_plausible_date(
    text: str,
    expiry_labels: tuple[str, ...] = (),
):
    """
    Find an expiry date anchored to its expiry caption.

    We intentionally do not search the whole document when expiry labels are
    available because birth and issue dates are unrelated to expiry.
    """
    from datetime import date as _date
    from datetime import datetime as _dt

    window = text or ""

    if expiry_labels:
        positions = [
            match.end()
            for label in expiry_labels
            for match in re.finditer(
                re.escape(label),
                window,
                re.IGNORECASE,
            )
        ]

        if not positions:
            return None

        window = " ".join(
            text[position:position + 40]
            for position in positions
        )

    best = None

    for match in _DATE_IN_TEXT.finditer(window):
        raw = "-".join(match.groups())

        for fmt in (
            "%d-%m-%Y",
            "%d-%b-%Y",
            "%d-%m-%y",
            "%d-%b-%y",
        ):
            try:
                parsed = _dt.strptime(
                    raw,
                    fmt,
                ).date()
            except ValueError:
                continue

            if (
                1990
                <= parsed.year
                <= _date.today().year + 60
            ):
                if best is None or parsed > best:
                    best = parsed

            break

    return best


def rotate_enabled() -> bool:
    """Whether to retry a sideways document."""
    return (
        os.getenv(
            "VERIFY_TRY_ROTATIONS",
            "true",
        )
        or "true"
    ).lower() == "true"


def min_confidence() -> float:
    raw = (
        os.getenv("VERIFY_MIN_CONFIDENCE") or ""
    ).strip()

    try:
        return float(raw) if raw else 0.30
    except ValueError:
        return 0.30


def _ink_below(
    image,
    token,
    height_factor: float = 2.2,
) -> float:
    """
    Estimate whether there is ink below a signature caption.

    This detects the presence of marks only. It does not authenticate a
    signature or establish that the marks belong to the document holder.
    """
    try:
        import numpy as np

        top = int(token.y1)
        bottom = int(
            token.y1
            + token.height * height_factor
        )

        caption_width = max(
            1.0,
            token.x1 - token.x0,
            image.width * 0.35,
        )

        left = int(
            max(
                0,
                token.x0 - caption_width * 0.2,
            )
        )

        right = int(
            min(
                image.width,
                token.x1 + caption_width * 1.4,
            )
        )

        if bottom <= top or right <= left:
            return 0.0

        crop = image.convert("L").crop(
            (
                left,
                top,
                right,
                bottom,
            )
        )

        pixels = np.asarray(
            crop,
            dtype="uint8",
        )

        if pixels.size == 0:
            return 0.0

        return float(
            (pixels < 128).sum()
        ) / float(pixels.size)

    except Exception as exc:
        logger.debug(
            "Signature strip could not be measured: %s",
            exc,
        )
        return 0.0


def signature_ink_threshold() -> float:
    raw = (
        os.getenv(
            "VERIFY_SIGNATURE_MIN_INK"
        )
        or ""
    ).strip()

    try:
        return float(raw) if raw else 0.02
    except ValueError:
        return 0.02


def _verdict_from_text(
    text: str,
    requested_class: str | None,
    started: float,
) -> "QuickVerification":
    """
    Classify and verify a document from an existing text layer.
    """
    compact = re.sub(
        r"[^A-Z0-9]",
        "",
        text.upper(),
    )

    spaced = re.sub(
        r"\s{2,}",
        " ",
        re.sub(
            r"[^A-Z0-9\s/-]",
            " ",
            text.upper(),
        ),
    )

    detected, score = classify(compact)

    checks: list[QuickCheck] = [
        QuickCheck(
            name="document_legible",
            passed=True,
            detail=(
                f"{len(text)} characters in the text layer"
            ),
        ),
        QuickCheck(
            name="document_class_identified",
            passed=(
                detected
                is not DocumentClass.UNKNOWN
            ),
            detail=(
                f"{detected.value} "
                f"(score {score})"
            ),
        ),
    ]

    reasons: list[str] = []

    if detected is DocumentClass.UNKNOWN:
        reasons.append(
            "DOC_CLASS_UNRECOGNISED"
        )

    identifier = None

    pattern = _IDENTIFIERS.get(detected)

    if pattern is not None:
        match = pattern[0].search(spaced)

        identifier = (
            match.group(0)
            if match
            else None
        )

        checks.append(
            QuickCheck(
                name="identifier_format_valid",
                passed=identifier is not None,
                detail=(
                    f"found {identifier}"
                    if identifier
                    else (
                        "no value matching "
                        f"{pattern[1]}"
                    )
                ),
            )
        )

        if identifier is None:
            reasons.append(
                "IDENTIFIER_NOT_FOUND"
            )

    if requested_class:
        wanted = (
            requested_class
            .upper()
            .replace("-", "_")
        )

        if wanted not in {
            "AUTO",
            "ANY",
        }:
            # One of the classes the assertion accepts, not one class.
            # ADDRESS_PROOF is a checklist SLOT: a licence, a passport and a
            # voter ID each satisfy it. A plain class name still resolves to
            # itself, so this is no looser -- a PAN against ADDRESS_PROOF is
            # still a mismatch.
            matched = _slots.satisfies(wanted, detected.value)

            checks.append(
                QuickCheck(
                    name="matches_requested_class",
                    passed=matched,
                    detail=(
                        f"requested {wanted}, "
                        f"found {detected.value}"
                    ),
                )
            )

            if not matched:
                reasons.append(
                    "DOC_CLASS_MISMATCH"
                )

    by_name = {
        check.name: check
        for check in checks
    }

    hard = (
        "document_class_identified",
        "matches_requested_class",
    )

    if any(
        not by_name[name].passed
        for name in hard
        if name in by_name
    ):
        status = "FAIL"

    elif "IDENTIFIER_NOT_FOUND" in reasons:
        status = "REVIEW"

    else:
        status = "PASS"

    return QuickVerification(
        document_class=detected,
        requested_class=requested_class,
        status=status,
        confidence=round(
            sum(
                1
                for check in checks
                if check.passed
            )
            / max(1, len(checks)),
            4,
        ),
        checks=checks,
        reason_codes=reasons,
        identifier_found=identifier,
        processing_ms=round(
            (
                time.perf_counter()
                - started
            )
            * 1000,
            2,
        ),
    )


class QuickCheck(BaseModel):
    name: str
    passed: bool
    detail: str | None = None


class QuickVerification(BaseModel):
    """
    A fast structural verdict.

    `status` is PASS, FAIL or REVIEW.

    `authenticity_checked` is always False so callers cannot interpret a
    structural PASS as proof that a document is genuine.
    """

    document_class: DocumentClass
    requested_class: str | None = None
    status: str
    confidence: float = 0.0
    authenticity_checked: bool = False
    checks: list[QuickCheck] = Field(
        default_factory=list
    )
    reason_codes: list[str] = Field(
        default_factory=list
    )
    identifier_found: str | None = None
    processing_ms: float = 0.0
    note: str = (
        "Structural verification only: document class, "
        "legibility and identifier format. This does not "
        "establish authenticity."
    )
    retry_hint: str | None = None


def _marker_hits(
    markers: tuple[tuple[str, float], ...],
    text: str,
    index: set[str] | None = None,
    fuzzy_text: str | None = None,
) -> float:
    """
    Score document markers against OCR text.

    Exact substring matching is preferred. For long markers a fuzzy fallback
    catches common OCR corruption in long captions.
    """
    total = 0.0

    for marker, weight in markers:
        if caption_matches(
            marker,
            text,
            fuzzy_text=fuzzy_text,
            index=index,
        ):
            total += weight

    return total


def classify(
    text_compact: str,
) -> tuple[DocumentClass, float]:
    """
    Score every supported document class and return the strongest match.
    """
    # One trigram index screens every caption in every class. Rebuilding it
    # per marker would cost more than the matching it saves.
    fuzzy_text = text_compact[:FUZZY_WINDOW]
    index = build_index(fuzzy_text)

    scores = {
        cls: _marker_hits(
            markers,
            text_compact,
            index,
            fuzzy_text,
        )
        for cls, markers in _MARKERS.items()
    }

    best = max(
        scores,
        key=scores.get,
    )

    score = min(
        1.0,
        scores[best],
    )

    if score < MIN_CLASS_SCORE:
        return (
            DocumentClass.UNKNOWN,
            round(score, 4),
        )

    return (
        best,
        round(score, 4),
    )


def _pdf_head_text(
    path: str,
    pages: int = 2,
) -> str:
    """
    Extract text from the first pages of a PDF when a text layer exists.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)

        return "\n".join(
            (
                page.extract_text()
                or ""
            )
            for page in reader.pages[:pages]
        )

    except Exception as exc:
        logger.debug(
            "Could not read PDF text layer: %s",
            exc,
        )
        return ""


def quick_verify_from_tokens(
    tokens,
    image=None,
    requested_class: str | None = None,
    classification: tuple[DocumentClass, float] | None = None,
) -> QuickVerification:
    """
    Verify an already-OCR'd document without running OCR again.

    This is the shared-evidence verification path used by the unified
    Document Agent workflow.

    Parameters
    ----------
    tokens:
        OCRToken-compatible objects. Each token must expose `text` and
        `confidence`; signature verification additionally uses x0/x1/y1/
        height when an image is supplied.

    image:
        Optional PIL image corresponding to the supplied OCR tokens. Used
        only for signature-ink measurement.

    requested_class:
        Optional caller-supplied document class.

    classification:
        Optional (class, score) the caller already computed from these same
        tokens. Classification is the most expensive step here, and the
        unified workflow needs the answer before this call in order to route
        identity against financial -- so without this it was paid twice per
        request on identical text.

    IMPORTANT
    -------
    This function NEVER invokes the OCR engine.

    The unified workflow can therefore do:

        OCR once
            -> classification
            -> extraction
            -> verification

    using the same OCR evidence.
    """
    started = time.perf_counter()

    try:
        if tokens is None:
            tokens = []

        # ---------------------------------------------------------------
        # Basic text preparation
        # ---------------------------------------------------------------
        raw_text = " ".join(
            str(
                getattr(
                    token,
                    "text",
                    "",
                )
                or ""
            )
            for token in tokens
        ).upper()

        compact = re.sub(
            r"[^A-Z0-9]",
            "",
            raw_text,
        )

        spaced = re.sub(
            r"[^A-Z0-9\s/-]",
            " ",
            raw_text,
        )

        spaced = re.sub(
            r"\s{2,}",
            " ",
            spaced,
        ).strip()

        checks: list[QuickCheck] = []
        reasons: list[str] = []

        # ---------------------------------------------------------------
        # Blank / legibility
        # ---------------------------------------------------------------
        blank = (
            len(tokens)
            <= BLANK_PAGE_TOKENS
        )

        if blank:
            reasons.append(
                "PAGE_BLANK"
            )

        legible = (
            len(tokens)
            >= MIN_TOKENS
        )

        checks.append(
            QuickCheck(
                name="document_legible",
                passed=legible,
                detail=(
                    f"{len(tokens)} text region(s) "
                    "recognised"
                ),
            )
        )

        if not legible:
            reasons.append(
                "DOC_ILLEGIBLE"
            )

        # ---------------------------------------------------------------
        # Classification
        # ---------------------------------------------------------------
        if classification is not None:
            detected, score = classification
        else:
            detected, score = classify(
                compact
            )

        checks.append(
            QuickCheck(
                name="document_class_identified",
                passed=(
                    detected
                    is not DocumentClass.UNKNOWN
                ),
                detail=(
                    f"{detected.value} "
                    f"(score {score})"
                ),
            )
        )

        if detected is DocumentClass.UNKNOWN:
            reasons.append(
                "DOC_CLASS_UNRECOGNISED"
            )

        # ---------------------------------------------------------------
        # Identifier format
        # ---------------------------------------------------------------
        identifier = None

        pattern = _IDENTIFIERS.get(
            detected
        )

        if pattern is not None:
            match = pattern[0].search(
                spaced
            )

            identifier = (
                match.group(0)
                if match
                else None
            )

            checks.append(
                QuickCheck(
                    name="identifier_format_valid",
                    passed=(
                        identifier
                        is not None
                    ),
                    detail=(
                        f"found {identifier}"
                        if identifier
                        else (
                            "no value matching "
                            f"{pattern[1]}"
                        )
                    ),
                )
            )

            if identifier is None:
                reasons.append(
                    "IDENTIFIER_NOT_FOUND"
                )

        # ---------------------------------------------------------------
        # Expiry
        # ---------------------------------------------------------------
        expiry_labels = _EXPIRY_LABELS.get(
            detected
        )

        if (
            expiry_labels
            and any(
                label in compact
                for label in expiry_labels
            )
        ):
            from datetime import date as _date

            latest = _latest_plausible_date(
                spaced,
                expiry_labels,
            )

            if latest is not None:
                valid = (
                    latest >= _date.today()
                )

                checks.append(
                    QuickCheck(
                        name="document_not_expired",
                        passed=valid,
                        detail=(
                            "latest date on document: "
                            f"{latest.isoformat()}"
                        ),
                    )
                )

                if not valid:
                    reasons.append(
                        "DOCUMENT_EXPIRED"
                    )

        # ---------------------------------------------------------------
        # OCR quality
        # ---------------------------------------------------------------
        mean_conf = (
            sum(
                float(
                    getattr(
                        token,
                        "confidence",
                        0.0,
                    )
                    or 0.0
                )
                for token in tokens
            )
            / len(tokens)
            if tokens
            else 0.0
        )

        scan_ok = (
            mean_conf
            >= POOR_SCAN_CONFIDENCE
        )

        checks.append(
            QuickCheck(
                name="scan_quality",
                passed=scan_ok,
                detail=(
                    f"mean OCR confidence "
                    f"{mean_conf:.2f}"
                ),
            )
        )

        if not scan_ok:
            reasons.append(
                "POOR_SCAN_QUALITY"
            )

        # ---------------------------------------------------------------
        # Signature presence
        #
        # Uses the existing image + OCR token positions.
        # No OCR is performed here.
        # ---------------------------------------------------------------
        if (
            detected
            in _SIGNATURE_EXPECTED
            and image is not None
        ):
            caption = next(
                (
                    token
                    for token in tokens
                    if any(
                        caption_text
                        in re.sub(
                            r"[^A-Z]",
                            "",
                            str(
                                getattr(
                                    token,
                                    "text",
                                    "",
                                )
                            ).upper(),
                        )
                        for caption_text
                        in _SIGNATURE_CAPTIONS
                    )
                ),
                None,
            )

            if caption is not None:
                ink = _ink_below(
                    image,
                    caption,
                )

                signed = (
                    ink
                    >= signature_ink_threshold()
                )

                checks.append(
                    QuickCheck(
                        name="signature_present",
                        passed=signed,
                        detail=(
                            f"{ink:.3f} ink density "
                            "below the signature caption"
                        ),
                    )
                )

                if not signed:
                    reasons.append(
                        "SIGNATURE_STRIP_BLANK"
                    )

            else:
                checks.append(
                    QuickCheck(
                        name="signature_present",
                        passed=False,
                        detail=(
                            "no signature caption "
                            "found on the document"
                        ),
                    )
                )

                reasons.append(
                    "SIGNATURE_CAPTION_MISSING"
                )

        # ---------------------------------------------------------------
        # Requested class
        # ---------------------------------------------------------------
        if requested_class:
            wanted = (
                requested_class
                .upper()
                .replace("-", "_")
            )

            if wanted not in {
                "AUTO",
                "ANY",
            }:
                matched = _slots.satisfies(
                    wanted, detected.value
                )

                checks.append(
                    QuickCheck(
                        name="matches_requested_class",
                        passed=matched,
                        detail=(
                            f"requested {wanted}, "
                            f"found {detected.value}"
                        ),
                    )
                )

                if not matched:
                    reasons.append(
                        "DOC_CLASS_MISMATCH"
                    )

        # ---------------------------------------------------------------
        # Final status
        # ---------------------------------------------------------------
        by_name = {
            check.name: check
            for check in checks
        }

        hard_failed = any(
            not by_name[name].passed
            for name in (
                "page_not_blank",
                "document_legible",
                "document_class_identified",
                "matches_requested_class",
                "document_not_expired",
            )
            if name in by_name
        )

        confidence = round(
            sum(
                1
                for check in checks
                if check.passed
            )
            / max(
                1,
                len(checks),
            ),
            4,
        )

        if hard_failed:
            status = "FAIL"

        elif (
            "IDENTIFIER_NOT_FOUND"
            in reasons
            or "POOR_SCAN_QUALITY"
            in reasons
            or score < min_confidence()
        ):
            status = "REVIEW"

        else:
            status = "PASS"

        return QuickVerification(
            document_class=detected,
            requested_class=requested_class,
            status=status,
            confidence=confidence,
            checks=checks,
            reason_codes=reasons,
            identifier_found=identifier,
            processing_ms=round(
                (
                    time.perf_counter()
                    - started
                )
                * 1000,
                2,
            ),
        )

    except Exception as exc:
        logger.exception(
            "Shared OCR verification failed"
        )

        return QuickVerification(
            document_class=DocumentClass.UNKNOWN,
            requested_class=requested_class,
            status="FAIL",
            checks=[
                QuickCheck(
                    name="verification_execution",
                    passed=False,
                    detail=(
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    ),
                )
            ],
            reason_codes=[
                "VERIFICATION_ERROR"
            ],
            processing_ms=round(
                (
                    time.perf_counter()
                    - started
                )
                * 1000,
                2,
            ),
        )


def quick_verify(
    image_or_path,
    requested_class: str | None = None,
) -> QuickVerification:
    """
    Verify a document without extracting its fields.

    This is the existing compatibility path.

    It owns its OCR pass and therefore remains independent from the unified
    Document Agent workflow. The unified workflow should use
    quick_verify_from_tokens() after it has already performed OCR.
    """
    started = time.perf_counter()

    from app.agents.document_agent import (
        preprocess as PP,
    )
    from app.agents.document_agent.ocr import (
        get_engine,
    )

    # ---------------------------------------------------------------
    # PDF text-layer fast path
    # ---------------------------------------------------------------
    came_from_pdf = (
        isinstance(
            image_or_path,
            (str, os.PathLike),
        )
        and str(
            image_or_path
        ).lower().endswith(".pdf")
    )

    if came_from_pdf:
        head = _pdf_head_text(
            str(image_or_path)
        )

        if len(head.strip()) >= 120:
            return _verdict_from_text(
                head,
                requested_class,
                started,
            )

        try:
            from pdf2image import (
                convert_from_path,
            )

            pages = convert_from_path(
                str(image_or_path),
                dpi=150,
                first_page=1,
                last_page=1,
            )

            image_or_path = (
                pages[0].convert("RGB")
                if pages
                else image_or_path
            )

        except Exception as exc:
            logger.warning(
                "Could not rasterise PDF "
                "for verification: %s",
                exc,
            )

    # ---------------------------------------------------------------
    # OCR
    # ---------------------------------------------------------------
    try:
        if isinstance(
            image_or_path,
            (str, os.PathLike),
        ):
            image = PP.standard(
                PP.load(
                    str(image_or_path)
                )
            )
        else:
            image = PP.standard(
                image_or_path
            )

        tokens, _ = get_engine().read_array(
            PP.to_array(image)
        )

    except Exception as exc:
        logger.warning(
            "Quick verification could not "
            "read the document: %s",
            exc,
        )

        return QuickVerification(
            document_class=DocumentClass.UNKNOWN,
            requested_class=requested_class,
            status="FAIL",
            checks=[
                QuickCheck(
                    name="document_readable",
                    passed=False,
                    detail=(
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    ),
                )
            ],
            reason_codes=[
                "DOC_UNREADABLE"
            ],
            processing_ms=round(
                (
                    time.perf_counter()
                    - started
                )
                * 1000,
                2,
            ),
        )

    # ---------------------------------------------------------------
    # Initial classification probe
    # ---------------------------------------------------------------
    detected_probe, probe_score = classify(
        re.sub(
            r"[^A-Z0-9]",
            "",
            " ".join(
                token.text
                for token in tokens
            ).upper(),
        )
    )

    # ---------------------------------------------------------------
    # Rotation fallback for legacy verification path
    # ---------------------------------------------------------------
    if (
        detected_probe
        is DocumentClass.UNKNOWN
        and rotate_enabled()
        and not came_from_pdf
    ):
        for angle in (
            180,
            90,
            270,
        ):
            try:
                rotated = image.rotate(
                    angle,
                    expand=True,
                )

                rotated_tokens, _ = (
                    get_engine().read_array(
                        PP.to_array(rotated)
                    )
                )

            except Exception as exc:
                logger.debug(
                    "Rotation %d failed: %s",
                    angle,
                    exc,
                )
                continue

            probe, score_r = classify(
                re.sub(
                    r"[^A-Z0-9]",
                    "",
                    " ".join(
                        token.text
                        for token in rotated_tokens
                    ).upper(),
                )
            )

            if (
                probe
                is not DocumentClass.UNKNOWN
            ):
                tokens = rotated_tokens
                image = rotated
                break

    # ---------------------------------------------------------------
    # Basic checks
    # ---------------------------------------------------------------
    checks: list[QuickCheck] = []
    reasons: list[str] = []

    blank = (
        len(tokens)
        <= BLANK_PAGE_TOKENS
    )

    if blank:
        reasons.append(
            "PAGE_BLANK"
        )

    legible = (
        len(tokens)
        >= MIN_TOKENS
    )

    checks.append(
        QuickCheck(
            name="document_legible",
            passed=legible,
            detail=(
                f"{len(tokens)} text region(s) "
                "recognised"
            ),
        )
    )

    if not legible:
        reasons.append(
            "DOC_ILLEGIBLE"
        )

    # ---------------------------------------------------------------
    # Classification
    # ---------------------------------------------------------------
    raw_text = " ".join(
        token.text
        for token in tokens
    ).upper()

    compact = re.sub(
        r"[^A-Z0-9]",
        "",
        raw_text,
    )

    spaced = re.sub(
        r"[^A-Z0-9\s/-]",
        " ",
        raw_text,
    )

    spaced = re.sub(
        r"\s{2,}",
        " ",
        spaced,
    )

    detected, score = classify(
        compact
    )

    checks.append(
        QuickCheck(
            name="document_class_identified",
            passed=(
                detected
                is not DocumentClass.UNKNOWN
            ),
            detail=(
                f"{detected.value} "
                f"(score {score})"
            ),
        )
    )

    if detected is DocumentClass.UNKNOWN:
        reasons.append(
            "DOC_CLASS_UNRECOGNISED"
        )
        reasons.append(
            "TRY_FULL_VERIFICATION"
        )

    # ---------------------------------------------------------------
    # Identifier
    # ---------------------------------------------------------------
    identifier = None

    pattern = _IDENTIFIERS.get(
        detected
    )

    if pattern is not None:
        match = pattern[0].search(
            spaced
        )

        identifier = (
            match.group(0)
            if match
            else None
        )

        checks.append(
            QuickCheck(
                name="identifier_format_valid",
                passed=(
                    identifier
                    is not None
                ),
                detail=(
                    f"found {identifier}"
                    if identifier
                    else (
                        f"no value matching "
                        f"{pattern[1]}"
                    )
                ),
            )
        )

        if identifier is None:
            reasons.append(
                "IDENTIFIER_NOT_FOUND"
            )

    # ---------------------------------------------------------------
    # Expiry
    # ---------------------------------------------------------------
    expiry_labels = _EXPIRY_LABELS.get(
        detected
    )

    if (
        expiry_labels
        and any(
            label in compact
            for label in expiry_labels
        )
    ):
        from datetime import date as _date

        latest = _latest_plausible_date(
            spaced,
            expiry_labels,
        )

        if latest is not None:
            valid = (
                latest >= _date.today()
            )

            checks.append(
                QuickCheck(
                    name="document_not_expired",
                    passed=valid,
                    detail=(
                        "latest date on document: "
                        f"{latest.isoformat()}"
                    ),
                )
            )

            if not valid:
                reasons.append(
                    "DOCUMENT_EXPIRED"
                )

    # ---------------------------------------------------------------
    # OCR quality
    # ---------------------------------------------------------------
    if tokens:
        mean_conf = (
            sum(
                token.confidence
                for token in tokens
            )
            / len(tokens)
        )

        legible_enough = (
            mean_conf >= 0.55
        )

        checks.append(
            QuickCheck(
                name="scan_quality",
                passed=legible_enough,
                detail=(
                    f"mean OCR confidence "
                    f"{mean_conf:.2f}"
                ),
            )
        )

        if not legible_enough:
            reasons.append(
                "POOR_SCAN_QUALITY"
            )

    # ---------------------------------------------------------------
    # Signature
    # ---------------------------------------------------------------
    if (
        detected
        in _SIGNATURE_EXPECTED
        and image is not None
    ):
        caption = next(
            (
                token
                for token in tokens
                if any(
                    caption_text
                    in re.sub(
                        r"[^A-Z]",
                        "",
                        token.text.upper(),
                    )
                    for caption_text
                    in _SIGNATURE_CAPTIONS
                )
            ),
            None,
        )

        if caption is not None:
            ink = _ink_below(
                image,
                caption,
            )

            signed = (
                ink
                >= signature_ink_threshold()
            )

            checks.append(
                QuickCheck(
                    name="signature_present",
                    passed=signed,
                    detail=(
                        f"{ink:.3f} ink density "
                        "below the signature caption"
                    ),
                )
            )

            if not signed:
                reasons.append(
                    "SIGNATURE_STRIP_BLANK"
                )

        else:
            checks.append(
                QuickCheck(
                    name="signature_present",
                    passed=False,
                    detail=(
                        "no signature caption "
                        "found on the document"
                    ),
                )
            )

            reasons.append(
                "SIGNATURE_CAPTION_MISSING"
            )

    # ---------------------------------------------------------------
    # Requested class
    # ---------------------------------------------------------------
    if requested_class:
        wanted = (
            requested_class
            .upper()
            .replace("-", "_")
        )

        if wanted not in {
            "AUTO",
            "ANY",
        }:
            matched = _slots.satisfies(
                wanted, detected.value
            )

            checks.append(
                QuickCheck(
                    name="matches_requested_class",
                    passed=matched,
                    detail=(
                        f"requested {wanted}, "
                        f"found {detected.value}"
                    ),
                )
            )

            if not matched:
                reasons.append(
                    "DOC_CLASS_MISMATCH"
                )

    # ---------------------------------------------------------------
    # Final verdict
    # ---------------------------------------------------------------
    confidence = round(
        sum(
            1
            for check in checks
            if check.passed
        )
        / max(
            1,
            len(checks),
        ),
        4,
    )

    by_name = {
        check.name: check
        for check in checks
    }

    hard_failed = any(
        not by_name[name].passed
        for name in (
            "page_not_blank",
            "document_legible",
            "document_class_identified",
            "matches_requested_class",
            "document_not_expired",
        )
        if name in by_name
    )

    if hard_failed:
        status = "FAIL"

    elif (
        "IDENTIFIER_NOT_FOUND"
        in reasons
        or "POOR_SCAN_QUALITY"
        in reasons
        or score < min_confidence()
    ):
        status = "REVIEW"

    else:
        status = "PASS"

    hint = None

    if detected is DocumentClass.UNKNOWN:
        hint = (
            "No rotation or contrast recovery is attempted "
            "on this path. If the document is sideways or a "
            "poor scan, send it to POST /verify/{type} instead."
        )

    return QuickVerification(
        document_class=detected,
        requested_class=requested_class,
        retry_hint=hint,
        status=status,
        confidence=confidence,
        checks=checks,
        reason_codes=reasons,
        identifier_found=identifier,
        processing_ms=round(
            (
                time.perf_counter()
                - started
            )
            * 1000,
            2,
        ),
    )


__all__ = [
    "quick_verify",
    "quick_verify_from_tokens",
    "QuickVerification",
    "QuickCheck",
    "DocumentClass",
    "classify",
]