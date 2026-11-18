
"""
Document Agent pipeline.

    classify -> extract -> normalize -> validate -> confidence -> result

Normalization happens inside the field extractors (they return canonical
values); this module owns orchestration, validation, confidence and timing.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.document_agent import validate as V
from app.agents.document_agent.normalize import name_key
from app.agents.document_agent.classify import classify
from app.agents.document_agent.fields.dl import extract_dl_fields
from app.agents.document_agent.fields.passport import extract_passport_fields
from app.agents.document_agent.fields.voter import extract_voter_fields
from app.agents.document_agent.fields.pan import extract_pan_fields
from app.agents.document_agent import (
    name_spacing,
    preprocess,
    preprocess_plan,
    quality,
)
from app.agents.document_agent.ocr import get_engine
from app.agents.document_agent.schemas import (
    DocumentExtractionResult,
    DocumentStatus,
    DocumentType,
    ExtractedField,
    FieldStatus,
    OCRToken,
    ProcessingTimes,
    ValidationStatus,
)

logger = logging.getLogger(__name__)

# field -> (validator, required)
PAN_SPEC = {
    "pan_number": (V.validate_pan, True),
    "name": (V.validate_name, True),
    "father_name": (V.validate_name, False),
    "date_of_birth": (V.validate_dob, True),
}

DL_SPEC = {
    "dl_number": (V.validate_dl_number, True),
    "name": (V.validate_name, True),
    "guardian_name": (V.validate_name, False),
    "date_of_birth": (V.validate_dob, True),
    "date_of_issue": (V.validate_past_date, False),
    "valid_till": (V.validate_expiry, False),
    "address": (
        lambda v: (
            (ValidationStatus.VALID, None)
            if v
            else (ValidationStatus.NOT_VALIDATED, None)
        ),
        False,
    ),
    "pin_code": (V.validate_pin, False),
    "vehicle_classes": (V.validate_vehicle_classes, False),
}

# Confidence below this is reported but flagged UNCERTAIN for human review.
UNCERTAIN_BELOW = 0.55


# relation_name (2026-11-04): a Voter ID's father / husband name lost its space ("RAMESHVERMA") on 24 of 36 synthetic
# held-out cards -- the same OCR line merge as the holder's name, recovered by the same letters-identical guard
NAME_FIELDS = {"name", "father_name", "guardian_name", "relation_name"}

# A single word at least this long suggests the recogniser merged two names.
# The letters-identical guard in name_spacing still decides what is accepted,
# so a false trigger costs time but can never corrupt a value.
SUSPICIOUS_WORD_LEN = 8


def _split_by_card_words(value: str, field_name: str, raw: dict) -> str | None:
    """
    A merged name re-spaced with the words the card's OTHER name fields already carry, or None.

    Every long merged word must split, exactly and entirely, into those words (at least two of them, each 2+
    letters); the letters are never changed, only grouped. A word that cannot be split that way leaves the whole
    name to the Tesseract recovery, as before.
    """
    vocab = {w for name in NAME_FIELDS if name != field_name
             for w in str((raw.get(name) or (None,))[0] or "").upper().split() if len(w) >= 2 and w.isalpha()}
    if not vocab:
        return None

    def split(word: str) -> list[str] | None:
        best: dict[int, list[str]] = {0: []}
        for i in range(1, len(word) + 1):
            for w in vocab:
                j = i - len(w)
                if j >= 0 and j in best and word[j:i] == w:
                    cand = best[j] + [w]
                    if i not in best or len(cand) < len(best[i]):
                        best[i] = cand
        if len(word) in best:
            return best[len(word)]
        # ONE first name the card does not repeat ("RAMESH" + the shared surname "SHARMA"): a single unknown part of
        # 3+ letters at the start or the end, the rest entirely the card's own words
        for cut in range(3, len(word) - 1):
            head, tail = word[:cut], word[cut:]
            rest = split_known(tail)
            if rest:
                return [head] + rest
            rest = split_known(word[:len(word) - cut])
            if rest and len(word[len(word) - cut:]) >= 3:
                return rest + [word[len(word) - cut:]]
        return None

    def split_known(word: str) -> list[str] | None:
        best: dict[int, list[str]] = {0: []}
        for i in range(1, len(word) + 1):
            for w in vocab:
                j = i - len(w)
                if j >= 0 and j in best and word[j:i] == w:
                    cand = best[j] + [w]
                    if i not in best or len(cand) < len(best[i]):
                        best[i] = cand
        return best.get(len(word))

    out, changed = [], False
    for word in value.upper().split():
        if len(word) < SUSPICIOUS_WORD_LEN:
            out.append(word)
            continue
        parts = split(word)
        if not parts or len(parts) < 2:
            return None
        out.extend(parts)
        changed = True
    return " ".join(out) if changed else None


def _build_field(
    value,
    token,
    validator,
    field_name: str = "",
) -> ExtractedField:
    if value is None:
        return ExtractedField(status=FieldStatus.MISSING)

    status, error = validator(value)
    ocr_conf = float(token.confidence) if token is not None else 0.0

    # Confidence combines how well OCR read the text with whether the value
    # survived deterministic validation. An INVALID value is never confident.
    if status is ValidationStatus.INVALID:
        confidence = min(ocr_conf, 0.30)
        field_status = FieldStatus.UNCERTAIN
    else:
        confidence = ocr_conf
        field_status = (
            FieldStatus.UNCERTAIN
            if ocr_conf < UNCERTAIN_BELOW
            else FieldStatus.EXTRACTED
        )

    return ExtractedField(
        value=value,
        confidence=round(confidence, 4),
        status=field_status,
        validation=status,
        validation_error=error,
        evidence=(token.text if token is not None else None),
        ocr_confidence=round(ocr_conf, 4),
        match_key=(
            name_key(str(value))
            if field_name in NAME_FIELDS
            else None
        ),
    )


def extract_from_tokens(
    tokens: list[OCRToken],
    *,
    ocr_ms: float = 0.0,
    ocr_engine: str = "",
    image=None,
    force_type: DocumentType | None = None,
) -> DocumentExtractionResult:
    """
    Run the pipeline over already-OCR'd tokens.

    `image` is optional and used only to recover word spacing inside name
    fields. Without it the pipeline behaves exactly as before, so callers
    that supply tokens alone are unaffected.

    Name spacing optimization:
      - First determine whether any name field is suspicious.
      - If at least one suspicious name exists, run Tesseract ONCE over the
        whole image using name_spacing.word_map().
      - Reuse those word boxes for every suspicious name field.
      - recover_from_words() applies the existing letters-identical safety
        guard before accepting any recovered value.

    This avoids running Tesseract separately for name, father_name and/or
    guardian_name.
    """
    started = time.perf_counter()

    # -----------------------------------------------------------------------
    # Classification
    # -----------------------------------------------------------------------
    t0 = time.perf_counter()
    doc_type, classification_confidence = classify(tokens)
    # The reverse of an ID card carries no masthead, so it classifies as
    # UNKNOWN on its own. When the front has already identified the
    # document, that answer is carried over rather than rediscovered.
    if force_type is not None and doc_type is DocumentType.UNKNOWN:
        doc_type = force_type
        classification_confidence = max(classification_confidence, 0.5)
    classification_ms = (time.perf_counter() - t0) * 1000

    if doc_type is DocumentType.UNKNOWN:
        total = (time.perf_counter() - started) * 1000 + ocr_ms

        return DocumentExtractionResult(
            document_type=DocumentType.UNKNOWN,
            status=DocumentStatus.UNSUPPORTED,
            classification_confidence=round(
                classification_confidence,
                4,
            ),
            ocr_engine=ocr_engine,
            errors=[
                "Document type could not be identified from OCR content."
            ],
            processing=ProcessingTimes(
                ocr_ms=round(ocr_ms, 2),
                classification_ms=round(classification_ms, 2),
                total_ms=round(total, 2),
            ),
        )

    # -----------------------------------------------------------------------
    # Field extraction
    # -----------------------------------------------------------------------
    spec = (
        PAN_SPEC
        if doc_type is DocumentType.PAN
        else VOTER_SPEC if doc_type is DocumentType.VOTER_ID
        else PASSPORT_SPEC if doc_type is DocumentType.PASSPORT
        else DL_SPEC
    )

    extractor = {
        DocumentType.PAN: extract_pan_fields,
        DocumentType.VOTER_ID: extract_voter_fields,
        DocumentType.PASSPORT: extract_passport_fields,
    }.get(doc_type, extract_dl_fields)

    t0 = time.perf_counter()
    raw = extractor(tokens)
    extraction_ms = (time.perf_counter() - t0) * 1000

    # -----------------------------------------------------------------------
    # Name spacing recovery
    #
    # IMPORTANT OPTIMIZATION:
    #
    # OLD:
    #   name          -> Tesseract crop
    #   father_name   -> Tesseract crop
    #   guardian_name -> Tesseract crop
    #
    # NEW:
    #   suspicious names
    #        ↓
    #   ONE Tesseract whole-image word pass
    #        ↓
    #   reuse word boxes for every suspicious name
    #
    # The existing letters-identical guard remains the final safety check.
    # -----------------------------------------------------------------------
    spacing_ms = 0.0
    spacing_calls = 0

    if image is not None and name_spacing.enabled():

        suspicious_fields: list[tuple[str, str, OCRToken]] = []

        # Cheap pass first. Do not invoke Tesseract at all unless at least
        # one name actually looks suspicious.
        for field_name in NAME_FIELDS:
            value, token = raw.get(
                field_name,
                (None, None),
            )

            if value is None or token is None:
                continue

            words = str(value).split()

            if not words:
                continue

            if max(len(word) for word in words) < SUSPICIOUS_WORD_LEN:
                continue

            # THE CARD'S OTHER NAMES FIRST (2026-11-05): a guardian "RAJENDRAMAHALE" beside a holder "MAHALE MANSI
            # RAJENDRA" is the same letters as two words the card already spaced. Split by those words -- letters
            # identical, as the Tesseract guard requires -- and the ~2 s crop read is not needed.
            sibling = _split_by_card_words(str(value), field_name, raw)
            if sibling is not None:
                raw[field_name] = (sibling, token)
                continue

            suspicious_fields.append(
                (
                    field_name,
                    str(value),
                    token,
                )
            )

        # Only pay for Tesseract when there is something worth recovering.
        if suspicious_fields:
            t_spacing = time.perf_counter()

            try:
                # Targeted crops, NOT a whole-image Tesseract pass.
                #
                # Measured on the sample set, one whole-image word map costs
                # 842-1520ms while cropping just the suspicious name regions
                # costs 368-419ms for the same result. The crop path also
                # handles tokens that merged a label with its value
                # ("S/D/W of: AJIT SINGH"), which the word map does not.
                # CONCURRENTLY: each name is an independent Tesseract read of its
                # own crop (a separate process), so two names cost one read's
                # time instead of two -- the same reads, the same guard, the
                # same results. Measured on a licence: 2 reads, ~345 ms in series.
                spacing_calls += len(suspicious_fields)
                if len(suspicious_fields) > 1:
                    from concurrent.futures import ThreadPoolExecutor

                    with ThreadPoolExecutor(max_workers=len(suspicious_fields)) as pool:
                        recovered = list(pool.map(
                            lambda item: name_spacing.recover(image, item[2], value=item[1]),
                            suspicious_fields))
                else:
                    recovered = [name_spacing.recover(image, token, value=value)
                                 for _, value, token in suspicious_fields]
                for (field_name, _value, token), spaced in zip(suspicious_fields, recovered):
                    if spaced:
                        raw[field_name] = (spaced, token)

            except Exception as exc:
                # Name spacing is an enhancement. Never fail the document
                # extraction because the optional recovery pass failed.
                logger.debug(
                    "Optimized name spacing failed: %s",
                    exc,
                )

            spacing_ms = (
                time.perf_counter() - t_spacing
            ) * 1000

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------
    t0 = time.perf_counter()

    fields: dict[str, ExtractedField] = {}

    for name, (validator, _required) in spec.items():
        value, token = raw.get(
            name,
            (None, None),
        )

        fields[name] = _build_field(
            value,
            token,
            validator,
            field_name=name,
        )

    validation_ms = (
        time.perf_counter() - t0
    ) * 1000

    # -----------------------------------------------------------------------
    # Cross-field validation
    # -----------------------------------------------------------------------
    warnings = V.cross_validate_dates(
        fields.get(
            "date_of_birth",
            ExtractedField(),
        ).value,
        fields.get(
            "date_of_issue",
            ExtractedField(),
        ).value,
        fields.get(
            "valid_till",
            ExtractedField(),
        ).value,
    )

    # -----------------------------------------------------------------------
    # Required-field status
    # -----------------------------------------------------------------------
    required = [
        n
        for n, (_v, req) in spec.items()
        if req
    ]

    missing_required = [
        n
        for n in required
        if fields[n].status is FieldStatus.MISSING
    ]

    invalid_required = [
        n
        for n in required
        if fields[n].validation is ValidationStatus.INVALID
    ]

    if missing_required or invalid_required:
        status = DocumentStatus.PARTIAL
    else:
        status = DocumentStatus.SUCCESS

    if len(missing_required) == len(required):
        status = DocumentStatus.FAILED

    errors = []

    if missing_required:
        errors.append(
            f"Required fields missing: {missing_required}"
        )

    if invalid_required:
        errors.append(
            f"Required fields failed validation: {invalid_required}"
        )

    # -----------------------------------------------------------------------
    # Total timing
    # -----------------------------------------------------------------------
    total = (
        (time.perf_counter() - started) * 1000
        + ocr_ms
    )

    return DocumentExtractionResult(
        document_type=doc_type,
        status=status,
        classification_confidence=round(
            classification_confidence,
            4,
        ),
        fields=fields,
        ocr_engine=ocr_engine,
        errors=errors,
        warnings=warnings,
        processing=ProcessingTimes(
            ocr_ms=round(ocr_ms, 2),
            classification_ms=round(classification_ms, 2),
            extraction_ms=round(extraction_ms, 2),
            validation_ms=round(validation_ms, 2),
            name_spacing_ms=round(spacing_ms, 2),
            name_spacing_calls=spacing_calls,
            total_ms=round(total, 2),
        ),
    )


PASSPORT_SPEC: dict[str, tuple] = {
    # The MRZ carries a check digit on every field, so these are verified by
    # the document itself rather than by a format rule imposed here.
    "passport_number": (V.validate_passthrough, True),
    "name": (V.validate_name, True),
    "date_of_birth": (V.validate_dob, True),
    "surname": (V.validate_name, False),
    "given_names": (V.validate_name, False),
    "nationality": (V.validate_passthrough, False),
    "issuing_country": (V.validate_passthrough, False),
    "sex": (V.validate_passthrough, False),
    "date_of_expiry": (V.validate_passthrough, False),
    "personal_number": (V.validate_passthrough, False),

    # Required. A passport whose composite check digit fails has a misread
    # MRZ, and every value taken from it is suspect -- that must surface as a
    # failed field rather than pass quietly.
    "mrz_verified": (V.validate_bool_true, True),
    "mrz_valid": (V.validate_bool_true, False),
    "mrz_warnings": (V.validate_passthrough, False),
}


VOTER_SPEC: dict[str, tuple] = {
    # Required: the number and the elector's name are what a KYC check needs.
    "epic_number": (V.validate_epic, True),
    "name": (V.validate_name, True),
    # Optional: a card prints a relation, and either a date of birth OR an
    # age -- never both. Marking either required would make every card
    # PARTIAL.
    "relation_name": (V.validate_name, False),
    "relation_type": (V.validate_passthrough, False),
    "gender": (V.validate_gender, False),
    "date_of_birth": (V.validate_dob, False),
    "age": (V.validate_age, False),
    "address": (V.validate_passthrough, False),
}


def merge_results(
    results: list[DocumentExtractionResult],
) -> DocumentExtractionResult:
    """
    Combine per-page results for one document.

    An ID card split across pages carries different fields on each side. The
    first page that produced a value for a field wins, and a later page can
    only fill a gap -- it never overwrites something already read, so a
    low-confidence reverse-side echo cannot displace a clean front-side value.
    """
    usable = [r for r in results if r.document_type is not DocumentType.UNKNOWN]
    if not usable:
        return results[0] if results else DocumentExtractionResult(
            document_type=DocumentType.UNKNOWN, status=DocumentStatus.FAILED
        )

    # The page that identified the document leads. Choosing whichever page had
    # the most fields let the reverse side -- dense with address and note text
    # but carrying no masthead -- take over, and its low-confidence guess at a
    # name displaced the correct one printed on the front.
    base = max(usable, key=lambda r: r.classification_confidence)

    for other in usable:
        if other is base or other.document_type is not base.document_type:
            continue
        for name, field in other.fields.items():
            if field.status is not FieldStatus.EXTRACTED:
                continue
            existing = base.fields.get(name)
            if existing is None or existing.status is not FieldStatus.EXTRACTED:
                base.fields[name] = field
                continue

            incoming_rank = _evidence_rank(field)
            existing_rank = _evidence_rank(existing)

            if incoming_rank > existing_rank:
                # A value that passes its format check outranks one that does
                # not, however confidently the recogniser read the bad one --
                # OCR confidence measures legibility, not correctness.
                base.fields[name] = field
            elif (
                incoming_rank == existing_rank
                and field.confidence > existing.confidence + 0.15
            ):
                # Same standing: only a clearly better read replaces an
                # existing value, so a marginal difference never disturbs a
                # good one.
                base.fields[name] = field

    # Each page result already carries the CUMULATIVE OCR time of its own
    # escalation passes, so summing them again double-counts: one card
    # reported 92624ms of OCR inside a 14888ms request. Take the largest
    # per-page figure, which is the real cost of the slowest page.
    base.processing.ocr_ms = round(
        max((r.processing.ocr_ms for r in results), default=0.0), 2
    )
    base.processing.ocr_passes = sum(r.processing.ocr_passes for r in results)
    base.warnings.append(f"Merged {len(results)} page(s) of one document.")

    spec = _spec_for(base.document_type)
    missing = [
        n for n, (_v, required) in spec.items()
        if required and (
            base.fields.get(n) is None
            or base.fields[n].status is not FieldStatus.EXTRACTED
        )
    ]
    base.status = DocumentStatus.SUCCESS if not missing else DocumentStatus.PARTIAL
    return base


_VALIDATION_RANK = {
    ValidationStatus.VALID: 2,
    ValidationStatus.NOT_VALIDATED: 1,
    ValidationStatus.INVALID: 0,
}


def _evidence_rank(field: ExtractedField) -> int:
    """
    How much a page's reading of a field should be trusted against another's.

    Whether the value survived its format check comes first: a front-side PAN
    that validates must not be displaced by a reverse-side echo that merely
    read louder.
    """
    return _VALIDATION_RANK.get(field.validation, 1)


def _spec_for(document_type: DocumentType) -> dict[str, tuple]:
    if document_type is DocumentType.PAN:
        return PAN_SPEC
    if document_type is DocumentType.VOTER_ID:
        return VOTER_SPEC
    if document_type is DocumentType.PASSPORT:
        return PASSPORT_SPEC
    return DL_SPEC


def _quality(
    result: DocumentExtractionResult,
) -> tuple[int, int]:
    """(required fields resolved, total fields resolved) -- higher is better."""
    if result.document_type is DocumentType.UNKNOWN:
        return (-1, -1)

    # Voter ID and Passport were both measured against DL_SPEC, so a complete
    # EPIC card looked like a licence missing dl_number and valid_till. That
    # made escalation fire on documents that had nothing left to recover.
    spec = _spec_for(result.document_type)

    required = sum(
        1
        for n, (_v, req) in spec.items()
        if req
        and result.fields.get(n)
        and result.fields[n].status is FieldStatus.EXTRACTED
    )

    total = sum(
        1
        for f in result.fields.values()
        if f.status is FieldStatus.EXTRACTED
    )

    return (required, total)


def _is_complete(
    result: DocumentExtractionResult,
) -> bool:
    """
    Whether a second OCR pass is worth its cost.

    Previously this demanded status == SUCCESS, so a document missing only an
    OPTIONAL field triggered the enhanced pass -- a full extra OCR run on a
    1.6x larger image, several seconds, for a field that may simply not be
    printed on that card. Escalation now happens only when a REQUIRED field
    is absent or invalid.
    """
    if result.document_type is DocumentType.UNKNOWN:
        return False

    spec = _spec_for(result.document_type)

    for field_name, (_validator, required) in spec.items():
        if not required:
            continue

        field = result.fields.get(field_name)

        if field is None or field.status is FieldStatus.MISSING:
            return False

        if field.validation is ValidationStatus.INVALID:
            return False

    return True


def extract_document_image(
    image,
    engine_name: str | None = None,
    force_type: DocumentType | None = None,
) -> DocumentExtractionResult:
    """
    Run the pipeline on an in-memory PIL image.

    Preferred for uploads. Writing the upload to disk makes on-access
    antivirus scan the new file, and that scan competes with OCR for CPU on
    the same cores. Measured on the reporting host, the SAME image took
    1035ms of OCR in-memory versus 9416ms routed through a temporary file.
    """
    try:
        engine = get_engine(engine_name)

    except Exception as exc:
        logger.exception("OCR engine unavailable")

        return DocumentExtractionResult(
            document_type=DocumentType.UNKNOWN,
            status=DocumentStatus.FAILED,
            errors=[f"OCR engine unavailable: {exc}"],
        )

    return _run_passes(
        engine,
        image,
        force_type=force_type,
    )


def extract_document(
    image_path: str,
    engine_name: str | None = None,
    force_type: DocumentType | None = None,
) -> DocumentExtractionResult:
    """
    Full path: OCR the image, then run the pipeline.

    A standard pass handles good scans. If it does not produce a complete
    document, the image is retried with contrast enhancement and then with
    rotations. Escalation only costs time on documents that actually need it,
    and the best attempt wins -- never the last one.
    """
    try:
        engine = get_engine(engine_name)

    except Exception as exc:
        logger.exception("OCR engine unavailable")

        return DocumentExtractionResult(
            document_type=DocumentType.UNKNOWN,
            status=DocumentStatus.FAILED,
            errors=[f"OCR engine unavailable: {exc}"],
        )

    try:
        image = preprocess.load(image_path)

    except Exception as exc:
        logger.exception(
            "Could not open %s",
            image_path,
        )

        return DocumentExtractionResult(
            document_type=DocumentType.UNKNOWN,
            status=DocumentStatus.FAILED,
            ocr_engine=engine.name,
            errors=[
                f"Could not open image: {type(exc).__name__}: {exc}"
            ],
        )

    return _run_passes(
        engine,
        image,
        force_type=force_type,
    )


@dataclass
class Recognition:
    """
    One completed recognition, including the evidence it was based on.

    `_run_passes` used to discard the tokens and the image variant that
    actually won, returning only the extraction. Verification then had to be
    handed the ORIGINAL image and the standard-pass tokens, so a document that
    only succeeded after rotation was verified against evidence nobody had
    read. Keeping the winning pair lets extraction and verification share one
    OCR pass without that mismatch.
    """

    tokens: list[OCRToken]
    image: Any
    result: DocumentExtractionResult
    ocr_ms: float
    engine_name: str = ""
    passes: int = 0
    labels: list[str] = field(default_factory=list)
    #: What the image quality analysis measured on the ORIGINAL image.
    #:
    #: Carried on the recognition because verification needs it and
    #: re-measuring downstream would analyse whichever variant happened to
    #: win -- a deskewed, contrast-boosted copy is not the thing the
    #: customer uploaded, and reporting its quality would describe our own
    #: preprocessing back to them.
    quality: Any = None
    #: The second-engine vote (voting.py): ran, filled fields, disagreements.
    voting: Any = None


def _fill_missing_from_passes(best: "Recognition", attempts: list["Recognition"]) -> None:
    """
    A field the winning pass did NOT read, taken from another pass over the SAME
    image that read it and validated it -- never replacing a value the winner has.

    dl4: the full frame read the holder's name, the card crop read the guardian
    and the validity; whichever won, the other's fields were thrown away.
    """
    result = best.result
    spec = _spec_for(result.document_type)
    filled: list[str] = []
    for name in spec:
        own = result.fields.get(name)
        if own is not None and own.status is FieldStatus.EXTRACTED:
            continue
        for other in attempts:
            if other is best or other.result.document_type is not result.document_type:
                continue
            field = other.result.fields.get(name)
            if (field is not None and field.status is FieldStatus.EXTRACTED
                    and field.validation is ValidationStatus.VALID):
                result.fields[name] = field
                filled.append(name)
                break
    if not filled:
        return
    result.warnings.append("fields_from_other_pass=" + ",".join(filled))
    missing = [n for n, (_v, required) in spec.items()
               if required and (result.fields.get(n) is None
                                or result.fields[n].status is not FieldStatus.EXTRACTED)]
    if result.status is DocumentStatus.PARTIAL and not missing:
        result.status = DocumentStatus.SUCCESS


def mrz_band_enabled() -> bool:
    return (os.getenv("DOCUMENT_OCR_MRZ_BAND") or "true").strip().lower() not in {"false", "0", "no", "off"}


def _mrz_like(text: str) -> bool:
    """An MRZ line as OCR reads it: long, and dense with the '<' filler."""
    t = (text or "").replace(" ", "")
    return len(t) >= 28 and t.count("<") >= 3


#: the MRZ band is enlarged to about this width before it is read again (a 44-character OCR-B line wants ~30 px/char)
MRZ_BAND_WIDTH = 1400


def _mrz_band(recognition: "Recognition"):
    """(band image, its box in the pass's image, scale) around the MRZ lines the pass found, or None."""
    lines = [t for t in recognition.tokens if _mrz_like(t.text)]
    image = recognition.image
    if not lines or image is None:
        return None
    x0, y0 = min(t.x0 for t in lines), min(t.y0 for t in lines)
    x1, y1 = max(t.x1 for t in lines), max(t.y1 for t in lines)
    h = max(t.y1 - t.y0 for t in lines)
    box = (max(0, int(x0 - h)), max(0, int(y0 - h * 1.5)),
           min(image.width, int(x1 + h)), min(image.height, int(y1 + h * 1.5)))
    if box[2] - box[0] < 32 or box[3] - box[1] < 16:
        return None
    crop = image.crop(box)
    scale = max(1.0, MRZ_BAND_WIDTH / crop.width)
    if scale > 1.0:
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)))
    return crop, box, scale


