from __future__ import annotations

"""
PAN-specific primary-name extractor.

Design goals
------------
* Reuse the ONE Surya OCR result already produced upstream.
* Never run OCR again.
* Prefer explicit PAN name evidence over generic heuristics.
* Use OCR bounding boxes and field order.
* Reject PAN headers, DOB, PAN number, father/mother/spouse fields,
  addresses, repeated OCR garbage and institutional text.
* Support common PAN layouts:
    1. "नाम / Name AMIT AKHILESH PANDEY"
    2. "Name" followed by the name
    3. "Name: AMIT AKHILESH PANDEY"
    4. Name split across multiple OCR boxes/lines
    5. Older layouts where the name is found between the PAN header and DOB.
* Conservative on ambiguous cases. A wrong name is worse than None.
"""

import re
from dataclasses import dataclass
from typing import Any, Iterable


# ============================================================================
# CONFIG
# ============================================================================

MAX_NAME_TOKENS = 6
MIN_NAME_LENGTH = 3
MAX_NAME_LENGTH = 70

LINE_Y_TOLERANCE = 14.0
MAX_NAME_LINES = 2

# In a normal PAN card the holder-name area is close to the top/middle,
# but we deliberately use relative positions instead of fixed pixel values.
HEADER_TO_NAME_MAX_LINES = 5
LABEL_TO_VALUE_MAX_Y_GAP = 180.0

# A candidate must beat competing candidates by this amount before we
# automatically accept it in an ambiguous layout.
AMBIGUITY_MARGIN = 8.0


# ============================================================================
# RESULT
# ============================================================================


@dataclass(slots=True)
class PANNameResult:
    value: str
    confidence: float
    score: float
    source: str


@dataclass(slots=True)
class _Item:
    index: int
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]
    raw: Any


# ============================================================================
# TEXT NORMALIZATION
# ============================================================================


def _text(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get("text", "")
    else:
        value = getattr(item, "text", "")
    return str(value or "").strip()


def _confidence(item: Any) -> float:
    if isinstance(item, dict):
        value = item.get("confidence", item.get("score", 0.0))
    else:
        value = getattr(
            item,
            "confidence",
            getattr(item, "score", 0.0),
        )

    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0

    if value > 1.0:
        value /= 100.0

    return max(0.0, min(1.0, value))


def _bbox(item: Any) -> tuple[float, float, float, float]:
    if isinstance(item, dict):
        value = item.get("bbox", item.get("box"))
    else:
        value = getattr(
            item,
            "bbox",
            getattr(item, "box", None),
        )

    if value is None:
        return (0.0, 0.0, 0.0, 0.0)

    try:
        # xyxy
        if (
            isinstance(value, (list, tuple))
            and len(value) == 4
            and all(
                isinstance(v, (int, float))
                for v in value
            )
        ):
            return tuple(float(v) for v in value)  # type: ignore[return-value]

        # polygon
        points = []
        for point in value:
            if hasattr(point, "tolist"):
                point = point.tolist()
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                points.append((float(point[0]), float(point[1])))

        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return (
                min(xs),
                min(ys),
                max(xs),
                max(ys),
            )
    except (TypeError, ValueError, IndexError):
        pass

    return (0.0, 0.0, 0.0, 0.0)


def _clean(value: str) -> str:
    value = str(value or "")
    value = value.replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t:/|,;._-")


def _compact(value: str) -> str:
    return re.sub(
        r"[^A-Z0-9]",
        "",
        _clean(value).upper(),
    )


def _alpha_compact(value: str) -> str:
    return re.sub(
        r"[^A-Z]",
        "",
        _clean(value).upper(),
    )


# ============================================================================
# PAN-SPECIFIC FIELD DETECTION
# ============================================================================


HEADER_TERMS = {
    "INCOME",
    "TAX",
    "DEPARTMENT",
    "GOVT",
    "GOVERNMENT",
    "INDIA",
    "PERMANENT",
    "ACCOUNT",
    "NUMBER",
    "CARD",
    "PHOTO",
    "IDENTITY",
    "IDENTIFICATION",
    "SIGNATURE",
}

RELATION_TERMS = {
    "FATHER",
    "FATHERS",
    "MOTHER",
    "MOTHERS",
    "SPOUSE",
    "HUSBAND",
    "WIFE",
    "SON",
    "DAUGHTER",
    "RELATIVE",
}

ADDRESS_TERMS = {
    "ADDRESS",
    "ROAD",
    "STREET",
    "LANE",
    "COLONY",
    "VILLAGE",
    "DISTRICT",
    "STATE",
    "CITY",
    "PIN",
    "Pincode",
}

DATE_LABEL_TERMS = {
    "DATEOFBIRTH",
    "DOB",
    "BIRTHDATE",
}

PAN_LABEL_TERMS = {
    "PERMANENTACCOUNTNUMBER",
    "ACCOUNTNUMBER",
    "PAN",
}


def _is_date(value: str) -> bool:
    value = _clean(value)

    return bool(
        re.fullmatch(
            r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}",
            value,
        )
        or re.fullmatch(
            r"\d{8}",
            value,
        )
    )


