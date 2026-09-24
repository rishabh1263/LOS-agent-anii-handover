"""
Reconstructing a statement's table from where the words are on the page.

WHY THIS EXISTS. The scanned path read the printed ruling lines and
placed tokens into the boxes they drew. On the sample that failed,
the detector found a ruled block on page 1 -- the account summary --
and one or two stray lines on pages 2 to 4, which is where the
transactions actually are. Pages with fewer than three ruled rows are
skipped, so the pipeline read the one page with no transactions on it
and skipped the three that had them, then reported that no table
could be reconstructed. Nothing was wrong with the OCR: 483 tokens
came back, including every date, narration and amount.

SO THE TABLE IS FOUND FROM THE TOKENS, NOT FROM THE PRINTING. A
statement's columns are where its own header captions are, and its
rows are wherever a date appears in the date column. Both are read
off the page; neither is configured.

GENERIC, NOT PER-BANK. Nothing here knows which bank it is reading.
The captions come from the shared vocabulary in `tables`, the column
edges from the header's own geometry, and the row boundaries from the
dates. A layout plugin would have been the wrong shape for a problem
whose answer is "look at where the header is".

IT PRODUCES CELLS AND STOPS. Parsing dates and amounts, pairing
debits against credits, checking the running balance and deciding a
verdict all happen exactly where they already did -- this hands
`rows_to_transactions` the same shape the ruled path hands it, and
nothing downstream can tell which one it came from.
"""

from __future__ import annotations

import logging
import re
import statistics
from typing import Any

from app.agents.bank_statement import parse as P
from app.agents.bank_statement import tables as T

logger = logging.getLogger(__name__)

#: Fields that make a row of captions a header rather than a coincidence.
#: A date column and two money columns is the least that identifies a
#: transaction table; a summary block has captions too, and this is what
#: keeps "Account Summary" from being read as one.
_REQUIRED = ("date",)
_MONEY = ("debit", "credit", "balance")
_MINIMUM_MONEY_COLUMNS = 2

#: How far apart two header captions may sit vertically and still belong
#: to the same header. OCR reads a wrapped caption ("Cheque" above
#: "No/Reference") as two lines, so this is generous -- expressed in
#: token heights rather than pixels, because it has to hold at any DPI.
_HEADER_BAND = 3.0

#: How far apart two tokens may sit vertically and still be one printed
#: line.
_LINE_BAND = 0.7


#: How close an OCR'd caption must be to a known one to count as it.
#: "Dobit" for Debit, "Balanco" for Balance, "Chequo" for Cheque -- a
#: scan at 200 dpi mis-reads a letter or two in almost every caption on
#: the sample, and an exact match found the header on one page in four.
#: High enough that "Credit" and "Debit" (0.67) never trade places.
#: Lowered from 0.78 once the guards above were in place. "Post Cale"
#: -- the scan's reading of "Post Date" -- scores 0.75, and without it
#: a page has only one date column, which is what left page four of
#: the sample splitting its rows.
_CAPTION_RATIO = 0.74


def field_of(text: str) -> str | None:
    """
    Which column a caption names, allowing for OCR spelling.

    EXACT FIRST, ALWAYS. The shared vocabulary decides, and only when
    it finds nothing is the nearest caption considered -- so a
    statement whose captions are read correctly behaves exactly as it
    does on the digital path.

    THE VOCABULARY IS NOT COPIED. It is read from `tables`, so a
    caption added for digital statements is understood here too.
    """
    from difflib import SequenceMatcher

    # A CAPTION IS A WORD OR TWO, AND HAS NO DIGITS IN IT.
    #
    # Without this, two kinds of data were read as captions and took
    # a column each. "34,075.60CR" normalises to "cr", which IS the
    # credit caption, so every balance in the column claimed to be a
    # header. And "CASH WITHDRAWAL SELF" is close enough to
    # "withdrawal" to match, which put the debit column at the left
    # edge of the page and mapped the narration into it -- the
    # narration digits then parsed as amounts and the debits summed
    # to ninety-eight billion rupees.
    raw = (text or "").strip()
    if (not raw
            or any(c.isdigit() for c in raw)
            or len(raw) > 22
            # TWO WORDS AT MOST. Every caption a statement prints is
            # one or two -- "Balance", "Value Date", "No/Reference".
            # At three, "CASH WITHDRAWAL SELF" matched "withdrawal"
            # and claimed the debit column at the left of the page.
            or len(raw.split()) > 2):
        return None

    exact = T.map_columns([text])
    if exact:
        return next(iter(exact))

    key = T._norm(text or "")
    if len(key) < 3:
        return None

    best: tuple[float, str | None] = (0.0, None)
    for field, captions in T._HEADERS.items():
        for caption in captions:
            if len(caption) < 4:
                continue
            ratio = SequenceMatcher(None, key, caption).ratio()
            if ratio > best[0]:
                best = (ratio, field)

    return best[1] if best[0] >= _CAPTION_RATIO else None


#: A cell that is nothing but digits, separators and an optional
#: Cr/Dr marker -- in other words, an amount.
_MONEY_CELL = re.compile(r"^[0-9][0-9.,]*(?:\s*(?:CR|DR|Cr|Dr))?$")


