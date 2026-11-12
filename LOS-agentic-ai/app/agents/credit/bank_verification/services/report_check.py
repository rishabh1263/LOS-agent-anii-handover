"""
THE AGENCY'S REPORT, READ AND CHECKED against the case on screen (owner 2026-10-27: "agency report compare with our
data on screen and usme bhi basic checks, sab values config").

    read(content, kind)        the report's text: the PDF's text layer, else OCR (a photo / scan of the report)
    check(repo, case_id, ...)  basic checks + field comparisons -> {"status", "comparisons", "checks", "notes"}

BASIC CHECKS (config agency_report.basic): the report could be read; it names THIS case (its case id or application
number); it carries a date that is neither in the future nor older than `max_age_days`.
COMPARISONS (config agency_report.labels): customer name vs the applicant, account number vs the account verified,
bank vs the bank verified, the ownership result written in the report vs the one the agency selected, the agency's name
vs the assigned agency. A value the report does not print is "not found" (REVIEW), never assumed to match.

Status: any check or comparison that disagrees -> FAIL; anything required not found -> REVIEW_REQUIRED; else PASS.
A POSITIVE needs PASS (config agency_report.required_for_positive). Same shape as a rule's comparisons, so the screen
shows it the same way. Nothing here decides ownership: it checks that the paper says what the case says.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.agents.credit.bank_verification import normalize
from app.agents.credit.bank_verification.config import cfg, say, section
from app.agents.credit.bank_verification.repository import Repository
from app.agents.credit.bank_verification.schemas import Comparison, mask


def spec() -> dict[str, Any]:
    return section("agency_report") or {}


# ---- reading ---------------------------------------------------------------------------------------------------------
def read(content: bytes, kind: str) -> str:
    """The report's text, ligatures and full-width forms folded (NFKC: "Veriﬁcation" -> "Verification")."""
    import unicodedata

    return unicodedata.normalize("NFKC", _read(content, kind))


def _read(content: bytes, kind: str) -> str:
    """The report's text: a PDF's own text layer, else OCR of its pages (config ocr_max_pages) or of the image."""
    kind = (kind or "").upper()
    if kind == "PDF":
        import pymupdf

        try:
            with pymupdf.open(stream=content, filetype="pdf") as doc:
                text = "\n".join(page.get_text() or "" for page in doc)
                if text.strip():
                    return text
                images = [_page_image(doc[i]) for i in range(min(doc.page_count, int(spec().get("ocr_max_pages", 3))))]
        except Exception:  # noqa: BLE001 - a damaged file reads as nothing; the check says it could not be read
            return ""
    else:
        from io import BytesIO

        from PIL import Image

        try:
            images = [Image.open(BytesIO(content)).convert("RGB")]
        except Exception:  # noqa: BLE001
            return ""
    return "\n".join(_ocr(image) for image in images)


def _page_image(page):
    from PIL import Image

    pix = page.get_pixmap(dpi=int(spec().get("ocr_dpi", 200)))
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _ocr(image) -> str:
    try:
        from app.agents.bank_statement.extract import _tokens_to_lines
        from app.agents.document_agent import preprocess as PP
        from app.agents.document_agent.ocr import get_engine

        tokens, _ = get_engine().read_array(PP.to_array(PP.standard(image)))
        return _tokens_to_lines(tokens)
    except Exception:  # noqa: BLE001 - no OCR engine: nothing read, said as such
        return ""


# ---- what the report says ----------------------------------------------------------------------------------------------
def _value(text: str, field: str) -> str | None:
    """A "Label: value" line of the report (config labels). With label_at_line_start the label must open its line and
    a separator follow it -- a label inside a sentence is never read as one."""
    from app.agents.credit.bank_verification.services.statement_service import _label_value

    labels = [str(x) for x in (spec().get("labels") or {}).get(field) or []]
    pattern = (spec().get("value_patterns") or {}).get(field)
    if not labels:
        return None
    if not spec().get("label_at_line_start", True):
        return _label_value(text, labels, pattern)
    for label in sorted(labels, key=len, reverse=True):
        words = r"\s*".join(re.escape(part) for part in label.split())
        head = re.compile(rf"(?i)^\s*{words}(?![a-z])\.?\s*[:：\-–]\s*(.+)$")
        for line in str(text or "").splitlines():
            found = head.match(line)
            if not found:
                continue
            value = re.split(r"\s{3,}", found.group(1).strip())[0].strip()
            if pattern:
                hit = re.search(pattern, value.replace(" ", "") if field == "account_number" else value)
                if hit:
                    return hit.group(0)
                continue
            if value:
                return value[:80]
    return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in spec().get("date_formats") or []:
        for piece in (value, value.split()[0] if value.split() else value):
            try:
                return datetime.strptime(piece.strip(" .,"), str(fmt)).date()
            except ValueError:
                continue
    return None


