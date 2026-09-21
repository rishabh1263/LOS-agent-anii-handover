"""
Bank statement extraction pipeline.

Routing is the important part: a statement with an embedded text layer is
parsed directly and returns in seconds, while a scanned statement is reported
as REQUIRES_OCR rather than being rasterised inline. A 26-page scan takes
roughly thirty seconds to OCR, which does not belong in a synchronous request
-- it belongs on the platform's existing AI job queue.
"""

from __future__ import annotations

import logging
import os
import re
import time
from decimal import Decimal
from pathlib import Path

from app.agents.bank_statement import parse as P
from app.agents.bank_statement.schemas import (
    BankStatementResult, ExtractionStatus, SourceKind, StatementPeriod,
    Transaction,
)

logger = logging.getLogger(__name__)

# A page needs at least this much text to count as digital. Scanned pages
# often carry a few characters of header furniture from a stamp or watermark.
MIN_CHARS_PER_PAGE = 60


def _int_env(name: str, default: int) -> int:
    """An empty env value counts as unset; int("") would otherwise raise."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def page_cap() -> int:
    """Maximum pages read. Beyond this the result is explicitly PARTIAL."""
    return _int_env("BANK_STATEMENT_MAX_PAGES", 150)


def inline_ocr_pages() -> int:
    """
    Page count up to which a scanned statement is OCR'd inline.

    A blanket "scanned means asynchronous" rule is too blunt: a two-page
    WhatsApp statement OCRs in a couple of seconds and there is no reason to
    make the caller queue it. A fifty-page scan is a different matter, so the
    cutoff is deliberately low and configurable.
    """
    # Default 0: inline OCR is DISABLED.
    #
    # It was measured at 22s locally and 103s on the reporting host for a
    # two-page statement, and the rows it produced did not reconcile against
    # the running balance. A path that is both slow and untrustworthy should
    # not run by default -- returning REQUIRES_OCR in milliseconds lets the
    # caller queue the work instead of waiting two minutes for figures nobody
    # should use.
    #
    # Set to a small number to re-enable once column reconstruction from OCR
    # tokens is verified against more scanned samples.
    # Enabled: grid reconstruction now reconciles on the scanned sample
    # (26/26 rows), so the path is trustworthy for short statements. Long
    # scans still belong on the asynchronous queue, hence the low cap.
    return _int_env("BANK_STATEMENT_INLINE_OCR_PAGES", 4)


def time_budget_ms() -> int:
    """
    Wall-clock budget for parsing.

    Without this a very long statement runs until the orchestrator's timeout
    fires and the caller gets a 502 with nothing at all. Stopping early and
    returning the rows parsed so far is strictly more useful: the caller sees
    real transactions plus a warning saying the document was truncated.
    """
    return _int_env("BANK_STATEMENT_TIME_BUDGET_MS", 25_000)


def _page_texts(path: str) -> tuple[list[str], int]:
    """
    Extract text per page.

    pypdf is tried first because it is markedly faster: on a 53-page HDFC
    statement it returned the full text in ~2.2s against ~14.7s for
    pdfplumber, and the row parser works from line text rather than cell
    geometry, so the extra layout fidelity buys nothing here.

    pdfplumber is kept as a fallback for PDFs whose text pypdf cannot reach.
    """
    from pypdf import PdfReader

    texts: list[str] = []
    try:
        reader = PdfReader(path)
        texts = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        logger.debug("pypdf extraction failed (%s); falling back", exc)
        texts = []

    with_text = sum(1 for t in texts if len(t.strip()) >= MIN_CHARS_PER_PAGE)

    # Only pay for pdfplumber when pypdf found nothing usable.
    if texts and with_text == 0:
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                plumbed = [(page.extract_text() or "") for page in pdf.pages]
            plumbed_with_text = sum(
                1 for t in plumbed if len(t.strip()) >= MIN_CHARS_PER_PAGE
            )
            if plumbed_with_text > 0:
                return plumbed, plumbed_with_text
        except Exception as exc:
            logger.debug("pdfplumber fallback failed: %s", exc)

    return texts, with_text


def extract_via_tables(
    path: str, started: float
) -> tuple[list[Transaction], bool]:
    """
    Read transactions from real table cells.

    This is the correct path for any statement whose PDF carries a ruled
    table, which covers every digital statement seen. Text-line parsing is
    kept only as a fallback for layouts without table structure.

    Returns (rows, budget_exhausted). THE SECOND VALUE MATTERS. Abandoning
    this path because the document has no table structure and abandoning it
    because the clock ran out look identical from the outside -- an empty or
    short list either way -- and the caller then falls back to text-line
    parsing and reports whatever arithmetic that produces. On a statement
    that HAS a table, the fallback's numbers are not a second opinion; they
    are a worse reading of the same page. Saying which reason applied is what
    lets the caller report "we could not parse this" instead of "this
    statement does not add up".
    """
    try:
        import pdfplumber
    except ImportError:
        logger.debug("pdfplumber unavailable; falling back to text parsing.")
        return [], False

    from app.agents.bank_statement import tables as T

    out: list[Transaction] = []
    mapping: dict[str, int] | None = None
    carry: dict | None = None
    cap = page_cap()
    budget = time_budget_ms()
    exhausted = False

    try:
        with pdfplumber.open(path) as pdf:
            for index, page in enumerate(pdf.pages, start=1):
                if index > cap:
                    exhausted = True
                    break
                if (time.perf_counter() - started) * 1000 > budget:
                    exhausted = True
                    break
                try:
                    table = page.extract_table()
                except Exception:
                    continue
                if not table:
                    continue

                if mapping is None:
                    found = T.find_header_row(table)
                    if found:
                        mapping, header_index = found
                        body = table[header_index + 1:]
                    else:
                        # Header unreadable: infer the layout from the data,
                        # which is regular even when the caption row is not.
                        mapping = T.infer_columns(table)
                        if not mapping:
                            continue
                        body = table
                else:
                    # Later pages repeat the header; skip it when present.
                    body = table
                    if T.map_columns(table[0]).get("date") is not None and not (
                        table[0] and (table[0][mapping["date"]] or "").strip()
                        and any(ch.isdigit() for ch in (table[0][mapping["date"]] or ""))
                    ):
                        body = table[1:]

                # Header detection runs on the UNSPLIT table -- a wrapped
                # two-line header ("Value\nDate") still matches as one
                # caption. Only the body needs expanding: a borderless
                # table's data cells arrive multi-line, one row per
                # transaction glued together with no ruling to separate
                # them.
                # Balance is the more reliable anchor: it changes on every
                # transaction and pdfplumber never merges two balance figures
                # onto one line, whereas the date column can (two same-day
                # transactions occasionally read as one date line, silently
                # dropping a row when date was used as the anchor).
                anchor = mapping.get("balance", mapping.get("date"))
                body = T.split_merged_cells(body, anchor_col=anchor)

                page_rows, carry = T.rows_to_transactions(
                    body, mapping, index, carry
                )
                out.extend(page_rows)
            if carry:
                out.extend(T.flush(carry, len(pdf.pages)))

        # Verify the debit/credit assignment against the running balance
        # instead of trusting the column order.
        out, swapped = T.orient_movements(out)
        if swapped:
            logger.info("Debit/credit columns were swapped; corrected by balance check.")

        # A whole-column swap (above) is one failure mode; a borderless
        # table's debit/credit VALUES landing on the wrong ROW entirely is
        # another. Balance-derived deltas correct that row-by-row, and are
        # a no-op wherever the existing values already agree.
        out, derived = T.derive_movements_from_balance(out)
        if derived:
            logger.info(
                "%d row(s) had their debit/credit re-derived from the "
                "running balance rather than trusted from column position.",
                derived,
            )
    except Exception as exc:
        logger.warning("Table extraction failed: %s", exc)
        return [], exhausted

    return out, exhausted


def _header_text(texts: list[str]) -> str:
    """
    Page one, in full.

    The issuer's letterhead is not reliably the first thing pypdf extracts:
    on a real HDFC statement it was line 50 of 73 on page one, with the
    transaction table's own header and first rows extracted BEFORE it,
    despite the letterhead being what a person sees first when looking at
    the page. Restricting to the first several lines therefore missed the
    letterhead outright on that statement.

    Scanning the whole of page one is safe here because `detect_bank` now
    excludes UPI-handle-embedded matches ("@okhdfcbank"), which was the
    original reason this was narrowed to a small window in the first place
    -- with that false-positive source closed off, there is no need to
    withhold the rest of the page from the search.
    """
    if not texts:
        return ""
    return texts[0]


def extract_via_grid(path: str, started: float) -> list[Transaction]:
    """
    Read a scanned statement by reconstructing its printed table grid.

    Rasterise, find the ruled lines, OCR the page once, then place each token
    into the cell its centre falls in. From there the same column mapping and
    balance verification used for digital PDFs applies unchanged.
    """
    try:
        import numpy as np
        from pdf2image import convert_from_path

        from app.agents.bank_statement import grid as G
        from app.agents.bank_statement import tables as T
        from app.agents.document_agent import preprocess as PP
        from app.agents.document_agent.ocr import get_engine
    except ImportError as exc:
        logger.debug("Grid extraction unavailable: %s", exc)
        return []

    try:
        images = convert_from_path(path, dpi=200)
    except Exception as exc:
        logger.warning("Could not rasterise PDF: %s", exc)
        return []

    engine = get_engine()
    out: list[Transaction] = []
    mapping: dict[str, int] | None = None
    carry: dict | None = None
    cap = page_cap()
    budget = time_budget_ms()

    for index, image in enumerate(images, start=1):
        if index > cap or (time.perf_counter() - started) * 1000 > budget:
            break

        rgb = image.convert("RGB")
        try:
            row_ys, col_xs = G.detect_grid(np.array(image.convert("L")))
        except Exception as exc:
            logger.warning("Grid detection failed on page %d: %s", index, exc)
            continue
        if len(col_xs) < 3 or len(row_ys) < 3:
            continue

        try:
            tokens, _ = engine.read_array(PP.to_array(rgb))
        except Exception as exc:
            logger.warning("OCR failed on page %d: %s", index, exc)
            continue

        cells = G.cells_from_tokens(tokens, row_ys, col_xs)
        if not cells:
            continue

        if mapping is None:
            found = T.find_header_row(cells)
            if found:
                mapping, header_index = found
                body = cells[header_index + 1:]
            else:
                mapping = T.infer_columns(cells)
                if not mapping:
                    continue
                body = cells
        else:
            body = cells

        page_rows, carry = T.rows_to_transactions(body, mapping, index, carry)
        out.extend(page_rows)

    if carry:
        from app.agents.bank_statement import tables as T2

        out.extend(T2.flush(carry, len(images)))

    if out:
        from app.agents.bank_statement import tables as T3

        out, swapped = T3.orient_movements(out)
        if swapped:
            logger.info("Debit/credit corrected by balance check (scanned).")
    return out


def _ocr_pages(path: str) -> list[str]:
    """
    Rasterise and OCR a small scanned PDF.

    Deliberately bounded by the caller: this is only reached for documents
    short enough that the cost is acceptable in a synchronous request.
    """
    try:
        from pdf2image import convert_from_path

        from app.agents.document_agent import preprocess as PP
        from app.agents.document_agent.ocr import get_engine
    except ImportError as exc:
        logger.debug("Inline OCR unavailable: %s", exc)
        return []

    try:
        images = convert_from_path(path, dpi=200)
    except Exception as exc:
        logger.warning("Could not rasterise PDF for inline OCR: %s", exc)
        return []

    engine = get_engine()
    out: list[str] = []
    for image in images:
        try:
            tokens, _ = engine.read_array(
                PP.to_array(PP.standard(image.convert("RGB")))
            )
        except Exception as exc:
            logger.warning("Inline OCR failed on a page: %s", exc)
            out.append("")
            continue
        # Rebuild reading order: group tokens into lines by vertical position,
        # because the row parser works from lines, not scattered tokens.
        out.append(_tokens_to_lines(tokens))
    return out


def _tokens_to_lines(tokens) -> str:
    """Group OCR tokens into text lines by vertical proximity."""
    if not tokens:
        return ""
    ordered = sorted(tokens, key=lambda t: (t.cy, t.x0))
    lines: list[list] = [[ordered[0]]]
    for token in ordered[1:]:
        last = lines[-1][-1]
        tolerance = max(6.0, last.height * 0.6)
        if abs(token.cy - last.cy) <= tolerance:
            lines[-1].append(token)
        else:
            lines.append([token])
    return "\n".join(
        " ".join(t.text for t in sorted(line, key=lambda x: x.x0))
        for line in lines
    )


def classify_source(pages: int, with_text: int) -> SourceKind:
    if with_text == 0:
        return SourceKind.SCANNED
    if with_text < pages * 0.6:
        return SourceKind.MIXED
    return SourceKind.DIGITAL


def _rows_from_page(text: str, page_no: int) -> list[Transaction]:
    """
    Assemble transactions from one page.

    A line beginning with a date opens a row; lines that follow without a date
    are narration continuation and are folded into the open row. This is what
    makes multi-line UPI and NEFT descriptions survive intact.
    """
    rows: list[Transaction] = []
    current: dict | None = None

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        date_text = P.is_transaction_start(stripped)
        if date_text:
            parsed = P.parse_date(date_text)
            if parsed is None:
                continue
            if current:
                rows.append(_finalise(current, page_no))

            rest = stripped[len(date_text):].strip()
            # A second leading date is the value date.
            value_date = None
            second = P.is_transaction_start(rest)
            if second:
                value_date = P.parse_date(second)
                if value_date:
                    rest = rest[len(second):].strip()

            current = {
                "date": parsed,
                "value_date": value_date,
                "narration_parts": [],
                "amounts": [],
                "line": rest,
            }
            _absorb(current, rest)
        elif current is not None:
            _absorb(current, stripped)

    if current:
        rows.append(_finalise(current, page_no))
    return rows


def _absorb(row: dict, text: str) -> None:
    """Split a line into trailing amounts and leading narration."""
    # Narration text is noisy -- UPI references, UTR numbers, timestamps --
    # and _AMOUNT_RE's bare-digit fallback (fine for an isolated table cell)
    # picks up fragments of all of it here. The stricter narration-safe
    # pattern requires a decimal point or thousands grouping, which every
    # genuine amount in these statements has and no reference number does.
    amounts = [m for m in P._AMOUNT_IN_TEXT_RE.finditer(text)]
    if amounts:
        first_money = amounts[0].start()
        narration = text[:first_money].strip()
        for match in amounts:
            value = P.parse_amount(match.group(0))
            if value is not None:
                row["amounts"].append(value)
    else:
        narration = text
    narration = re.sub(r"\s+", " ", narration).strip(" -|")
    if narration:
        row["narration_parts"].append(narration)


def _finalise(row: dict, page_no: int) -> Transaction:
    """
    Turn accumulated pieces into a transaction.

    Column identity is inferred from position rather than guessed: on every
    layout seen, the balance is the LAST money value on the row, and the
    movement is the one before it. Which side that movement belongs to is left
    to the reconciliation pass, which can compare against the running balance.
    """
    amounts: list[Decimal] = row["amounts"]
    balance = amounts[-1] if amounts else None
    movement = amounts[-2] if len(amounts) >= 2 else None

    return Transaction(
        date=row["date"],
        value_date=row["value_date"],
        narration=" ".join(row["narration_parts"])[:300],
        debit=None,
        credit=None,
        balance=balance,
        page=page_no,
        reference=None,
    ) if movement is None else Transaction(
        date=row["date"],
        value_date=row["value_date"],
        narration=" ".join(row["narration_parts"])[:300],
        debit=movement,
        credit=None,
        balance=balance,
        page=page_no,
        reference=None,
    )


def _assign_sides(rows: list[Transaction]) -> int:
    """
    Decide debit vs credit from the running balance.

    A movement is a credit when the balance rose and a debit when it fell.
    This is derived evidence rather than a column guess, so it works across
    layouts that order the money columns differently. Returns the number of
    rows that could be resolved.
    """
    resolved = 0
    previous: Decimal | None = None
    for row in rows:
        movement = row.debit
        if movement is not None and row.balance is not None and previous is not None:
            if row.balance > previous:
                row.credit, row.debit = movement, None
            else:
                row.credit, row.debit = None, movement
            resolved += 1
        if row.balance is not None:
            previous = row.balance
    return resolved


def _drop_date_outliers(
    rows: list[Transaction],
) -> tuple[list[Transaction], int]:
    """
    Remove rows whose date sits far outside the statement's own period.

    A single misread year turns a six-month statement into a ten-year one
    ("125 months covered" on a real HDFC file), which then corrupts the period
    and any downstream averaging. The bulk of rows agree on the true window,
    so the median month is a reliable anchor: anything more than a year away
    from it is a parse error, not a transaction.
    """
    if len(rows) < 10:
        return rows, 0

    ordinals = sorted(r.date.toordinal() for r in rows)
    median = ordinals[len(ordinals) // 2]
    kept = [r for r in rows if abs(r.date.toordinal() - median) <= 400]
    return kept, len(rows) - len(kept)


def extract_bank_statement(path: str) -> BankStatementResult:
    """Entry point. Digital statements parse here; scans are routed out."""
    started = time.perf_counter()

    if not Path(path).exists():
        return BankStatementResult(
            status=ExtractionStatus.FAILED,
            source_kind=SourceKind.SCANNED,
            errors=[f"File not found: {path}"],
        )

    try:
        texts, with_text = _page_texts(path)
    except Exception as exc:
        logger.exception("Could not open statement")
        return BankStatementResult(
            status=ExtractionStatus.FAILED,
            source_kind=SourceKind.SCANNED,
            errors=[f"Could not open PDF: {type(exc).__name__}: {exc}"],
            processing_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    pages = len(texts)
    kind = classify_source(pages, with_text)
    joined = "\n".join(texts)

    result = BankStatementResult(
        status=ExtractionStatus.SUCCESS,
        source_kind=kind,
        bank=P.detect_bank(_header_text(texts)),
        account_number_masked=P.mask_account(joined),
        pages=pages,
        pages_with_text=with_text,
    )

    # A scanned statement is handled by extract_via_grid below, which OCRs
    # each page once and places tokens into reconstructed table cells. An
    # earlier full-page OCR pass here ran the recogniser a SECOND time on the
    # same pages, doubling the cost and consuming the time budget the grid
    # path then needed.

    if kind is SourceKind.SCANNED and pages > inline_ocr_pages():
        result.status = ExtractionStatus.REQUIRES_OCR
        result.warnings.append(
            f"No text layer on any of {pages} pages. OCR of a document this "
            "size does not belong in a synchronous request; route it to the "
            "asynchronous extraction queue."
        )
        result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
        return result

    # Table extraction only applies to a PDF that carries a text layer.
    # Running it on a scan finds nothing and burns the time budget that the
    # OCR path then needs -- which left one two-page statement at 105s with
    # zero rows.
    rows: list[Transaction] = []
    column_aware = False
    if kind is SourceKind.SCANNED and pages <= inline_ocr_pages():
        rows = extract_via_grid(path, started)
        if rows:
            column_aware = True
            result.warnings.append(
                f"Scanned statement: {pages} page(s) read by reconstructing "
                "the printed table grid."
            )
    table_budget_exhausted = False
    if not rows and kind is not SourceKind.SCANNED:
        rows, table_budget_exhausted = extract_via_tables(path, started)
        if rows:
            column_aware = True
            result.warnings.append("Rows read from table cells.")
    cap = page_cap()
    budget = time_budget_ms()
    truncated_at: int | None = None

    if kind is SourceKind.SCANNED and not rows:
        result.status = ExtractionStatus.REQUIRES_OCR
        result.warnings.append(
            f"Scanned statement of {pages} page(s): no table grid could be "
            "reconstructed. Route it to the asynchronous extraction queue."
        )
        result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
        return result

    # Text-line parsing is a fallback only: it runs when the PDF had no
    # usable table structure. Column positions are always preferred.
    #
    # IT IS NOT A FALLBACK FOR RUNNING OUT OF TIME. A statement whose table
    # path was abandoned mid-way HAS a table; parsing its text lines instead
    # produces rows whose debit and credit sides are inferred from the running
    # balance rather than read from the bank's own columns, and on a long
    # statement that inference goes wrong. The arithmetic then fails, and the
    # failure reads as "this statement does not add up" -- an adverse finding
    # about the customer's document, caused entirely by our own clock.
    #
    # Measured on samples/real_batch/bank_generic.pdf:
    #
    #   budget 25000ms  table path completes   1196 rows   reconciles
    #   budget  8000ms  table path completes    159 rows   reconciles
    #   budget  3000ms  table path abandoned   1339 rows   DOES NOT reconcile
    #
    # The third row is not a worse statement. It is the same statement read
    # badly, and before this it was reported as a reconciliation failure.
    if table_budget_exhausted and not rows:
        result.status = ExtractionStatus.PARTIAL
        result.warnings.append(
            "Parsing stopped before the table could be read: the time budget "
            "ran out. No transactions were extracted. This is a limit of this "
            "service, not a finding about the statement -- raise "
            "BANK_STATEMENT_TIME_BUDGET_MS or route the document to the "
            "asynchronous extraction queue."
        )
        result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
        return result

    text_source = [] if rows else list(enumerate(texts, start=1))
    # The budget for THIS loop starts now. Time already spent rasterising and
    # OCR'ing is real work, not overrun, and charging it here meant a scanned
    # statement was truncated before a single page was parsed.
    parse_started = time.perf_counter()
    for index, text in text_source:
        if index > cap:
            truncated_at = index - 1
            break
        if (time.perf_counter() - parse_started) * 1000 > budget:
            truncated_at = index - 1
            break
        if len(text.strip()) < MIN_CHARS_PER_PAGE:
            continue
        rows.extend(_rows_from_page(text, index))

    rows.sort(key=lambda r: (r.date, r.page))
    rows, dropped = _drop_date_outliers(rows)

    # Sides are inferred from the running balance ONLY for rows that came from
    # text-line parsing. Rows read from table cells or a reconstructed grid
    # already carry the bank's own debit and credit columns, and re-deriving
    # them made correct output look unresolved: 27 verified rows were reported
    # as "resolved for 17 of 27" and the whole statement marked PARTIAL.
    resolved = len(rows) if column_aware else _assign_sides(rows)

    result.transactions = rows
    result.transaction_count = len(rows)

    if rows:
        result.period = StatementPeriod(
            start=rows[0].date,
            end=rows[-1].date,
            months_covered=round((rows[-1].date - rows[0].date).days / 30.44, 1),
        )
        # THE BASELINE COMES FROM THE DOCUMENT WHERE THE DOCUMENT STATES IT.
        #
        # _assign_sides settles which SIDE a movement is on from the running
        # balance, but keeps the magnitude the column parse produced and
        # cannot touch row one at all -- it has no previous balance to
        # compare against. Both gaps showed up on one real statement: the
        # first row kept the wrong side (moving the derived opening by twice
        # its amount) and the last row kept a magnitude that was actually the
        # balance figure beside it.
        #
        # derive_movements_from_balance already solves exactly this, by
        # arithmetic rather than alignment; it was simply never run on this
        # path. Seeding it with the PRINTED opening extends it to row one.
        printed_opening = P.find_printed_opening_balance(joined)

        if printed_opening is not None:
            from app.agents.bank_statement.tables import (
                derive_movements_from_balance,
            )

            rows, rederived = derive_movements_from_balance(
                rows, printed_opening
            )
            if rederived:
                logger.info(
                    "%d row(s) had their movement re-derived from the running "
                    "balance against the printed opening balance.",
                    rederived,
                )

        balances = [r.balance for r in rows if r.balance is not None]
        if balances:
            if printed_opening is not None:
                opening = printed_opening
            else:
                # Nothing printed: the first row's balance is the balance
                # AFTER that transaction, so the baseline is that figure with
                # the first movement removed. Using it directly left
                # reconciliation short by exactly the first row's amount.
                first = next((r for r in rows if r.balance is not None), None)
                opening = balances[0]
                if first is not None:
                    opening = (
                        opening
                        - (first.credit or Decimal(0))
                        + (first.debit or Decimal(0))
                    )
            result.opening_balance = opening
            result.opening_balance_printed = printed_opening is not None
            result.closing_balance = balances[-1]
        result.total_credit = sum(
            (r.credit for r in rows if r.credit is not None), Decimal(0)
        )
        result.total_debit = sum(
            (r.debit for r in rows if r.debit is not None), Decimal(0)
        )
        from app.agents.bank_statement.tables import reconcile

        # The one check a wrong column map cannot survive: if the movements
        # do not explain the change in balance, the parse is not trustworthy.
        printed = P.find_printed_closing_balance(joined)
        verdict, reason = reconcile(
            result.opening_balance,
            result.closing_balance,
            result.total_credit,
            result.total_debit,
            printed_closing=printed,
            truncated=truncated_at is not None or table_budget_exhausted,
            reached_end=P.has_end_marker(joined),
            # Every page of the file was read and nothing cut the parse
            # short, so there is no unread tail for rows to be missing from.
            # Independent of whether this bank prints a closing balance or
            # an end-of-statement line, which is the point.
            parsed_to_last_page=(
                truncated_at is None
                and not table_budget_exhausted
                and pages <= cap
            ),
        )
        result.balance_reconciles = verdict
        if reason:
            result.warnings.append(f"Not verified: {reason}")
        if verdict is not True:
            # An unverified statement must never read as complete.
            result.status = ExtractionStatus.PARTIAL

    if table_budget_exhausted and truncated_at is None:
        result.warnings.append(
            "The table parse was cut short by the time budget, so the rows "
            "below are incomplete and completeness could not be established. "
            "Not a finding about the statement."
        )

    if truncated_at is not None:
        result.status = ExtractionStatus.PARTIAL
        result.warnings.append(
            f"Stopped after page {truncated_at} of {pages}: hit the page cap "
            f"({cap}) or the {budget} ms budget. Rows up to that point are "
            "returned; raise BANK_STATEMENT_MAX_PAGES or route the document "
            "to the asynchronous queue for a full parse."
        )

    if dropped:
        result.warnings.append(
            f"{dropped} row(s) dropped: date far outside the statement period, "
            "which indicates a misread rather than a transaction."
        )

    if not rows:
        result.status = ExtractionStatus.PARTIAL
        result.warnings.append("Text layer present but no transaction rows recognised.")
    elif result.balance_reconciles:
        # The running balance already proves every movement is on the correct
        # side. The older per-row counter belongs to the text-parsing path and
        # under-reports on the grid path, which was marking a fully reconciled
        # statement PARTIAL.
        result.status = ExtractionStatus.SUCCESS
    elif resolved < len(rows) * 0.8:
        result.status = ExtractionStatus.PARTIAL
        result.warnings.append(
            f"Debit/credit side resolved for {resolved} of {len(rows)} rows; "
            "the running balance does not reconcile, so treat these rows as "
            "unverified."
        )

    if kind is SourceKind.MIXED:
        result.warnings.append(
            f"Only {with_text} of {pages} pages carry text; the remainder need OCR."
        )

    result.processing_ms = round((time.perf_counter() - started) * 1000, 2)
    return result


__all__ = ["extract_bank_statement", "classify_source"]
