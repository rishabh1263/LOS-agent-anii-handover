"""
Unified Document Agent workflow.

Public workflow:

    upload
        -> document preparation
        -> OCR / classification
        -> identity OR financial routing
        -> extraction
        -> structural verification
        -> unified result

Design principles
-----------------
1. The API exposes one stable document-processing contract.
2. Identity-document extraction reuses the same OCR tokens for verification.
3. Financial processing remains delegated to the existing Financial Agent.
4. Existing specialised extractors are not reimplemented here.
5. KYC/Name Match is intentionally left as a future workflow stage.
6. Agent-specific implementation details never become part of the public
   response contract.

Execution model
---------------
Everything below the async entry point is synchronous and runs on the document
executor, never on the event loop. OCR itself is handed to the serialised OCR
worker. Recognition, PDF rasterisation and pypdf parsing all block for
seconds at a time; running them inline made a single upload stall every other
request on the process, readiness probes included.

Multi-page PDFs
---------------
Each page is recognised and extracted on its own and the per-page results are
combined with pipeline.merge_results(). Token coordinates are page-local, so
concatenating pages into one token list put page three's text at page one's
coordinates -- labels matched values from the wrong side of the document, and
the signature-ink check measured one page's caption against another page's
pixels.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from app.agents.document_agent import pipeline, preprocess
from app.agents.document_agent.ocr import (
    get_engine,
    get_render_executor,
    run_document,
    submit_ocr,
)
from app.agents.document_agent.schemas import (
    DocumentExtractionResult,
    DocumentStatus,
    DocumentType,
)
from app.agents.financial.agent import (
    process_financial_document,
)
from app.agents.financial.schemas import (
    FinancialDocumentType,
    FinancialResult,
)
from app.agents.verification.basic import (
    DocumentClass,
    QuickVerification,
    classify as classify_document_class,
    quick_verify_from_tokens,
)

logger = logging.getLogger(__name__)


MAX_UPLOAD_BYTES = 25 * 1024 * 1024

ALLOWED_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".pdf",
}

DEFAULT_MAX_PDF_PAGES = 4

VALID_OPERATIONS = {"VERIFY", "EXTRACT"}


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()

    if not raw:
        return default

    try:
        return int(raw)
    except ValueError:
        return default


def max_pdf_pages() -> int:
    """How many pages of a PDF are read before the rest is ignored."""

    return max(1, _int_env("DOCUMENT_MAX_PDF_PAGES", DEFAULT_MAX_PDF_PAGES))


def pdf_render_dpi() -> int:
    """Rasterisation resolution for identity PDFs."""

    return max(72, _int_env("DOCUMENT_PDF_DPI", 300))


def escalation_enabled() -> bool:
    """
    Whether a disappointing OCR pass is retried with contrast and rotation.

    On by default: it is what makes a sideways phone photo or a faint
    photocopy readable. Switch off where a hard latency budget matters more
    than recovering poor scans.
    """

    return (
        os.getenv("DOCUMENT_AGENT_ESCALATION", "true") or "true"
    ).strip().lower() == "true"


def verification_enabled_for(document_class: DocumentClass) -> bool:
    """
    Whether structural verification runs for this document class.

    Delegates to the Verification Agent so one switch governs the whole
    service: VERIFICATION_ENABLED globally, VERIFICATION_ENABLED_<CLASS> per
    class. Configuration lives with the agent that owns the decision rather
    than being re-read here.

    Turning verification OFF does NOT open the extraction gate. EXTRACT
    requires a verification PASS, and a class that is not verified cannot
    produce one, so it returns a SKIPPED verdict and a null extraction. That
    is deliberate: a silent bypass would hand out fields from a document
    nothing had checked, which is exactly what the gate exists to prevent.
    """

    from app.agents.verification.agent import enabled_for

    return enabled_for(document_class.value)


# ---------------------------------------------------------------------------
# Document classification mapping
#
# One table per relationship. These were previously spelled out inline at each
# call site, so the same mapping existed in several places and could drift.
# ---------------------------------------------------------------------------

_CLASS_TO_TYPE: dict[DocumentClass, DocumentType] = {
    DocumentClass.PAN: DocumentType.PAN,
    DocumentClass.DRIVING_LICENCE: DocumentType.DRIVING_LICENCE,
    DocumentClass.VOTER_ID: DocumentType.VOTER_ID,
    DocumentClass.PASSPORT: DocumentType.PASSPORT,
}

_TYPE_TO_CLASS: dict[DocumentType, DocumentClass] = {
    document_type: document_class
    for document_class, document_type in _CLASS_TO_TYPE.items()
}

_IDENTITY_CLASSES = set(_CLASS_TO_TYPE)

_CLASS_TO_FINANCIAL_TYPE: dict[DocumentClass, FinancialDocumentType] = {
    DocumentClass.BANK_STATEMENT: FinancialDocumentType.BANK_STATEMENT,
    DocumentClass.ITR: FinancialDocumentType.ITR,
    DocumentClass.SALARY_SLIP: FinancialDocumentType.SALARY_SLIP,
    DocumentClass.SALE_DEED: FinancialDocumentType.SALE_DEED,
}

_FINANCIAL_CLASSES = set(_CLASS_TO_FINANCIAL_TYPE)

# Classes this service recognises but deliberately never extracts.
#
# Aadhaar is verified through the external verification service. Reading the
# card here would mean holding Aadhaar numbers this system has no reason to
# hold, so there is no Aadhaar extractor and there must not be one: the
# document is classified, reported as requiring external verification, and
# left alone.
_EXTERNAL_ONLY_CLASSES = {DocumentClass.AADHAAR}


def _validate_filename(filename: str) -> None:
    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"Unsupported file type: {suffix or 'unknown'}"
        )


def _document_class_to_category(
    document_class: DocumentClass,
) -> str | None:
    if document_class in _IDENTITY_CLASSES:
        return "IDENTITY"

    if document_class in _FINANCIAL_CLASSES:
        return "FINANCIAL"

    return None


# ---------------------------------------------------------------------------
# OCR evidence helpers
# ---------------------------------------------------------------------------

def _compact_text(tokens) -> str:
    """
    Alphanumeric-only OCR text, matching what the verification classifier
    builds internally.

    The workflow used to keep every alphanumeric character, including
    Devanagari, while the verifier stripped down to A-Z0-9. The two therefore
    classified the SAME tokens from different text, so routing and
    verification could disagree about what the document was.
    """

    raw = " ".join(
        str(getattr(token, "text", "") or "")
        for token in tokens
    ).upper()

    return re.sub(r"[^A-Z0-9]", "", raw)


def _compact_layer_text(text: str) -> str:
    """The same normalisation, for a PDF text layer."""

    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _mean_confidence(tokens) -> float:
    if not tokens:
        return 0.0

    return round(
        sum(float(getattr(token, "confidence", 0.0) or 0.0) for token in tokens)
        / len(tokens),
        4,
    )


# A page that produced this much text was recognised perfectly well; it is
# simply not an identity document, and neither contrast nor rotation will turn
# a dense page of a bank statement into one. Identity cards are sparse -- real
# samples here yield 7 to 20 regions, while full scanned pages yield 31 to 119
# -- so the count separates "unreadable" from "readable but not an ID".
DENSE_PAGE_TOKENS = 30


def _document_resolved(
    operation: str,
    result: DocumentExtractionResult,
) -> bool:
    """
    Whether the identity pipeline has everything this operation needs.

    VERIFY only reports class, legibility and identifier format, so an
    identified document is already everything it can use. EXTRACT needs every
    required field present and valid.
    """

    if result.document_type is DocumentType.UNKNOWN:
        return False

    if operation == "VERIFY":
        return True

    return result.status is DocumentStatus.SUCCESS


def _escalation_satisfied(
    recognition: pipeline.Recognition,
) -> bool:
    """
    Whether another OCR pass could still change the answer.

    Two things this deliberately does NOT escalate on, both measured against
    the real sample set:

    An already-identified document. Re-reading one with contrast or rotation
    did not recover a single additional field (3->3, 3->3, 4->4, 4->4), so
    escalating on incomplete REQUIRED fields spent two to three seconds per
    partial document for nothing. It also means VERIFY and EXTRACT now do
    identical OCR work, so the two cannot reach different verdicts about the
    same file.

    A dense but unidentified page. A scanned bank statement page reads
    perfectly (48 to 119 regions at 0.86+ confidence); it is simply not an
    identity document, and no contrast or rotation will make it one.

    What it DOES escalate -- a sparse, unidentified page -- is the case that
    pays: a Voter ID photographed sideways goes from UNKNOWN with no fields to
    VOTER_ID with four.
    """

    if recognition.result.document_type is not DocumentType.UNKNOWN:
        return True

    return len(recognition.tokens) >= DENSE_PAGE_TOKENS


def _recognise(
    image,
    *,
    force_type: DocumentType | None = None,
    escalate: bool = True,
    rotate: bool = True,
) -> pipeline.Recognition | None:
    """
    Run recognition on the serialised OCR worker.

    Extraction and verification both consume the Recognition that comes back,
    so they always agree on which image variant and which tokens were used.
    """

    engine = get_engine()

    return submit_ocr(
        pipeline.recognise,
        engine,
        image,
        force_type=force_type,
        accept=_escalation_satisfied,
        escalate=escalate and escalation_enabled(),
        rotate=rotate,
    )


# ---------------------------------------------------------------------------
# Response envelope
#
# Every path returns through here so the public shape cannot drift between
# the image route, the PDF route and the failure routes.
# ---------------------------------------------------------------------------

def _type_value(document_type: Any) -> str:
    if document_type is None:
        return "UNKNOWN"

    return (
        document_type.value
        if hasattr(document_type, "value")
        else str(document_type)
    )


class Timings:
    """
    Per-stage wall-clock, in milliseconds.

    Carried through a request so the public response can report where the time
    actually went. Without it "the request took four seconds" is untriageable:
    a slow rasterisation and a slow recogniser look identical from outside.
    """

    __slots__ = ("ocr_ms", "classification_ms", "verification_ms",
                 "extraction_ms", "kyc_ms", "_started")

    def __init__(self) -> None:
        self.ocr_ms = 0.0
        self.classification_ms = 0.0
        self.verification_ms = 0.0
        self.extraction_ms = 0.0
        self.kyc_ms = 0.0
        self._started = time.perf_counter()

    def total_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000

    def as_dict(self) -> dict[str, float]:
        return {
            "ocr_ms": round(self.ocr_ms, 2),
            "classification_ms": round(self.classification_ms, 2),
            "verification_ms": round(self.verification_ms, 2),
            "extraction_ms": round(self.extraction_ms, 2),
            "kyc_ms": round(self.kyc_ms, 2),
            "total_ms": round(self.total_ms(), 2),
        }


def _timed(timings: "Timings", attribute: str, function, *args, **kwargs):
    """Run `function`, adding its wall-clock to one Timings field."""
    started = time.perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        setattr(timings, attribute, getattr(timings, attribute) + elapsed)


def _envelope(
    *,
    request_id: str,
    status: str,
    document_type: Any,
    category: str | None,
    supported: bool,
    extraction: dict[str, Any] | None,
    verification: dict[str, Any] | None,
    ocr_status: str,
    ocr_confidence: float,
    classification_status: str,
    classification_confidence: float,
    errors: list[dict[str, Any]],
    timings: Timings | None = None,
    kyc: dict[str, Any] | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """
    The one public response shape.

    `processing` carries BOTH the per-stage millisecond timings that are the
    canonical contract AND the original ocr/classification status-and-
    confidence objects. The latter are redundant for new callers but existing
    ones read them, so they stay: adding keys is backwards compatible,
    removing them is not.
    """
    processing: dict[str, Any] = {
        "ocr": {
            "status": ocr_status,
            "confidence": ocr_confidence,
        },
        "classification": {
            "status": classification_status,
            "confidence": classification_confidence,
        },
    }
    processing.update((timings or Timings()).as_dict())

    envelope: dict[str, Any] = {
        "request_id": request_id,
        "status": status,
        "document": {
            "type": _type_value(document_type),
            "category": category,
            "supported": supported,
        },
        "extraction": extraction,
        "verification": verification,
        # Populated by the LOS flow, which is the only caller holding more
        # than one document. A single document has nothing to cross-check.
        "kyc": kyc,
        "summary": summary if summary is not None else "",
        "processing": processing,
        "errors": errors,
    }

    if summary is None:
        from app.agents.los.summary import build_summary

        # DETERMINISTIC, ALWAYS. This runs once PER DOCUMENT, inside the
        # document executor, and the LOS flow throws the result away -- it
        # writes its own summary over the whole application. Letting it
        # consult the model made a five-document application issue six
        # blocking model calls and discard five of them, for a sentence no
        # client ever saw. The application-level summary in
        # app/agents/los/flow.py is the one that may use the model.
        envelope["summary"], _source = build_summary(envelope, use_llm=False)

    return envelope


def _requested_class_mismatch(
    requested_class: str | None,
    detected: Any,
) -> str | None:
    """
    The caller's asserted type, when it disagrees with what was found.

    Returns the normalised requested type on a mismatch and None otherwise --
    including when nothing was asserted, or when the assertion is a value this
    service does not recognise, which is a bad request rather than a wrong
    document and is refused at the API boundary.
    """
    wanted = (requested_class or "").strip().upper()
    if not wanted or wanted in {"AUTO", "ANY"}:
        return None
    try:
        DocumentClass(wanted)
    except ValueError:
        return None
    return None if wanted == _type_value(detected) else wanted


def _type_mismatch_response(
    *,
    request_id: str,
    detected: Any,
    requested: str,
    classification_confidence: float,
    ocr_confidence: float,
    timings: "Timings | None" = None,
) -> dict[str, Any]:
    """
    The caller sent a document of the wrong type.

    One definition, so the identity and financial routes cannot disagree
    about what a mismatch means. Extraction is null and no specialist ran:
    the document may be perfectly valid, but it is not the one that was
    asked for, and running its extractor would release fields against a slot
    the caller did not request.
    """
    return _envelope(
        request_id=request_id,
        status="REJECTED",
        document_type=detected,
        category=None,
        supported=True,
        extraction=None,
        verification={
            "status": "FAIL",
            "checks": {"matches_requested_class": "FAIL"},
            "reason_codes": [DOCUMENT_TYPE_MISMATCH],
        },
        ocr_status="SUCCESS",
        ocr_confidence=ocr_confidence,
        classification_status="SUCCESS",
        classification_confidence=classification_confidence,
        errors=[{
            "code": DOCUMENT_TYPE_MISMATCH,
            "message": (
                f"Expected {requested} but the document was identified as "
                f"{_type_value(detected)}."
            ),
        }],
        timings=timings,
    )


def _unreadable(
    *,
    request_id: str,
    code: str,
    message: str,
    timings: "Timings | None" = None,
) -> dict[str, Any]:
    """The shared shape for a document that produced no usable evidence."""

    return _envelope(
        request_id=request_id,
        status="FAILED",
        document_type=DocumentType.UNKNOWN,
        category=None,
        supported=False,
        extraction=None,
        verification={
            "status": "FAIL",
            "checks": {
                "document_legible": "FAIL",
            },
        },
        ocr_status="FAILED",
        ocr_confidence=0.0,
        classification_status="FAILED",
        classification_confidence=0.0,
        errors=[
            {
                "code": code,
                "message": message,
            }
        ],
        timings=timings,
    )


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _serialise_extraction(
    result: DocumentExtractionResult,
) -> dict[str, Any]:
    """
    Convert the internal document extraction model into the public
    extraction.fields contract.

    Internal field metadata such as OCR confidence and validation remains
    available to the workflow but the outer API contract stays stable.
    """
    fields: dict[str, Any] = {}

    for name, field in (
        result.fields or {}
    ).items():
        value = getattr(
            field,
            "value",
            None,
        )

        if value is not None:
            fields[name] = value

    # HOW WELL EACH FIELD WAS READ, for consumers that need to weigh the
    # value rather than just use it -- KYC folds it into its confidence
    # figure. Kept on a sibling key so `fields` stays exactly the published
    # contract, and NOT published itself: app/agents/los/response.py copies
    # only `fields` onto the response.
    #
    # This is the extractor's own field confidence, not OCR data. No token,
    # box or raw OCR string is exposed here or anywhere downstream of it.
    quality: dict[str, float] = {}
    for name, field in (result.fields or {}).items():
        if getattr(field, "value", None) is None:
            continue
        confidence = getattr(field, "confidence", None)
        if confidence is None:
            continue
        try:
            quality[name] = round(float(confidence), 4)
        except (TypeError, ValueError):
            continue

    payload: dict[str, Any] = {"fields": fields}
    if quality:
        payload["field_quality"] = quality
    return payload


#: The verification layer's internal name for a wrong document type, and the
#: name the API publishes. Renamed at the boundary rather than inside the
#: verifier: DOC_CLASS_MISMATCH describes a document CLASS, which is this
#: service's word, while a client asked for a document TYPE.
_PUBLIC_REASON_CODES = {"DOC_CLASS_MISMATCH": "DOCUMENT_TYPE_MISMATCH"}

#: The caller uploaded the wrong document. Published so a client can tell
#: "this is not the document you asked me for" apart from "this document is
#: unreadable" -- they need different things from the applicant.
DOCUMENT_TYPE_MISMATCH = "DOCUMENT_TYPE_MISMATCH"


def _serialise_identity_verification(
    result: QuickVerification,
    *,
    tokens: list[Any] | None = None,
    quality_report: Any = None,
    extracted_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    The public verification block for an identity document.

    NOW CARRIES SCORE AND CONFIDENCE. It did not, and the fields were
    simply absent from every PAN, licence, passport and voter ID -- the
    reusable scoring framework existed and was wired to the bank-statement
    path only, so a reviewer triaging identity documents had a verdict and
    nothing to rank by.

    `status` IS UNCHANGED AND STILL COMES FROM THE GATES. The scoring
    module returns a verdict of its own and it is deliberately discarded:
    the structural gates in basic.py and the post-extraction rules in
    rules.py decide, exactly as before, and a second opinion computed here
    could only ever disagree with them. The numbers describe the verdict;
    they do not participate in reaching it.
    """
    checks = {
        check.name: (
            "PASS"
            if check.passed
            else "FAIL"
        )
        for check in result.checks
    }

    payload: dict[str, Any] = {
        "status": result.status,
        "checks": checks,
        # Previously dropped here. The verifier recorded exactly why it
        # failed and this serialiser kept only the verdict, so a client was
        # told FAIL with no reason -- including for a document type mismatch,
        # which is the one failure the caller can actually fix.
        #
        # Only on a verdict that is NOT a pass. A passing document also
        # accumulates advisory notes ("SIGNATURE_CAPTION_MISSING"), and
        # publishing those would put reason codes on a clean document, where
        # they read as problems rather than as the diagnostics they are.
        "reason_codes": (
            [
                _PUBLIC_REASON_CODES.get(code, code)
                for code in (result.reason_codes or [])
            ]
            if result.status != "PASS"
            else []
        ),
    }

    payload.update(_identity_score(
        document_class=str(getattr(result.document_class, "value",
                                   result.document_class)),
        verification=result,
        tokens=tokens,
        quality_report=quality_report,
        extracted_fields=extracted_fields,
        passing=result.status == "PASS",
    ))

    return payload


