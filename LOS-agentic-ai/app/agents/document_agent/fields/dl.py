"""
Driving Licence field extraction.

State layouts vary, so everything is label-anchored or pattern-anchored:
  - DL number    : state-code pattern, or the token after a "DL No" label
  - Dates        : role assigned by the nearest label (DOI / Valid Till / DOB)
  - Name / S-D-W : label-driven, tolerant of merged label+value tokens
  - Address      : the "Add" label plus continuation lines until PIN
  - Vehicle class: known COV codes appearing after the authorisation header

Fields genuinely absent from a licence are reported MISSING, never invented.
"""

from __future__ import annotations

import re
from datetime import date

from app.agents.document_agent.fields.common import (
    DATE_RE, FOREIGN_NAME_LABELS, compact, find_label, is_gallery_timestamp,
    is_label_token, looks_like_name, region_around, strip_inline_label,
    value_after_label,
)
from app.agents.document_agent.normalize import (
    normalize_address, normalize_date, normalize_dl_number,
    normalize_legacy_dl_number, normalize_name, normalize_pin,
)
from app.agents.document_agent.schemas import OCRToken
from app.agents.document_agent.validate import (
    INDIAN_STATE_CODES, VEHICLE_CLASSES, dob_precedes_issue, plausible_dob,
)

_DL_PATTERN = re.compile(r"([A-Z]{2}[0-9]{2}[0-9A-Z]{9,13})")

# Older state series: serial / office code / year of issue.
_LEGACY_DL_PATTERN = re.compile(r"\b\d{1,6}\s*/\s*[A-Z]{1,4}\s*/\s*\d{4}\b")
_DL_LABELS = ["DLNO", "DLNO:", "LICENCENO", "LICENSENO"]
_NAME_LABELS = ["NAME"]
# "S/W/D" and "S/D/W" are the same caption with the letters in a different
# order, and issuers disagree about which to use. DigiLocker prints S/W/D
# while printed cards print S/D/W; holding only one order dropped the
# relation on every DigiLocker licence, because compacting "S/W/D" gives SWD
# and the list held only SDW.
_GUARDIAN_LABELS = [
    "SDWOF", "SDW", "SWDOF", "SWD", "DSWOF", "WDSOF",
    "SONOF", "DAUGHTEROF", "WIFEOF", "FATHER",
]
# "DOR" is not a caption anyone prints: it is how OCR reads "DOB:" when the
# colon fuses into the B, which happened on 3 of 5 dense back-of-card
# layouts. Listed as a character-confusion alias, the same way the
# "HUSBANO'SNAME" D-for-O misread is handled elsewhere.
#
# Safe to add precisely because it collides with nothing: the dangerous
# confusion here would be DOB against DOI, which are one character apart and
# would swap date of birth with date of issue. That is why these labels are
# never matched fuzzily -- only exact aliases are listed.
_DOB_LABELS = ["DOB", "DATEOFBIRTH", "DOR"]
# "Issue Date" (no "of", unabbreviated) is how a real Telangana DL prints
# this caption. The narrower list matched neither "DOI" nor "DATEOFISSUE"
# against it, so the field went unfound despite the value sitting right
# there in the OCR output.
_DOI_LABELS = ["DOI", "DATEOFISSUE", "ISSUEDATE"]
# "CDOI" (the card's own date of issue, reprinted when a card is replaced)
# CONTAINS "DOI", so it matched as the issue-date caption and supplied its own
# date. On a real Karnataka licence that returned the card-issue date instead
# of the licence issue date printed a few lines above it.
_DOI_EXCLUDE = ["CDOI"]
# Real cards split this into "Validity (NT)" / "Validity (TR)" -- non-
# transport and transport/commercial classes each with their own expiry --
# rather than a single "Valid Till" caption. Matching bare "VALIDITY" picks
# up whichever comes first, which will not always be the intended one when
# a card prints both; a value here should be read as "a" validity date on
# the card, not confirmed as the specific class expected.
_VALID_LABELS = ["VALIDTILL", "VALIDUPTO", "VALIDTO", "EXPIRY", "VALIDITY"]
_ADDRESS_LABELS = ["ADD", "ADDRESS"]


