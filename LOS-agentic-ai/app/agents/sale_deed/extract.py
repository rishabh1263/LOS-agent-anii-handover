"""
Sale Deed extraction.

Every real Sale Deed sample tested was a scanned, multi-page PDF with no text
layer. Of ten, only a minority carried a readable page: an e-Stamp Certificate
cover sheet (SHCIL / NEWIMPACC-style) in English, listing First Party, Second
Party, Consideration Price and Stamp Duty. The rest were either handwritten,
in a regional script, or too poor a scan for any OCR engine to read -- deed
BODY content (the actual conveyance text, property schedule in full) was not
readable on a single real sample, in any language tried.

This extractor is therefore scoped to what was proven readable: the e-Stamp
cover page, when one is present and legible. It does not attempt to read the
deed body. Hindi (Devanagari) OCR is available (tesseract-ocr-hin) and is
tried as a fallback, but no real sample produced usable structured fields
from handwritten Hindi body text -- that content stays out of scope.
"""

from __future__ import annotations

import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.agents.sale_deed import labels, templates
from app.agents.sale_deed.schemas import (
    DeedSubtype,
    FieldEvidence,
    SaleDeedResult,
    SaleDeedStatus,
    TemplateKind,
)


def _page_count(path: str) -> int:
    """Total pages, for reporting how much of the document was looked at."""
    try:
        import pymupdf

        with pymupdf.open(path) as document:
            return len(document)
    except Exception:  # pragma: no cover - defensive
        return 0


def _read_text_layer(path: str, budget: int) -> tuple[int, str] | None:
    """
    The best embedded-text page, when the PDF carries one worth reading.

    Returns the page index and its text, or None when there is no usable
    layer. "Usable" is deliberately strict: a scanned deed often carries a
    few stray characters, and treating those as a text layer would skip the
    OCR that actually reads the document.
    """
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - pymupdf is a hard dependency
        return None

    try:
        with pymupdf.open(path) as document:
            best: tuple[int, str] | None = None
            for index in range(min(len(document), budget)):
                text = document[index].get_text() or ""
                if len(text.strip()) < 200:
                    continue
                if best is None or len(text) > len(best[1]):
                    best = (index + 1, text)
            return best
    except Exception as exc:
        logger.debug("Text layer unreadable for %s: %s", path, exc)
        return None

logger = logging.getLogger(__name__)

# How many pages of the deed are OCR'd looking for the e-Stamp cover. It is
# usually page 1; a few extra pages are tried because some scans interleave
# a blank or photo page first.
# Configurable because deeds are long: a 28-page real sample carried its
# cover on page 2, but binding order is not guaranteed and a fixed budget of
# 4 silently gives up on a deed whose cover sits further in. Each extra page
# costs one OCR pass, so the default stays small and raising it is deliberate.
DEFAULT_MAX_PAGES_TRIED = 6

# Retained so callers that imported the old constant keep working.
MAX_PAGES_TRIED = DEFAULT_MAX_PAGES_TRIED


def max_pages_tried() -> int:
    """The page budget, overridable with SALE_DEED_MAX_PAGES."""
    try:
        value = int(os.getenv("SALE_DEED_MAX_PAGES", "") or "")
    except ValueError:
        return DEFAULT_MAX_PAGES_TRIED
    return max(1, min(value, 40))


_ARTICLE_RE = re.compile(r"Article\s+\d+\s+[A-Za-z]+", re.IGNORECASE)
# Matched case-insensitively and normalised afterwards. OCR routinely drops
# case partway through these long alphanumeric runs -- a real certificate
# read "SUBIN-UPUPTAtQR60d084a4aza4022069" -- and an upper-case-only pattern
# discards the reference entirely rather than recovering a usable one.
_REGISTRATION_RE = re.compile(r"\b(?:SUBIN|IN)-[A-Za-z0-9]{10,40}\b", re.IGNORECASE)

