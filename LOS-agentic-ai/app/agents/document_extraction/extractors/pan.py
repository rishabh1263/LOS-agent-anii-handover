from __future__ import annotations

import re
from typing import Any

from app.agents.document_agent.ocr import OCRToken


# ============================================================================
# PAN NUMBER
# ============================================================================

PAN_NUMBER_PATTERN = re.compile(
    r"^[A-Z]{5}[0-9]{4}[A-Z]$"
)


# ============================================================================
# NORMALIZATION
# ============================================================================

def _clean_text(value: Any) -> str:
    value = str(value or "").strip()

    return re.sub(
        r"\s+",
        " ",
        value,
    )


def _normalize(value: Any) -> str:
    value = _clean_text(value).upper()

    value = re.sub(
        r"[^A-Z0-9 ]",
        " ",
        value,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


# ============================================================================
# OCR TOKEN HELPERS
# ============================================================================

def _text(item: Any) -> str:
    return _clean_text(
        getattr(item, "text", "")
    )


def _confidence(item: Any) -> float:
    try:
        return float(
            getattr(
                item,
                "confidence",
                0.0,
            )
            or 0.0
        )
    except (TypeError, ValueError):
        return 0.0


def _bbox(item: Any) -> list[float]:
    try:
        return [
            float(getattr(item, "x0")),
            float(getattr(item, "y0")),
            float(getattr(item, "x1")),
            float(getattr(item, "y1")),
        ]
    except (TypeError, ValueError, AttributeError):
        return []


def _bbox_top(item: Any) -> float | None:
    bbox = _bbox(item)

    if len(bbox) != 4:
        return None

    return bbox[1]


def _bbox_bottom(item: Any) -> float | None:
    bbox = _bbox(item)

    if len(bbox) != 4:
        return None

    return bbox[3]


# ============================================================================
# PAN NUMBER
# ============================================================================

def _is_pan_number(text: str) -> bool:

    normalized = re.sub(
        r"\s+",
        "",
        _clean_text(text).upper(),
    )

    return bool(
        PAN_NUMBER_PATTERN.fullmatch(
            normalized
        )
    )


def _find_pan_index(
    items: list[Any],
) -> int | None:

    for index, item in enumerate(items):

        if _is_pan_number(
            _text(item)
        ):
            return index

    return None


# ============================================================================
# DATE
# ============================================================================

def _is_date(text: str) -> bool:

    patterns = (
        r"\b\d{2}[/-]\d{2}[/-]\d{4}\b",
        r"\b\d{2}[/-]\d{2}[/-]\d{2}\b",
        r"\b\d{4}[/-]\d{2}[/-]\d{2}\b",
    )

    return any(
        re.search(
            pattern,
            text,
        )
        for pattern in patterns
    )


# ============================================================================
# NOISE
# ============================================================================

def _is_noise(text: str) -> bool:

    normalized = _normalize(text)

    if not normalized:
        return True

    if _is_pan_number(normalized):
        return True

    if _is_date(normalized):
        return True

    known_phrases = {
        "INCOME TAX DEPARTMENT",
        "GOVT OF INDIA",
        "GOVT O F INDIA",
        "GOVERNMENT OF INDIA",
        "PERMANENT ACCOUNT NUMBER",
        "PERMANENT ACCOUNT",
        "ACCOUNT NUMBER",
        "PAN CARD",
        "SIGNATURE",
        "DATE OF BIRTH",
        "FATHERS NAME",
        "FATHER S NAME",
    }

    if normalized in known_phrases:
        return True

    words = normalized.split()

    institutional_words = {
        "INCOME",
        "NCOME",
        "TAX",
        "DEPARTMENT",
        "GOVT",
        "GOVERNMENT",
        "INDIA",
    }

    if sum(
        word in institutional_words
        for word in words
    ) >= 2:
        return True

    if (
        "SIGNATURE" in words
        or "DOB" in words
        or "DATE" in words
    ):
        return True

    if (
        "FATHER" in words
        or "FATHERS" in words
    ):
        return True

    digit_count = sum(
        character.isdigit()
        for character in normalized
    )

    if digit_count >= 3:
        return True

    return False


# ============================================================================
# NAME CANDIDATE
# ============================================================================

def _is_name_candidate(
    text: str,
) -> bool:

    text = _clean_text(text)

    if not text:
        return False

    if _is_noise(text):
        return False

    normalized = _normalize(text)

    # OCR may return:
    #
    # LAXMISANTOSHGUPTA
    #
    # instead of:
    #
    # LAXMI SANTOSH GUPTA
    #
    # Therefore we do NOT require spaces here.

    letters_only = re.sub(
        r"[^A-Z]",
        "",
        normalized,
    )

    if not letters_only:
        return False

    if not letters_only.isalpha():
        return False

    if not (
        5 <= len(letters_only) <= 40
    ):
        return False

    # Reject very short labels.
    if len(letters_only) < 5:
        return False

    return True


# ============================================================================
# NAME LABEL
# ============================================================================

def _is_name_label(
    text: str,
) -> bool:

    normalized = _normalize(text)

    normalized = normalized.replace(
        " ",
        "",
    )

    return normalized == "NAME"


# ============================================================================
# FATHER LABEL
# ============================================================================

def _is_father_label(
    text: str,
) -> bool:

    normalized = _normalize(text)

    normalized = normalized.replace(
        " ",
        "",
    )

    return normalized in {
        "FATHER",
        "FATHERSNAME",
        "FATHERNAME",
    }


# ============================================================================
# LABEL-BASED EXTRACTION
# ============================================================================

def _extract_after_label(
    items: list[Any],
    label_index: int,
) -> str | None:

    label_item = items[label_index]

    label_bottom = _bbox_bottom(
        label_item
    )

    candidates: list[
        tuple[float, str]
    ] = []

    # Only inspect a few following OCR tokens.
    for index in range(
        label_index + 1,
        min(
            label_index + 5,
            len(items),
        ),
    ):

        item = items[index]

        text = _text(item)

        if not _is_name_candidate(text):
            continue

        candidate_top = _bbox_top(item)

        distance = 0.0

        if (
            label_bottom is not None
            and candidate_top is not None
        ):
            distance = max(
                0.0,
                candidate_top - label_bottom,
            )

        # Very important:
        # The PAN /Name label is immediately above the holder name.
        #
        # Reward proximity heavily.
        score = (
            _confidence(item) * 70.0
            - min(distance, 500.0) * 0.05
        )

        # Strong bonus for immediate next token.
        if index == label_index + 1:
            score += 35.0

        candidates.append(
            (
                score,
                text,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda value: value[0],
        reverse=True,
    )

    return candidates[0][1]


# ============================================================================
# LAYOUT-BASED EXTRACTION
# ============================================================================

def _extract_layout_name(
    items: list[Any],
) -> str | None:

    pan_index = _find_pan_index(items)

    if pan_index is None:
        return None

    pan_item = items[pan_index]

    pan_top = _bbox_top(pan_item)

    candidates: list[
        tuple[float, str]
    ] = []

    for index, item in enumerate(items):

        text = _text(item)

        if not _is_name_candidate(text):
            continue

        if _is_pan_number(text):
            continue

        candidate_top = _bbox_top(item)

        score = (
            _confidence(item) * 35.0
        )

        # ---------------------------------------------------------------
        # Name should be ABOVE PAN number.
        # ---------------------------------------------------------------

        if (
            pan_top is not None
            and candidate_top is not None
        ):

            vertical_distance = (
                pan_top - candidate_top
            )

            if (
                30 <= vertical_distance <= 180
            ):
                score += 40.0

            elif (
                0 < vertical_distance < 300
            ):
                score += 20.0

            elif vertical_distance < 0:
                score -= 40.0

        # ---------------------------------------------------------------
        # Earlier text is preferred.
        # ---------------------------------------------------------------

        if index <= pan_index:
            score += 10.0

        # ---------------------------------------------------------------
        # Name immediately before PAN number is strong.
        # ---------------------------------------------------------------

        if index == pan_index - 1:
            score += 25.0

        candidates.append(
            (
                score,
                text,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda value: value[0],
        reverse=True,
    )

    return candidates[0][1]


# ============================================================================
# FALLBACK
# ============================================================================

def _extract_fallback_name(
    items: list[Any],
) -> str | None:

    candidates: list[
        tuple[float, str]
    ] = []

    for index, item in enumerate(items):

        text = _text(item)

        if not _is_name_candidate(text):
            continue

        score = (
            _confidence(item) * 50.0
        )

        if index <= 7:
            score += 10.0

        candidates.append(
            (
                score,
                text,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda value: value[0],
        reverse=True,
    )

    return candidates[0][1]


# ============================================================================
# MAIN PAN NAME EXTRACTION
# ============================================================================

def extract_pan_name(
    ocr_items: list[Any],
) -> str | None:
    """
    Extract PAN holder name.

    Priority:

        1. /Name label
        2. PAN layout
        3. Generic fallback
    """

    if not ocr_items:
        return None

    # ========================================================================
    # 1. LABEL
    # ========================================================================

    for index, item in enumerate(ocr_items):

        if _is_name_label(
            _text(item)
        ):

            name = _extract_after_label(
                ocr_items,
                index,
            )

            if name:
                return name

    # ========================================================================
    # 2. LAYOUT
    # ========================================================================

    name = _extract_layout_name(
        ocr_items
    )

    if name:
        return name

    # ========================================================================
    # 3. FALLBACK
    # ========================================================================

    return _extract_fallback_name(
        ocr_items
    )


# ============================================================================
# NAME RESULT
# ============================================================================

class PANNameResult:
    """
    Small result object used by the document extraction agent.
    """

    def __init__(
        self,
        value: str,
        confidence: float,
        source: str = "pan_layout",
    ) -> None:

        self.value = value
        self.confidence = float(
            confidence
        )
        self.score = float(
            confidence
        )
        self.source = source

    def __repr__(self) -> str:

        return (
            "PANNameResult("
            f"value={self.value!r}, "
            f"confidence={self.confidence:.4f}, "
            f"source={self.source!r}"
            ")"
        )


# ============================================================================
# PUBLIC COMPATIBILITY FUNCTION
# ============================================================================

def extract_name(
    items: list[Any],
) -> PANNameResult | None:
    """
    Compatibility API used by DocumentExtractionAgent.
    """

    if not items:
        return None

    name = extract_pan_name(
        items
    )

    if not name:
        return None

    normalized_name = _normalize(
        name
    )

    selected_confidence = 0.0

    for item in items:

        if (
            _normalize(
                _text(item)
            )
            == normalized_name
        ):

            selected_confidence = _confidence(
                item
            )

            break

    return PANNameResult(
        value=name,
        confidence=selected_confidence,
        source="name_label",
    )


# ============================================================================
# PAN FIELD EXTRACTION
# ============================================================================

def extract_pan_fields(
    ocr_items: list[Any],
) -> dict[str, Any]:

    result = extract_name(
        ocr_items
    )

    if result is None:

        return {
            "name": None,
            "confidence": 0.0,
        }

    return {
        "name": result.value,
        "confidence": round(
            result.confidence,
            4,
        ),
    }


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "PANNameResult",
    "extract_name",
    "extract_pan_name",
    "extract_pan_fields",
]