def _looks_like_pan(value: str) -> bool:
    compact = re.sub(r"\s+", "", value.upper())
    return bool(
        re.fullmatch(
            r"[A-Z]{5}[0-9]{4}[A-Z]",
            compact,
        )
    )


def _is_relation_label(value: str) -> bool:
    compact = _alpha_compact(value)

    return any(
        term in compact
        for term in (
            "FATHER",
            "FATHERSNAME",
            "FATHERNAME",
            "MOTHER",
            "MOTHERSNAME",
            "MOTHERNAME",
            "SPOUSE",
            "SPOUSENAME",
            "HUSBAND",
            "WIFE",
            "RELATIVE",
        )
    )


def _is_name_label(value: str) -> bool:
    compact = _alpha_compact(value)

    if _is_relation_label(value):
        return False

    if (
        "DATEOFBIRTH" in compact
        or compact == "DOB"
        or "SIGNATURE" in compact
        or "ADDRESS" in compact
    ):
        return False

    # Exact and common OCR variants.
    if compact in {
        "NAME",
        "TNAME",
        "INAME",
        "NNAME",
        "VNAME",
        "LNAME",
    }:
        return True

    # Elector's Name / Applicant Name / Customer Name etc.
    if "NAME" in compact:
        return True

    return False


def _is_header(value: str) -> bool:
    compact = _alpha_compact(value)

    if not compact:
        return True

    patterns = (
        "INCOMETAX",
        "TAXDEPARTMENT",
        "DEPARTMENT",
        "GOVTOFINDIA",
        "GOVERNMENTOFINDIA",
        "PERMANENTACCOUNT",
        "PERMANENTACCOUNTNUMBER",
        "ACCOUNTNUMBER",
        "SIGNATURE",
    )

    if any(pattern in compact for pattern in patterns):
        return True

    words = set(re.findall(r"[A-Z]+", value.upper()))

    # Do not classify a real name as header merely because one common word
    # appears. Require at least two strong header words.
    header_hits = len(words.intersection(HEADER_TERMS))

    return header_hits >= 2


def _is_boundary(value: str) -> bool:
    compact = _alpha_compact(value)

    if _is_relation_label(value):
        return True

    if _is_name_label(value):
        return True

    if "DATEOFBIRTH" in compact or compact == "DOB":
        return True

    if "SIGNATURE" in compact:
        return True

    if "ADDRESS" in compact:
        return True

    if "ACCOUNTNUMBER" in compact:
        return True

    return False


# ============================================================================
# NAME VALIDATION
# ============================================================================