def _extract_dl_number(tokens: list[OCRToken]) -> tuple[str | None, OCRToken | None]:
    label = find_label(tokens, _DL_LABELS)
    if label is not None:
        match = _DL_PATTERN.search(compact(label.text))
        if match:
            normalized = normalize_dl_number(match.group(1))
            if normalized and normalized[:2] in INDIAN_STATE_CODES:
                return normalized, label
        value = value_after_label(
            tokens, label, lambda t: bool(_DL_PATTERN.search(compact(t.text)))
        )
        if value is not None:
            match = _DL_PATTERN.search(compact(value.text))
            if match:
                return normalize_dl_number(match.group(1)), value

    for token in tokens:
        match = _DL_PATTERN.search(compact(token.text))
        if match:
            normalized = normalize_dl_number(match.group(1))
            if normalized and normalized[:2] in INDIAN_STATE_CODES:
                return normalized, token

    # Several states still issue the older serial/office/year form, which the
    # modern pattern cannot match at all: a real Telangana card prints
    # "39712/NLG/1997". Tried last so a modern number always wins on a card
    # carrying both.
    for token in tokens:
        for candidate in _LEGACY_DL_PATTERN.findall(token.text.upper()):
            legacy = normalize_legacy_dl_number(candidate)
            if legacy:
                return legacy, token
    return None, None


def _dated(tokens: list[OCRToken]) -> list[tuple[str, OCRToken]]:
    out = []
    for token in tokens:
        for raw in DATE_RE.findall(token.text):
            iso = normalize_date(raw)
            if iso:
                out.append((iso, token))
    return out


def _date_after_label_in_token(
    token: OCRToken, labels: list[str]
) -> str | None:
    """
    The first date printed AFTER one of `labels` inside a single token.

    Walks the token's text compacting as it goes, so the caption is located
    on the same normalised form `find_label` matched on -- punctuation and
    spacing differ between issuers and OCR passes, and matching the raw text
    would find the caption on some cards and not others.

    Returns None when the caption is present but no date follows it, so the
    caller can fall back rather than treating absence as a failure.
    """
    text = token.text or ""

    best_end = None
    for index in range(len(text)):
        key = compact(text[: index + 1])
        if any(key.endswith(pattern) for pattern in labels):
            best_end = index + 1
            break

    if best_end is None:
        return None

    match = DATE_RE.search(text[best_end:])
    if not match:
        return None

    return normalize_date(match.group(0))


def _labelled_date(
    tokens: list[OCRToken], labels: list[str],
    exclude: OCRToken | None = None,
    exclude_labels: list[str] | None = None,
) -> tuple[str | None, OCRToken | None]:
    """
    Find a date near one of `labels`.

    `exclude` lets a caller retry after a collision: on a crowded header
    row, two adjacent date captions ("Issue Date", "Validity (TR)") can both
    have their nearest-by-y search land on the same value when it sits
    slightly closer in y than the token's own genuinely correct value further
    along the row. Retrying with that token excluded recovers the real,
    distinct value instead of leaving the field empty.
    """
    label = find_label(tokens, labels, exclude=exclude_labels)
    if label is None:
        return None, None

    # A whole row can arrive as ONE token carrying two captions and two
    # dates: "DOI:25/09/2023 VALID TILL:25/09/2043". Taking the token's first
    # date gives the issue date for both fields, so the expiry is either
    # wrong or -- once collision avoidance rejects the duplicate -- missing.
    # Reading the date that follows the MATCHED caption inside the token
    # resolves each field to its own value.
    scoped = _date_after_label_in_token(label, labels)
    if scoped and (exclude is None or label is not exclude):
        return scoped, label

    inline = _dated([label])
    if inline and (exclude is None or inline[0][1] is not exclude):
        return inline[0]
    value = value_after_label(
        tokens, label,
        lambda t: (
            bool(DATE_RE.search(t.text))
            and not is_gallery_timestamp(t.text)
            and (exclude is None or t is not exclude)
        ),
    )
    if value is not None:
        found = _dated([value])
        if found:
            return found[0]
    return None, None


def _labelled_name(
    tokens: list[OCRToken],
    labels: list[str],
    words: list[str],
    exclude: list[str] | None = None,
) -> tuple[str | None, OCRToken | None]:
    label = find_label(tokens, labels, exclude=exclude)
    if label is None:
        return None, None
    inline = strip_inline_label(label.text, words)
    inline = re.sub(r"^[:\s·.\-]+", "", inline)
    normalized = normalize_name(inline)
    if normalized and len(normalized) >= 4 and not is_label_token(inline):
        return normalized, label

    value = value_after_label(tokens, label, looks_like_name)
    if value is None:
        return None, None
    return normalize_name(value.text), value


