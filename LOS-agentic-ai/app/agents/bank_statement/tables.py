"""
Column-aware row assembly.

An earlier version read each line as text and took "the last two numbers" as
the movement and the balance. That is unsafe: narrations carry reference
numbers, so a line reading

    UPI/DR/606004360436/JUICE CO/HDFC/vyapar.168/Sent  54.00  51.41

yields the amounts 606, 004, 360, 436, 168, 54.00, 51.41 and the wrong two
win. Columns cannot be inferred from text; they have to come from position.

Two sources provide that position, and both feed the same mapping code:
  * digital PDFs -- pdfplumber returns real table cells
  * scanned pages -- OCR tokens carry x coordinates
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.agents.bank_statement.parse import parse_amount, parse_date

# A money cell holds ONLY a number. Narrations contain digits too --
# "UPI/CR/606004360436" -- and a loose test classified the narration column as
# money, which then shifted every other column. Requiring the whole cell to be
# numeric is what separates an amount from a reference number.
_PURE_AMOUNT = re.compile(r"^[\u20b9]?\s*-?\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?$|^-?\d+(?:\.\d{1,2})?$")


def is_money_cell(text: str) -> bool:
    value = (text or "").strip()
    if value in {"-", "--", ""}:
        return True          # an empty movement column, still a money column
    return bool(_PURE_AMOUNT.match(value))
from app.agents.bank_statement.schemas import Transaction

# Header captions per logical column. Matched on a normalised form so
# "Withdrawal (Dr)" and "WITHDRAWALS" both land on the same column.
_HEADERS: dict[str, tuple[str, ...]] = {
    "date": ("txndate", "transactiondate", "trandate", "postdate", "date"),
    "value_date": ("valuedate", "valuedt", "valdate"),
    "narration": ("description", "particulars", "narration", "remarks",
                  "transactionremarks", "details"),
    "reference": ("refno", "reference", "chqno", "chequeno", "instrument"),
    "debit": ("withdrawals", "withdrawal", "withdraw", "debit", "dr", "paidout"),
    "credit": ("deposits", "deposit", "credit", "cr", "paidin"),
    "balance": ("closingbalance", "balance", "runningbalance"),
    # The statement's own row numbering. Not every bank prints one; where it
    # does, a gap in it is proof of a row the parser did not read.
    "serial": ("srno", "slno", "sno", "serialno", "serial", "txnno", "transactionno",
               "seqno", "sequence"),
}

#: How a movement value came to sit on its row (Transaction.placement).
CELL, LINE = "CELL", "LINE"
BALANCE_ORDERED, UNRESOLVED_ORDER, POSITIONAL = ("BALANCE_ORDERED", "UNRESOLVED_ORDER",
                                                 "POSITIONAL")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def map_columns(header_row: list[str | None]) -> dict[str, int]:
    """
    Map logical fields to column indices using the statement's own header.

    Longer captions are tested first so "closing balance" is not claimed by
    the shorter "balance", and each column is assigned at most once.
    """
    mapping: dict[str, int] = {}
    taken: set[int] = set()

    ordered = sorted(
        _HEADERS.items(),
        key=lambda kv: -max(len(s) for s in kv[1]),
    )
    # A bare "#" / "No." caption is a serial column; it normalises to
    # nothing, so it is matched before the letters-only comparison.
    for index, cell in enumerate(header_row):
        if (cell or "").strip().lower() in {"#", "no.", "no", "sr.", "sl."}:
            mapping["serial"] = index
            taken.add(index)
            break

    for field, captions in ordered:
        if field in mapping:
            continue
        for index, cell in enumerate(header_row):
            if index in taken:
                continue
            key = _norm(cell or "")
            if not key:
                continue
            if any(key == c or (len(c) > 3 and c in key) for c in captions):
                mapping[field] = index
                taken.add(index)
                break
    return mapping


def split_merged_cells(
    rows: list[list[str | None]], date_col: int | None = None,
    anchor_col: int | None = None,
) -> list[list[str | None]]:
    """
    Expand a borderless table's multi-line cells into one row per line.

    pdfplumber's table detector needs ruled lines to find row boundaries. A
    statement with no ruling still has clean COLUMNS, but every cell in a
    column comes back as one multi-line block with the row boundary lost.

    Naively zipping every column's lines by index breaks the moment
    Description wraps MORE times than Date or the amount columns -- which it
    routinely does, carrying address and reference details that only appear
    under Description. On a real Standard Chartered statement this produced
    694 misaligned rows where narration text belonged to one transaction and
    the amounts sitting beside it belonged to another.

    The BALANCE column is the anchor, not the date: a running balance
    changes on every single transaction, so its line count is a reliable
    per-transaction count. The date column looked like the natural choice
    but is not reliable here -- pdfplumber's line-splitting sometimes
    merges two SAME-DATE transactions onto what reads as one date line
    (6 date-lines against 10 genuine transactions, confirmed against the
    balance column's 10, on a real statement), which silently dropped
    rows when date was used as the anchor. Every other column is consumed
    in that same proportion; any surplus -- routinely Description, which
    carries multi-line addresses and references no other column repeats --
    becomes continuation text appended to whichever transaction is
    currently open, not a new row.
    """
    if anchor_col is None:
        anchor_col = date_col
    if anchor_col is None:
        return _split_naive(rows)

    expanded: list[list[str | None]] = []
    for row in rows:
        split_cells = [(cell or "").split("\n") for cell in row]
        anchor_lines = [l for l in split_cells[anchor_col] if l.strip()] if anchor_col < len(split_cells) else []

        if len(anchor_lines) <= 1:
            expanded.append(row)
            continue

        count = len(anchor_lines)
        new_rows = [[""] * len(row) for _ in range(count)]
        for col_index, lines in enumerate(split_cells):
            content = [l for l in lines if l.strip()] or [""]
            if col_index == anchor_col:
                for i in range(count):
                    new_rows[i][col_index] = content[i] if i < len(content) else ""
                continue
            # Distribute this column's lines across the `count` transactions
            # in order; any lines beyond `count` are continuation text and
            # are appended to the LAST transaction rather than starting a
            # new, misaligned row.
            for i in range(min(count, len(content))):
                new_rows[i][col_index] = content[i]
            if len(content) > count:
                extra = " ".join(content[count:])
                new_rows[-1][col_index] = (new_rows[-1][col_index] + " " + extra).strip()

        expanded.extend(r for r in new_rows if any((c or "").strip() for c in r))
    return expanded


def _direction(delta: Decimal) -> int:
    """The EXACT sign: a 0.45 charge is a movement, and only zero is none."""
    if delta > 0:
        return 1
    if delta < 0:
        return -1
    return 0


def place_by_balance_direction(
    balances: list[Decimal | None], debits: list[str], credits: list[str],
    prev_balance: Decimal | None,
) -> list[tuple[str, str]] | None:
    """
    Put a merged block's movement LINES on their rows, in column order.

    THE VALUES ARE READ; ONLY THEIR ORDER IS TRUSTED. pdfplumber hands a
    borderless table's Withdrawals column as one block of N lines and its
    Deposits column as another of M lines, both in document order, while
    the Balance column has one line per transaction. Which column the next
    transaction's amount came from is exactly what the balance direction
    says: the balance fell, so the next unconsumed withdrawal line is this
    row's; it rose, so the next deposit line is. Each line is used once, in
    order, and no figure is ever made up.

    Row one has no previous balance unless the caller knows it, so it may
    be a debit, a credit, or a movement-free line (a "balance forward"
    caption printed as a row). Every placement is then CHECKED: a placed
    value must equal the balance change on its row. Exactly one consistent
    placement is accepted; none, or more than one, returns None and the
    caller falls back to positional distribution and says so.
    """
    n = len(balances)
    if n == 0 or any(b is None for b in balances):
        return None
    if not debits and not credits:
        return [("", "")] * n

    directions: list[int | None] = [None] * n
    if prev_balance is not None:
        directions[0] = _direction(balances[0] - prev_balance)
    for i in range(1, n):
        directions[i] = _direction(balances[i] - balances[i - 1])

    first_options: list[int] = [directions[0]] if directions[0] is not None else [-1, 1, 0]
    solutions: list[list[tuple[str, str]]] = []
    for first in first_options:
        dirs = [first] + directions[1:]
        placed: list[tuple[str, str]] = []
        di = ci = 0
        ok = True
        for d in dirs:
            if d == -1:
                if di >= len(debits):
                    ok = False
                    break
                placed.append((debits[di], ""))
                di += 1
            elif d == 1:
                if ci >= len(credits):
                    ok = False
                    break
                placed.append(("", credits[ci]))
                ci += 1
            else:
                placed.append(("", ""))
        if not ok or di != len(debits) or ci != len(credits):
            continue
        # THE CHECK: every placed value must be the change its row shows.
        for i, (dt, ct) in enumerate(placed):
            if i == 0 and prev_balance is None:
                continue
            previous = balances[i - 1] if i else prev_balance
            delta = balances[i] - previous
            value = parse_amount(dt or ct)
            if dt or ct:
                if value is None or abs(abs(delta) - value) >= Decimal("1.00"):
                    ok = False
                    break
            elif delta != 0:
                ok = False
                break
        if ok:
            solutions.append(placed)
    return solutions[0] if len(solutions) == 1 else None


_TOTALS_LINE = re.compile(r"^\W*(?:grand\s+)?total", re.IGNORECASE)


def _ends_with_totals_line(split_cells: list[list[str]], mapping: dict[str, int],
                           balances: list[Decimal | None]) -> bool:
    """The block's last line is a TOTALS caption whose balance repeats the previous one."""
    narration_col = mapping.get("narration")
    if narration_col is None or narration_col >= len(split_cells) or len(balances) < 2:
        return False
    lines = [l for l in split_cells[narration_col] if l.strip()]
    if not lines or not _TOTALS_LINE.search(lines[-1]):
        return False
    return balances[-1] is not None and balances[-2] is not None \
        and abs(balances[-1] - balances[-2]) < Decimal("1.00")


def split_merged_cells_ordered(
    rows: list[list[str | None]], mapping: dict[str, int],
    prev_balance: Decimal | None = None,
) -> tuple[list[list[str | None]], list[str]]:
    """
    `split_merged_cells`, with the movement columns placed by column order
    and the balance direction (see `place_by_balance_direction`) instead of
    by line position. Returns the rows and, per row, how its movement was
    placed (CELL / BALANCE_ORDERED / UNRESOLVED_ORDER / POSITIONAL).
    """
    balance_col = mapping.get("balance")
    anchor = balance_col if balance_col is not None else mapping.get("date")
    debit_col, credit_col = mapping.get("debit"), mapping.get("credit")
    if anchor is None:
        out = _split_naive(rows)
        return out, [POSITIONAL] * len(out)

    expanded: list[list[str | None]] = []
    placements: list[str] = []
    for row in rows:
        split_cells = [(cell or "").split("\n") for cell in row]
        anchor_lines = ([l for l in split_cells[anchor] if l.strip()]
                        if anchor < len(split_cells) else [])
        if len(anchor_lines) <= 1:
            expanded.append(row)
            placements.append(CELL)
            if balance_col is not None and anchor_lines:
                bal = parse_amount(anchor_lines[0])
                if bal is not None:
                    prev_balance = bal
            continue

        count = len(anchor_lines)
        new_rows: list[list[str | None]] = [[""] * len(row) for _ in range(count)]
        movement_cols = {c for c in (debit_col, credit_col) if c is not None}
        ordered = balance_col is not None and bool(movement_cols)
        for col_index, lines in enumerate(split_cells):
            content = [l for l in lines if l.strip()] or [""]
            if col_index == anchor:
                for i in range(count):
                    new_rows[i][col_index] = content[i] if i < len(content) else ""
                continue
            if ordered and col_index in movement_cols:
                continue                      # placed below, by balance direction
            for i in range(min(count, len(content))):
                new_rows[i][col_index] = content[i]
            if len(content) > count:
                extra = " ".join(content[count:])
                new_rows[-1][col_index] = (new_rows[-1][col_index] + " " + extra).strip()

        placement = CELL
        if ordered:
            balances = [parse_amount(l) for l in anchor_lines]

            def lines_of(col: int | None) -> list[str]:
                if col is None or col >= len(split_cells):
                    return []
                return [l for l in split_cells[col] if l.strip()]

            debits, credits = lines_of(debit_col), lines_of(credit_col)
            placed = place_by_balance_direction(balances, debits, credits, prev_balance)
            if placed is None and _ends_with_totals_line(split_cells, mapping, balances):
                # A TOTALS LINE closes the table on its last page: the column
                # totals sit in the movement columns and the closing balance
                # is repeated. It is not a transaction. Placement is retried
                # without it, and the line itself is dropped.
                placed = place_by_balance_direction(
                    balances[:-1], debits[:-1] if debits else [],
                    credits[:-1] if credits else [], prev_balance)
                if placed is not None:
                    balances = balances[:-1]
                    new_rows = new_rows[:-1]
                    count -= 1
            if placed is not None:
                for i, (dt, ct) in enumerate(placed):
                    if debit_col is not None:
                        new_rows[i][debit_col] = dt
                    if credit_col is not None:
                        new_rows[i][credit_col] = ct
                placement = BALANCE_ORDERED
            else:
                # By position, and SAID SO: the values may sit on the wrong
                # rows, and reconciliation will report the disagreement.
                for col_index in movement_cols:
                    content = lines_of(col_index)
                    for i in range(min(count, len(content))):
                        new_rows[i][col_index] = content[i]
                placement = UNRESOLVED_ORDER
            if balances and balances[-1] is not None:
                prev_balance = balances[-1]

        kept = [r for r in new_rows if any((c or "").strip() for c in r)]
        expanded.extend(kept)
        placements.extend([placement] * len(kept))
    return expanded, placements


def _split_naive(rows: list[list[str | None]]) -> list[list[str | None]]:
    """Fallback when the date column is not yet known: split by line index."""
    expanded: list[list[str | None]] = []
    for row in rows:
        split_cells = [(cell or "").split("\n") for cell in row]
        height = max((len(c) for c in split_cells), default=1)
        if height <= 1:
            expanded.append(row)
            continue
        for line_index in range(height):
            new_row = [
                cell[line_index] if line_index < len(cell) else ""
                for cell in split_cells
            ]
            if any(c.strip() for c in new_row):
                expanded.append(new_row)
    return expanded


def find_header_row(rows: list[list[str | None]]) -> tuple[dict[str, int], int] | None:
    """The first row that names a date column and two money columns."""
    for index, row in enumerate(rows[:12]):
        mapping = map_columns(row)
        money = {"debit", "credit", "balance"} & mapping.keys()
        if "date" in mapping and len(money) >= 2:
            return mapping, index
    return None


def is_header_row(row: list[str | None]) -> bool:
    """True when a row is a repeated column-caption row rather than data."""
    mapping = map_columns(row)
    money = {"debit", "credit", "balance"} & mapping.keys()
    return "date" in mapping and len(money) >= 2


def _cell(row: list[str | None], mapping: dict[str, int], field: str) -> str:
    index = mapping.get(field)
    if index is None or index >= len(row):
        return ""
    return (row[index] or "").strip()


def rows_to_transactions(
    rows: list[list[str | None]],
    mapping: dict[str, int],
    page_no: int,
    carry: dict | None = None,
    prev_balance: Decimal | None = None,
    source: str = "READ",
    placements: list[str] | None = None,
) -> tuple[list[Transaction], dict | None]:
    """
    Convert table rows into transactions.

    `source` says how the cells were obtained (READ: a digital PDF's cells;
    OCR: tokens off a scan) and is carried on every transaction as its
    amount provenance. `placements`, aligned with `rows`, says how each
    row's movement was placed (split_merged_cells_ordered).

    A row without a date continues the previous one: banks wrap long
    narrations across rows, and dropping those loses the payee. `carry` lets an
    open row continue onto the next page.

    `prev_balance` carries the running balance across page and carry
    boundaries so the debit/credit disambiguation below has a reference point
    even for the very first row of a page.
    """
    out: list[Transaction] = []
    open_row: dict | None = carry

    for row_index, row in enumerate(rows):
        placement = placements[row_index] if placements and row_index < len(placements) \
            else CELL
        if not any((c or "").strip() for c in row):
            continue

        # Banks repeat the caption row at the top of every page. Left in, it
        # is treated as narration and silently swallows the previous
        # transaction's continuation.
        if is_header_row(row):
            continue

        date_text = _cell(row, mapping, "date")
        parsed = parse_date(date_text) if date_text else None
        inherited = False

        if parsed is None:
            # A row with no date but ITS OWN balance and movement is a
            # transaction, not a continuation: two same-day transactions
            # print one date line, and the table extractor merges them.
            # Folding such a row into the previous narration silently
            # dropped its amount AND its balance. It takes the open row's
            # date, and says so.
            own_balance = (parse_amount(_cell(row, mapping, "balance"))
                           if "balance" in mapping else None)
            own_movement = (parse_amount(_cell(row, mapping, "debit")) is not None
                            or parse_amount(_cell(row, mapping, "credit")) is not None)
            if open_row is not None and own_balance is not None and own_movement:
                parsed = open_row["date"]
                inherited = True
            else:
                if open_row is not None:
                    extra = _cell(row, mapping, "narration")
                    if extra:
                        open_row["narration"] += " " + extra
                continue

        if open_row is not None:
            out.append(_build(open_row, page_no))
            if open_row.get("balance") is not None:
                prev_balance = open_row["balance"]

        debit = parse_amount(_cell(row, mapping, "debit"))
        credit = parse_amount(_cell(row, mapping, "credit"))
        balance = parse_amount(_cell(row, mapping, "balance"))

        # A real transaction is a debit OR a credit, never both. Reconstructing
        # a borderless table by zipping each column's lines by position can
        # place both a withdrawal AND a deposit value into the same row slot
        # when withdrawals and deposits are genuinely on DIFFERENT rows in the
        # source but their two columns wrap into different numbers of lines.
        # The running balance settles it: whichever side's magnitude actually
        # explains the change from the previous balance is the real one.
        side_source = "COLUMN"
        if debit is not None and credit is not None and balance is not None and prev_balance is not None:
            delta = balance - prev_balance
            if abs(delta - credit) < abs(delta - (-debit)):
                debit = None
            else:
                credit = None
            side_source = "BALANCE_DIRECTION"     # both are READ; the balance picked the side

        serial_text = _cell(row, mapping, "serial")
        serial = int(serial_text) if serial_text.isdigit() else None

        open_row = {
            "date": parsed,
            "value_date": parse_date(_cell(row, mapping, "value_date")),
            "narration": _cell(row, mapping, "narration").replace("\n", " "),
            "reference": _cell(row, mapping, "reference") or None,
            "debit": debit,
            "credit": credit,
            "balance": balance,
            "amount_source": source if (debit is not None or credit is not None) else "MISSING",
            "side_source": side_source,
            "placement": placement,
            "serial": serial,
            "date_inherited": inherited,
        }

    return out, open_row


def _build(row: dict, page_no: int) -> Transaction:
    return Transaction(
        date=row["date"],
        value_date=row["value_date"],
        narration=re.sub(r"\s+", " ", row["narration"]).strip()[:300],
        reference=row["reference"],
        debit=row["debit"],
        credit=row["credit"],
        balance=row["balance"],
        page=page_no,
        amount_source=row.get("amount_source", "READ"),
        side_source=row.get("side_source", "COLUMN"),
        placement=row.get("placement", CELL),
        serial=row.get("serial"),
        date_inherited=bool(row.get("date_inherited", False)),
    )


def annotate_against_balance(
    rows: list[Transaction], opening_balance: Decimal | None = None,
) -> tuple[int, int]:
    """
    DIAGNOSTIC ONLY. For each row, record what the running balance implies
    the movement was (`balance_delta`) and whether the READ amount agrees
    (`agrees_with_balance`). Nothing here changes a debit or a credit --
    the amount on a row is what was read, or None.

    Returns (rows that disagree, rows with no readable amount).
    """
    disagreeing = missing = 0
    previous: Decimal | None = opening_balance
    for row in rows:
        row.balance_delta = None
        row.agrees_with_balance = None
        if row.debit is None and row.credit is None:
            missing += 1
            row.amount_source = "MISSING"
        if row.balance is None:
            previous = None
            continue
        if previous is not None:
            delta = row.balance - previous
            row.balance_delta = delta
            movement = (row.credit or Decimal(0)) - (row.debit or Decimal(0))
            row.agrees_with_balance = abs(delta - movement) < Decimal("1.00")
            if not row.agrees_with_balance:
                disagreeing += 1
        previous = row.balance
    return disagreeing, missing


def serial_gaps(rows: list[Transaction]) -> int | None:
    """
    Rows the statement's own numbering proves were not read, or None when
    the statement prints no usable numbering (fewer than 80% of rows carry
    one, or it is not increasing).
    """
    serials = [r.serial for r in rows if r.serial is not None]
    if len(rows) < 2 or len(serials) < max(2, int(len(rows) * 0.8)):
        return None
    if any(b <= a for a, b in zip(serials, serials[1:])):
        return None
    return sum(b - a - 1 for a, b in zip(serials, serials[1:]))


def flush(carry: dict | None, page_no: int) -> list[Transaction]:
    """Emit a row left open at the end of the document."""
    return [_build(carry, page_no)] if carry else []


def reconcile(
    opening: Decimal | None,
    closing: Decimal | None,
    total_credit: Decimal,
    total_debit: Decimal,
    *,
    printed_closing: Decimal | None = None,
    truncated: bool = False,
    reached_end: bool = False,
    parsed_to_last_page: bool = False,
    missing_amounts: int = 0,
    disagreeing_rows: int = 0,
    rows_not_read: int | None = None,
    pages_unread: int = 0,
    scanned: bool = False,
) -> tuple[bool | None, str | None]:
    """
    Whether the extraction can be trusted. Returns (verdict, reason).

    ON INDEPENDENTLY READ AMOUNTS ONLY. `total_credit` and `total_debit` are
    sums of figures read off the document; no amount was derived from the
    balance. When a required amount could not be read, when the statement's
    own numbering proves rows were not read, or when a page carried no text
    the parser could read, the answer is None -- INCONCLUSIVE -- and the
    reason says which. A check that ran on incomplete evidence and passed
    would be exactly the self-confirmation this module must never produce.

    Two different questions have to be answered, and an earlier version
    answered only the first:

      1. Are the rows internally consistent? Opening plus credits minus debits
         must equal the closing figure.

      2. Are they COMPLETE? Question one cannot see missing rows. Drop the
         tail of a statement and the surviving chain still balances perfectly,
         because the closing figure is simply taken from whatever the last
         surviving row happened to be. A real extraction was reported as
         reconciled with 18 of 27 rows for exactly this reason.

    Completeness is settled by the balance the statement itself prints, which
    is the only figure on the document independent of the rows. Without it,
    and whenever the parse was cut short, the verdict is None -- unknown --
    rather than True. Claiming verification that was not performed is worse
    than admitting it could not be.
    """
    if truncated:
        return None, (
            "parsing stopped before the end of the document, so completeness "
            "could not be established"
        )

    if pages_unread:
        return None, (
            f"{pages_unread} page(s) carry no text layer and were not read, so "
            "the rows are incomplete and completeness could not be established"
        )

    if rows_not_read:
        return None, (
            f"the statement's own numbering shows {rows_not_read} row(s) were "
            "not read, so completeness could not be established"
        )

    if missing_amounts:
        return None, (
            f"{missing_amounts} row(s) have no readable amount, so the movements "
            "cannot be checked against the balance"
        )

    if opening is None or closing is None:
        return None, "no opening or closing balance was recovered"

    expected = opening + total_credit - total_debit
    if abs(expected - closing) >= Decimal("1.00"):
        if scanned:
            # OCR read these figures. A sum that does not add up says the
            # reading is uncertain, not that the document is wrong.
            return None, (
                "the figures read from this scan do not add up to its closing "
                "balance, so they could not be confirmed"
            )
        return False, (
            f"movements do not explain the balance: expected {expected}, "
            f"rows end at {closing}"
        )

    if disagreeing_rows:
        # The totals agree but individual rows do not: values sit on the
        # wrong rows, or errors cancel. Not something to certify.
        return None, (
            f"{disagreeing_rows} row(s) disagree with the running balance even "
            "though the totals match, so the rows could not be confirmed"
        )

    # A printed figure that equals the OPENING balance is an opening caption,
    # not a closing one. Statement headers label these inconsistently, and
    # treating an opening figure as the closing balance reported a correct
    # 694-row extraction as failed.
    if (
        printed_closing is not None
        and opening is not None
        and abs(printed_closing - opening) < Decimal("1.00")
    ):
        printed_closing = None

    if printed_closing is None:
        if reached_end:
            # No closing figure is printed, but the end-of-statement marker
            # was reached, so no rows are missing from the tail.
            return True, None

        if parsed_to_last_page:
            # THE LAYOUT-INDEPENDENT COMPLETENESS SIGNAL.
            #
            # No printed closing balance and no end-of-statement marker --
            # both of which are things a particular bank chooses to print --
            # but the parser read every page of the file and was not cut
            # short. The tail cannot be missing, because there is no tail
            # left unread.
            #
            # This is what the marker was a proxy for. Requiring the marker
            # itself sent a correctly-parsed Standard Chartered statement to
            # REVIEW for printing its pages differently from the banks the
            # marker list was built from, which is a parser limitation
            # reported as a doubt about the customer's document.
            #
            # It is not a weaker check. Dropped rows are already caught
            # above: `expected` is computed from the rows and compared with
            # the balance the LAST ROW states, so a gap anywhere breaks the
            # chain and returns False before this point is reached.
            return True, None

        return None, (
            "rows are internally consistent, but neither a closing balance "
            "nor an end-of-statement marker was found to confirm completeness"
        )

    if abs(printed_closing - closing) >= Decimal("1.00"):
        return False, (
            f"the statement prints a closing balance of {printed_closing} but "
            f"the extracted rows end at {closing}: rows are missing"
        )

    return True, None


__all__ = [
    "BALANCE_ORDERED", "CELL", "LINE", "POSITIONAL", "UNRESOLVED_ORDER",
    "annotate_against_balance", "find_header_row", "flush", "map_columns",
    "place_by_balance_direction", "reconcile", "rows_to_transactions", "serial_gaps",
    "split_merged_cells_ordered",
]


def infer_columns(rows: list[list[str | None]]) -> dict[str, int] | None:
    """
    Work out the column map from the DATA when the header is unusable.

    Statement headers are frequently lost in extraction -- one HDFC file
    yields ['', '', '', '', '', '', 'Balance'] -- while the data rows below
    are perfectly regular. The data is therefore the more reliable signal:

      * columns that parse as dates are the transaction and value dates
      * the last column that parses as money is the balance
      * the widest text column is the narration
      * money columns between narration and balance are the movements

    Which movement column is debit and which is credit is NOT guessed here.
    `orient_movements` decides that by testing both against the running
    balance, so the answer is verified rather than assumed.
    """
    sample = [r for r in rows if r and sum(1 for c in r if (c or "").strip()) >= 3]
    if len(sample) < 3:
        return None

    width = max(len(r) for r in sample)
    date_cols: list[int] = []
    money_cols: list[int] = []
    text_len: dict[int, int] = {}

    for col in range(width):
        values = [(r[col] or "").strip() for r in sample if col < len(r)]
        non_empty = [v for v in values if v and v not in {"-", "--"}]
        if not non_empty:
            continue

        dates = sum(1 for v in non_empty if parse_date(v) is not None)
        if dates >= len(non_empty) * 0.8:
            date_cols.append(col)
            continue

        money = sum(1 for v in values if is_money_cell(v))
        has_amount = sum(1 for v in non_empty if is_money_cell(v))
        if money >= len(values) * 0.9 and has_amount >= 1:
            money_cols.append(col)
            continue

        text_len[col] = sum(len(v) for v in non_empty) // max(1, len(non_empty))

    if not date_cols or len(money_cols) < 2:
        return None

    mapping: dict[str, int] = {"date": date_cols[0]}
    if len(date_cols) > 1:
        mapping["value_date"] = date_cols[1]
    if text_len:
        mapping["narration"] = max(text_len, key=text_len.get)

    mapping["balance"] = money_cols[-1]
    movements = money_cols[:-1]
    if len(movements) >= 2:
        mapping["debit"] = movements[-2]
        mapping["credit"] = movements[-1]
    elif movements:
        mapping["debit"] = movements[-1]
    return mapping


def orient_movements(
    rows: list[Transaction],
) -> tuple[list[Transaction], bool]:
    """
    Confirm which movement column is the debit by checking the balance.

    Both assignments are scored against the running balance and the better one
    kept. This turns an unverifiable guess into a measured decision: if the
    columns were swapped, the arithmetic says so.

    Returns the rows and whether swapping was applied.
    """
    def score(transactions: list[Transaction]) -> int:
        hits = 0
        previous: Decimal | None = None
        for row in transactions:
            if row.balance is None:
                continue
            if previous is not None:
                delta = row.balance - previous
                movement = (row.credit or Decimal(0)) - (row.debit or Decimal(0))
                if abs(delta - movement) < Decimal("1.00"):
                    hits += 1
            previous = row.balance
        return hits

    straight = score(rows)

    swapped = [r.model_copy(update={"debit": r.credit, "credit": r.debit}) for r in rows]
    crossed = score(swapped)

    if crossed > straight:
        return swapped, True
    return rows, False