# OCR fragments that are especially dangerous on PAN cards.
LABEL_FRAGMENTS = {
    "INCOME",
    "INCOM",
    "TAX",
    "DEPART",
    "DEPARTM",
    "DEPARTMENT",
    "GOVT",
    "GOVER",
    "INDIA",
    "PERMAN",
    "PERMANE",
    "PERMANENT",
    "ACCOUN",
    "ACCOUNT",
    "NUMBE",
    "NUMBER",
    "SIGNAT",
    "SIGNATURE",
    "DATE",
    "BIRTH",
    "FATHER",
    "FATHE",
    "MOTHER",
    "SPOUSE",
    "NAME",
}


def _token_is_name_like(token: str) -> bool:
    token = token.strip(".-'")

    if len(token) < 2:
        return False

    if not re.fullmatch(
        r"[A-Za-z][A-Za-z.'-]*",
        token,
    ):
        return False

    upper = token.upper()

    if upper in LABEL_FRAGMENTS:
        return False

    # Obvious OCR label fragments.
    if len(upper) >= 5:
        for label in (
            "PERMANENT",
            "ACCOUNT",
            "NUMBER",
            "DEPARTMENT",
            "SIGNATURE",
            "INCOME",
            "FATHER",
            "MOTHER",
        ):
            # Small edit distance protection without importing a heavy library.
            same_prefix = 0
            for a, b in zip(upper, label):
                if a != b:
                    break
                same_prefix += 1

            if (
                same_prefix >= 5
                and abs(len(upper) - len(label)) <= 2
            ):
                return False

    return True


def _valid_name(value: str) -> bool:
    value = _clean(value)

    if not value:
        return False

    if not MIN_NAME_LENGTH <= len(value) <= MAX_NAME_LENGTH:
        return False

    if _is_date(value):
        return False

    if _looks_like_pan(value):
        return False

    if _is_header(value):
        return False

    if _is_boundary(value):
        return False

    # Names extracted from this pipeline should be Latin text. Hindi/other
    # label text is allowed in the OCR stream but not as the returned name.
    if not re.fullmatch(
        r"[A-Za-z][A-Za-z .'\-]*",
        value,
    ):
        return False

    tokens = [
        token.strip(".-'")
        for token in value.split()
        if token.strip(".-'")
    ]

    if not tokens or len(tokens) > MAX_NAME_TOKENS:
        return False

    if any(
        not _token_is_name_like(token)
        for token in tokens
    ):
        return False

    letters = re.sub(
        r"[^A-Za-z]",
        "",
        value,
    )

    if len(letters) < MIN_NAME_LENGTH:
        return False

    # A one-token name is allowed, but must be reasonably long.
    if len(tokens) == 1 and len(letters) < 5:
        return False

    # Avoid sentence-like OCR becoming a name.
    if len(tokens) >= 5:
        suspicious_words = {
            "RECEIVED",
            "SPECIAL",
            "AGENT",
            "CHARGE",
            "MONDAY",
            "TUESDAY",
            "WEDNESDAY",
            "THURSDAY",
            "FRIDAY",
            "SATURDAY",
            "SUNDAY",
        }

        if any(
            token.upper() in suspicious_words
            for token in tokens
        ):
            return False

    return True


def _normalize_name(value: str) -> str | None:
    value = _clean(value)

    if not _valid_name(value):
        return None

    return " ".join(
        token.strip(".-'").title()
        for token in value.split()
    )


# ============================================================================
# GEOMETRY
# ============================================================================


def _center(item: _Item) -> tuple[float, float]:
    return (
        (item.bbox[0] + item.bbox[2]) / 2.0,
        (item.bbox[1] + item.bbox[3]) / 2.0,
    )


def _height(item: _Item) -> float:
    return max(
        1.0,
        item.bbox[3] - item.bbox[1],
    )


def _same_line(a: _Item, b: _Item) -> bool:
    _, ay = _center(a)
    _, by = _center(b)

    tolerance = max(
        LINE_Y_TOLERANCE,
        min(
            _height(a),
            _height(b),
        ) * 0.75,
    )

    return abs(ay - by) <= tolerance