def _identity_score(
    *,
    document_class: str,
    verification: QuickVerification,
    tokens: list[Any] | None,
    quality_report: Any,
    extracted_fields: dict[str, Any] | None,
    passing: bool,
) -> dict[str, Any]:
    """
    The two numbers, and the image-quality notes that explain a non-pass.

    Never raises: a document must not fail to return because a score could
    not be computed. A missing score is reported as absent, which is what
    it was before this existed.
    """
    try:
        from app.agents.verification import identity_checks, rules, scoring

        required = rules._required_fields(document_class)
        aliases = rules._field_aliases(document_class)
        fields = extracted_fields or {}

        # Resolved through the SAME alias map the required-field rule
        # uses. Scoring a field as missing that the rule counts as present
        # would put the score and the verdict at odds on the very document
        # the alias exists for.
        resolved = {
            name: rules._resolve_field(name, fields, aliases)
            for name in required
        }

        assessment = scoring.assess(
            document_class,
            identity_checks.checks_for(
                verification,
                required_fields=required,
                extracted_fields=resolved,
            ),
        )
        published = assessment.public()

        confidence = identity_checks.confidence_for(
            published["verification_confidence"],
            tokens=tokens,
            quality_report=quality_report,
            required_fields=required,
            extracted_fields=resolved,
        )

        block: dict[str, Any] = {
            "verification_score": published["verification_score"],
            "verification_confidence": confidence,
        }

        # IMAGE-QUALITY NOTES ONLY ON A NON-PASS. A passing document
        # carrying DOCUMENT_IMAGE_BLURRY reads as a problem with it, when
        # the blur was recovered and every field validated. These make a
        # REVIEW actionable; they do not annotate success.
        if not passing:
            evidence_codes = identity_checks.evidence_reason_codes(
                quality_report)
            evidence_reasons = identity_checks.evidence_reasons(quality_report)
            if evidence_codes:
                block["evidence_reason_codes"] = evidence_codes
            if evidence_reasons:
                block["reasons"] = evidence_reasons

        return block

    except Exception:  # pragma: no cover - scoring must never break a read
        logger.exception("Identity verification scoring failed")
        return {}


