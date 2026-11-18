"""
A statement FILE -> what it says (StatementData) + what the agent found about the file (DocumentFacts).

    PDF  the existing bank statement agent (app/agents/bank_statement) reads the transactions and balances of a digital
         statement; the header (bank, account number, type, IFSC) is read here from its printed labels. A scan, a
         password-protected or a damaged file is reported as such -- the CPA enters the details by hand. Nothing is
         ever filled in from demo data for an uploaded file.
    CSV  '# key: value' header lines, then date,narration,reference,debit,credit,balance (deterministic fixtures).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import tempfile
from datetime import date, datetime
from pathlib import Path

from app.agents.credit.bank_verification import normalize
from app.agents.credit.bank_verification.config import cfg, say, section
from app.agents.credit.bank_verification.schemas import (BankDetails, Direction, DocumentFacts, ExtractionStatus,
                                                         StatementData, Transaction, money)


class UploadError(ValueError):
    """A file that cannot be accepted at all (wrong type, empty, too large). `code` is the API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sniff(content: bytes, file_name: str | None, allowed: list[str]) -> str:
    """The file's real type by its CONTENT ('pdf' / 'csv' / 'png' / 'jpg'), checked against `allowed`."""
    limits = section("uploads")
    if not content:
        raise UploadError("EMPTY_FILE", say("reader.empty"))
    if len(content) > int(limits.get("max_bytes", 10 * 1024 * 1024)):
        raise UploadError("FILE_TOO_LARGE", say("reader.too_large", mb=int(limits.get("max_bytes")) // (1024 * 1024)))
    if content.startswith(b"%PDF-"):
        kind = "pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif content.startswith(b"\xff\xd8\xff"):
        kind = "jpg"
    else:
        kind = "csv" if _looks_like_csv(content) else "unknown"
    suffix = Path(file_name or "").suffix.lower().lstrip(".")
    if kind == "unknown" or kind not in allowed:
        raise UploadError("UNSUPPORTED_FILE_TYPE", say("reader.wrong_type", types=", ".join(a.upper() for a in allowed)))
    if suffix and suffix != kind and not (kind == "jpg" and suffix == "jpeg"):
        raise UploadError("FILE_TYPE_MISMATCH", say("reader.type_mismatch", suffix=suffix, kind=kind.upper()))
    return kind


def _looks_like_csv(content: bytes) -> bool:
    try:
        text = content[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return "," in text and "\x00" not in text


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def fingerprint(data: StatementData) -> str | None:
    """The same account and the same transactions give the same fingerprint, whatever the file's bytes."""
    number = normalize.account_number(data.details.account_number)
    if not number or normalize.is_masked(number) or not data.transactions:
        return None
    rows = sorted(f"{t.date.isoformat()}|{t.amount}|{t.direction.value}" for t in data.transactions)
    return hashlib.sha256((number + "\n" + "\n".join(rows)).encode()).hexdigest()


def read(content: bytes, file_name: str | None, *, seeded: bool = False) -> tuple[StatementData | None, DocumentFacts]:
    """Read one statement file. Raises UploadError for a file that is not a statement type at all."""
    kind = sniff(content, file_name, list(section("uploads").get("statement_types") or ["pdf", "csv"]))
    facts = DocumentFacts(file_name=file_name, kind=kind.upper(), size=len(content), sha256=sha256(content),
                          seeded=seeded)
    data = _read_pdf(content, facts) if kind == "pdf" else _read_csv(content, facts)
    if data is not None:
        facts.fingerprint = fingerprint(data)
    return data, facts


# ---- PDF -------------------------------------------------------------------------------------------------------------

def _read_pdf(content: bytes, facts: DocumentFacts) -> StatementData | None:
    import pymupdf

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception:  # noqa: BLE001 - any parser failure is a damaged file, said plainly
        facts.extraction_status = ExtractionStatus.UNREADABLE
        facts.notes.append(say("reader.damaged"))
        return None
    with doc:
        facts.pages = doc.page_count
        facts.encrypted = bool(doc.needs_pass or doc.is_encrypted)
        facts.metadata = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
        if facts.encrypted:
            facts.extraction_status = ExtractionStatus.UNREADABLE
            facts.notes.append(say("reader.encrypted"))
            return None
        if facts.pages == 0:
            facts.extraction_status = ExtractionStatus.UNREADABLE
            facts.notes.append(say("reader.no_pages"))
            return None
        limit = int(section("uploads").get("max_pdf_pages") or 0)       # 0 / none: every page is read
        if limit and facts.pages > limit:
            facts.extraction_status = ExtractionStatus.UNREADABLE
            facts.notes.append(say("reader.too_many_pages", pages=facts.pages, limit=limit))
            return None
        texts = [page.get_text() or "" for page in doc]
    facts.pages_with_text = sum(1 for t in texts if t.strip())
    if facts.pages_with_text == 0:
        if scans_enabled():
            return _read_scan(content, facts)
        facts.extraction_status = ExtractionStatus.UNREADABLE
        facts.notes.append(say("reader.no_text"))
        return None
    details, period, opening, closing = _header("\n".join(texts[:1]), "\n".join(texts))
    data = StatementData(details=details, period_start=period[0], period_end=period[1],
                         opening_balance=opening, closing_balance=closing)
    if facts.pages_with_text < facts.pages:
        facts.notes.append(say("reader.pages_without_text", n=facts.pages - facts.pages_with_text, pages=facts.pages))
    else:
        _transactions_from_pdf(content, data, facts)
    facts.extraction_status = _status(data, facts)
    return data


def scans_enabled() -> bool:
    """Scanned statements are read by OCR (config scans.enabled; BANK_VERIFICATION_SCANS=off for fast tests)."""
    import os

    override = (os.getenv("BANK_VERIFICATION_SCANS") or "").strip().lower()
    if override:
        return override in ("1", "on", "true", "yes")
    return bool((section("scans") or {}).get("enabled", False))


def _extract(content: bytes):
    """The bank statement agent's reading of the file. Its page cap, time budget, date window and inline-OCR page
    limit are raised for THIS read only (config reader_limits): a long or scanned statement is read to its last page."""
    from app.agents.bank_statement import extract_bank_statement
    from app.agents.bank_statement.extract import raised_limits

    limits = {str(k): int(v) for k, v in (section("reader_limits") or {}).items()}
    with tempfile.TemporaryDirectory() as folder, raised_limits(limits):
        path = Path(folder) / "statement.pdf"
        path.write_bytes(content)
        return extract_bank_statement(str(path))


def _read_scan(content: bytes, facts: DocumentFacts) -> StatementData | None:
    """A scanned statement: the rows by the reader's OCR grid, the header (bank, account, type) from page 1's OCR."""
    facts.scanned = True
    if (section("scans") or {}).get("parallel_header", True):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(1, thread_name_prefix="bv-header-ocr") as pool:
            header = pool.submit(_ocr_first_page, content)
            result = _extract(content)
            header_text = header.result()
    else:
        result = _extract(content)
        header_text = _ocr_first_page(content)
    details, period, opening, closing = _header(header_text, header_text)
    data = StatementData(details=details, period_start=period[0], period_end=period[1],
                         opening_balance=opening, closing_balance=closing)
    _apply(result, data, facts)
    if not data.transactions and not any([details.bank_name, details.account_number]):
        facts.extraction_status = ExtractionStatus.UNREADABLE
        facts.notes.append(say("reader.scan_unreadable"))
        return None
    facts.pages_with_text = facts.pages                        # every page was read (by OCR)
    facts.notes.insert(0, say("reader.scan_read", pages=facts.pages))
    facts.extraction_status = _status(data, facts)
    return data


def _ocr_first_page(content: bytes) -> str:
    """Page 1 of a scan as text lines (the header the labels are read from); '' when OCR is unavailable."""
    try:
        import pymupdf
        from PIL import Image

        from app.agents.bank_statement.extract import _tokens_to_lines
        from app.agents.document_agent import preprocess as PP
        from app.agents.document_agent.ocr import get_engine

        with pymupdf.open(stream=content, filetype="pdf") as doc:
            pix = doc[0].get_pixmap(dpi=int((section("scans") or {}).get("header_dpi", 200)))
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        tokens, _ = get_engine().read_array(PP.to_array(PP.standard(image)))
        return _tokens_to_lines(tokens)
    except Exception as exc:  # noqa: BLE001 - no OCR engine / unreadable page: the header is entered by hand
        import logging

        logging.getLogger(__name__).warning("bank verification: header OCR failed (%s)", type(exc).__name__)
        return ""


def _transactions_from_pdf(content: bytes, data: StatementData, facts: DocumentFacts) -> None:
    """A digital statement's rows and balances, read by the bank statement agent."""
    _apply(_extract(content), data, facts)


def _apply(result, data: StatementData, facts: DocumentFacts) -> None:
    """The reader's rows, balances, holder and masked number into the statement data (read amounts only)."""
    rows = []
    for i, t in enumerate(result.transactions):
        amount, direction = (t.credit, Direction.CREDIT) if t.credit else (t.debit, Direction.DEBIT)
        if amount is None or amount <= 0:
            facts.notes.append(say("reader.row_no_amount", n=i + 1, date=t.date))
            if t.date:
                facts.unread_rows.append(t.date)
            continue
        rows.append(Transaction(date=t.date, amount=amount, direction=direction, balance=t.balance,
                                narration=t.narration, reference=t.reference, seq=i))
    data.transactions = rows
    if data.opening_balance is None and result.opening_balance_printed:
        data.opening_balance = money(result.opening_balance)
    if data.closing_balance is None and result.closing_balance is not None:
        data.closing_balance = money(result.closing_balance)
    if not data.details.account_holder and result.account_holder:
        data.details.account_holder = result.account_holder
    if not data.details.bank_name and result.bank:
        data.details.bank_name = result.bank                          # the reader's own bank detection
    if not data.details.account_number and result.account_number_masked:
        data.details.account_number = result.account_number_masked          # masked: rule 1 says what it can
    facts.notes += list(result.warnings)
    if result.rows_disagreeing_with_balance:
        facts.notes.append(say("reader.balance_disagrees", n=result.rows_disagreeing_with_balance))


def _status(data: StatementData, facts: DocumentFacts) -> ExtractionStatus:
    needed = list(cfg().get("reader_complete_requires") or ["bank_name", "account_number", "account_type"])
    complete = all(getattr(data.details, field) for field in needed) and bool(data.transactions) \
        and facts.pages_with_text == facts.pages
    return ExtractionStatus.COMPLETE if complete else ExtractionStatus.PARTIAL


def _squash(text: str) -> str:
    """Letters only, the usual OCR confusions folded on both sides (l / 1 -> i, 0 -> o): "ICICl" matches "ICICI"."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower()).translate(str.maketrans({"l": "i", "1": "i", "0": "o"}))


def _letterhead_bank(first_page: str) -> str | None:
    """The bank on the letterhead: a known bank (config known_banks) found in the first lines, spaces and case ignored
    (OCR joins words: "StateBankofIndia"); else the first line naming a bank, its joined words spaced again."""
    every = [ln.strip() for ln in first_page.splitlines() if ln.strip()]
    lines = every[:8]
    known = sorted((str(b) for b in cfg().get("known_banks") or []), key=len, reverse=True)
    # the letterhead first, then anywhere on page 1 (a logo-only letterhead: the name is in the address / footer)
    for line in lines + every[8:]:
        squashed = _squash(line)
        for name in known:
            if _squash(name) and _squash(name) in squashed:
                return name
    line = next((ln for ln in lines if re.search(r"(?i)bank", ln) and not re.search(r"(?i)statement|account|ifsc", ln)), None)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", line) if line else None


def _label_value(text: str, labels: list[str], pattern: str | None = None) -> str | None:
    """A labelled value from a statement header, as real statements print it: "A/C No : 1234" on one line (also in
    the middle of a line holding other labels), or the label alone with the value on the next line. With `pattern`
    the value must match it (the match is returned), else the next occurrence / label is tried."""
    lines = [ln.strip() for ln in str(text or "").splitlines()]
    for label in sorted(labels, key=len, reverse=True):
        # OCR often drops the space inside a label ("AccountNumber"): the label's own spaces are optional
        words = r"\s*".join(re.escape(part) for part in label.split())
        head = re.compile(rf"(?i)(?<![a-z]){words}(?![a-z])\.?\s*[:：\-–]?\s*")
        for i, line in enumerate(lines):
            found = head.search(line)
            if not found:
                continue
            rest = line[found.end():].strip(" :：-–")
            if not rest:                                     # the value on the next non-empty line
                rest = next((ln for ln in lines[i + 1:i + 3] if ln.strip()), "")
            rest = re.split(r"\s{3,}", rest)[0].strip()      # another label further along the same line
            if not rest:
                continue
            if pattern:
                value = re.search(pattern, rest.replace(" ", "") if "account" in label or "a/c" in label else rest)
                if value:
                    return value.group(0)
                continue
            return rest[:80]
    return None


def _date(text: str) -> date | None:
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _account_type(value: str | None) -> str | None:
    """The account type as the form writes it ("Savings"), from what the header printed. A scan's header often puts
    the labels on one line and their values on the next ("Account No CIF No Account Type" / ": 123 : 456 : SB GEN"):
    the last value is the type's. A value no account-type keyword names is not guessed -- None, the CPA types it."""
    if not value:
        return None
    from app.agents.credit.bank_verification import normalize

    last = re.split(r"[:：]", value)[-1].strip() or value
    canonical = normalize.account_type(last)
    if canonical not in (cfg().get("account_types") or {}):
        return None
    shown = {str(t).casefold(): str(t) for t in (cfg().get("form_suggestions") or {}).get("account_types") or []}
    return shown.get(canonical.casefold(), canonical.title())


def _unlabelled_holder(first_page: str) -> str | None:
    """The holder's name printed alone on the line above the customer id (config statement_holder), else None."""
    spec = section("statement_holder") or {}
    above = [str(x).lower() for x in spec.get("above_labels") or []]
    banned = {str(x).lower() for x in spec.get("not_a_name") or []}
    lines = [ln.strip() for ln in str(first_page or "").splitlines() if ln.strip()]
    for i, line in enumerate(lines[1:], start=1):
        if not any(re.match(rf"(?i){re.escape(label)}(?![a-z])", line) for label in above):
            continue
        name = lines[i - 1]
        words = name.split()
        if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,79}", name) and len(words) <= int(spec.get("max_words", 6)) \
                and not {w.lower().strip(".") for w in words} & banned:
            return name
        return None
    return None


def _header(first_page: str, whole: str) -> tuple[BankDetails, tuple[date | None, date | None], object, object]:
    labels = section("statement_labels")
    patterns = section("value_patterns")
    pick = {k: _label_value(first_page, list(v or []), patterns.get(k)) for k, v in labels.items()}
    bank = pick.get("bank_name")
    if not bank or not re.search(r"(?i)bank", bank):
        bank = _letterhead_bank(first_page) or bank
    number = pick.get("account_number")
    number = "".join(number.split()) if number else None
    if not pick.get("ifsc"):
        # an IFSC printed without a label (in the branch address block)
        loose = re.search(rf"\b{patterns.get('ifsc', '[A-Z]{4}0[A-Z0-9]{6}')}\b", first_page)
        pick["ifsc"] = loose.group(0) if loose else None
    if pick.get("ifsc"):
        pick["ifsc"] = pick["ifsc"].upper()
        if not bank:                                   # no bank name printed: the IFSC's bank (config ifsc_banks)
            bank = (cfg().get("ifsc_banks") or {}).get(pick["ifsc"][:4])
    period = (None, None)
    if pick.get("period"):
        parts = re.split(r"(?i)\s+to\s+|\s+-\s+", pick["period"])
        if len(parts) == 2:
            period = (_date(parts[0]), _date(parts[1]))
    closing = _label_value(whole, list(labels.get("closing_balance") or []))

    def amount(value: str | None):
        try:
            return money(value)
        except ValueError:
            return None

    details = BankDetails(bank_name=bank, account_number=number, account_type=_account_type(pick.get("account_type")),
                          ifsc=pick.get("ifsc"), account_holder=pick.get("account_holder") or _unlabelled_holder(first_page))
    return details, period, amount(pick.get("opening_balance")), amount(closing)


# ---- CSV -------------------------------------------------------------------------------------------------------------

def _read_csv(content: bytes, facts: DocumentFacts) -> StatementData | None:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        facts.extraction_status = ExtractionStatus.INVALID
        facts.notes.append(say("reader.csv_not_utf8"))
        return None
    header: dict[str, str] = {}
    body = []
    for line in text.splitlines():
        if line.startswith("#"):
            key, _, value = line[1:].partition(":")
            header[key.strip().lower()] = value.strip()
        elif line.strip():
            body.append(line)
    facts.pages = facts.pages_with_text = 1
    rows = list(csv.DictReader(io.StringIO("\n".join(body))))
    want = {"date", "debit", "credit"}
    if not rows or not want <= set(rows[0]):
        facts.extraction_status = ExtractionStatus.INVALID
        facts.notes.append(say("reader.csv_columns"))
        return None
    transactions = []
    for i, row in enumerate(rows):
        try:
            credit, debit = money(row.get("credit")), money(row.get("debit"))
            if bool(credit) == bool(debit):
                raise ValueError(say("reader.csv_one_side"))
            day = _date(str(row.get("date") or ""))
            if day is None:
                raise ValueError(say("reader.csv_bad_date", value=repr(row.get("date"))))
            transactions.append(Transaction(date=day, amount=credit or debit,
                                            direction=Direction.CREDIT if credit else Direction.DEBIT,
                                            balance=money(row.get("balance")), narration=row.get("narration") or "",
                                            reference=row.get("reference") or None, seq=i))
        except ValueError as exc:
            facts.notes.append(say("reader.csv_row", n=i + 1, problem=exc))
    period = (None, None)
    if header.get("period"):
        parts = re.split(r"(?i)\s+to\s+", header["period"])
        if len(parts) == 2:
            period = (_date(parts[0]), _date(parts[1]))
    try:
        opening, closing = money(header.get("opening_balance")), money(header.get("closing_balance"))
    except ValueError:
        opening = closing = None
        facts.notes.append(say("reader.csv_bad_balance"))
    data = StatementData(
        details=BankDetails(bank_name=header.get("bank_name"), account_number=header.get("account_number"),
                            account_type=header.get("account_type"), ifsc=header.get("ifsc"),
                            account_holder=header.get("account_holder")),
        transactions=transactions, opening_balance=opening, closing_balance=closing,
        period_start=period[0], period_end=period[1])
    facts.extraction_status = _status(data, facts)
    return data