def _prepare_items(items: Iterable[Any]) -> list[_Item]:
    prepared: list[_Item] = []

    for index, item in enumerate(items or []):
        text = _clean(_text(item))

        if not text:
            continue

        prepared.append(
            _Item(
                index=index,
                text=text,
                confidence=_confidence(item),
                bbox=_bbox(item),
                raw=item,
            )
        )

    prepared.sort(
        key=lambda item: (
            item.bbox[1],
            item.bbox[0],
            item.index,
        )
    )

    return prepared


# ============================================================================
# INLINE LABEL + VALUE
# ============================================================================


def _extract_inline_name(text: str) -> str | None:
    """
    Handles:
        Name: AMIT AKHILESH PANDEY
        Name AMIT AKHILESH PANDEY
        नाम / Name AMIT AKHILESH PANDEY
        Elector's Name: Nidhi Ajit Singh
    """

    raw = _clean(text)

    patterns = (
        r"(?:^|[\s/])NAME\s*[:\-]?\s*(.+)$",
        r"\bNAME\b\s+(.+)$",
        r"ELECTOR'?S\s+NAME\s*[:\-]?\s*(.+)$",
        r"(?:APPLICANT|CUSTOMER|BORROWER)\s+NAME\s*[:\-]?\s*(.+)$",
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            raw,
            flags=re.IGNORECASE,
        )

        if not match:
            continue

        candidate = _normalize_name(
            match.group(1)
        )

        if candidate:
            return candidate

    return None


# ============================================================================
# VISUAL LINES
# ============================================================================


def _group_lines(items: list[_Item]) -> list[list[_Item]]:
    if not items:
        return []

    lines: list[list[_Item]] = []

    for item in items:
        _, item_y = _center(item)
        placed = False

        for line in lines:
            avg_y = sum(
                _center(member)[1]
                for member in line
            ) / len(line)

            tolerance = max(
                LINE_Y_TOLERANCE,
                _height(item) * 0.75,
            )

            if abs(item_y - avg_y) <= tolerance:
                line.append(item)
                placed = True
                break

        if not placed:
            lines.append([item])

    for line in lines:
        line.sort(
            key=lambda item: (
                item.bbox[0],
                item.index,
            )
        )

    lines.sort(
        key=lambda line: (
            min(item.bbox[1] for item in line),
            min(item.bbox[0] for item in line),
        )
    )

    return lines


# ============================================================================
# CANDIDATE SCORING
# ============================================================================


def _candidate_score(
    name: str,
    confidence: float,
    source: str,
    *,
    vertical_position: float = 0.5,
    token_count: int | None = None,
    repeated_penalty: float = 0.0,
) -> float:
    tokens = token_count or len(name.split())

    score = confidence * 45.0

    if source == "inline":
        score += 55.0
    elif source == "label_same_line":
        score += 45.0
    elif source == "label_below":
        score += 38.0
    elif source == "header_region":
        score += 24.0
    elif source == "visual_sequence":
        score += 15.0
    else:
        score += 0.0

    # 2-4 token person names are the strongest generic shape.
    if 2 <= tokens <= 4:
        score += 15.0
    elif tokens == 1:
        score += 4.0
    elif tokens >= 5:
        score -= 12.0

    # Penalize candidates containing repeated words.
    words = [word.upper() for word in name.split()]
    duplicates = len(words) - len(set(words))
    score -= duplicates * 14.0
    score -= repeated_penalty

    # Prefer candidates nearer to the expected holder-name region.
    score += max(
        0.0,
        min(10.0, vertical_position * 10.0),
    )

    return score