def _ownership(value: str | None) -> str | None:
    """The ownership result in the report's words, mapped by config ownership_words (the longest phrase wins)."""
    folded = normalize.text(value)
    if not folded:
        return None
    pairs = [(normalize.text(w), status) for status, words in (spec().get("ownership_words") or {}).items()
             for w in words or []]
    for word, status in sorted(pairs, key=lambda p: len(p[0] or ""), reverse=True):
        if word and re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", folded):
            return str(status)
    return None


def _same_name(a: str | None, b: str | None) -> bool:
    """Names match when the words match, in any order; a name with an extra middle name / initial still matches when
    at least `name_min_common_words` words are shared (config)."""
    wa, wb = set((normalize.text(a) or "").split()), set((normalize.text(b) or "").split())
    if not wa or not wb:
        return False
    if wa == wb or _squash(a) == _squash(b):          # OCR drops spaces: "IshaanVerma" is "Ishaan Verma"
        return True
    common = wa & wb
    return len(common) >= int(spec().get("name_min_common_words", 2)) and (common == wa or common == wb)


def _squash(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _same_account(report: str | None, ours: str | None) -> tuple[bool | None, str | None]:
    """(match, note): full numbers compare exactly; a masked one on the report by its visible last digits (at least
    config masked_min_digits), said so in the note."""
    a, b = normalize.account_number(report), normalize.account_number(ours)
    if not a or not b:
        return None, None
    if normalize.is_masked(a):
        tail = normalize.visible_suffix(a)
        if len(tail) < int(spec().get("masked_min_digits", 4)):
            return None, say("agency_report.notes.masked_short")
        return b.endswith(tail), say("agency_report.notes.masked", n=len(tail))
    return a == b, None


# ---- the check ---------------------------------------------------------------------------------------------------------
def _ours(repo: Repository, case_id: str) -> dict[str, Any]:
    """The case as the screen shows it: applicant, refs, agency, and the account + bank of the latest run."""
    from app.agents.credit.bank_verification.services import verification_service

    case = repo.case(case_id) or {}
    found = verification_service.latest(repo, case_id)
    ev = found[1] if found else {}
    form = ev.get("form") or {}
    statement = (ev.get("statement") or {}).get("details") or {}
    agencies = {a["id"]: a["name"] for a in cfg().get("agencies") or []}
    return {"applicant_name": case.get("applicant_name"), "case_id": case.get("case_id"),
            "application_ref": case.get("application_ref"),
            "account_number": form.get("account_number") or statement.get("account_number"),
            "bank_name": form.get("bank_name") or statement.get("bank_name"),
            "account_type": form.get("account_type") or statement.get("account_type"),
            "transactions": _screen_transactions(form, ev.get("statement")),
            "agency": agencies.get(case.get("assigned_agency") or "")}


def _screen_transactions(form: dict[str, Any], statement: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The latest transactions the case shows: the ones on the form; when the form has none, the statement's own
    latest rows as the agent read them (the report is never compared with an empty list)."""
    typed = [t for t in form.get("transactions") or [] if t.get("date") and t.get("amount")]
    if typed or not statement:
        return typed
    from app.agents.credit.bank_verification.schemas import StatementData
    from app.agents.credit.bank_verification.services import transaction_service

    rows = transaction_service.latest(StatementData.model_validate(statement).transactions).rows
    return [{"date": r.date.isoformat(), "amount": str(r.amount), "direction": r.direction.value} for r in rows]


def check(repo: Repository, case_id: str, content: bytes, kind: str, *, ownership: str | None,
          agency_id: str | None = None, today: date | None = None) -> dict[str, Any]:
    s = spec()
    words = lambda key, **v: say(f"agency_report.{key}", **v)      # noqa: E731
    today = today or date.today()
    text = read(content, kind)
    ours = _ours(repo, case_id)
    if agency_id:
        ours["agency"] = {a["id"]: a["name"] for a in cfg().get("agencies") or []}.get(agency_id) or ours["agency"]
    required = set(s.get("required") or [])
    rows: list[Comparison] = []
    checks: list[Comparison] = []

    def row(target: list[Comparison], field: str, left: str | None, right: str | None, match: bool | None,
            note: str | None = None) -> None:
        target.append(Comparison(field=field, left_label=words(f"fields.{field}"), left=left,
                                 right_label=words("on_screen"), right=right, match=match, note=note))

    # basic checks
    readable = len(text.strip()) >= int(s.get("min_text_chars", 40))
    row(checks, "readable", words("answer_yes") if readable else words("answer_no"), None, readable or None,
        None if readable else words("notes.unreadable"))
    if not readable:
        return _verdict(rows, checks, required, words, unreadable=True)
    own = next((t for t in s.get("own_report_titles") or [] if re.search(rf"(?i)^\s*{re.escape(str(t))}", text.strip())),
               None)
    row(checks, "report_kind", own or words("answer_yes"), None, False if own else True,
        words("notes.own_report") if own else None)
    if own:
        return _verdict(rows, checks, required, words)
    folded = re.sub(r"\s+", "", text).upper()
    refs = [r for r in (ours["case_id"], ours["application_ref"]) if r]
    named = next((r for r in refs if re.sub(r"\s+", "", r).upper() in folded), None)
    other = None if named else _other_case_ref(repo, case_id, folded)
    row(checks, "case_ref", named or other, " / ".join(refs), True if named else (False if other else None),
        words("notes.other_case") if other else (None if named else words("notes.not_found")))
    printed_date = _value(text, "report_date")
    when = _date(printed_date)
    if when is None:
        row(checks, "report_date", printed_date, None, None, words("notes.not_found"))
    elif when > today:
        row(checks, "report_date", when.strftime("%d-%m-%Y"), today.strftime("%d-%m-%Y"), False, words("notes.future"))
    elif (today - when).days > int(s.get("max_age_days", 30)):
        row(checks, "report_date", when.strftime("%d-%m-%Y"), today.strftime("%d-%m-%Y"), False,
            words("notes.too_old", days=s.get("max_age_days", 30)))
    else:
        row(checks, "report_date", when.strftime("%d-%m-%Y"), today.strftime("%d-%m-%Y"), True)

    # the report's values against the screen's
    name = _value(text, "applicant_name")
    row(rows, "applicant_name", name, ours["applicant_name"],
        _same_name(name, ours["applicant_name"]) if name else None, None if name else words("notes.not_found"))
    number = _value(text, "account_number")
    match, note = _same_account(number, ours["account_number"])
    row(rows, "account_number", mask(number) if number and not normalize.is_masked(number) else number,
        mask(ours["account_number"]), match, note or (None if number else words("notes.not_found")))
    bank = _value(text, "bank_name")
    row(rows, "bank_name", bank, ours["bank_name"],
        (normalize.bank_name(bank) == normalize.bank_name(ours["bank_name"])) if bank and ours["bank_name"] else None,
        None if bank else words("notes.not_found"))
    kind = _value(text, "account_type")
    row(rows, "account_type", kind, ours["account_type"],
        (normalize.account_type(kind) == normalize.account_type(ours["account_type"])) if kind and ours["account_type"]
        else None, None if kind else words("notes.not_found"))
    _transaction_rows(text, ours["transactions"], rows, words)
    said = _ownership(_value(text, "ownership"))
    chosen = str(ownership) if ownership else None
    labels = s.get("ownership_labels") or {}
    row(rows, "ownership", labels.get(said, said), labels.get(chosen, chosen),
        (said == chosen) if said and chosen else None, None if said else words("notes.not_found"))
    agency = _value(text, "agency")
    row(rows, "agency", agency, ours["agency"],
        (_squash(ours["agency"]) in _squash(agency) or _squash(agency) in _squash(ours["agency"]))
        if agency and ours["agency"] and _squash(agency) else None,
        None if agency else words("notes.not_found"))
    return _verdict(rows, checks, required, words)


def _transactions(text: str) -> list[dict[str, Any]]:
    """The report's transaction lines (config transaction_labels): date, amount and side, as printed."""
    from decimal import Decimal

    s = spec()
    labels = [str(x) for x in s.get("transaction_labels") or []]
    sides = {normalize.text(w): side for side, ws in (s.get("direction_words") or {}).items() for w in ws or []}
    out = []
    for line in str(text or "").splitlines():
        squashed = line.strip()
        if not any(re.match(rf"(?i){r'\s*'.join(map(re.escape, label.split()))}(?![a-z])", squashed) for label in labels):
            continue
        when = re.search(str(s.get("date_pattern")), squashed)
        amount = re.search(str(s.get("amount_pattern")), squashed[when.end():] if when else squashed)
        side = next((v for w, v in sides.items() if w and re.search(rf"(?i)(?<![a-z]){re.escape(w)}(?![a-z])",
                                                                     normalize.text(squashed) or "")), None)
        out.append({"date": _date(when.group(0)) if when else None,
                    "amount": Decimal(amount.group(0).replace(",", "")) if amount else None, "direction": side,
                    "shown": squashed.split(":", 1)[-1].strip()[:60]})
    return out


def _transaction_rows(text: str, screen: list[dict[str, Any]], rows: list[Comparison], words) -> None:
    """Each transaction the report lists must be one of the latest transactions on screen, all three facts equal."""
    from decimal import Decimal

    from app.agents.credit.bank_verification.rules.transaction_matching import said
    from app.agents.credit.bank_verification.schemas import EnteredTransaction

    listed = _transactions(text)
    ours = [EnteredTransaction.model_validate(t) for t in screen]
    if not listed:
        rows.append(Comparison(field="transactions", left_label=words("fields.transactions"), left=None,
                               right_label=words("on_screen"), right=f"{len(ours)}", match=None,
                               note=words("notes.not_found")))
        return
    unused = list(ours)
    for n, t in enumerate(listed, start=1):
        key = (t["date"], Decimal(t["amount"]) if t["amount"] is not None else None, t["direction"])
        hit = next((o for o in unused if (o.date, o.amount, o.direction.value if o.direction else None) == key), None)
        label = words("fields.transaction", n=n)
        if hit is not None:
            unused.remove(hit)
            rows.append(Comparison(field=f"transaction_{n}", left_label=label, left=t["shown"],
                                   right_label=words("on_screen"), right=said(hit), match=True))
            continue
        near = max(unused or ours, key=lambda o: (o.date == key[0]) * 4 + (o.amount == key[1]) * 2 +
                   ((o.direction.value if o.direction else None) == key[2]), default=None)
        parts = [words(f"notes.parts.{name}") for name, a, b in
                 (("date", key[0], near.date if near else None), ("amount", key[1], near.amount if near else None),
                  ("direction", key[2], near.direction.value if near and near.direction else None)) if a != b]
        # "what differs" only for a row that shares the date or the amount (as rule 2 does); else it is not on screen --
        # and the screen's own transaction at that position is shown beside it, so the two can be read side by side
        close = near is not None and (near.date == key[0] or near.amount == key[1])
        shown = said(near) if close else (said(ours[n - 1]) if n - 1 < len(ours) else None)
        rows.append(Comparison(field=f"transaction_{n}", left_label=label, left=t["shown"],
                               right_label=words("on_screen"), right=shown, match=False,
                               note=words("notes.differs", parts=", ".join(parts)) if close else words("notes.not_on_screen")))


def _other_case_ref(repo: Repository, case_id: str, folded: str) -> str | None:
    """Another case's id / application number printed on this report (the wrong report attached)."""
    for c in repo.cases():
        if c["case_id"] == case_id:
            continue
        for ref in (c["case_id"], c["application_ref"]):
            if ref and re.sub(r"\s+", "", ref).upper() in folded:
                return ref
    return None


def _verdict(rows: list[Comparison], checks: list[Comparison], required: set[str], words,
             unreadable: bool = False) -> dict[str, Any]:
    every = checks + rows
    if any(c.match is False for c in every):
        status = "FAIL"
    elif unreadable or any(c.match is None and c.field in required for c in every):
        status = "REVIEW_REQUIRED"
    else:
        status = "PASS"
    differ = [c.left_label for c in every if c.match is False]
    unknown = [c.left_label for c in every if c.match is None and c.field in required]
    reason = {"PASS": words("pass"), "FAIL": words("fail", items=", ".join(differ).lower()),
              "REVIEW_REQUIRED": words("unreadable") if unreadable else
              words("review", items=", ".join(unknown).lower())}[status]
    return {"status": status, "reason": reason,
            "checks": [c.model_dump(mode="json") for c in checks],
            "comparisons": [c.model_dump(mode="json") for c in rows]}