def card_crop_enabled() -> bool:
    return (os.getenv("DOCUMENT_OCR_CARD_CROP") or "true").strip().lower() not in {"false", "0", "no", "off"}


def _crop_for_optional() -> bool:
    """Whether the card-crop pass also runs for a card whose REQUIRED fields are complete but an optional one is
    missing. Default from the A/B on the synthetic dev set (2026-11-04); env DOCUMENT_OCR_CROP_FOR_OPTIONAL."""
    return (os.getenv("DOCUMENT_OCR_CROP_FOR_OPTIONAL") or "false").strip().lower() in {"true", "1", "yes", "on"}


#: The crop pass runs only when the text spans less than this share of the frame.
CARD_CROP_MAX_AREA = 0.6


def _card_crop(recognition: "Recognition"):
    """The read text's bounding box plus a margin, cut from the pass's own image; None if it fills the frame."""
    tokens = [t for t in recognition.tokens if (t.x1 - t.x0) > 0 and (t.y1 - t.y0) > 0]
    image = recognition.image
    if len(tokens) < 3 or image is None:
        return None
    x0, y0 = min(t.x0 for t in tokens), min(t.y0 for t in tokens)
    x1, y1 = max(t.x1 for t in tokens), max(t.y1 for t in tokens)
    if (x1 - x0) * (y1 - y0) >= CARD_CROP_MAX_AREA * image.width * image.height:
        return None
    mx, my = (x1 - x0) * 0.08 + 6, (y1 - y0) * 0.08 + 6
    box = (max(0, int(x0 - mx)), max(0, int(y0 - my)),
           min(image.width, int(x1 + mx)), min(image.height, int(y1 + my)))
    if box[2] - box[0] < 32 or box[3] - box[1] < 32:
        return None
    return image.crop(box)