# The exact shape of a genuine issuer reference, taken from the real
# certificates in the corpus:
#
#   IN-UP04991166365567V              IN- + state + 14 digits + check letter
#   IN-UP58921054623548Y
#   IN-UP90999379970911W
#   SUBIN-UPUP1413860408484324402238Y SUBIN- + code + 22 digits + letter
#
# An UNBROKEN digit run and a TRAILING LETTER are both required, and they are
# what a damaged read loses. Two real failures this rejects:
#
#   SUBIN-UPUPTATQR60D084A4AZA4022069  letters scattered through the digits
#   IN-UUP9099937997091                digits mostly right, no check letter,
#                                      state code misread as UUP
#
# The second is the dangerous one: it is mostly digits and would pass a ratio
# test, while being a reference that does not exist. A near-miss is worse
# than a blank, because it looks like something a reviewer could go and check.
_REFERENCE_SHAPE_RE = re.compile(r"^(?:SUBIN|IN)-[A-Z]{2,4}\d{12,24}[A-Z]$")


def reference_is_plausible(value: str | None) -> bool:
    """
    Whether a registration reference is well enough formed to report.

    Defined here, next to the pattern that finds them, and reused by the
    capability layer so the extractor and the verdict cannot disagree about
    what counts as a usable reference.
    """
    if not value:
        return False

    return bool(_REFERENCE_SHAPE_RE.match(value.strip().upper()))
_DATE_RE = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b")

_AMOUNT_RE = re.compile(r"(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?)")


def _parse_amount(text: str) -> Decimal | None:
    match = _AMOUNT_RE.search(text or "")
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


# The exact English SHCIL template was the only pattern proven on real
# samples, but "First Party" is one issuer's wording for a concept every
# deed states: who is conveying, and who is receiving. Other e-Stamp
# vendors and manually-typed deeds use different captions for the same
# field, so each role is matched against several known synonyms rather
# than one fixed string -- the same approach used for the DL guardian
# caption, which needed "Son/Daughter/Wife of" as well as "S/D/W of".
#
# Hindi variants are included for when a deed's cover page is in
# Devanagari, but are UNVERIFIED: no real sample tested here had a Hindi
# e-Stamp cover, only Hindi in the (out-of-scope) deed body. They are
# additive and cannot make an already-working English match worse.
FIRST_PARTY_CAPTIONS = (
    "First Party", "1st Party", "Party 1", "Seller", "Vendor", "Executant",
    "\u0935\u093f\u0915\u094d\u0930\u0947\u0924\u093e",
)
SECOND_PARTY_CAPTIONS = (
    "Second Party", "2nd Party", "Party 2", "Buyer", "Purchaser", "Claimant",
    "Second Party (Claimant)", "\u0915\u094d\u0930\u0947\u0924\u093e",
)


def _value_after(lines: list[str], captions) -> str | None:
    """
    The text following any of several caption variants on the same line.

    Tries each variant in order and returns the first match, so a deed using
    "Vendor" instead of "First Party" is still read without the caller
    needing to know which wording this particular issuer used.
    """
    if isinstance(captions, str):
        captions = (captions,)

    best: str | None = None
    best_score = 0.0

    for caption in captions:
        pattern = re.compile(re.escape(caption) + r"(.*)", re.IGNORECASE)
        for line in lines:
            match = pattern.search(line)
            if not match:
                continue

            for candidate in _candidates_from_tail(match.group(1)):
                score = _name_score(candidate)
                if score > best_score:
                    best, best_score = candidate, score

    return best if best_score >= _NAME_SCORE_FLOOR else None