def _serialise_financial_extraction(
    result: FinancialResult,
    include_detail: bool = True,
    include_signals: bool = True,
) -> dict[str, Any]:
    """
    Keep the unified public contract while preserving all financial
    document-specific fields under extraction.fields.

    `detail` is the specialist parser's full output -- every transaction row
    on a bank statement, which runs to megabytes. It is the one genuinely
    large thing in this response, so callers that want it ask for it.
    """
    fields: dict[str, Any] = {}

    # Explicit normalised fields.
    explicit = {
        "pan": result.pan,
        "name": result.name,
        "account_number_masked":
            result.account_number_masked,
        "employer_name":
            result.employer_name,
        "period_start":
            result.period_start,
        "period_end":
            result.period_end,
        "signals":
            result.signals.model_dump(
                mode="json"
            ),
        # Derived evidence for a later risk engine: counts, sums and minima
        # over the statement's own rows. Absent unless the document supports
        # it. The rows themselves stay in `detail`, which this path does not
        # request.
        "evidence": result.evidence,
    }

    # INCOME ANALYSIS IS A LATER STAGE'S OUTPUT.
    #
    # `signals` carries average monthly credit, net salary and total
    # credits/debits; `evidence` carries derived aggregates for a risk
    # engine. Both are financial ANALYSIS, and a FOS response must not
    # contain them -- the FOS stage verifies that a statement is a readable,
    # coherent document, and has no authority to say anything about income.
    #
    # The credit stage asks for them by leaving this on, which is the
    # default, so /api/v1/los/process is unchanged.
    if not include_signals:
        explicit.pop("signals", None)
        explicit.pop("evidence", None)

    for key, value in explicit.items():
        if value is not None:
            fields[key] = value

    # Document-specific parser output remains nested rather than changing
    # the outer API contract.
    if result.detail and include_detail:
        fields["detail"] = result.detail

    return {
        "fields": fields,
    }