_ROTATION_POOL = None
_ROTATION_LOCK = threading.Lock()


def _parallel_rotations() -> bool:
    # OFF by default: MEASURED 2026-10-14 on 12 real passports, three engines reading at once took 20-32 s per
    # passport against 8-10 s in series on this host (same chosen pass every time). Kept for other hardware.
    return (os.getenv("DOCUMENT_OCR_PARALLEL_ROTATIONS", "false") or "false").strip().lower() == "true"


def _read_rotations(variants: list, engine=None) -> list:
    """(tokens, ocr_ms) -- or the exception -- per variant, in order. Concurrent on a 3-thread pool whose threads
    each own an OCR engine (thread name prefix "ocr" -> ocr.get_engine gives a per-thread engine; no session shared).
    None for each when parallel reads are off: the caller then reads them one by one as before."""
    from app.agents.document_agent import ocr as _ocr

    # only for the real RapidOCR engine: any other engine (Tesseract, a test double) is read in order as before
    if not _parallel_rotations() or len(variants) < 2 or type(engine) is not _ocr.RapidOCREngine:
        return [None] * len(variants)
    global _ROTATION_POOL
    from concurrent.futures import ThreadPoolExecutor

    with _ROTATION_LOCK:
        if _ROTATION_POOL is None:
            _ROTATION_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix=f"{_ocr._OCR_THREAD_PREFIX}-rotate")

    def read(variant):
        try:
            return _ocr.get_engine().read_array(preprocess.to_array(variant))
        except Exception as exc:  # noqa: BLE001 - reported by attempt() as that pass's failure
            return exc

    return list(_ROTATION_POOL.map(read, [v for _, v in variants]))