def _candidates_from_tail(tail: str) -> list[str]:
    """
    The possible values sitting after a caption on its line.

    The e-Stamp template is two columns -- caption on the left, value on the
    right, a colon between them -- and OCR fills the gap with whatever it
    made of the background pattern. A real line read:

        fos A First Party iki ; ASHOK KUMAR, SON OF DHANIRAM seitas| im

    Taking the text immediately after the caption yields "iki". The value is
    the segment after the SEPARATOR, so every separated segment is offered as
    a candidate and scored, rather than trusting position alone.
    """
    segments: list[str] = []

    # Preferred reading: everything after the FIRST separator. OCR sprinkles
    # stray colons inside values -- a real line read "ANIL: KUMAR AND' SUNIL"
    # -- so splitting on every separator and taking a piece truncates the
    # value to "KUMAR AND". Taking the remainder keeps it whole.
    first = re.search(r"[:;>|]", tail)
    if first:
        remainder = tail[first.end():]
        # Past the caption boundary, further separators are OCR debris inside
        # the value, not field boundaries: a real line read "ANIL: KUMAR AND'
        # SUNIL", and treating that colon as a boundary truncates a joint
        # party to "ANIL". Flattened to spaces so the value survives whole.
        segments.append(re.sub(r"(?<=[A-Za-z])[:;|](?=[\sA-Za-z])", " ", remainder))

    # Then each separated piece, for templates where the value really is one
    # field between separators.
    segments.extend(
        segment for segment in re.split(r"[:;>|]", tail) if segment.strip()
    )

    # The whole tail last, for templates using no separator at all.
    if tail.strip():
        segments.append(tail)

    candidates: list[str] = []
    for segment in segments:
        cleaned = _clean_name(segment)
        if cleaned:
            candidates.append(cleaned)

    return candidates


def _clean_name(value: str) -> str | None:
    """Trim a candidate to the run of characters a name can be made of."""
    value = value.strip(" :.-_|,")
    if not value:
        return None

    match = re.match(r"^[A-Za-z][A-Za-z\s./',&-]*", value)
    if not match:
        return None

    cleaned = re.sub(r"\s{2,}", " ", match.group(0)).strip(" ,.-&")
    if not cleaned:
        return None

    return _trim_trailing_noise(cleaned)


def _trim_trailing_noise(value: str) -> str | None:
    """
    Drop the OCR debris that trails a value on a patterned page.

    Real reads ended "...SON OF DHANIRAM seitas" and "...AND SUNIL KU ea":
    the name is right and a fragment of the security pattern is stuck to the
    end. A trailing word that is short and lower-case is not part of a name
    printed in capitals, so it goes.
    """
    words = value.split()

    while words:
        last = words[-1]
        stray = (
            (last.islower() and len(last) <= 6)
            or (len(last) <= 2 and not last.isupper())
        )
        if stray and len(words) > 1:
            words.pop()
            continue
        break

    return " ".join(words).strip(" ,.-&") or None


# Words that mark running prose. A registration page carries sentences as
# well as fields -- "...and their identifier, who have admitted execution
# before me..." -- and a caption match landing in one of those sentences
# produced a first party of "s, and their identifier, who have admitted".
_PROSE_WORDS = {
    "AND", "THE", "THIS", "THAT", "WHO", "HAVE", "HAS", "BEEN", "WAS", "WERE",
    "FOR", "WITH", "FROM", "THEIR", "THEM", "BEFORE", "AFTER", "OTHER",
    "ADMITTED", "EXECUTION", "REGISTRATION", "PRESENTED", "DOCUMENT", "FOUND",
    "ADMISSIBLE", "NAMES", "PHOTOGRAPHS", "FINGERPRINTS", "SIGNATURES",
    "AFFIXED", "REVERSE", "PAGE", "PAID", "TOTAL", "YEAR", "BOOK", "VOLUME",
}


# Words that are captions, not values. OCR frequently runs one caption into
# the next column, and without this a party name comes back as "Second Party".
_LABEL_WORDS = {
    "FIRST", "SECOND", "PARTY", "PURCHASED", "DESCRIPTION", "DOCUMENT",
    "PROPERTY", "CONSIDERATION", "PRICE", "STAMP", "DUTY", "PAID", "AMOUNT",
    "CERTIFICATE", "ACCOUNT", "REFERENCE", "UNIQUE", "DOC", "ISSUED", "DATE",
    "ARTICLE", "GIFT", "CONVEYANCE", "SALE", "DEED", "RUPEES", "ONLY",
    "INDIA", "NON", "JUDICIAL", "GOVERNMENT", "VENDOR", "LICENCE",
}