def _serialise_financial_verification(
    result: FinancialResult,
) -> dict[str, Any]:
    """
    The financial verdict, WITH ITS REASONS.

    This used to return a status and a `checks` dict and nothing else -- no
    reason codes at all, because the key did not exist on this path. Every
    bank statement that did not pass therefore came back as:

        verification: REVIEW
        reason_codes: []

    A field officer was told a document needed review and nothing about what
    to do with it. The verdict was usually right; it was simply unexplained,
    and an unexplained REVIEW is one nobody can act on or appeal.

    The verdict itself is unchanged where the evidence is conclusive: a
    reconciliation that demonstrably fails is still a hard gate to FAIL. What
    changed is that an INCONCLUSIVE result now says so, in a sentence a field
    officer can read, with a code a queue can route on.
    """
    from app.agents.verification import financial_checks, scoring

    document_type = str(
        getattr(result.document_type, "value", result.document_type) or ""
    ).upper()

    # A verifier that supplied no named evidence of its own -- ITR and
    # salary slips do not -- is scored from the NORMALISED result instead.
    #
    # THE VERDICT IS UNCHANGED. It is still derived from `verified`,
    # exactly as before, and the scoring module's own status is discarded:
    # these documents produced this verdict before scoring existed and
    # they must produce it after. What is new is that they now carry the
    # two numbers and a reason code, which they did not.
    if not result.verification_checks:
        status = "REVIEW"
        if result.verified is True:
            status = "PASS"
        elif result.verified is False:
            status = "FAIL"

        checks: dict[str, Any] = {"financial_integrity": status}
        if result.verification_note:
            checks["note"] = result.verification_note

        payload: dict[str, Any] = {"status": status, "checks": checks}

        derived = scoring.assess(
            document_type or "FINANCIAL_DOCUMENT",
            financial_checks.checks_for(result),
        )
        published = derived.public()
        payload["verification_score"] = published["verification_score"]
        payload["verification_confidence"] = financial_checks.confidence_for(
            published["verification_confidence"], result,
        )

        if status != "PASS":
            # The derived codes are more specific than the blanket
            # VERIFICATION_INCONCLUSIVE this used to emit for everything:
            # they distinguish a scan awaiting OCR from an unreadable file
            # from an integrity check that ran and failed.
            payload["reason_codes"] = (
                published["reason_codes"] or ["VERIFICATION_INCONCLUSIVE"]
            )
            payload["reasons"] = published["reasons"] or [
                "This document needs review because its verification could "
                "not be completed."
            ]
        return payload

    assessment = scoring.assess(
        # The document's OWN type, not BANK_STATEMENT. Hardcoding the one
        # type that had evidence meant per-type weights configured for any
        # other could never take effect -- a configuration surface that
        # silently did nothing.
        document_type or "BANK_STATEMENT",
        [
            scoring.Check(
                name=c.get("name", ""),
                outcome=c.get("outcome", scoring.Outcome.UNKNOWN),
                weight=float(c.get("weight", 1.0)),
                reason_code=c.get("reason_code"),
                reason=c.get("reason"),
                hard_gate=bool(c.get("hard_gate")),
                gate_verdict=c.get("gate_verdict", scoring.FAIL),
            )
            for c in result.verification_checks
        ],
    )

    published = assessment.public()
    return {
        "status": assessment.status,
        "checks": {"financial_integrity": assessment.status},
        "verification_score": published["verification_score"],
        "verification_confidence": published["verification_confidence"],
        "reason_codes": published["reason_codes"],
        "reasons": published["reasons"],
    }


def _identity_status(
    *,
    verification: QuickVerification,
    extraction: DocumentExtractionResult | None,
) -> str:
    """Resolve the public status for an identity-document workflow."""

    verification_status = getattr(
        verification.status,
        "value",
        verification.status,
    )

    if verification_status == "FAIL":
        return "REJECTED"

    if verification_status == "REVIEW":
        return "REVIEW"

    if extraction is None:
        return "SUCCESS"

    extraction_status = getattr(
        extraction.status,
        "value",
        extraction.status,
    )

    if extraction_status == "SUCCESS":
        return "SUCCESS"

    if extraction_status == "PARTIAL":
        return "PARTIAL"

    return "REJECTED"


def _overall_status_identity(
    extraction: DocumentExtractionResult,
    verification: QuickVerification,
) -> str:
    """
    Resolve the public workflow status.

    SUCCESS:
        Extraction and verification are usable.

    REVIEW:
        Document is potentially valid but evidence is incomplete or uncertain.

    REJECTED:
        Structural verification failed.

    FAILED:
        Processing itself failed.
    """
    if verification.status == "FAIL":
        return "REJECTED"

    if extraction.status is DocumentStatus.FAILED:
        return "FAILED"

    if (
        verification.status == "REVIEW"
        or extraction.status
        is DocumentStatus.PARTIAL
    ):
        return "REVIEW"

    return "SUCCESS"


def _overall_status_financial(
    result: FinancialResult,
) -> str:
    if result.status.value == "FAILED":
        return "FAILED"

    if result.status.value == "UNSUPPORTED":
        return "REJECTED"

    if (
        result.status.value == "PARTIAL"
        or result.status.value == "REQUIRES_OCR"
    ):
        return "REVIEW"

    if result.verified is False:
        return "REJECTED"

    if result.verified is None:
        return "REVIEW"

    return "SUCCESS"


# ---------------------------------------------------------------------------
# Financial workflow
# ---------------------------------------------------------------------------