def _result(
    name: str,
    confidence: float,
    score: float,
    source: str,
) -> PANNameResult:
    # Field confidence is deliberately not identical to raw OCR confidence.
    # Position/source evidence contributes to the score, then gets bounded.
    evidence = max(
        0.0,
        min(
            1.0,
            0.45
            + confidence * 0.40
            + min(score, 100.0) / 100.0 * 0.15,
        ),
    )

    return PANNameResult(
        value=name,
        confidence=round(evidence, 4),
        score=round(score, 2),
        source=source,
    )


# ============================================================================
# EXPLICIT LABEL EXTRACTION
# ============================================================================


def _explicit_candidates(
    items: list[_Item],
) -> list[PANNameResult]:
    candidates: list[PANNameResult] = []

    for label in items:
        if not _is_name_label(label.text):
            continue

        # Inline is the strongest evidence.
        inline = _extract_inline_name(label.text)

        if inline:
            candidates.append(
                _result(
                    inline,
                    label.confidence,
                    _candidate_score(
                        inline,
                        label.confidence,
                        "inline",
                    ),
                    "pan_inline_name_label",
                )
            )
            continue

        _, label_y = _center(label)

        # Same-line value to the right.
        same_line_items = [
            item
            for item in items
            if item.index != label.index
            and not _is_boundary(item.text)
            and not _is_header(item.text)
            and _valid_name(item.text)
            and _same_line(label, item)
            and item.bbox[0] >= label.bbox[0]
        ]

        if same_line_items:
            same_line_items.sort(
                key=lambda item: (
                    item.bbox[0],
                    item.index,
                )
            )

            value = _normalize_name(
                " ".join(
                    item.text
                    for item in same_line_items[:MAX_NAME_TOKENS]
                )
            )

            if value:
                confidence = sum(
                    item.confidence
                    for item in same_line_items
                ) / len(same_line_items)

                candidates.append(
                    _result(
                        value,
                        confidence,
                        _candidate_score(
                            value,
                            confidence,
                            "label_same_line",
                        ),
                        "pan_label_same_line",
                    )
                )

        # Value below the label.
        below: list[_Item] = []

        for item in items:
            if item.index == label.index:
                continue

            if _is_boundary(item.text) or _is_header(item.text):
                continue

            if not _valid_name(item.text):
                continue

            _, item_y = _center(item)
            vertical_gap = item_y - label_y

            if (
                vertical_gap >= -10.0
                and vertical_gap <= LABEL_TO_VALUE_MAX_Y_GAP
            ):
                below.append(item)

        below.sort(
            key=lambda item: (
                item.bbox[1],
                item.bbox[0],
                item.index,
            )
        )

        if below:
            first = below[0]

            first_line = [
                item
                for item in below
                if _same_line(first, item)
            ]

            first_line.sort(
                key=lambda item: (
                    item.bbox[0],
                    item.index,
                )
            )

            value = _normalize_name(
                " ".join(
                    item.text
                    for item in first_line[:MAX_NAME_TOKENS]
                )
            )

            if value:
                confidence = sum(
                    item.confidence
                    for item in first_line
                ) / len(first_line)

                candidates.append(
                    _result(
                        value,
                        confidence,
                        _candidate_score(
                            value,
                            confidence,
                            "label_below",
                        ),
                        "pan_label_below",
                    )
                )

    return candidates


# ============================================================================
# PAN HOLDER REGION
# ============================================================================