_NAME_SCORE_FLOOR = 0.55


def _name_score(candidate: str) -> float:
    """
    How much a candidate looks like a person or party name.

    Scored rather than accepted, because the previous first-match rule let
    OCR noise through as fact: real pages produced a first party of "s",
    "iki" and "oe a". A wrong name is worse than a missing one -- it flows
    into KYC name matching and into the case file as though someone had read
    it off the document.
    """
    if not candidate:
        return 0.0

    text = candidate.strip()
    letters = [c for c in text if c.isalpha()]

    # Too short to be a name. "s", "iki" and "oe a" all die here.
    if len(letters) < 4:
        return 0.0

    words = [w for w in re.split(r"[\s./',&-]+", text.upper()) if w]
    if not words:
        return 0.0

    # A candidate made only of caption words is the next column, not a value.
    if all(word in _LABEL_WORDS for word in words):
        return 0.0

    # Running prose is not a name. Two or more sentence words means the
    # caption matched inside an endorsement paragraph rather than a field.
    prose_hits = sum(1 for word in words if word in _PROSE_WORDS)
    if prose_hits >= 2:
        return 0.0

    # A long run of words is a sentence, not a party name -- even a joint
    # party rarely exceeds a handful of words.
    if len(words) > 8:
        return 0.0

    # Half or more of the words starting lower case is OCR of body text or of
    # the background pattern, not a printed field value: these templates print
    # party names in capitals, so a real name scores zero here.
    lower_words = [w for w in text.split() if w and w[0].islower()]
    if len(lower_words) >= max(1, len(words) / 2):
        return 0.0

    score = 0.5

    # Real names on this template are printed in capitals.
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    score += 0.25 * upper_ratio

    # Two or more words of reasonable length: "ASHOK KUMAR", not "iki".
    solid_words = [w for w in words if len(w) >= 3]
    if len(solid_words) >= 2:
        score += 0.2
    elif len(solid_words) == 1 and len(letters) >= 6:
        score += 0.1

    # Relationship markers are strong evidence this is a party line.
    if re.search(r"\b(?:S/?O|D/?O|W/?O|SON OF|DAUGHTER OF|WIFE OF)\b", text.upper()):
        score += 0.2

    # Stray caption words mixed in cost a little, but do not disqualify:
    # "ANIL KUMAR AND SUNIL KUMAR" is a real value.
    label_hits = sum(1 for word in words if word in _LABEL_WORDS)
    score -= 0.15 * (label_hits / len(words))

    return max(0.0, min(1.0, score))


# Signals strong enough to identify a registration page on their own.
#
# Caption matching alone is too brittle: on a real sample photographed off a
# phone screen at an angle, OCR rendered "Certificate No." as "Certi<ate No.",
# "Purchased by" as "Purc<ased by" and "Description" as "Descri<tion". Not one
# caption survived, so a perfectly legible e-Stamp certificate -- reference,
# both parties, stamp duty all readable -- was reported as unsupported.
#
# These strings survive that damage because they are long, distinctive and
# printed large: the masthead, the issuer's system name, and the reference
# format itself.
_STRONG_PAGE_SIGNALS = (
    "INDIANONJUDICIAL",
    "NONJUDICIAL",
    "NEWIMPACC",
    "SHCIL",
    "ESTAMP",
    "SUMMARYOFENDORSEMENT",
)

# An issuer reference is itself proof of a registration page. Matched
# case-insensitively because OCR routinely drops case on these long
# alphanumeric runs.
_REFERENCE_SIGNAL_RE = re.compile(r"(?:SUBIN|IN)-[A-Z0-9]{8,40}", re.IGNORECASE)


