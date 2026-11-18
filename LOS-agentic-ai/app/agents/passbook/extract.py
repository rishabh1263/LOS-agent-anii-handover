"""
PASSBOOK FIELDS FROM OCR TOKENS -- deterministic, label-anchored, no LLM, nothing invented.

    bank_name, account_holder_name, account_number, ifsc_code, branch_name

Every field carries a status, a confidence and its evidence (the OCR text it came from, kept for audit and never
logged):

    PRESENT     read, well-formed
    MISSING     not printed / not found
    MASKED      printed masked (XXXXXXX1234) -- the visible digits are kept, the rest is NEVER reconstructed
    UNREADABLE  the label is there but its value is not legible
    INVALID     read, but not a possible value (an account number of 6 digits, a malformed IFSC)
    AMBIGUOUS   two different values for the same field on one page -- a person decides

OCR TEXT IS DATA. Nothing read from the page can change a rule, a threshold or a decision path; it only fills values.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agents.passbook import ifsc as IFSC

PRESENT, MISSING, MASKED, UNREADABLE, INVALID, AMBIGUOUS = (
    "PRESENT", "MISSING", "MASKED", "UNREADABLE", "INVALID", "AMBIGUOUS")

# A label at the start of a row (after OCR, upper-cased). Order matters: the more specific label first.
_LABELS: list[tuple[str, re.Pattern]] = [
    ("account_holder_name", re.compile(
        r"^(?:IN\s*THE\s*NAME\s*OF|NAME\s*OF\s*(?:THE\s*)?(?:ACCOUNT\s*HOLDER|A\s*/?\s*C\s*HOLDER|CUSTOMER)S?|"
        r"ACCOUNT\s*HOLDER(?:\s*NAME)?|A\s*/?\s*C\s*HOLDER(?:\s*NAME)?|CUSTOMER\s*NAME|HOLDER\s*NAME|NAME)\b")),
    ("account_number", re.compile(
        r"^(?:SB\s*|SAVINGS\s*|CURRENT\s*)?(?:A\s*/\s*C|AC|ACCOUNT|ACCT|ACC)\s*\.?\s*(?:NO|NUMBER|NUM|#)\b\.?")),
    ("ifsc_code", re.compile(r"^(?:IFSC|IFS)\s*(?:CODE)?\b")),
    # "Branch Phone No." / "Branch Address" / "Branch Code" / "Branch Manager" are other fields, never the branch
    # name (2026-10-17: "Branch Phone No" -> branch "…PUBLIC" on a real Union Bank passbook)
    # (whole words only: a branch NAMED "TELGI" / "PHALTAN" / "ADDANKI" is still a branch)
    ("branch_name", re.compile(r"^BRANCH(?:\s*NAME\b|\b(?!\s*(?:PHON\w*|PH\b|PH\.|ADD?R\w*|CODE\b|MANAG\w*|TEL\b|"
                               r"TEL\.|TELEPHONE|E-?MAIL|NO\b|NO\.|NUMBER)))")),
    ("customer_id", re.compile(r"^(?:CIF\s*(?:NO|NUMBER)?|CUSTOMER\s*(?:ID|NO|NUMBER))\b")),
    ("micr", re.compile(r"^MICR\s*(?:CODE)?\b")),
    ("mobile", re.compile(r"^(?:MOBILE|PHONE|MOB|TEL)\b")),
    ("other", re.compile(r"^(?:ADDRESS|ACCOUNT\s*TYPE|TYPE\s*OF\s*A\s*/?\s*C|PRODUCT|DATE\s*OF\s*OPENING|OPENED\s*ON|"
                         r"OPEN\s*DATE|A\s*/\s*C\s*OPEN\s*DATE|NOMINEE|NOMINATION|JOINT\s*HOLDER|EMAIL)\b")),
]
_SEPARATOR = re.compile(r"^[\s:;.\-–—=]+")
_MASK_RE = re.compile(r"^[X*•#xX]{2,}[X*•#xX\d\s-]*\d{2,6}$")
_DIGITISH = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}
# (HR / HRS: the OCR reads a printed "MR" / "MRS" as "HR" -- 2026-10-17, a real Union Bank passbook)
_HONORIFICS = re.compile(r"^(?:(?:MR|MRS|MS|MISS|SHRI|SMT|KUM|KUMARI|DR|M/S|HR|HRS)\.\s*|"
                         r"(?:MR|MRS|MS|MISS|SHRI|SMT|KUM|KUMARI|DR|M/S|HR|HRS)\s+)")
_NOT_VALUE_TOKEN = re.compile(r"^\(?(?:MR|MRS|MS|MISS|SHRI|SMT|KUM|KUMARI|DR|M/S|HR|HRS|I{1,3}|IV|[1-4])[.)]?$")
_PHONE_SHAPED = re.compile(r"(?:91)?[6-9]\d{9}|0\d{9,10}")
_NAME_OK = re.compile(r"^[A-Z][A-Z .'-]*[A-Z.]$")


@dataclass
class Field:
    value: str | None = None
    status: str = MISSING
    confidence: float = 0.0
    source: str | None = None              # label | pattern | ifsc_master | header
    evidence: str | None = None            # the OCR text (audit only; never logged)
    visible_digits: str | None = None      # MASKED: the digits that are printed
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("evidence", None)
        return out


@dataclass
class Row:
    tokens: list
    text: str
    top: float
    bottom: float
    confidence: float


def _nfkc(token):
    """Full-width punctuation the recogniser emits ("：", "，") as ASCII, on a copy of the token."""
    text = unicodedata.normalize("NFKC", token.text or "")
    return token if text == token.text else token.model_copy(update={"text": text})


def rows_of(tokens: list) -> list[Row]:
    """Tokens grouped into visual rows (vertical overlap), each left to right."""
    rows: list[list] = []
    for t in sorted((_nfkc(t) for t in tokens), key=lambda t: (t.cy, t.x0)):
        h = max(1.0, t.y1 - t.y0)
        if rows:
            last = rows[-1]
            cy = sum(x.cy for x in last) / len(last)
            lh = max(1.0, sum(x.y1 - x.y0 for x in last) / len(last))
            if abs(t.cy - cy) <= 0.5 * min(h, lh):
                last.append(t)
                continue
        rows.append([t])
    out = []
    for r in rows:
        r.sort(key=lambda t: t.x0)
        out.append(Row(r, " ".join(t.text for t in r), min(t.y0 for t in r), max(t.y1 for t in r),
                       min(float(t.confidence or 0) for t in r)))
    return out


def _upper(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").upper()).strip()


def _value_tokens(row: Row, label_end: int, page_width: float) -> tuple[str, float]:
    """The value printed right of a label: from the label's end up to the first wide gap (a photo box, a second
    column). Returns (text, min confidence)."""
    text = row.text
    joined, conf, prev_x1 = [], 1.0, None
    consumed = 0
    for t in row.tokens:
        start, consumed = consumed, consumed + len(t.text) + 1
        if consumed <= label_end:
            prev_x1 = t.x1
            continue
        piece = t.text if start >= label_end else t.text[max(0, label_end - start):]
        if prev_x1 is not None and joined and (t.x0 - prev_x1) > 0.12 * page_width:
            break
        if piece.strip():
            joined.append(piece)
            # an honorific / enumerator is not the value: "HR" for a printed "MR" read at 0.68 put a name read at
            # 0.99 to review (2026-10-22, real Union Bank passbook)
            if not _NOT_VALUE_TOKEN.match(_upper(piece)):
                conf = min(conf, float(t.confidence or 0))
        prev_x1 = t.x1
    value = _SEPARATOR.sub("", " ".join(joined)).strip()
    return (value, conf if joined else 0.0) if value else (text[label_end:].strip(" :.-"), 0.0)


def _label_starts(upper: str) -> list[int]:
    """Where a row's label may begin. A BILINGUAL label ("खाता क्र. / Account No.") reaches the OCR as junk
    before the English words -- "IT BRANCH", "TT /Branch", ".Account No", "a / Branch Phone" (2026-10-17, real
    passbooks: every such label was missed). So: the row start, after leading non-letters, after a "/" in the
    first 40 characters, after one leading junk word of at most 3 letters."""
    starts = [0]
    lead = re.match(r"^[^A-Z]+", upper)
    if lead:
        starts.append(lead.end())
    starts += [m.end() for m in re.finditer(r"/\s*", upper[:40])]
    junk = re.match(r"^[^\sA-Z]*[A-Z]{1,3}[^\sA-Z]*\s+", upper)
    if junk:
        starts.append(junk.end())
    return list(dict.fromkeys(starts))


#: labels also looked for INSIDE a row: a second column ("Name of A/c Holder ...   A/c No."), or a label printed
#: after another value ("08546-270466 IFSC Code : UBIN0542296")
_MID_ROW = ("account_number", "ifsc_code", "customer_id", "micr", "mobile")


def _token_at(row: Row, char_pos: int):
    consumed = 0
    for t in row.tokens:
        start, consumed = consumed, consumed + len(t.text) + 1
        if start <= char_pos < consumed:
            return t
    return row.tokens[-1] if row.tokens else None


def _column_value(next_row: Row, x0: float, page_width: float) -> tuple[str, float]:
    """The value printed UNDER a mid-row label: the next row's tokens from the label's left edge on (a little to its
    left allowed), up to the first wide gap."""
    picked, conf, prev = [], 1.0, None
    for t in sorted(next_row.tokens, key=lambda t: t.x0):
        if t.x1 < x0 - 0.04 * page_width:
            continue
        if prev is not None and t.x0 - prev.x1 > 0.12 * page_width:
            break
        picked.append(t.text)
        conf = min(conf, float(t.confidence or 0))
        prev = t
    return (" ".join(picked).strip(" :.-"), conf if picked else 0.0)


def labelled(rows: list[Row], page_width: float, account_digits: tuple[int, int] | None = None,
             below_rows: int = 0) -> dict[str, list[tuple[str, float, str]]]:
    """field -> [(value text, confidence, evidence)] for every labelled row (a label row without a value takes the
    next row when that row carries no label of its own: a two-line layout). A label may sit behind bilingual junk
    (_label_starts) or in a second column (_MID_ROW: its value right of it, or under it on the next row)."""
    found: dict[str, list[tuple[str, float, str]]] = {}
    for i, row in enumerate(rows):
        upper = _upper(row.text)
        first = None
        for start in _label_starts(upper):
            for name, pattern in _LABELS:
                m = pattern.match(upper[start:])
                if m:
                    first = (name, start + m.end(), start)
                    break
            if first:
                break
        if first:
            name, end, _ = first
            value, conf = _value_tokens(row, end, page_width)
            if not value and i + 1 < len(rows):
                nxt = rows[i + 1]
                if not any(p.match(_upper(nxt.text)) for _, p in _LABELS) and _is_latin(nxt.text):
                    value, conf = _SEPARATOR.sub("", nxt.text).strip(), nxt.confidence
            if not value and name == "account_number" and account_digits:
                value, conf = _number_below(rows[i + 1:i + 1 + below_rows], *account_digits)
            found.setdefault(name, []).append((value, conf, row.text))
        # SECOND-COLUMN / LATER labels in the same row
        for name in _MID_ROW:
            if first and first[0] == name:
                continue
            body = dict(_LABELS)[name].pattern.lstrip("^")
            for m in re.finditer(r"(?:(?<=\s)|(?<=[/.:]))(?:" + body + ")", upper):
                if first and m.start() < first[1]:
                    continue
                value, conf = _value_tokens(row, m.end(), page_width)
                if not value and i + 1 < len(rows):
                    token = _token_at(row, m.start())
                    if token is not None:
                        value, conf = _column_value(rows[i + 1], token.x0, page_width)
                if value:
                    found.setdefault(name, []).append((value, conf, row.text))
                break
    return found


def _number_below(rows: list[Row], lo: int, hi: int) -> tuple[str, float]:
    """An Account No. label printed with nothing beside it, its number a row or two lower -- also on a row that
    carries another label (2026-10-22, real Union Bank passbook: the number on the "In the Name of" row). Only a
    token that is a whole account-length number, the first one found going down."""
    for row in rows:
        for t in row.tokens:
            compact = re.sub(r"[\s-]", "", t.text)
            if compact.isdigit() and lo <= len(compact) <= hi and not _PHONE_SHAPED.fullmatch(compact):
                return compact, float(t.confidence or 0)
    return "", 0.0


def _is_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) > 0.8


def _digits(raw: str) -> tuple[str, bool]:
    """A value that should be digits: OCR letter-for-digit confusions repaired only when the value is mostly digits.
    Returns (digits, repaired)."""
    s = re.sub(r"[\s\-./]", "", raw.upper())
    if not s:
        return "", False
    digitish = sum(c.isdigit() or c in _DIGITISH for c in s)
    if digitish / len(s) < 0.85 or sum(c.isdigit() for c in s) / len(s) < 0.6:
        return re.sub(r"\D", "", s) if s.isdigit() else "", False
    fixed = "".join(_DIGITISH.get(c, c) for c in s)
    return (fixed, fixed != s) if fixed.isdigit() else ("", False)


def account_number(found: dict, rows: list[Row], bank_code: str | None, rules: dict) -> Field:
    lo, hi = rules["min_digits"], rules["max_digits"]
    values = [v for v in found.get("account_number", []) if v[0]]
    # a label whose value has no digit at all holds other text, not a misread number (2026-10-22, real Union Bank
    # passbook: "Account No. MR <name>", the number printed by the IFSC): the page's one account number is looked for
    if values and not any(re.search(r"\d", v[0]) for v in values):
        values, found = [], {k: v for k, v in found.items() if k != "account_number"}
    if found.get("account_number") and not values:
        return Field(status=UNREADABLE, source="label", notes=["label printed, value not legible"])
    candidates: list[Field] = []
    for raw, conf, evidence in values:
        compact = re.sub(r"\s", "", raw.upper())
        if _MASK_RE.match(compact):
            visible = re.sub(r"\D", "", compact)[-4:]
            candidates.append(Field(None, MASKED, conf, "label", evidence, visible_digits=visible,
                                    notes=["printed masked; never reconstructed"]))
            continue
        digits, repaired = _digits(raw.split(" ")[0] if len(raw.split(" ")[0]) >= lo else raw)
        if not digits:
            candidates.append(Field(None, UNREADABLE, conf, "label", evidence))
        elif not lo <= len(digits) <= hi:
            candidates.append(Field(digits, INVALID, conf, "label", evidence,
                                    notes=[f"{len(digits)} digits; an account number has {lo}-{hi}"]))
        else:
            candidates.append(Field(digits, PRESENT, conf * (0.9 if repaired else 1.0), "label", evidence,
                                    notes=["OCR letter/digit confusion repaired"] if repaired else []))
    if not candidates:
        # no label: one unlabelled 9-18 digit run is taken, never a guess between several
        excluded = {re.sub(r"\D", "", v[0]) for k in ("customer_id", "micr", "mobile") for v in found.get(k, [])}
        runs = []
        for row in rows:
            for m in re.finditer(r"(?<!\d)\d{%d,%d}(?!\d)" % (lo, hi), re.sub(r"[\s-]", "", row.text)):
                # a PHONE is never an account number: +91 / 91 + a mobile, a 10-digit mobile, a 0-STD landline
                # (2026-10-17: "917860276679" beside the real account made it ambiguous)
                if m.group() not in excluded and not _PHONE_SHAPED.fullmatch(m.group()):
                    runs.append((m.group(), row.confidence, row.text))
        distinct = {r[0] for r in runs}
        if len(distinct) == 1:
            digits, conf, evidence = runs[0]
            return _with_bank_rule(Field(digits, PRESENT, conf * 0.85, "pattern", evidence,
                                         notes=["no account-number label; the only account-length number"]),
                                   bank_code, rules)
        if len(distinct) > 1:
            return Field(status=AMBIGUOUS, source="pattern", notes=[f"{len(distinct)} unlabelled candidates"])
        return Field(status=MISSING)
    present = [c for c in candidates if c.status == PRESENT]
    if len({c.value for c in present}) > 1:
        return Field(status=AMBIGUOUS, source="label", notes=["two different labelled account numbers"])
    best = present[0] if present else candidates[0]
    return _with_bank_rule(best, bank_code, rules) if best.status == PRESENT else best


def _with_bank_rule(f: Field, bank_code: str | None, rules: dict) -> Field:
    lengths = (rules.get("lengths") or {}).get(bank_code or "")
    if lengths and f.value and len(f.value) not in lengths:
        f.notes.append(f"{len(f.value)} digits; {bank_code} account numbers have {sorted(lengths)}")
        f.status = INVALID
    return f


def ifsc_code(found: dict, rows: list[Row]) -> Field:
    labelled_values = [(IFSC.repair(re.sub(r"\s", "", v[0])[:11]) or _first(IFSC.find_in_text(v[0])), v[1], v[2])
                       for v in found.get("ifsc_code", []) if v[0]]
    page = []
    for row in rows:
        for code in IFSC.find_in_text(row.text):
            page.append((code, row.confidence, row.text))
    good = [v for v in labelled_values if v[0]] or page
    # several candidates: the ones the RBI master lists win (a stray IFSC-shaped string is never a branch)
    if len({v[0] for v in good}) > 1 and IFSC.available():
        listed = [v for v in good if IFSC.lookup(v[0])]
        good = listed or good
    distinct = list(dict.fromkeys(v[0] for v in good))
    if len(distinct) > 1:
        return Field(status=AMBIGUOUS, source="label" if labelled_values else "pattern",
                     notes=[f"{len(distinct)} different IFSCs on the page"])
    if distinct:
        code, conf, evidence = good[0]
        return Field(code, PRESENT, conf, "label" if labelled_values and labelled_values[0][0] else "pattern",
                     evidence)
    if found.get("ifsc_code"):
        return Field(status=INVALID if any(v[0] for v in found["ifsc_code"]) else UNREADABLE, source="label",
                     evidence=found["ifsc_code"][0][2], notes=["not a well-formed IFSC"])
    return Field(status=MISSING)


def _first(items):
    return items[0] if items else None


# a joint-holder enumerator before the name: "i) RAHUL ...", "ii)", "1)", "(a)"
_ENUMERATOR = re.compile(r"^\(?(?:I{1,3}|IV|[1-4]|[A-D])\)\s*")


def _not_a_name(name: str, not_a_name: frozenset[str]) -> bool:
    """Every word an account-type word (passbook.yaml verification.not_a_name_words): "SB GENERAL"."""
    words = name.replace(".", " ").split()
    return bool(words) and all(w in not_a_name for w in words)


def holder_name(found: dict, min_letters: int, not_a_name: frozenset[str] = frozenset()) -> Field:
    values = [v for v in found.get("account_holder_name", []) if v[0]]
    if not values:
        return Field(status=UNREADABLE if found.get("account_holder_name") else MISSING, source="label")
    names = []
    for raw, conf, evidence in values:
        name = _HONORIFICS.sub("", _ENUMERATOR.sub("", _upper(raw).strip(" :.-")))
        name = re.sub(r"\s+", " ", re.sub(r"[^A-Z .'-]", " ", name)).strip(" .-")
        if (sum(c.isalpha() for c in name) < min_letters or re.search(r"\d", raw) or not _NAME_OK.match(name)
                or _not_a_name(name, not_a_name)):
            names.append(Field(None, UNREADABLE, conf, "label", evidence))
        else:
            names.append(Field(name, PRESENT, conf, "label", evidence))
    present = [n for n in names if n.status == PRESENT]
    if len({n.value for n in present}) > 1:
        return Field(status=AMBIGUOUS, source="label", notes=["two different holder names"])
    return present[0] if present else names[0]


# A name printed with its honorific ("MR SYED OMAR IQBAL") but not beside its label -- a real passbook's printer is
# often a line off: the name sits one line under "Name of A/c Holder" (Andhra Bank, the address on the label's line)
# or on the "Account No." line above "In the Name of" (Union Bank). Never a relation's name (S/O, D/O, W/O ...).
_HONORIFIC_NAME = re.compile(r"(?<![A-Z])(?:MR|MRS|MS|MISS|SHRI|SMT|KUMARI|KUM|HR|HRS)\.?\s+([A-Z][A-Z .']{1,60})")
_RELATION = re.compile(r"(?<![A-Z])(?:S\s*/\s*O|D\s*/\s*O|W\s*/\s*O|C\s*/\s*O|SON\s+OF|DAUGHTER\s+OF|WIFE\s+OF|"
                       r"FATHER|MOTHER|HUSBAND|GUARDIAN|NOMINEE)(?![A-Z])")
# the label as a word START: OCR glues a label to the next text ("NamelfHDESH" for "Name of" + the address)
_NAME_LABEL = re.compile(r"(?<![A-Z])(?:NAME|HOLDER)")


def holder_name_near_label(rows: list[Row], window: int, min_letters: int, max_words: int) -> Field:
    """The holder's name by its honorific, on a line within `window` rows of a name label (above or below)."""
    upper = [_upper(r.text) for r in rows]
    labels = [i for i, t in enumerate(upper) if _NAME_LABEL.search(t)]
    seen: list[Field] = []
    for i in sorted({j for k in labels for j in range(max(0, k - window), min(len(rows), k + window + 1))}):
        text = upper[i]
        m = _HONORIFIC_NAME.search(text)
        if not m or _RELATION.search(text[:m.start()][-12:] + text[m.start():]):
            continue
        words = []
        for w in m.group(1).split():
            w = w.strip(".'")
            if not w or not w.isalpha():
                break
            words.append(w)
            if len(words) >= max_words:
                break
        name = " ".join(words)
        if sum(c.isalpha() for c in name) >= min_letters and _NAME_OK.match(name):
            seen.append(Field(name, PRESENT, float(rows[i].confidence), "honorific_near_label", rows[i].text))
    if len({f.value for f in seen}) > 1:
        return Field(status=AMBIGUOUS, source="honorific_near_label", notes=["two different holder names"])
    return seen[0] if seen else Field(status=MISSING, source="honorific_near_label")