def normalise_amount(text: str) -> str:
    """
    Repair an amount whose separators OCR misread.

    "63.618.86" IS ONE NUMBER, NOT A TYPO TO REJECT. At 200 dpi a
    comma and a full stop are a few pixels apart, and Indian grouping
    puts two separators in most four-figure balances -- so the scan
    reads 63,618.86 as 63.618.86, which parses as 63.61 and turns a
    sixty-three thousand rupee balance into sixty-three rupees. Every
    row after it then fails the running-balance check.

    THE LAST SEPARATOR IS THE DECIMAL POINT; the others are grouping.
    That holds for both conventions and needs no guess about which
    one the bank used.

    ONLY ON THIS PATH, and only on cells that are entirely an amount.
    The digital parser sees separators the publisher actually wrote
    and must keep reading them literally.
    """
    value = (text or "").strip()
    if not _MONEY_CELL.match(value):
        return value

    marker = ""
    for suffix in ("CR", "DR", "Cr", "Dr"):
        if value.upper().endswith(suffix.upper()):
            marker = value[-len(suffix):]
            value = value[:-len(suffix)].strip()
            break

    separators = [c for c in value if c in ".,"]
    if len(separators) < 2:
        return (value + marker) if marker else value

    head, _, tail = value.rpartition(separators[-1])
    head = head.replace(".", "").replace(",", "")
    repaired = f"{head}.{tail}" if tail else head
    return repaired + marker


def _height(tokens: list[Any]) -> float:
    heights = [abs(t.y1 - t.y0) for t in tokens if abs(t.y1 - t.y0) > 0]
    return statistics.median(heights) if heights else 20.0


def _centre(token: Any) -> float:
    return (token.x0 + token.x1) / 2


def header_of(tokens: list[Any]) -> tuple[list[Any], dict[str, int]] | None:
    """
    The header captions, and what each column means. None if not a table.

    THIS IS ALSO THE PAGE TEST. A page whose tokens contain no date
    caption and two money captions is not a transaction page, and the
    caller skips it -- which is how the summary page stops being read
    as transactions.
    """
    if not tokens:
        return None

    band = _height(tokens) * _HEADER_BAND
    candidates = [(t, field_of(t.text)) for t in tokens]
    candidates = [(t, f) for t, f in candidates if f]

    for anchor, _ in sorted(candidates, key=lambda pair: pair[0].y0):
        row = sorted(
            ((t, f) for t, f in candidates if abs(t.y0 - anchor.y0) <= band),
            key=lambda pair: _centre(pair[0]),
        )

        # ONE CAPTION PER COLUMN, and the leftmost wins. A wrapped
        # caption is read as two tokens ("Cheque" over "No/Reference")
        # and both name the same column.
        seen: set[str] = set()
        header: list[Any] = []
        mapping: dict[str, int] = {}
        dates: list[int] = []
        for token, field in row:
            # A REPEATED CAPTION STILL GETS A COLUMN, just not the
            # field. THIS IS WHY AMOUNTS WENT MISSING.
            #
            # The statement prints two date columns -- Post Date and
            # Value Date -- and both name "date". Skipping the second
            # outright left no boundary between them, so BOTH date
            # tokens fell in column zero. A row opens on a date, so
            # every transaction opened twice, twenty pixels apart,
            # and its balance and its debit were split across the two
            # halves. Twenty rows ended up holding no amount at all.
            #
            # Keeping the column and dropping only the duplicate
            # mapping puts a boundary where the page has one: the
            # second date lands in a column nothing is mapped to, and
            # stops splitting the row.
            if field in ("date", "value_date"):
                dates.append(len(header))
            if field not in seen:
                seen.add(field)
                mapping[field] = len(header)
            header.append(token)

        # A DATE COLUMN AND TWO MONEY COLUMNS. `value_date` counts as
        # the date when no plain date caption was read -- the sample's
        # pages lead with "Value Date" and put "Post Date" last.
        if "date" not in mapping and "value_date" in mapping:
            mapping["date"] = mapping.pop("value_date")

        if all(f in mapping for f in _REQUIRED) and \
                len(set(mapping) & set(_MONEY)) >= _MINIMUM_MONEY_COLUMNS:
            mapping["_date_columns"] = dates or [mapping["date"]]
            return header, mapping

    return None


def boundaries_of(header: list[Any]) -> list[float]:
    """
    Where one column ends and the next begins.

    THE MIDPOINT BETWEEN CAPTIONS, not the caption's own width. A
    narration is far wider than the word "Description" above it and
    starts well to its left; bounded by the caption, most of it would
    fall outside every column.
    """
    edges: list[float] = [float("-inf")]
    for left, right in zip(header, header[1:]):
        edges.append((left.x1 + right.x0) / 2)
    edges.append(float("inf"))
    return edges