def _amount_near_caption(
    lines: list[str],
    captions: tuple[str, ...],
    window: int = 2,
) -> Decimal | None:
    """
    An amount on a line near its caption, above or below.

    The e-Stamp amount block prints the figure and its words on separate
    lines, and OCR reorders them: on a real certificate the caption came out
    as "Rg Simp Guy saa oy" with the actual "5,000" on the line ABOVE it. A
    strictly same-line or strictly below-label rule finds neither.

    The window is deliberately small. Widen it and an unrelated number from
    elsewhere on a busy page becomes the stamp duty, which is the failure
    this whole extractor is most careful to avoid.
    """
    lowered = [caption.lower() for caption in captions]

    for index, line in enumerate(lines):
        haystack = line.lower()
        if not any(caption in haystack for caption in lowered):
            # OCR damages these captions badly, so a looser fallback: the
            # distinctive words, in order, anywhere on the line.
            if not ("duty" in haystack and "stamp" in haystack):
                continue

        start = max(0, index - window)
        end = min(len(lines), index + window + 1)
        for offset in range(start, end):
            amount = _parse_amount(lines[offset])
            if amount is not None:
                return amount

    return None


# A page carrying a usable reference is as good as this gets; the scan stops
# there rather than paying for further OCR passes.
_STRONG_PAGE_SCORE = 4


def _has_devanagari(text: str) -> bool:
    """
    Whether a page carries Devanagari script.

    Tesseract's English model still emits a few Devanagari codepoints from
    ornament and seal noise, so a handful of characters is not evidence of a
    Hindi page; a genuine Hindi form produces far more.
    """
    return sum(1 for c in text if "ऀ" <= c <= "ॿ") >= 12


def _page_score(text: str) -> int:
    """
    How much usable evidence a candidate page carries.

    Used to choose BETWEEN pages that all look like registration pages. A
    plausible reference dominates, because it is the one field that is both
    checkable and unique to the real cover.
    """
    score = 0

    for candidate in _REGISTRATION_RE.finditer(text):
        if reference_is_plausible(candidate.group(0).upper()):
            score += 4
            break

    upper = text.upper()
    for caption in ("FIRST PARTY", "SECOND PARTY", "STAMP DUTY", "CONSIDERATION"):
        if caption in upper:
            score += 1

    if _ARTICLE_RE.search(text):
        score += 1
    if _DATE_RE.search(text):
        score += 1

    return score


def _is_estamp_page(text: str) -> bool:
    """
    Whether this page is a registration/e-Stamp page rather than deed body.

    Two independent routes, because either one alone misses real documents:
    a strong printed signal, or two weaker captions agreeing.
    """
    upper = text.upper()
    compact = re.sub(r"[^A-Z0-9]", "", upper)

    if any(signal in compact for signal in _STRONG_PAGE_SIGNALS):
        return True

    if _REFERENCE_SIGNAL_RE.search(text):
        return True

    markers = (
        [c.upper() for c in FIRST_PARTY_CAPTIONS if c.isascii()]
        + [c.upper() for c in SECOND_PARTY_CAPTIONS if c.isascii()]
        + ["CONSIDERATION", "STAMP DUTY", "ARTICLE", "REGISTRATION"]
    )
    return sum(1 for marker in markers if marker in upper) >= 2


def _ocr_page(image, lang: str) -> str:
    import pytesseract

    try:
        return pytesseract.image_to_string(image, lang=lang) or ""
    except Exception as exc:
        logger.warning("Sale deed OCR failed (lang=%s): %s", lang, exc)
        return ""