def _financial_response(
    path: str,
    *,
    document_class: DocumentClass,
    classification_confidence: float,
    operation: str,
    request_id: str,
    ocr_status: str,
    ocr_confidence: float,
    timings: "Timings | None" = None,
    include_detail: bool = True,
    include_signals: bool = True,
    requested_class: str | None = None,
) -> dict[str, Any]:
    """
    Delegate financial extraction to the existing Financial Agent.

    This workflow deliberately does not reimplement bank statement, ITR,
    salary slip or sale deed extraction. Those remain owned by their
    specialised parsers; this function only routes and normalises.

    THE EXPECTED-TYPE CHECK LIVES HERE, not at the four call sites that reach
    this function -- one image route and three PDF routes. It was previously
    at none of them: a bank statement uploaded against expected_types=PAN came
    back REVIEW with no reason code, telling the caller the document was
    uncertain when in fact they had sent the wrong one. Enforcing it at the
    single point every route passes through is what stops the four from
    drifting apart again.
    """

    timings = timings or Timings()

    # Before the parser runs, so the wrong specialist never executes and no
    # fields are released against a slot the caller did not ask for.
    mismatch = _requested_class_mismatch(requested_class, document_class)
    if mismatch is not None:
        return _type_mismatch_response(
            request_id=request_id,
            detected=document_class,
            requested=mismatch,
            classification_confidence=classification_confidence,
            ocr_confidence=ocr_confidence,
            timings=timings,
        )

    financial_result = _timed(
        timings, "extraction_ms",
        process_financial_document,
        path,
        document_type=_CLASS_TO_FINANCIAL_TYPE.get(document_class),
    )

    verification = _serialise_financial_verification(
        financial_result
    )

    # VERIFY never exposes extraction, and EXTRACT exposes it only behind a
    # verification PASS -- the same gate the identity route applies.
    extraction: dict[str, Any] | None = None

    if operation == "EXTRACT" and verification["status"] == "PASS":
        extraction = _serialise_financial_extraction(
            financial_result,
            include_detail=include_detail,
            include_signals=include_signals,
        )

    return _envelope(
        request_id=request_id,
        status=_overall_status_financial(financial_result),
        document_type=document_class,
        category="FINANCIAL",
        supported=(
            financial_result.status.value != "UNSUPPORTED"
        ),
        extraction=extraction,
        verification=verification,
        ocr_status=ocr_status,
        ocr_confidence=ocr_confidence,
        classification_status="SUCCESS",
        classification_confidence=classification_confidence,
        errors=_financial_reasons(financial_result),
        timings=timings,
    )


def _financial_reasons(
    financial_result: FinancialResult,
) -> list[dict[str, str]]:
    """
    Structured reasons for a financial outcome.

    Previously only `errors` were surfaced. A scanned statement produces no
    errors -- it produces a WARNING saying it has no text layer and needs to
    be routed to OCR -- so the caller received REVIEW with zero fields and an
    empty reason list, which reads as "we looked and found nothing" rather
    than "this is a scan we have not read yet". Those are different answers
    and a queueing caller has to tell them apart.
    """
    reasons = [
        {"code": "FINANCIAL_PROCESSING_ERROR", "message": error}
        for error in financial_result.errors
    ]

    status_value = financial_result.status.value

    if status_value == "REQUIRES_OCR":
        reasons.append(
            {
                "code": "REQUIRES_OCR",
                "message": (
                    "This statement is a scan with no text layer. It has not "
                    "been read; route it to the asynchronous OCR queue."
                ),
            }
        )
    elif status_value == "UNSUPPORTED":
        reasons.append(
            {
                "code": "FINANCIAL_DOCUMENT_UNSUPPORTED",
                "message": (
                    financial_result.warnings[0]
                    if financial_result.warnings
                    else "This document could not be identified as a "
                    "supported financial document."
                ),
            }
        )

    if status_value != "SUCCESS":
        # Warnings explain why something is short of success. On a SUCCESS
        # they are commentary -- "28 rows were re-derived from the running
        # balance" is worth logging, but surfacing it as a review reason on a
        # result that did succeed invites a reviewer to look for a problem
        # that is not there.
        reasons.extend(
            {"code": "FINANCIAL_REVIEW_REASON", "message": warning}
            for warning in financial_result.warnings
        )

    if not reasons and status_value != "SUCCESS":
        # A non-success outcome must never be unexplained: whatever sent it
        # to review is what the reviewer needs to see.
        reasons.append(
            {
                "code": "FINANCIAL_REVIEW_REASON",
                "message": (
                    f"Financial extraction ended as {status_value} without "
                    "usable figures."
                ),
            }
        )

    return reasons


# ---------------------------------------------------------------------------
# Identity response
# ---------------------------------------------------------------------------

