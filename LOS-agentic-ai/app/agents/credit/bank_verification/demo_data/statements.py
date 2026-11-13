"""
SYNTHETIC bank statements for the demo: real, readable PDF (or CSV) files built from a statement spec, so the agent
reads them exactly as it would read an upload. The layout's words come from bank_verification_demo.yaml
`statement_layout`; every bank, person and number is fictional.

The PDF bytes are deterministic (fixed metadata dates, no fresh file id), so the same spec always gives the same
SHA-256 -- which is what lets the duplicate scenario reuse one file across two cases.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import pymupdf

from app.agents.credit.bank_verification.config import demo


def _layout() -> dict[str, Any]:
    return demo()["statement_layout"]


def _amount(value: Decimal | None) -> str:
    return "" if value is None else f"{value:,.2f}"


def _lines(spec: dict[str, Any]) -> tuple[list[str], list[list[str]], list[str]]:
    """(header lines, table rows, footer lines) of a statement spec."""
    d, lab = spec["details"], _layout()["labels"]
    header = [
        f"{d['bank_name']}",
        _layout()["heading"],
        f"{lab['holder']}: {d['account_holder']}",
        f"{lab['number']}: {d['account_number']}",
        f"{lab['type']}: {d['account_type']}",
        f"{lab['ifsc']}: {d['ifsc']}",
        f"{lab['period']}: {spec['period_start']:%d-%m-%Y} to {spec['period_end']:%d-%m-%Y}",
        f"{lab['opening']}: {_amount(spec['opening_balance'])}",
    ]
    rows = []
    for t in spec["transactions"]:
        debit = t["amount"] if t["direction"] == "DEBIT" else None
        credit = t["amount"] if t["direction"] == "CREDIT" else None
        rows.append([f"{t['date']:%d-%m-%Y}", t.get("narration", ""), t.get("reference") or "",
                     _amount(debit), _amount(credit), _amount(t.get("balance"))])
    footer = [f"{lab['closing']}: {_amount(spec['closing_balance'])}", _layout()["footer"]]
    return header, rows, footer


def build_pdf(spec: dict[str, Any], *, producer: str | None = None, modified: str | None = None) -> bytes:
    """A digital statement PDF with a text layer. `producer` / `modified` set the metadata (the tamper scenarios)."""
    layout = _layout()
    header, rows, footer = _lines(spec)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 50
    for i, line in enumerate(header):
        page.insert_text((40, y), line, fontsize=14 if i == 0 else 9, fontname="helv")
        y += 20 if i == 0 else 14
    y += 10
    columns = list(zip(layout["columns"], (40, 110, 290, 360, 430, 500)))
    numeric = set(layout["columns"][3:])                        # debit / credit / balance: right-aligned
    for title, x in columns:
        page.insert_text((x, y), title, fontsize=9, fontname="hebo")
    y += 14
    for row in rows:
        if y > 790:
            page = doc.new_page(width=595, height=842)
            y = 50
        for (title, x), value in zip(columns, row):
            if title in numeric and value:
                width = pymupdf.get_text_length(value, fontname="helv", fontsize=8.5)
                page.insert_text((x + 55 - width, y), value, fontsize=8.5, fontname="helv")
            else:
                page.insert_text((x, y), value[:34], fontsize=8.5, fontname="helv")
        y += 13
    y += 10
    for line in footer:
        page.insert_text((40, y), line, fontsize=9, fontname="helv")
        y += 14
    doc.set_metadata({
        "title": layout["metadata_title"],
        "author": spec["details"]["bank_name"],
        "creator": layout["generator"],
        "producer": producer or layout["generator"],
        "creationDate": layout["fixed_date"],
        "modDate": modified or layout["fixed_date"],
    })
    out = io.BytesIO()
    doc.save(out, garbage=3, deflate=True, no_new_id=True)
    doc.close()
    return out.getvalue()


def build_scanned_pdf(spec: dict[str, Any], dpi: int | None = None) -> bytes:
    """The same statement as an IMAGE-only PDF (no text layer): what a phone scan looks like to the agent."""
    layout = _layout()
    digital = pymupdf.open(stream=build_pdf(spec), filetype="pdf")
    scanned = pymupdf.open()
    for page in digital:
        pix = page.get_pixmap(dpi=int(dpi or layout.get("scan_dpi", 110)))
        target = scanned.new_page(width=page.rect.width, height=page.rect.height)
        target.insert_image(target.rect, pixmap=pix)
    scanned.set_metadata({"producer": layout["scanner"], "creationDate": layout["fixed_date"],
                          "modDate": layout["fixed_date"]})
    out = io.BytesIO()
    scanned.save(out, garbage=3, deflate=True, no_new_id=True)
    return out.getvalue()


def build_csv(spec: dict[str, Any]) -> bytes:
    """A CSV statement: '# key: value' header lines, then date,narration,reference,debit,credit,balance."""
    d = spec["details"]
    lines = [f"# bank_name: {d['bank_name']}", f"# account_holder: {d['account_holder']}",
             f"# account_number: {d['account_number']}", f"# account_type: {d['account_type']}",
             f"# ifsc: {d['ifsc']}", f"# period: {spec['period_start']:%Y-%m-%d} to {spec['period_end']:%Y-%m-%d}",
             f"# opening_balance: {spec['opening_balance']}", f"# closing_balance: {spec['closing_balance']}",
             "date,narration,reference,debit,credit,balance"]
    for t in spec["transactions"]:
        debit = t["amount"] if t["direction"] == "DEBIT" else ""
        credit = t["amount"] if t["direction"] == "CREDIT" else ""
        lines.append(f"{t['date']:%Y-%m-%d},{t.get('narration', '').replace(',', ' ')},{t.get('reference') or ''},"
                     f"{debit},{credit},{t.get('balance') or ''}")
    return ("\n".join(lines) + "\n").encode("utf-8")