# Content that is a known OTHER field, never address text. On a crowded
# card every value -- DOB, valid-till, name, DL number, vehicle class,
# blood group, date of issue -- sits in the SAME x-aligned column below the
# address label, interleaved with the genuine address lines rather than
# separated from them. A real card had its address swallow all of these:
# "ARCEK VALIDTILL: 06/01/1989 21/04/2035(NT) NARAYANAPPA KA0520150009483
# ... COV:MCWG ... DOI:22/04/2015". Filtering known-other-field lines out
# (rather than stopping at the first one, which would cut off genuine
# address lines that come AFTER them in this interleaved layout) keeps the
# real address text while dropping what plainly is not address content.
_NOT_ADDRESS_MARKERS = ("COV", "VALID", "B.G", "BG:", "DOI", "SIGN", "ISSUING")


def _looks_like_non_address(text: str) -> bool:
    if DATE_RE.search(text):
        return True
    key = compact(text)
    if key.startswith("PIN"):
        return True
    if any(marker.replace(".", "").replace(":", "") in key for marker in _NOT_ADDRESS_MARKERS):
        return True
    # A DL-number-shaped token: 2 letters, several digits, no address
    # punctuation (a genuine address line nearly always carries a comma).
    if re.fullmatch(r"[A-Z]{2}\d{10,17}", key) and "," not in text:
        return True
    # A malformed/partially-separated date ("22/042015", OCR having dropped
    # one of the separators) does not match DATE_RE but is still a run of
    # 6+ digits with no letters -- address text does not look like that.
    digits_only = re.sub(r"[^0-9]", "", text)
    if len(digits_only) >= 6 and not re.search(r"[A-Za-z]", text):
        return True
    return False


def _extract_address(
    tokens: list[OCRToken], exclude: set[int] | None = None,
) -> tuple[str | None, OCRToken | None]:
    label = find_label(tokens, _ADDRESS_LABELS)
    if label is None:
        return None, None

    exclude = exclude or set()
    parts = [strip_inline_label(label.text, ["add", "address"]).lstrip(": ")]
    following = sorted(
        [
            t for t in tokens
            if t.cy > label.cy
            and abs(t.x0 - label.x0) < label.height * 4
            and id(t) not in exclude
        ],
        key=lambda t: t.cy,
    )
    for token in following:
        if (token.cy - label.cy) > label.height * 12:
            break
        if _looks_like_non_address(token.text):
            continue
        parts.append(token.text)

    combined = " ".join(p for p in parts if p)
    return normalize_address(combined), label


def _extract_pin(tokens: list[OCRToken]) -> tuple[str | None, OCRToken | None]:
    for token in tokens:
        key = compact(token.text)
        if key.startswith("PIN"):
            pin = normalize_pin(key.replace("PIN", "", 1))
            if pin:
                return pin, token
    return None, None


def _extract_vehicle_classes(tokens: list[OCRToken]) -> tuple[list[str] | None, OCRToken | None]:
    found: list[str] = []
    evidence: OCRToken | None = None
    for token in tokens:
        key = compact(token.text)
        for cov in VEHICLE_CLASSES:
            code = compact(cov)
            if code and key == code and cov.upper() not in found:
                found.append(cov.upper())
                evidence = evidence or token
    return (found or None), evidence