def _header_region_candidates(
    items: list[_Item],
) -> list[PANNameResult]:
    """
    PAN layouts commonly place the holder name after the bilingual government
    header and before DOB/PAN-number information.

    This is only a fallback. Explicit Name labels always outrank this region.
    """

    if not items:
        return []

    # Find the latest strong header occurrence.
    header_indices = [
        index
        for index, item in enumerate(items)
        if _is_header(item.text)
    ]

    header_index = max(
        header_indices
    ) if header_indices else -1

    # Stop before the first DOB/PAN-number field after the header.
    stop_index = len(items)

    for index in range(
        max(0, header_index + 1),
        len(items),
    ):
        text = items[index].text

        if (
            _is_date(text)
            or _looks_like_pan(text)
            or _is_relation_label(text)
        ):
            stop_index = index
            break

    region_start = max(
        0,
        header_index + 1,
    )

    # Avoid scanning the entire document if the header is absent.
    if header_index < 0:
        region_start = max(
            0,
            len(items) - 8,
        )

    region = items[
        region_start:stop_index
    ]

    candidates: list[PANNameResult] = []

    for item in region:
        text = item.text

        if (
            _is_header(text)
            or _is_boundary(text)
            or _is_date(text)
            or _looks_like_pan(text)
        ):
            continue

        value = _normalize_name(text)

        if not value:
            continue

        # Position bonus: candidates close to the top of the holder region
        # are preferred, but not hard-coded to a pixel location.
        relative = 1.0

        if region:
            first_y = region[0].bbox[1]
            last_y = region[-1].bbox[3]
            span = max(1.0, last_y - first_y)
            relative = 1.0 - (
                (item.bbox[1] - first_y) / span
            )
            relative = max(0.0, min(1.0, relative))

        candidates.append(
            _result(
                value,
                item.confidence,
                _candidate_score(
                    value,
                    item.confidence,
                    "header_region",
                    vertical_position=relative,
                ),
                "pan_header_region",
            )
        )

    # Reconstruct adjacent visual tokens/boxes on the same row.
    lines = _group_lines(region)

    for line in lines:
        valid = [
            item
            for item in line
            if (
                not _is_header(item.text)
                and not _is_boundary(item.text)
                and not _is_date(item.text)
                and not _looks_like_pan(item.text)
                and _valid_name(item.text)
            )
        ]

        if len(valid) < 2:
            continue

        value = _normalize_name(
            " ".join(
                item.text
                for item in valid[:MAX_NAME_TOKENS]
            )
        )

        if not value:
            continue

        confidence = sum(
            item.confidence
            for item in valid
        ) / len(valid)

        # Strong penalty for repeated OCR tokens such as:
        # MEMPHIS MEMPHIS
        words = [
            word.upper()
            for word in value.split()
        ]
        repeated_penalty = max(
            0,
            len(words) - len(set(words)),
        ) * 18.0

        candidates.append(
            _result(
                value,
                confidence,
                _candidate_score(
                    value,
                    confidence,
                    "visual_sequence",
                    repeated_penalty=repeated_penalty,
                ),
                "pan_visual_sequence",
            )
        )

    return candidates


# ============================================================================
# GENERIC FALLBACK — VERY CONSERVATIVE
# ============================================================================


def _safe_fallback_candidates(
    items: list[_Item],
) -> list[PANNameResult]:
    """
    Last resort only.

    This deliberately does NOT accept arbitrary English OCR as a name.
    It only accepts clean 2-4 token candidates with good OCR confidence.
    """

    results: list[PANNameResult] = []

    for item in items:
        value = _normalize_name(item.text)

        if not value:
            continue

        tokens = value.split()

        if not 2 <= len(tokens) <= 4:
            continue

        if item.confidence < 0.72:
            continue

        # Repeated-token penalty.
        words = [word.upper() for word in tokens]
        repeated_penalty = (
            len(words) - len(set(words))
        ) * 25.0

        results.append(
            _result(
                value,
                item.confidence,
                _candidate_score(
                    value,
                    item.confidence,
                    "fallback",
                    repeated_penalty=repeated_penalty,
                ),
                "pan_conservative_fallback",
            )
        )

    return results


# ============================================================================
# DEDUPLICATION / SELECTION
# ============================================================================


def _deduplicate(
    candidates: list[PANNameResult],
) -> list[PANNameResult]:
    unique: dict[str, PANNameResult] = {}

    for candidate in candidates:
        key = re.sub(
            r"\s+",
            " ",
            candidate.value.upper(),
        ).strip()

        previous = unique.get(key)

        if (
            previous is None
            or candidate.score > previous.score
        ):
            unique[key] = candidate

    return list(unique.values())


