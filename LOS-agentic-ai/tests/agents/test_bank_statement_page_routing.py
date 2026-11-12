"""
PAGE-LEVEL ROUTING (2026-11-02, Phase 1; app/agents/bank_statement/page_routing.py): each page of a statement is read
from its text layer or OCR'd by its OWN shape, and a MIXED statement's scanned pages are read -- not counted and
skipped. Synthetic PDFs only (made here with PyMuPDF): no real statement is used.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pymupdf
import pytest

from app.agents.bank_statement import page_routing as R

LINE = "01/04/2026  UPI/PAYMENT/ACME STORES/REF123456  1,250.00  48,750.00"


def _page_image(doc: pymupdf.Document, page: pymupdf.Page) -> None:
    """A full-page grey 'scan' image."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 560), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(page.rect, pixmap=pix)


def _pdf(tmp_path, kinds: list[str]) -> str:
    """One page per kind: text (a digital page), scan (image only), scan+header (image + a typed header line),
    garbled (a text layer of unreadable characters)."""
    doc = pymupdf.open()
    for kind in kinds:
        page = doc.new_page()
        if kind in ("scan", "scan+header"):
            _page_image(doc, page)
        if kind == "text":
            for i in range(25):
                page.insert_text((40, 60 + 20 * i), LINE, fontsize=9)
        if kind == "scan+header":
            page.insert_text((40, 30), "STATE BANK OF INDIA  ACCOUNT STATEMENT  BRANCH: MAIN ROAD", fontsize=10)
            page.insert_text((40, 45), "Statement period 01/04/2026 to 30/04/2026", fontsize=9)
        if kind == "garbled":
            page.insert_text((40, 60), "�" * 300, fontsize=9)
    path = tmp_path / "statement.pdf"
    doc.save(path)
    return str(path)


def test_each_page_is_routed_by_its_own_shape(tmp_path):
    pages = R.inspect_pages(_pdf(tmp_path, ["text", "scan", "scan+header"]))
    assert [(p.route, p.reason) for p in pages] == [
        (R.DIGITAL, "TEXT"), (R.OCR, "NO_TEXT"), (R.OCR, "SCAN_WITH_HEADER")]
    assert pages[1].image_coverage > 0.9 and pages[0].image_coverage == 0
    assert R.route_counts(pages) == (1, 2)


def test_the_rule_reads_its_limits_from_config(monkeypatch):
    monkeypatch.setitem(R.settings(), "text_chars_with_image", 100)
    assert R.classify(300, 1.0, 0.95) == (R.DIGITAL, "TEXT")               # enough text over the image
    monkeypatch.setitem(R.settings(), "text_chars_with_image", 400)
    assert R.classify(300, 1.0, 0.95) == (R.OCR, "SCAN_WITH_HEADER")
    assert R.classify(5000, 0.2, 0.0) == (R.OCR, "GARBLED_TEXT")


# ---- a MIXED statement: its scanned pages are OCR'd, never silently skipped
def _row(page: int, day: int, balance: str):
    from app.agents.bank_statement.schemas import Transaction

    return Transaction(date=date(2026, 4, day), narration="ROW", debit=Decimal("10"), balance=Decimal(balance),
                       page=page)


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    """Pages 1 and 3 digital, page 2 a scan: the digital pages' rows come from the text layer as before."""
    from app.agents.bank_statement import extract as X

    path = _pdf(tmp_path, ["text", "scan", "text"])
    monkeypatch.setattr(X, "extract_via_native_columns", lambda p: [])
    monkeypatch.setattr(X, "extract_via_tables", lambda p, started: ([_row(1, 1, "990"), _row(3, 3, "970")], False))
    calls = []
    return X, path, calls


def test_the_scanned_page_of_a_mixed_statement_is_ocrd_and_its_rows_kept(mixed, monkeypatch):
    X, path, calls = mixed

    def grid(p, started, only_pages=None):
        calls.append(only_pages)
        return [_row(2, 2, "980")]

    monkeypatch.setattr(X, "extract_via_grid", grid)
    result = X.extract_bank_statement(path)
    assert calls == [{2}]                                        # only the scanned page, never the digital ones
    assert [r.page for r in result.transactions] == [1, 2, 3]
    assert result.source_kind.value == "MIXED" and result.pages_unread == 0
    assert result.status.value == "SUCCESS"


def test_a_scanned_page_that_cannot_be_read_makes_the_reading_partial(mixed, monkeypatch):
    X, path, _ = mixed
    monkeypatch.setattr(X, "extract_via_grid", lambda p, started, only_pages=None: [])
    monkeypatch.setattr(X, "extract_via_columns", lambda p, started, only_pages=None: [])
    result = X.extract_bank_statement(path)
    assert result.pages_unread == 1 and result.status.value == "PARTIAL"     # never an apparent success
    assert any("could not be read" in w and "pages 2" in w for w in result.warnings)


def test_only_the_pages_asked_for_are_rendered(tmp_path, monkeypatch):
    from app.agents.bank_statement import extract as X

    rendered = []

    def convert(path, dpi=200, first_page=None, last_page=None):
        rendered.append((first_page, last_page))
        from PIL import Image

        return [Image.new("RGB", (10, 10), "white")]

    monkeypatch.setattr("pdf2image.convert_from_path", convert)
    images = X._ScannedPages(_pdf(tmp_path, ["text", "scan", "text"]), {2}).images()
    assert rendered == [(2, 2)] and images[0] is None and images[2] is None and images[1] is not None
    assert isinstance(np.zeros(1), np.ndarray)