def _identity_response(
    *,
    request_id: str,
    operation: str,
    document_class: DocumentClass,
    classification_confidence: float,
    verification: QuickVerification | None,
    recognition_result: DocumentExtractionResult | None,
    ocr_confidence: float,
    supported_when_unverified: bool,
    extra_errors: list[dict[str, Any]],
    timings: "Timings | None" = None,
    # EVIDENCE FOR THE CONFIDENCE FIGURE, not for the verdict. Optional so
    # every existing caller keeps working unchanged; a caller that does
    # not supply them gets a confidence computed from conclusiveness
    # alone, which is what the scoring module did before.
    tokens: list[Any] | None = None,
    quality_report: Any = None,
) -> dict[str, Any]:
    """
    Build the identity response, applying the VERIFY/EXTRACT gate.

    The gate is unchanged and deliberate: VERIFY never returns extraction, and
    EXTRACT returns it only when verification is PASS. REVIEW and FAIL both
    return a null extraction.

    `verification` is None when the class is switched off through
    VERIFICATION_ENABLED_<CLASS>. That is reported as SKIPPED and still
    returns a null extraction -- a document nothing has checked never yields
    fields.
    """

    timings = timings or Timings()

    document_type = _CLASS_TO_TYPE.get(
        document_class,
        DocumentType.UNKNOWN,
    )

    if verification is None:
        return _envelope(
            request_id=request_id,
            status="REVIEW",
            document_type=document_type,
            category="IDENTITY",
            supported=supported_when_unverified,
            extraction=None,
            verification={
                "status": "SKIPPED",
                "checks": {},
            },
            ocr_status="SUCCESS",
            ocr_confidence=ocr_confidence,
            classification_status="SUCCESS",
            timings=timings,
            classification_confidence=classification_confidence,
            errors=extra_errors
            + [
                {
                    "code": "VERIFICATION_DISABLED",
                    "message": (
                        "Structural verification is switched off for "
                        f"{document_class.value}. Extraction stays withheld "
                        "because it is gated on a verification PASS."
                    ),
                }
            ],
        )

    if operation == "VERIFY":
        return _envelope(
            request_id=request_id,
            status=_identity_status(
                verification=verification,
                extraction=None,
            ),
            document_type=document_type,
            category="IDENTITY",
            supported=supported_when_unverified,
            extraction=None,
            verification=_serialise_identity_verification(verification),
            ocr_status="SUCCESS",
            ocr_confidence=ocr_confidence,
            classification_status="SUCCESS",
            classification_confidence=classification_confidence,
            errors=extra_errors,
            timings=timings,
        )

    if verification.status != "PASS" or recognition_result is None:
        return _envelope(
            request_id=request_id,
            status="REJECTED",
            document_type=document_type,
            category="IDENTITY",
            supported=supported_when_unverified,
            extraction=None,
            verification=_serialise_identity_verification(verification),
            ocr_status="SUCCESS",
            ocr_confidence=ocr_confidence,
            classification_status="SUCCESS",
            classification_confidence=classification_confidence,
            errors=extra_errors,
            timings=timings,
        )

    extraction = recognition_result

    # The verification classifier decides the public type because it covers
    # financial classes too; the extraction pipeline fills in only when the
    # verifier could not name the document at all.
    resolved_class = document_class

    if (
        resolved_class is DocumentClass.UNKNOWN
        and extraction.document_type is not DocumentType.UNKNOWN
    ):
        resolved_class = _TYPE_TO_CLASS.get(
            extraction.document_type,
            DocumentClass.UNKNOWN,
        )

    errors = list(extra_errors)

    supported = extraction.document_type is not DocumentType.UNKNOWN
    status = _overall_status_identity(extraction, verification)

    if resolved_class in _EXTERNAL_ONLY_CLASSES:
        # Recognised, and deliberately not extracted here. Aadhaar is verified
        # through the external verification service, not by reading the card:
        # this codebase has no Aadhaar extractor and must not grow one.
        #
        # Reported as REVIEW rather than SUCCESS because nothing was
        # established about the document. The generic extractor error is
        # dropped -- "could not be identified from OCR content" is untrue and
        # misleading when the class WAS identified and extraction was never
        # attempted.
        status = "REVIEW"
        errors = [
            error
            for error in errors
            if error.get("code") != "DOCUMENT_PROCESSING_ERROR"
        ]
        errors.append(
            {
                "code": "EXTERNAL_VERIFICATION_REQUIRED",
                "message": (
                    f"{resolved_class.value} is verified through the external "
                    "verification service. No fields are extracted from the "
                    "document here, and none of its contents are established."
                ),
            }
        )
    else:
        errors.extend(
            {
                "code": "DOCUMENT_PROCESSING_ERROR",
                "message": error,
            }
            for error in extraction.errors
        )

        if status == "SUCCESS" and not supported:
            # Verification checks the document is legible and well formed;
            # they can all pass on a document no extractor can read. Calling
            # that SUCCESS reports an empty result as a completed one.
            status = "REVIEW"

    # ------------------------------------------------------------------
    # POST-EXTRACTION VALIDATION
    #
    # Structural verification has passed, which means the document is legible,
    # correctly classified and carries an identifier of the right shape. That
    # is the point at which this used to stop -- and it is why every readable
    # PAN in the sample corpus reached PASS, including a photograph of a
    # screen.
    #
    # The rules below need the EXTRACTED fields, which is why they could not
    # run inside verification: required fields present, the identifier's own
    # internal structure, consistency between fields, plausible dates. They
    # may only DOWNGRADE the verdict.
    #
    # A downgrade WITHDRAWS the extraction as well. The gate's rule is that
    # fields are released only behind a pass, and a verdict that is no longer
    # a pass must not keep the fields it was granted while it was one.
    # ------------------------------------------------------------------
    serialised_extraction = _serialise_extraction(extraction)

    # Serialised FIRST, so the score can be told which fields actually
    # arrived. Scoring a document before knowing what was extracted from
    # it would report field coverage of zero on a complete document.
    verification_payload = _serialise_identity_verification(
        verification,
        tokens=tokens,
        quality_report=quality_report,
        extracted_fields=(serialised_extraction or {}).get("fields") or {},
    )

    from app.agents.verification import rules as _rules

    fields = (serialised_extraction or {}).get("fields") or {}
    ruled_status, ruled_codes, rule_detail = _rules.apply(
        document_class=_type_value(resolved_class),
        status=verification.status,
        fields=fields,
        reason_codes=list(verification_payload.get("reason_codes") or []),
    )

    verification_payload["authenticity"] = rule_detail.get("authenticity")
    if rule_detail.get("advisories"):
        # Reported beside the verdict, deliberately NOT in reason_codes: a
        # note on a clean document reads as a problem with it.
        verification_payload["advisories"] = [
            a["code"] for a in rule_detail["advisories"]
        ]
    if ruled_status != verification.status:
        verification_payload["status"] = ruled_status
        verification_payload["reason_codes"] = ruled_codes
        serialised_extraction = None
        status = "REJECTED" if ruled_status == "FAIL" else "REVIEW"
        errors.extend(
            {"code": finding["code"], "message": finding["detail"]}
            for finding in rule_detail.get("findings", [])
        )

    return _envelope(
        request_id=request_id,
        status=status,
        document_type=(
            resolved_class
            if resolved_class is not DocumentClass.UNKNOWN
            else "UNKNOWN"
        ),
        category=_document_class_to_category(resolved_class),
        supported=supported,
        extraction=serialised_extraction,
        verification=verification_payload,
        ocr_status="SUCCESS",
        ocr_confidence=ocr_confidence,
        classification_status=(
            "SUCCESS"
            if resolved_class is not DocumentClass.UNKNOWN
            else "FAILED"
        ),
        classification_confidence=classification_confidence,
        errors=errors,
        timings=timings,
    )


# ---------------------------------------------------------------------------
# Public workflow
# ---------------------------------------------------------------------------

async def process_document(
    *,
    file_bytes: bytes,
    filename: str,
    operation: str = "VERIFY",
    requested_class: str | None = None,
    request_id: str,
    include_detail: bool = True,
    include_signals: bool = True,
) -> dict[str, Any]:
    """
    Main unified Document Agent workflow.

    Parameters
    ----------
    file_bytes:
        Uploaded document bytes.

    filename:
        Original upload filename. Used only for format handling.

    operation:
        VERIFY returns a structural verdict only. EXTRACT additionally returns
        fields, and only when verification passed.

    requested_class:
        Optional caller hint. AUTO/ANY means automatic classification.

    request_id:
        Request correlation identifier.

    Returns
    -------
    dict
        Stable unified response envelope.
    """

    _validate_filename(filename)

    operation = operation.strip().upper()

    if operation not in VALID_OPERATIONS:
        raise ValueError(
            "operation must be VERIFY or EXTRACT."
        )

    if not file_bytes:
        raise ValueError(
            "Uploaded file is empty."
        )

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError(
            "Uploaded file exceeds the "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit."
        )

    # Off the event loop from here: decoding, rasterisation, recognition and
    # the financial parsers all block, some of them for seconds.
    return await run_document(
        _process_document_sync,
        file_bytes,
        filename,
        operation,
        requested_class,
        request_id,
        include_detail,
        include_signals,
    )