def _select(
    candidates: list[PANNameResult],
) -> PANNameResult | None:
    if not candidates:
        return None

    candidates = _deduplicate(candidates)

    candidates.sort(
        key=lambda candidate: (
            candidate.score,
            candidate.confidence,
            len(candidate.value.split()),
        ),
        reverse=True,
    )

    best = candidates[0]

    if len(candidates) == 1:
        return best

    second = candidates[1]

    # If two candidates are essentially equally strong and came from weak
    # evidence, return None instead of hallucinating a winner.
    if (
        best.score - second.score < AMBIGUITY_MARGIN
        and best.source not in {
            "pan_inline_name_label",
            "pan_label_same_line",
        }
        and second.source not in {
            "pan_inline_name_label",
            "pan_label_same_line",
        }
    ):
        return None

    return best


# ============================================================================
# DEBUG
# ============================================================================


def _print_debug(
    candidates: list[PANNameResult],
    selected: PANNameResult | None,
) -> None:
    print(
        "\n========== PAN NAME EXTRACTION =========="
    )

    if selected is None:
        print("STATUS: NAME NOT_FOUND")
    else:
        print(
            "SELECTED:",
            repr(selected.value),
        )
        print(
            "SOURCE:",
            selected.source,
        )
        print(
            "SCORE:",
            round(selected.score, 2),
        )
        print(
            "FIELD CONFIDENCE:",
            round(selected.confidence, 4),
        )

    if candidates:
        print("CANDIDATES:")

        ranked = sorted(
            candidates,
            key=lambda item: (
                item.score,
                item.confidence,
            ),
            reverse=True,
        )

        for index, candidate in enumerate(ranked[:10]):
            print(
                f"  [{index}] "
                f"{candidate.value!r} "
                f"SCORE={candidate.score:.2f} "
                f"CONF={candidate.confidence:.4f} "
                f"SOURCE={candidate.source}"
            )

    print(
        "=========================================="
    )


# ============================================================================
# PUBLIC API
# ============================================================================


def extract_pan_name(
    items: Iterable[Any],
    *,
    debug: bool = True,
) -> PANNameResult | None:
    """
    Extract the PAN holder's primary name from already-produced OCR items.

    IMPORTANT:
        This function performs ZERO OCR inference.

    It consumes:
        OCRTextItem
        dicts with text/confidence/bbox
        other objects exposing text/confidence/bbox

    Priority:
        1. Inline Name label
        2. Explicit Name label + same-line value
        3. Explicit Name label + below value
        4. PAN holder-name region
        5. Conservative fallback

    Returns None when evidence is ambiguous.
    """

    prepared = _prepare_items(items)

    if not prepared:
        if debug:
            _print_debug([], None)
        return None

    explicit = _explicit_candidates(
        prepared
    )

    # Explicit PAN label is authoritative within the OCR evidence.
    selected = _select(explicit)

    if selected is not None:
        if debug:
            _print_debug(explicit, selected)
        return selected

    region = _header_region_candidates(
        prepared
    )

    selected = _select(
        explicit + region
    )

    if selected is not None:
        if debug:
            _print_debug(
                explicit + region,
                selected,
            )
        return selected

    fallback = _safe_fallback_candidates(
        prepared
    )

    selected = _select(
        explicit + region + fallback
    )

    if debug:
        _print_debug(
            explicit + region + fallback,
            selected,
        )

    return selected


# ============================================================================
# COMPATIBILITY ALIAS
# ============================================================================


def extract_name(
    items: Iterable[Any],
) -> PANNameResult | None:
    """Compatibility alias for PAN-specific callers."""
    return extract_pan_name(
        items,
        debug=True,
    )


__all__ = [
    "PANNameResult",
    "extract_pan_name",
    "extract_name",
]