# The first look at a card. With DOCUMENT_OCR_ANGLE_CLS=retry (the default) it skips RapidOCR's per-line angle
# classifier; every later pass keeps it -- the card crop included: it runs only for a card the first pass left
# short, and on one real licence (dl4) it is the classifier on that crop that reads the holder's name.
_FIRST_PASSES = frozenset({"standard"})


def _read(engine, array, *, first: bool):
    """engine.read_array, telling RapidOCR to skip the angle classifier on a first pass in "retry" mode. Engines
    without the option (Tesseract ignores it; a test double may not take it) are called as before."""
    from app.agents.document_agent import ocr as _ocr

    if first and _ocr.angle_cls_mode() == "retry" and isinstance(engine, _ocr.RapidOCREngine):
        return engine.read_array(array, upright=True)
    return engine.read_array(array)


def _ocr_cls_retry(engine) -> bool:
    """Whether the first pass on this engine skipped the angle classifier (so a re-read with it can differ)."""
    from app.agents.document_agent import ocr as _ocr

    return _ocr.angle_cls_mode() == "retry" and isinstance(engine, _ocr.RapidOCREngine)


def recognise(
    engine,
    image,
    *,
    force_type: DocumentType | None = None,
    accept: Callable[[Recognition], bool] | None = None,
    escalate: bool = True,
    rotate: bool = True,
    final: Callable[[Recognition], bool] | None = None,
) -> Recognition | None:
    """
    OCR an image, escalating only while the caller is not satisfied.

    `accept` decides whether a pass is good enough to stop on. It receives the
    Recognition for that pass, so a caller can stop as soon as the document is
    classified (verification only needs the class and the identifier) rather
    than paying for field-completeness it will never look at. The default
    stops when every REQUIRED field is present and valid.

    `rotate` controls the orientation retries specifically. They exist for
    photographs of cards; a page rasterised from a PDF is upright by
    construction, and trying three more full-page passes on one is three more
    multi-second OCR runs that cannot change the answer.

    Returns None when no pass produced any text at all.
    """

    attempts: list[Recognition] = []

    cumulative_ocr_ms = 0.0

    telemetry: dict = {
        "source_width": image.width,
        "source_height": image.height,
        "processed_width": 0,
        "processed_height": 0,
        "ocr_thread": "",
        "ocr_passes": 0,
    }

    def satisfied(candidate: Recognition) -> bool:
        if accept is not None:
            return bool(accept(candidate))

        return _is_complete(candidate.result)

    def attempt(label: str, variant, read: tuple | None = None) -> Recognition | None:
        """One pass. `read` = (tokens, ocr_ms) already OCR'd elsewhere (the parallel rotations), else OCR it here."""
        nonlocal cumulative_ocr_ms

        try:
            telemetry["processed_width"] = variant.width
            telemetry["processed_height"] = variant.height
            telemetry["ocr_thread"] = threading.current_thread().name
            telemetry["ocr_passes"] += 1

            if isinstance(read, Exception):
                raise read
            tokens, ocr_ms = read if read is not None else _read(engine, preprocess.to_array(variant),
                                                                  first=label in _FIRST_PASSES)

        except Exception as exc:
            logger.warning("OCR pass '%s' failed: %s", label, exc)
            return None

        cumulative_ocr_ms += ocr_ms

        if not tokens:
            return None

        outcome = extract_from_tokens(
            tokens,
            ocr_ms=cumulative_ocr_ms,
            ocr_engine=engine.name,
            image=variant,
            force_type=force_type,
        )

        outcome.warnings.append(f"ocr_pass={label}")

        candidate = Recognition(
            tokens=tokens,
            image=variant,
            result=outcome,
            ocr_ms=cumulative_ocr_ms,
            engine_name=engine.name,
        )

        attempts.append(candidate)

        return candidate

    # WHAT IS WRONG WITH THE IMAGE, measured once, before any OCR.
    #
    # Used for two things and nothing else: choosing which recovery
    # transforms are worth trying, and explaining a document that does not
    # pass. It cannot fail a document -- see quality.py. Measuring costs a
    # few milliseconds of OpenCV on an image already in memory, against an
    # OCR pass that costs one to two seconds, so it pays for itself the
    # first time it lets one be skipped.
    report = quality.analyse(image)
    if not report.analysed:
        logger.debug("Image quality analysis unavailable; using legacy passes")

    standard_image = preprocess.ocr_ready(image)
    settled = False                       # `final` said no further pass can change the answer
    if final is None:
        current = attempt("standard", standard_image)
    else:
        # The first look WITHOUT name-spacing recovery (Tesseract, 0.4-1.3 s on a licence): if `final` says the
        # page is settled (another card than the one asked for, or an external-only document) its names are never
        # used. Otherwise the same tokens are extracted again WITH it -- identical to the plain path.
        from app.agents.document_agent import name_spacing as _spacing

        with _spacing.suppressed():
            current = attempt("standard", standard_image)
        if current is not None and final(current):
            escalate = rotate = False
            settled = True
        elif current is not None:
            spaced = extract_from_tokens(current.tokens, ocr_ms=current.ocr_ms, ocr_engine=engine.name,
                                         image=standard_image, force_type=force_type)
            spaced.warnings.append("ocr_pass=standard")
            current.result = spaced

    # THE MRZ BAND, RE-READ ALONE (2026-11-05). A passport whose first pass found the MRZ but could not verify it
    # (a digit dropped -- the birth date failed its check) paid three more FULL-PAGE passes (angle classifier, card
    # crop, a 2000-2400 px enhanced page): ~4-6 s on 9 / 12 hard synthetic passports. The two MRZ lines are already
    # located; cut just that band, enlarge it and read it again (one small image), swap those lines in. Kept only
    # when it resolves more (_quality); otherwise the usual escalation follows unchanged.
    if (current is not None and escalate and not settled and mrz_band_enabled()
            and current.result.document_type is DocumentType.PASSPORT and not _is_complete(current.result)):
        band = _mrz_band(current)
        if band is not None:
            band_image, box, scale = band
            try:
                band_tokens, band_ms = _read(engine, preprocess.to_array(band_image), first=False)
            except Exception as exc:  # noqa: BLE001 - the band is an extra chance, never a failure
                logger.warning("MRZ band pass failed: %s", exc)
                band_tokens, band_ms = [], 0.0
            cumulative_ocr_ms += band_ms
            telemetry["ocr_passes"] += 1
            if band_tokens:
                placed = [t.model_copy(update={"x0": box[0] + t.x0 / scale, "x1": box[0] + t.x1 / scale,
                                               "y0": box[1] + t.y0 / scale, "y1": box[1] + t.y1 / scale})
                          for t in band_tokens]
                merged = [t for t in current.tokens if not _mrz_like(t.text)] + placed
                outcome = extract_from_tokens(merged, ocr_ms=cumulative_ocr_ms, ocr_engine=engine.name,
                                              image=current.image, force_type=force_type)
                outcome.warnings.append("ocr_pass=mrz_band")
                candidate = Recognition(tokens=merged, image=current.image, result=outcome,
                                        ocr_ms=cumulative_ocr_ms, engine_name=engine.name)
                attempts.append(candidate)
                if _quality(outcome) > _quality(current.result):
                    current = candidate

    # THE SAME IMAGE WITH THE ANGLE CLASSIFIER, the first retry in "retry" mode: the first pass skipped it to save
    # time, and on a card it left short the classifier is the cheapest thing that can change the read (dl4: holder
    # name MISSING without it, OK with it -- one more pass of ~0.8 s instead of three escalations, ~5 s). Only for
    # an identified card missing a required field; a card the first pass completed never pays for it.
    if (current is not None and escalate and _ocr_cls_retry(engine)
            and current.result.document_type is not DocumentType.UNKNOWN and not _is_complete(current.result)):
        reread = attempt("standard_cls", standard_image)
        if reread is not None and _quality(reread.result) > _quality(current.result):
            current = reread

    # THE CARD, NOT THE DESK. A card photographed small on a table or a phone
    # screen leaves most of the frame to background, and the detector misses
    # whole lines of it: measured on two real licences (dl1, dl4) the holder's
    # name, the guardian and the validity were absent from the full frame and
    # read correctly from the card cropped out of it, at every scale and
    # rotation tried. One extra pass, only for an IDENTIFIED document still
    # missing a required field whose text fills little of the frame; the best
    # pass wins on _quality below, so it can only add fields.
    # 2026-11-04: the test was "any field missing" -- a Voter ID front never prints an address, so EVERY complete
    # card paid this pass for nothing. Now: a REQUIRED field missing (what the comment above always said), or an
    # optional one only when DOCUMENT_OCR_CROP_FOR_OPTIONAL is on.
    if (current is not None and not settled and card_crop_enabled()
            and current.result.document_type is not DocumentType.UNKNOWN
            and (not _is_complete(current.result)
                 or (_crop_for_optional()
                     and _quality(current.result)[1] < len(_spec_for(current.result.document_type))))):
        cropped = _card_crop(current)
        if cropped is not None:
            attempt("card_crop", preprocess.ocr_ready(cropped))

    if escalate:
        # TARGETED RECOVERY, only when the standard pass fell short, and
        # only for defects that were actually measured.
        #
        # This used to be one unconditional `enhance` -- upscale, grayscale
        # and global autocontrast together, whatever had gone wrong. A
        # clean colour card lost the colour channel the detector uses; a
        # merely-small image got a contrast stretch it did not need.
        #
        # Each variant below addresses one finding, and the loop STOPS at
        # the first pass good enough to satisfy the caller. An image with
        # no findings produces an empty plan and pays for nothing.
        if current is None or not satisfied(current):
            for variant in preprocess_plan.plan(report):
                try:
                    built = variant.build(image)
                    if built is image:          # nothing to change (e.g. no outline to flatten)
                        continue
                    candidate = attempt(variant.label, built)
                except Exception:
                    # A transform that fails costs its own variant and
                    # nothing else. The document still has every other
                    # route to being read.
                    logger.warning(
                        "Preprocessing variant '%s' failed", variant.label,
                        exc_info=True,
                    )
                    continue

                if candidate is not None and satisfied(candidate):
                    current = candidate
                    break

        # THE LEGACY COMBINED PASS, as a safety net rather than a first
        # response. It runs when the targeted variants did not recover the
        # document, and when quality analysis was unavailable -- so an
        # environment without OpenCV behaves exactly as it did before, and
        # this change can only add recoveries, never remove one.
        if current is None or not satisfied(current):
            legacy = preprocess_plan.fallback()
            enhanced = attempt(legacy.label, legacy.build(image))

            if enhanced is not None and satisfied(enhanced):
                current = enhanced

        # Rotations are only worth trying when the document is unreadable,
        # not merely incomplete. UNREADABLE INCLUDES "identified, nothing read":
        # a passport photographed sideways was classified from its header
        # (REPUBLIC OF INDIA reads at any angle) yet resolved ZERO fields, and
        # the identified-type check skipped the rotation that reads it
        # (passport_samples0_1, 2026-10-06). A rotated pass only replaces the
        # current one when it resolves more (_quality).
        best_so_far = max(attempts, key=lambda c: _quality(c.result)) if attempts else None
        unreadable = (best_so_far is None
                      or best_so_far.result.document_type is DocumentType.UNKNOWN
                      # no REQUIRED field read (a computed flag like mrz_valid=False
                      # still counts as a resolved field, so total is not the test)
                      or _quality(best_so_far.result)[0] == 0)
        if rotate and unreadable:
            variants = [(label, preprocess.ocr_ready(rotated)) for label, rotated in preprocess.rotations(image)]
            # THE THREE ROTATIONS ARE READ TOGETHER (2026-10-14): on 12 real passport photos 9 ran all three, one
            # after another, ~2-3 s each. Each is OCR'd on its own engine at the same time; the choice below is
            # the unchanged in-order loop (first rotation that resolves more wins), so the result is the same.
            reads = _read_rotations(variants, engine)
            for (label, rotated_image), read in zip(variants, reads):
                rotated_outcome = attempt(label, rotated_image, read)

                if (
                    rotated_outcome is not None
                    and rotated_outcome.result.document_type
                    is not DocumentType.UNKNOWN
                    and (best_so_far is None
                         or _quality(rotated_outcome.result) > _quality(best_so_far.result))
                ):
                    current = rotated_outcome
                    break

    if not attempts:
        return None

    best = max(attempts, key=lambda candidate: _quality(candidate.result))

    if len(attempts) > 1:
        best.result.warnings.append(f"ocr_passes_tried={len(attempts)}")
        _fill_missing_from_passes(best, attempts)

    best.ocr_ms = cumulative_ocr_ms
    best.passes = telemetry["ocr_passes"]
    best.labels = [
        warning.split("=", 1)[1]
        for candidate in attempts
        for warning in candidate.result.warnings
        if warning.startswith("ocr_pass=")
    ]

    best.result.processing.ocr_ms = round(cumulative_ocr_ms, 2)
    best.result.processing.source_width = telemetry["source_width"]
    best.result.processing.source_height = telemetry["source_height"]
    best.result.processing.processed_width = telemetry["processed_width"]
    best.result.processing.processed_height = telemetry["processed_height"]
    best.result.processing.ocr_thread = telemetry["ocr_thread"]
    best.result.processing.ocr_passes = telemetry["ocr_passes"]
    best.quality = report

    # TWO-ENGINE VOTING (voting.py): a required field the primary engine left
    # missing or invalid gets a second opinion; accepted only if it validates,
    # and a valid-but-different value is reported, never silently chosen.
    try:
        from app.agents.document_agent import voting

        best.voting = voting.second_opinion(best)
    except Exception:  # noqa: BLE001 - the primary result stands
        logger.warning("Second OCR opinion failed", exc_info=True)

    return best


def _run_passes(
    engine,
    image,
    force_type: DocumentType | None = None,
) -> DocumentExtractionResult:
    """Shared pass/escalation logic for both entry points."""
    outcome = recognise(
        engine,
        image,
        force_type=force_type,
    )

    if outcome is None:
        return DocumentExtractionResult(
            document_type=DocumentType.UNKNOWN,
            status=DocumentStatus.FAILED,
            ocr_engine=engine.name,
            errors=[
                "OCR returned no text. Image may be blank or unreadable."
            ],
            processing=ProcessingTimes(
                ocr_ms=0.0,
                total_ms=0.0,
            ),
        )

    return outcome.result


__all__ = [
    "extract_document",
    "extract_from_tokens",
    "merge_results",
    "recognise",
    "Recognition",
]