def _column_of(token: Any, edges: list[float]) -> int:
    """
    The column this token sits in, by overlap rather than by centre.

    A narration that begins left of its own caption overlaps two
    columns, and its centre can fall in the wrong one. The column it
    shares the most width with is the column it is printed in.
    """
    best, best_overlap = 0, -1.0
    for index in range(len(edges) - 1):
        left, right = edges[index], edges[index + 1]
        overlap = min(token.x1, right) - max(token.x0, left)
        if overlap > best_overlap:
            best, best_overlap = index, overlap
    return best


def lines_of(tokens: list[Any], band: float) -> list[list[Any]]:
    """Tokens grouped into the printed lines they sit on."""
    lines: list[list[Any]] = []
    for token in sorted(tokens, key=lambda t: (t.y0, t.x0)):
        if lines and abs(token.y0 - lines[-1][0].y0) <= band:
            lines[-1].append(token)
        else:
            lines.append([token])
    return [sorted(line, key=lambda t: t.x0) for line in lines]


def _money_columns(mapping: dict[str, int]) -> set[int]:
    return {mapping[f] for f in _MONEY if f in mapping}


def _anchor_column(below: list[Any], edges: list[float],
                   mapping: dict[str, int], candidates: list[int]) -> int:
    """
    Which date column starts a row, decided by where the amounts are.

    A STATEMENT WITH TWO DATE COLUMNS PRINTS THEM AT DIFFERENT
    HEIGHTS. On the sample, the value date sits about twenty pixels
    above the post date of the same transaction, and the balance and
    the debit sit between them. Anchored on the lower of the two,
    every amount fell just ABOVE its own anchor and was collected
    into the row before -- eleven transactions ended up holding no
    amount while the amounts themselves were sitting in the right
    columns all along.

    Rather than encode which caption a bank prints higher, each
    candidate is scored on how many of its rows contain an amount.
    The column that explains the amounts is the column that starts
    the rows.
    """
    money = _money_columns(mapping)
    if len(candidates) < 2 or not money:
        return candidates[0]

    best, best_score = candidates[0], -1
    for column in candidates:
        anchors = sorted(
            t.y0 for t in below
            if _column_of(t, edges) == column
            and P.parse_date(t.text) is not None
        )
        if not anchors:
            continue

        score = 0
        for index, start in enumerate(anchors):
            end = anchors[index + 1] if index + 1 < len(anchors) else float("inf")
            if any(start <= t.y0 < end and _column_of(t, edges) in money
                   and P.parse_amount(t.text) is not None for t in below):
                score += 1

        if score > best_score:
            best, best_score = column, score

    return best


def rows_of(tokens: list[Any]) -> tuple[list[list[str]], dict[str, int]] | None:
    """
    One cell row per transaction, in the shape the table parser expects.

    ROWS ARE BOUNDED BY DATES, not by printing. A transaction occupies
    as many printed lines as its narration needs, and its amounts are
    not always on the first of them -- the balance and the debit on
    the sample sit ten pixels apart on different lines. A new row
    starts where a date appears in the date column; everything after
    it belongs to that transaction.

    NOTHING IS OVERWRITTEN. Within a row the first value to arrive in
    a column stays, and narration accumulates. An amount that OCR
    placed on the continuation line therefore lands in its own column
    instead of being lost with the line it was read on.
    """
    found = header_of(tokens)
    if found is None:
        return None

    header, mapping = found
    candidates = mapping.pop("_date_columns", None) or [mapping.get("date")]
    edges = boundaries_of(header)
    width = len(header)
    band = _height(tokens) * _LINE_BAND

    below = [t for t in tokens if t.y0 > max(h.y1 for h in header)]
    date_column = _anchor_column(below, edges, mapping, candidates)

    rows: list[list[str]] = []
    current: list[str] | None = None

    for line in lines_of(below, band):
        placed: dict[int, list[str]] = {}
        for token in line:
            placed.setdefault(_column_of(token, edges), []).append(token.text)

        opens = False
        if date_column is not None:
            for text in placed.get(date_column, []):
                if P.parse_date(text) is not None:
                    opens = True
                    break

        if opens or current is None:
            current = [""] * width
            rows.append(current)

        narration_column = mapping.get("narration")
        for index, texts in placed.items():
            if index >= width:
                continue
            value = " ".join(texts).strip()
            if not value:
                continue
            if index == narration_column and current[index]:
                current[index] = f"{current[index]} {value}".strip()
            elif not current[index]:
                current[index] = (value if index == narration_column
                                  else normalise_amount(value))

    return ([[t.text for t in header]] + rows, mapping) if rows else None


def cells_for_page(tokens: list[Any]) -> tuple[list[list[str]],
                                               dict[str, int]] | None:
    """
    The page as table rows, or None when it is not a transaction page.

    The header row is kept at the top of the returned rows because
    `rows_to_transactions` already recognises and skips a repeated
    caption row, and removing it here would be a second place that
    has to agree about what a header looks like.
    """
    try:
        return rows_of(tokens)
    except Exception as exc:                          # pragma: no cover
        logger.warning("Column inference failed: %s", type(exc).__name__)
        return None


__all__ = ["boundaries_of", "cells_for_page", "header_of", "lines_of",
           "rows_of"]