def _process_document_sync(
    file_bytes: bytes,
    filename: str,
    operation: str,
    requested_class: str | None,
    request_id: str,
    include_detail: bool = True,
    include_signals: bool = True,
) -> dict[str, Any]:
    """Blocking body of the workflow. Runs on the document executor."""

    timings = Timings()
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        temp_path: str | None = None

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".pdf",
                delete=False,
            ) as temp:
                temp.write(file_bytes)
                temp.flush()
                temp_path = temp.name

            return _process_pdf(
                temp_path,
                operation=operation,
                requested_class=requested_class,
                request_id=request_id,
                timings=timings,
                include_detail=include_detail,
                include_signals=include_signals,
            )

        finally:
            if temp_path:
                _remove(temp_path)

    from io import BytesIO

    try:
        image = preprocess.load_stream(
            BytesIO(file_bytes)
        )

    except Exception as exc:
        # A truncated or mislabelled upload is the client's problem, not a
        # server fault. The PDF route already answers with the standard
        # envelope; images did not, and raised into a bare HTTP 500 instead.
        logger.warning(
            "Image could not be decoded request_id=%s: %r",
            request_id,
            exc,
        )

        return _unreadable(
            request_id=request_id,
            timings=timings,
            code="IMAGE_DECODE_FAILED",
            message=(
                "The uploaded image could not be decoded: "
                f"{type(exc).__name__}."
            ),
        )

    return _process_image(
        image,
        operation=operation,
        requested_class=requested_class,
        request_id=request_id,
        timings=timings,
        include_detail=include_detail,
        include_signals=include_signals,
    )


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        logger.debug(
            "Could not remove temporary document %s",
            path,
        )


# ---------------------------------------------------------------------------
# Image entry point
# ---------------------------------------------------------------------------

def _process_image(
    image,
    *,
    operation: str,
    requested_class: str | None,
    request_id: str,
    timings: "Timings | None" = None,
    include_detail: bool = True,
    include_signals: bool = True,
) -> dict[str, Any]:
    """Route a single image, escalating OCR only when a pass falls short."""

    timings = timings or Timings()

    recognition = _recognise(image)

    if recognition is None or not recognition.tokens:
        return _unreadable(
            request_id=request_id,
            code="OCR_EMPTY",
            message="OCR returned no readable text.",
            timings=timings,
        )

    tokens = recognition.tokens

    timings.ocr_ms += recognition.ocr_ms
    timings.extraction_ms += recognition.result.processing.extraction_ms

    classification = _timed(
        timings, "classification_ms",
        classify_document_class, _compact_text(tokens),
    )

    document_class, classification_confidence = classification

    ocr_confidence = _mean_confidence(tokens)

    # ---------------------------------------------------------------
    # Financial route
    #
    # Financial parsers own their document-specific processing. The OCR above
    # is used for classification only; the specialised financial parser
    # remains authoritative for extraction.
    # ---------------------------------------------------------------
    if document_class in _FINANCIAL_CLASSES:
        # Financial Agent parsers are path-based. Materialise the in-memory
        # image temporarily without changing their existing interfaces.
        with tempfile.NamedTemporaryFile(
            suffix=".png",
            delete=False,
        ) as temp:
            temp_path = temp.name

        try:
            preprocess.standard(image).save(
                temp_path,
                format="PNG",
            )

            return _financial_response(
                temp_path,
                requested_class=requested_class,
                document_class=document_class,
                classification_confidence=classification_confidence,
                operation=operation,
                request_id=request_id,
                ocr_status="SUCCESS",
                ocr_confidence=ocr_confidence,
                timings=timings,
                include_detail=include_detail,
                include_signals=include_signals,
            )

        finally:
            _remove(temp_path)

    # ---------------------------------------------------------------
    # Identity route.
    #
    # Verification and extraction consume the SAME recognition, so they agree
    # on which image variant and which tokens the verdict came from.
    # ---------------------------------------------------------------
    verification = (
        _timed(
            timings, "verification_ms",
            quick_verify_from_tokens,
            tokens,
            image=recognition.image,
            requested_class=requested_class,
            classification=classification,
        )
        if verification_enabled_for(document_class)
        else None
    )

    return _identity_response(
        request_id=request_id,
        operation=operation,
        document_class=document_class,
        classification_confidence=classification_confidence,
        verification=verification,
        recognition_result=recognition.result,
        ocr_confidence=ocr_confidence,
        supported_when_unverified=True,
        extra_errors=[],
        timings=timings,
        tokens=tokens,
        quality_report=recognition.quality,
    )


# ---------------------------------------------------------------------------
# PDF workflow
# ---------------------------------------------------------------------------

def _render_pdf_page(path: str, number: int):
    """
    Rasterise exactly one page.

    POPPLER IS FOUND ON PATH, OR NAMED BY THE ENVIRONMENT. Nothing is
    hardcoded: a build that ships Poppler somewhere the process PATH
    does not cover sets `POPPLER_PATH`, and a build where `pdftoppm` is
    already on PATH sets nothing and is unaffected.

    This exists because the alternative was tried. A developer machine
    needed a path, and the fix committed was the literal path of that
    machine's Poppler install -- which renders every PDF on that laptop
    and none anywhere else, including CI.
    """
    import os

    from pdf2image import convert_from_path

    options: dict[str, Any] = {}
    poppler_path = (os.getenv("POPPLER_PATH") or "").strip()
    if poppler_path:
        options["poppler_path"] = poppler_path

    pages = convert_from_path(
        path,
        dpi=pdf_render_dpi(),
        first_page=number,
        last_page=number,
        **options,
    )

    return pages[0] if pages else None


def _merge_pages(
    results: list[DocumentExtractionResult],
) -> DocumentExtractionResult:
    """
    Combine per-page extractions for one document.

    A single page is returned untouched: merge_results() recomputes status
    from the spec and would promote an all-fields-missing page from FAILED to
    PARTIAL, which the public status mapping treats very differently.
    """

    if len(results) == 1:
        return results[0]

    merged = pipeline.merge_results(results)

    if all(
        result.status is DocumentStatus.FAILED
        for result in results
    ):
        merged.status = DocumentStatus.FAILED

    return merged