def holder_name_below_label(rows: list[Row], window: int, min_letters: int, max_words: int,
                            not_a_name: frozenset[str] = frozenset()) -> Field:
    """The holder's name on a row within `window` rows UNDER a name label: the first row there that carries no
    label and is a name and nothing else -- 2 to `max_words` words of letters, not an account type (2026-10-22, real
    Union Bank passbook: "Name i) (SB GENERAL)", the name one row lower, its MR lost by the OCR). Never a
    relation's row; a row with a digit (an address, a date) is not a name."""
    upper = [_upper(r.text) for r in rows]
    seen: list[Field] = []
    for k, text in enumerate(upper):
        if not _NAME_LABEL.search(text) or _RELATION.search(text):
            continue
        for i in range(k + 1, min(len(rows), k + 1 + window)):
            row = upper[i]
            if any(p.match(row[s:]) for s in _label_starts(row) for _, p in _LABELS) or _RELATION.search(row):
                break
            if re.search(r"\d", row) or not _is_latin(rows[i].text):
                continue
            name = re.sub(r"\s+", " ", _HONORIFICS.sub("", _ENUMERATOR.sub("", row.strip(" :.-")))).strip(" .-")
            words = name.split()
            if (2 <= len(words) <= max_words and sum(c.isalpha() for c in name) >= min_letters
                    and _NAME_OK.match(name) and not _not_a_name(name, not_a_name)):
                seen.append(Field(name, PRESENT, float(rows[i].confidence), "line_below_label", rows[i].text))
                break
    if len({f.value for f in seen}) > 1:
        return Field(status=AMBIGUOUS, source="line_below_label", notes=["two different holder names"])
    return seen[0] if seen else Field(status=MISSING, source="line_below_label")