def extract_sale_deed(path: str) -> SaleDeedResult:
    """
    Read a Sale Deed's e-Stamp certificate cover page, if it has one.

    Tries English first (the template's own language), then Hindi as a
    fallback for the small amount of Devanagari text some copies mix in.
    Pages are tried in order until an e-Stamp page is recognised or the page
    budget runs out; if none is found the result is UNSUPPORTED rather than
    guessed from unrelated body text.
    """
    started = time.perf_counter()

    if not Path(path).exists():
        return SaleDeedResult(status=SaleDeedStatus.FAILED, errors=[f"File not found: {path}"])

    budget = max_pages_tried()

    # TEXT LAYER FIRST.
    #
    # Free, and sometimes the only source there is: one real sample renders
    # to a near-blank page carrying a scanner watermark while its embedded
    # text layer holds the whole Hindi form. Rasterising that document reads
    # nothing at all, and OCR'ing it twice reads nothing twice.
    layer = _read_text_layer(path, budget)
    if layer:
        page_index, layer_text = layer
        if _is_estamp_page(layer_text) or templates.classify_template(
            layer_text
        ) is not TemplateKind.UNKNOWN:
            result = _build_result(
                layer_text,
                pages=_page_count(path),
                page_found=page_index,
                language="hin" if labels.has_devanagari(layer_text) else "eng",
                started=started,
            )
            result.text_layer_used = True
            return result

    try:
        from pdf2image import convert_from_path
    except ImportError as exc:
        return SaleDeedResult(
            status=SaleDeedStatus.FAILED,
            errors=[f"pdf2image unavailable: {exc}"],
        )

    try:
        images = convert_from_path(
            path, dpi=250, first_page=1, last_page=budget
        )
    except Exception as exc:
        logger.warning("Could not rasterise sale deed: %s", exc)
        return SaleDeedResult(
            status=SaleDeedStatus.FAILED,
            errors=[f"Could not render PDF: {type(exc).__name__}: {exc}"],
            processing_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    pages = len(images)
    text = ""
    page_found = 0
    language = None

    # Pages are SCORED, not taken first-match.
    #
    # Broadening page detection made it find more real documents and also made
    # it stop sooner: on a 28-page deed whose usable cover is on page 2, a
    # page-1 masthead now matched and the good page was never reached. So a
    # page that yields a plausible reference wins outright and stops the scan
    # -- the common case stays one OCR pass -- while a weaker match is kept
    # only as a fallback and the scan continues.
    best_score = -1

    for index, image in enumerate(images, start=1):
        english = ""

        for lang in ("eng", "hin+eng"):
            # Hindi is a fallback, and a costly one: a second full OCR pass
            # per page doubles the worst case on a document that has no
            # usable cover at all. It is paid for only when the English pass
            # actually saw Devanagari on the page, which is the only
            # situation where it can help.
            if lang != "eng":
                if not hindi_enabled() or not _has_devanagari(english):
                    continue

            candidate = _ocr_page(image, lang)
            if lang == "eng":
                english = candidate

            recognised = _is_estamp_page(candidate) or (
                templates.classify_template(candidate) is not TemplateKind.UNKNOWN
            )
            if not recognised:
                continue

            score = _page_score(candidate)
            if score > best_score:
                best_score, text, page_found, language = (
                    score,
                    candidate,
                    index,
                    lang,
                )

            if score >= _STRONG_PAGE_SCORE:
                break
        if best_score >= _STRONG_PAGE_SCORE:
            break

    if not text:
        return SaleDeedResult(
            status=SaleDeedStatus.UNSUPPORTED,
            pages=pages,
            pages_scanned=pages,
            warnings=[
                f"No e-Stamp certificate page recognised in the first "
                f"{min(pages, budget)} page(s). The deed body itself "
                "is not read by this extractor: on every real sample tested "
                "it was handwritten, in a regional script, or too poor a "
                "scan for OCR."
            ],
            processing_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    return _build_result(
        text,
        pages=pages,
        page_found=page_found,
        language=language,
        started=started,
    )


def _build_result(
    text: str,
    *,
    pages: int,
    page_found: int,
    language: str | None,
    started: float,
) -> SaleDeedResult:
    """
    Turn one recognised page into a result.

    Shared by the text-layer path and the OCR path so both produce the same
    fields from the same evidence -- a document that happens to carry a text
    layer must not be read by different rules from one that does not.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    result = SaleDeedResult(
        status=SaleDeedStatus.SUCCESS,
        pages=pages,
        pages_scanned=pages,
        estamp_page=page_found,
        language=language,
    )

    result.template = templates.classify_template(text)
    if result.template is TemplateKind.UNKNOWN and _is_estamp_page(text):
        result.template = TemplateKind.ESTAMP_CERTIFICATE

    article = _ARTICLE_RE.search(text)
    result.article_type = article.group(0) if article else None
    result.subtype = templates.detect_subtype(text, result.article_type)

    # --- template-specific evidence ------------------------------------
    if result.template is TemplateKind.ENDORSEMENT_SUMMARY:
        for key, value in templates.extract_endorsement(text).items():
            setattr(result, key, value)

    if result.template is TemplateKind.REGISTRATION_FORM:
        for key, value in templates.extract_registration_form(text).items():
            setattr(result, key, value)

    if result.first_party is None:
        result.first_party = _value_after(lines, FIRST_PARTY_CAPTIONS)
    if result.second_party is None:
        result.second_party = _value_after(lines, SECOND_PARTY_CAPTIONS)
    if result.stamp_duty_paid_by is None:
        result.stamp_duty_paid_by = _value_after(
            lines, ("Stamp Duty Paid By", "Duty Paid By")
        )

    # Template-specific extraction ran first and may already have filled
    # these. The generic caption search must only ADD to that, never overwrite
    # it with a None: the endorsement path read a stamp duty straight out of
    # the registrar's sentence, and an unconditional assignment here threw it
    # away again.
    duty_captions = ("Stamp Duty Amount", "Duty Amount", "Stamp Duty")
    if result.stamp_duty_amount is None:
        duty_line = _value_after(lines, duty_captions)
        result.stamp_duty_amount = _parse_amount(duty_line) if duty_line else None

    if result.stamp_duty_amount is None:
        result.stamp_duty_amount = _amount_near_caption(lines, duty_captions)

    if result.consideration_price is None:
        price_line = _value_after(
            lines,
            ("Consideration Price", "Consideration Amount", "Sale Consideration"),
        )
        result.consideration_price = (
            _parse_amount(price_line) if price_line else None
        )
    if result.consideration_price is None:
        # The amount sometimes sits on the line below its own caption rather
        # than beside it.
        for i, line in enumerate(lines):
            if "consideration" in line.lower() and i + 1 < len(lines):
                result.consideration_price = _parse_amount(lines[i + 1])
                if result.consideration_price:
                    break

    # Normalised to upper case -- the issuer prints these in capitals, so the
    # mixed case OCR produces is damage -- and then checked for plausibility.
    # A reference that survives the shape test but not the digit test is
    # withheld: it looks checkable and is not, which is worse than absent.
    for candidate in _REGISTRATION_RE.finditer(text):
        normalised = candidate.group(0).upper()
        if reference_is_plausible(normalised):
            result.registration_reference = normalised
            break
    else:
        result.registration_reference = None
        if _REGISTRATION_RE.search(text):
            result.warnings.append(
                "A registration reference was visible but too badly read to "
                "be trusted; it is withheld rather than reported."
            )

    date = _DATE_RE.search(text)
    result.document_date = date.group(0) if date else None

    description_markers = ("HOUSE ON", "PLOT", "LAND AT", "PROPERTY AT")
    for line in lines:
        if any(m in line.upper() for m in description_markers):
            result.property_description = line[:250]
            break

    present = sum(
        1 for v in (
            result.article_type, result.first_party, result.second_party,
            result.consideration_price, result.registration_reference,
        ) if v is not None
    )
    result.confidence = round(present / 5, 4)

    required_missing = [
        n for n, v in (
            ("first_party", result.first_party),
            ("second_party", result.second_party),
        ) if v is None
    ]
    if required_missing:
        result.status = SaleDeedStatus.PARTIAL
        result.warnings.append(f"Required field(s) not found: {', '.join(required_missing)}")

    result.warnings.append(f"e-Stamp cover page found on page {page_found} of {pages}")
    result.warnings.append(
        "Deed body (property schedule, full legal text, signatures) is not "
        "extracted -- out of scope on the samples tested."
    )
    result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
    return result


def hindi_enabled() -> bool:
    return (os.getenv("SALE_DEED_HINDI_OCR", "true") or "true").lower() == "true"


__all__ = ["extract_sale_deed", "hindi_enabled"]