def _process_pdf(
    path: str,
    *,
    operation: str,
    requested_class: str | None,
    request_id: str,
    timings: "Timings | None" = None,
    include_detail: bool = True,
    include_signals: bool = True,
) -> dict[str, Any]:
    """
    Process a PDF through the unified document workflow.

    Routing:
      1. Use the PDF text layer when available for fast classification.
      2. Financial documents are delegated to the Financial Agent.
      3. Identity PDFs are rasterised page by page and processed through the
         same OCR, verification and extraction pipeline used by images.
      4. VERIFY never exposes extraction.
      5. EXTRACT exposes extraction only after verification PASS.
    """
    timings = timings or Timings()

    try:
        from pypdf import PdfReader

        reader = PdfReader(path)

        if not reader.pages:
            return _unreadable(
                request_id=request_id,
                timings=timings,
                code="PDF_EMPTY",
                message="PDF contains no pages.",
            )

        # ------------------------------------------------------------------
        # Fast text-layer classification. A digital PDF is classified without
        # rasterising or recognising anything at all.
        # ------------------------------------------------------------------
        head_text = "\n".join(
            (page.extract_text() or "")
            for page in reader.pages[:2]
        )

        compact = _compact_layer_text(head_text)

        document_class = DocumentClass.UNKNOWN
        classification_confidence = 0.0

        if compact:
            (
                document_class,
                classification_confidence,
            ) = _timed(timings, "classification_ms",
                       classify_document_class, compact)

        if document_class in _FINANCIAL_CLASSES:
            return _financial_response(
                path,
                requested_class=requested_class,
                document_class=document_class,
                classification_confidence=classification_confidence,
                operation=operation,
                request_id=request_id,
                ocr_status="NOT_REQUIRED",
                ocr_confidence=0.0,
                timings=timings,
                include_detail=include_detail,
                include_signals=include_signals,
            )

        # ------------------------------------------------------------------
        # Identity or unclassified: rasterise one page at a time.
        #
        # Rendering every page up front meant a scanned bank statement paid
        # four 300dpi renders and four OCR passes only to be handed to the
        # financial parser, which then read the file again from scratch.
        # ------------------------------------------------------------------
        total_pages = len(reader.pages)
        limit = max_pdf_pages()
        page_count = min(total_pages, limit)

        errors: list[dict[str, Any]] = []

        if total_pages > limit:
            errors.append(
                {
                    "code": "PDF_PAGES_TRUNCATED",
                    "message": (
                        f"Document has {total_pages} pages; only the first "
                        f"{limit} were read. Raise DOCUMENT_MAX_PDF_PAGES to "
                        "read more."
                    ),
                }
            )

        recognitions: list[pipeline.Recognition] = []
        rendered = 0
        carried_type: DocumentType | None = None

        # Rasterising the next page overlaps with recognising the current one.
        # They contend for nothing: rendering is a poppler subprocess, OCR is
        # the serialised engine.
        render_pool = get_render_executor()
        pending: Future | None = None

        try:
            for number in range(1, page_count + 1):
                if pending is None:
                    page_image = _render_pdf_page(path, number)
                else:
                    page_image = pending.result()
                    pending = None

                # Prefetch only once the document is known to be an identity
                # one, either from the text layer or from a previous page.
                # While the class is still UNKNOWN the very next step may hand
                # the file to the Financial Agent and return, and a speculative
                # 300dpi render would then be pure waste competing with the
                # financial parser for CPU.
                if (
                    number < page_count
                    and (
                        document_class in _IDENTITY_CLASSES
                        or carried_type is not None
                    )
                ):
                    pending = render_pool.submit(
                        _render_pdf_page, path, number + 1
                    )

                if page_image is None:
                    continue

                rendered += 1

                # The reverse of a card carries no masthead, so it classifies
                # as UNKNOWN alone. Carrying the front's answer forward is
                # what force_type exists for.
                #
                # Rotations are for photographs; a rasterised page is already
                # upright. Contrast recovery is tried on the first page only
                # -- if the scan was too faint to read there, the rest of the
                # same scan will be too, and paying for it per page turned a
                # four-page unreadable document into eight OCR passes.
                recognition = _recognise(
                    page_image,
                    force_type=carried_type,
                    escalate=(number == 1),
                    rotate=False,
                )

                if recognition is None or not recognition.tokens:
                    continue

                recognitions.append(recognition)
                timings.ocr_ms += recognition.ocr_ms
                timings.extraction_ms += (
                    recognition.result.processing.extraction_ms
                )

                if (
                    carried_type is None
                    and recognition.result.document_type
                    is not DocumentType.UNKNOWN
                ):
                    carried_type = recognition.result.document_type

                # A scanned financial document can only be identified after
                # OCR. Recognising it on the first page avoids rendering the
                # rest of a fifty-page statement to learn the same thing.
                if document_class is DocumentClass.UNKNOWN:
                    (
                        page_class,
                        page_confidence,
                    ) = _timed(
                        timings, "classification_ms",
                        classify_document_class,
                        _compact_text(recognition.tokens),
                    )

                    if page_class in _FINANCIAL_CLASSES:
                        return _financial_response(
                            path,
                            requested_class=requested_class,
                            document_class=page_class,
                            classification_confidence=page_confidence,
                            operation=operation,
                            request_id=request_id,
                            ocr_status="SUCCESS",
                            ocr_confidence=_mean_confidence(
                                recognition.tokens
                            ),
                            timings=timings,
                            include_detail=include_detail,
                include_signals=include_signals,
                        )

                    if page_class is not DocumentClass.UNKNOWN:
                        document_class = page_class
                        classification_confidence = page_confidence

                # Stop rendering once the document is genuinely resolved. A
                # dense but unidentified page is NOT resolved: the next page
                # may carry the header this one lacked.
                if _document_resolved(operation, recognition.result):
                    break

        finally:
            # Leaving early -- resolved, or handed to the Financial Agent --
            # abandons a page nobody will read.
            if pending is not None:
                pending.cancel()

        if not rendered:
            return _unreadable(
                request_id=request_id,
                timings=timings,
                code="PDF_RENDER_EMPTY",
                message="PDF could not be rendered.",
            )

        if not recognitions:
            return _unreadable(
                request_id=request_id,
                timings=timings,
                code="OCR_EMPTY",
                message="OCR returned no readable text.",
            )

        # ------------------------------------------------------------------
        # A financial document whose first page was inconclusive is still
        # worth identifying from everything that was read.
        # ------------------------------------------------------------------
        if document_class is DocumentClass.UNKNOWN:
            combined = "".join(
                _compact_text(recognition.tokens)
                for recognition in recognitions
            )

            (
                document_class,
                classification_confidence,
            ) = _timed(timings, "classification_ms",
                       classify_document_class, combined)

            if document_class in _FINANCIAL_CLASSES:
                return _financial_response(
                    path,
                    requested_class=requested_class,
                    document_class=document_class,
                    classification_confidence=classification_confidence,
                    operation=operation,
                    request_id=request_id,
                    ocr_status="SUCCESS",
                    ocr_confidence=_mean_confidence(
                        recognitions[0].tokens
                    ),
                    timings=timings,
                    include_detail=include_detail,
                include_signals=include_signals,
                )

        # ------------------------------------------------------------------
        # The page that identified the document leads. Verification reads that
        # page's tokens against that page's pixels -- mixing pages put one
        # page's signature caption over another page's image.
        # ------------------------------------------------------------------
        primary = max(
            recognitions,
            key=lambda recognition: (
                recognition.result.classification_confidence
            ),
        )

        # The document-level class is already resolved -- from the text layer
        # or from the pages that were read. Handing it to verification keeps
        # routing and the verdict on the same answer, and avoids classifying
        # the same evidence twice.
        verification = (
            _timed(
                timings, "verification_ms",
                quick_verify_from_tokens,
                primary.tokens,
                image=primary.image,
                requested_class=requested_class,
                classification=(document_class, classification_confidence),
            )
            if verification_enabled_for(document_class)
            else None
        )

        extraction = _merge_pages(
            [recognition.result for recognition in recognitions]
        )

        return _identity_response(
            request_id=request_id,
            operation=operation,
            document_class=document_class,
            classification_confidence=classification_confidence,
            verification=verification,
            recognition_result=extraction,
            ocr_confidence=_mean_confidence(primary.tokens),
            supported_when_unverified=(
                document_class in _IDENTITY_CLASSES
            ),
            extra_errors=errors,
            timings=timings,
            tokens=primary.tokens,
            quality_report=primary.quality,
        )

    except Exception as exc:
        logger.exception(
            "PDF document processing failed request_id=%s",
            request_id,
        )

        return _envelope(
            request_id=request_id,
            status="FAILED",
            document_type=DocumentType.UNKNOWN,
            category=None,
            supported=False,
            extraction=None,
            verification={
                "status": "FAIL",
                "checks": {},
            },
            ocr_status="FAILED",
            ocr_confidence=0.0,
            classification_status="FAILED",
            classification_confidence=0.0,
            timings=timings,
            errors=[
                {
                    "code": "PDF_PROCESSING_ERROR",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
        )


__all__ = [
    "process_document",
    "MAX_UPLOAD_BYTES",
    "ALLOWED_SUFFIXES",
]