def printed_branch(found: dict) -> Field:
    values = [v for v in found.get("branch_name", []) if v[0]]
    if not values:
        return Field(status=UNREADABLE if found.get("branch_name") else MISSING, source="label")
    # the first value that READS AS A PLACE: at least 3 letters, mostly letters. A digit run or a stray character
    # under the Branch label is never a branch (2026-10-17, real passbooks: "3" and "…1197" -- part of the account
    # number printed out of line -- came back as the branch name)
    for raw, conf, evidence in values:
        name = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 .,&()/'-]", " ", _upper(raw))).strip(" .,-")
        letters = sum(c.isalpha() for c in name)
        alnum = sum(c.isalnum() for c in name)
        if letters >= 3 and letters >= 0.6 * max(1, alnum):
            return Field(name, PRESENT, conf, "label", evidence)
    return Field(None, UNREADABLE, values[0][1], "label", values[0][2], notes=["the branch value is not a name"])


@dataclass
class _BankIndex:
    names: list[tuple[str, str]]        # (normalised name, code), longest first


_INDEX: _BankIndex | None = None


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _bank_index() -> _BankIndex:
    global _INDEX
    if _INDEX is None:
        names = []
        for code, name in IFSC.bank_names().items():
            n = _norm(re.sub(r"\b(?:LIMITED|LTD)\b\.?", "", name.upper()))
            if len(n) >= 6:
                names.append((n, code))
        _INDEX = _BankIndex(sorted(names, key=lambda x: -len(x[0])))
    return _INDEX


def printed_bank(rows: list[Row]) -> Field:
    """The bank the page names (its header): the longest bank name from the master found in the top rows, else
    anywhere. A bank code inside an IFSC never counts (it is not a printed bank name)."""
    if not rows:
        return Field(status=MISSING)
    top = rows[: max(3, len(rows) // 3)]
    for scope, conf_factor in ((top, 1.0), (rows, 0.85)):
        text = _norm(" ".join(r.text for r in scope if not IFSC.find_in_text(r.text)))
        for name, code in _bank_index().names:
            if name in text:
                row = next((r for r in scope if name in _norm(r.text)), scope[0])
                return Field(IFSC.bank_name(code), PRESENT, row.confidence * conf_factor, "header", row.text,
                             notes=[f"code {code}"])
    return Field(status=MISSING)


def code_of(bank_field: Field) -> str | None:
    for note in bank_field.notes:
        if note.startswith("code "):
            return note[5:]
    return None


__all__ = ["AMBIGUOUS", "Field", "INVALID", "MASKED", "MISSING", "PRESENT", "Row", "UNREADABLE", "account_number",
           "code_of", "holder_name", "ifsc_code", "labelled", "printed_bank", "printed_branch", "rows_of"]