def extract_dl_fields(tokens: list[OCRToken]) -> dict[str, tuple]:
    """Returns {field: (value, evidence_token)}."""
    out: dict[str, tuple] = {}

    dl_number, dl_token = _extract_dl_number(tokens)
    out["dl_number"] = (dl_number, dl_token)

    # NOTE: anchoring the search to a region around the licence number was
    # tried and measured WORSE (47.5% -> 45.0%) on the multi-state set: the
    # number appears on both the front and the back of a card, so the region
    # frequently locked onto the wrong one. Reverted deliberately.
    scope = tokens

    out["name"] = _labelled_name(
        scope, _NAME_LABELS, ["name"],
        exclude=_GUARDIAN_LABELS + FOREIGN_NAME_LABELS,
    )
    out["guardian_name"] = _labelled_name(
        scope, _GUARDIAN_LABELS,
        [
            # Abbreviated forms.
            "s/d/w of", "s/d/w", "s/o", "d/o", "w/o", "son of", "father",
            # A real Telangana DL spells this out in full rather than
            # abbreviating it: "Son/Daughter/Wife of:BUCHAIAH" in one OCR
            # token. "son of" alone is not a substring of the compacted
            # text because "daughter" interrupts it, so the inline value
            # was never found and a spatial fallback grabbed an address
            # fragment ("SARVEL") instead of the real guardian ("BUCHAIAH").
            "son/daughter/wife of", "sondaughterwifeof",
        ],
        exclude=FOREIGN_NAME_LABELS,
    )
    out["date_of_birth"] = _labelled_date(scope, _DOB_LABELS)

    # A reading that cannot be a birth date is evidence the search landed on
    # the wrong nearby value, exactly like the future-issue-date case below.
    # One real licence read its year as 1582. Retry without that token, and
    # leave the field missing rather than confidently wrong.
    dob_value, dob_tok = out["date_of_birth"]
    if dob_value and not plausible_dob(dob_value):
        retry = _labelled_date(scope, _DOB_LABELS, exclude=dob_tok)
        out["date_of_birth"] = retry if plausible_dob(retry[0]) else (None, None)

    out["date_of_issue"] = _labelled_date(
        scope, _DOI_LABELS, exclude_labels=_DOI_EXCLUDE
    )

    # A licence cannot be issued in the future -- unlike the earlier
    # collision-retry (which guessed between two EQUALLY plausible dates and
    # produced a swap), this is an independent, unambiguous signal that the
    # search landed on the wrong nearby value. On a crowded header row the
    # "Issue Date" label sat directly above two glued dates; the search
    # picked the wrong one, returning 2028 as an issue date while today is
    # 2026. Retrying excludes that token and accepts a plausible result;
    # otherwise the field is left missing rather than confidently wrong.
    doi_value, doi_tok = out["date_of_issue"]
    if doi_value:
        try:
            implausible = date.fromisoformat(doi_value) > date.today()
        except ValueError:
            implausible = False
        if implausible:
            retry = _labelled_date(
                scope, _DOI_LABELS, exclude=doi_tok,
                exclude_labels=_DOI_EXCLUDE,
            )
            retry_value = retry[0]
            if retry_value and date.fromisoformat(retry_value) <= date.today():
                out["date_of_issue"] = retry
            else:
                out["date_of_issue"] = (None, None)
    out["valid_till"] = _labelled_date(scope, _VALID_LABELS)

    # A holder cannot be born after the licence was issued. When the two
    # contradict each other one of them came from the wrong token, and the
    # birth date is the one without an independent plausibility check of its
    # own, so it is dropped. Missing beats wrong.
    if not dob_precedes_issue(out["date_of_birth"][0], out["date_of_issue"][0]):
        out["date_of_birth"] = (None, None)

    out["address"] = _extract_address(scope)
    out["pin_code"] = _extract_pin(scope)
    out["vehicle_classes"] = _extract_vehicle_classes(scope)

    # Fallback for layouts with no "Name" caption at all. A real Bihar DL
    # prints the holder's name directly beneath the DL number/date row with
    # no label preceding it, so the labelled search above finds nothing --
    # the same situation PAN already handles for its own unlabelled layouts.
    # Anchored on the DL number using the identical invariant: the name sits
    # below the identifier and above the guardian/address block.
    if out["name"][0] is None:
        dl_tok = out["dl_number"][1]
        used = {id(t) for t in (out["guardian_name"][1], out["address"][1]) if t}
        candidates = [
            t for t in scope
            if looks_like_name(t)
            and t.confidence >= 0.80
            and id(t) not in used
            and not DATE_RE.search(t.text)
        ]
        if dl_tok is not None:
            below = [t for t in candidates if t.cy > dl_tok.cy]
            if below:
                candidates = below
        # Only trust the address as an upper bound when it is geometrically
        # sane (below the DL number, as address always is). On one real card
        # address extraction itself latched onto a garbled "Validity" token
        # sitting ABOVE the name -- using that as a boundary excluded the
        # real name entirely rather than bounding the search usefully.
        # A low-confidence address extraction is not trustworthy enough to use
        # as a search boundary either: on the same card, "address" itself
        # was a 0.64-confidence misread of a validity date, sitting just
        # below the DL number -- close enough to pass a bare y-position
        # check, but still well above the real name.
        addr_tok = out["address"][1]
        if (
            addr_tok is not None
            and addr_tok.confidence >= 0.80
            and (dl_tok is None or addr_tok.cy > dl_tok.cy)
        ):
            candidates = [t for t in candidates if t.cy < addr_tok.cy]
        candidates.sort(key=lambda t: t.cy)
        if candidates:
            out["name"] = (normalize_name(candidates[0].text), candidates[0])

    # The name label can collide with the guardian label on some layouts.
    if (
        out["name"][1] is not None
        and out["guardian_name"][1] is not None
        and out["name"][1] is out["guardian_name"][1]
    ):
        out["guardian_name"] = (None, None)

    # Date of birth, date of issue and valid-till must each be distinct
    # dates. On a crowded label row (every caption on one line, values in a
    # single stacked column beneath) three separate label searches can land
    # on the SAME value when a genuine one was never read by OCR -- a real
    # card returned its date of birth as the valid-till date, which then
    # read as expired. Presenting one date under two official fields is a
    # stronger wrong signal than leaving the collided field unresolved, so
    # whichever field's label sits closer to the shared token wins and the
    # other is cleared.
    # Date of birth, date of issue and valid-till must each be distinct
    # dates. On a crowded label row two adjacent captions can both search
    # onto the same value -- retrying the second field excluding that token
    # was tried and made things WORSE on a real card: date_of_issue kept an
    # already-wrong pick (nearest by y, not by column) and valid_till's
    # retry then landed on the genuinely-correct OTHER date, producing two
    # confidently wrong, swapped values instead of one missing field. A
    # wrong date is worse than an absent one, so collisions are cleared
    # rather than resolved by guessing which field is more likely right.
    date_fields = ("date_of_birth", "date_of_issue", "valid_till")
    for i, field_a in enumerate(date_fields):
        for field_b in date_fields[i + 1:]:
            tok_a, tok_b = out[field_a][1], out[field_b][1]
            if tok_a is None or tok_a is not tok_b:
                continue

            # Sharing a token is only a collision when it produced the SAME
            # date. A compact back-of-card row arrives as one token carrying
            # two captions and two values -- "DOI:25/09/2023 VALID
            # TILL:25/09/2043" -- and each field reads the date following its
            # own caption. Those are two distinct, correctly-sourced values,
            # not two searches landing on one; clearing them loses the expiry
            # on every card printed that way.
            if out[field_a][0] and out[field_b][0] and out[field_a][0] != out[field_b][0]:
                continue

            if field_a == "date_of_birth":
                # Date of birth is independently sourced (its own label
                # search, separately age-checked) and never guessed at, so
                # if another field's search lands on that exact token it is
                # unambiguously wrong for that field -- unlike the DOI/
                # valid-till case below, this is not a guess between two
                # equally plausible candidates. A real card had valid_till's
                # search grab the DOB value ("06/01/1989") because it was
                # merely closer in y than the genuine valid-till date
                # further down the same column; retrying excludes that
                # specific, already-confirmed token.
                labels_b = {"date_of_issue": _DOI_LABELS, "valid_till": _VALID_LABELS}[field_b]
                retry_value, retry_tok = _labelled_date(scope, labels_b, exclude=tok_a)
                # The retry avoided the DOB collision, but on a card where
                # the field's genuine value was simply never read by OCR at
                # all (a real Bangalore DL's true valid-till date), it can
                # land on date_of_issue's token instead -- silently
                # duplicating one field's date into another. Checking
                # against every OTHER already-resolved date field, not just
                # the one that triggered this retry, catches that.
                # Two different OCR tokens can independently read the same
                # real-world date (separate detections that normalise to an
                # identical string), so object identity alone is not enough
                # here -- the VALUE is checked too. A real card's DOI was
                # read twice this way, and the retry silently duplicated it
                # into valid_till despite the tokens being distinct objects.
                other_field = next(f for f in date_fields if f not in (field_a, field_b))
                other_value, other_tok = out[other_field]
                duplicates_other = retry_tok is not None and (
                    retry_tok is other_tok or retry_value == other_value
                )
                if duplicates_other:
                    retry_value, retry_tok = None, None
                out[field_b] = (retry_value, retry_tok) if retry_value else (None, None)
            else:
                # date_of_issue vs valid_till: both are independently
                # plausible near-present-day dates with no reliable way to
                # tell which is which from a collision alone. Retrying here
                # previously swapped them (both wrong, confidently) on a
                # real card, so this case is still cleared rather than
                # guessed.
                out[field_b] = (None, None)

    return out


__all__ = ["extract_dl_fields"]
