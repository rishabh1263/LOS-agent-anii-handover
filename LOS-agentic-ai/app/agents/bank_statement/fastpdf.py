"""
A FAST PDF READER FOR BANK STATEMENTS -- PyMuPDF behind the two calls the parser makes.

MEASURED, NOT ASSUMED (2026-10-03, real Kotak statement, 39 pages): pypdf took
8.0 s for the page text that PyMuPDF returns in 0.12 s, and pdfplumber needed
~0.75-0.9 s per page for the transaction table -- so a 39-page native PDF was
projected over the upload budget and deferred to the background queue.

SAME SHAPES, SO NOTHING DOWNSTREAM CHANGES. `page_texts` returns one string per
page; `open_pdf(...).pages[i].extract_table()` returns the page's largest table
as rows of cell strings (None for an empty cell), exactly what pdfplumber's
`extract_table()` returns. The header mapping, merged-cell splitting, balance
orientation and reconciliation all run as before.

SELECTED BY CONFIGURATION, MEASURED: BOTH DEFAULTS ARE THE ORIGINAL ENGINES
(pypdf text, pdfplumber tables). PyMuPDF for either is opt-in
(BANK_STATEMENT_TEXT_ENGINE / BANK_STATEMENT_PDF_ENGINE = pymupdf): on a real
Canara statement it lost 371 of 524 rows either way. With PyMuPDF tables selected, a page it finds no table on falls back
to pdfplumber for that page.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def engine() -> str:
    """TABLES: pdfplumber by default. PyMuPDF tables were measured on 9 real statements:
    identical rows on 8, but Canara lost 371 of 524 rows and stopped reconciling, and
    it was not faster on 6 of 9 -- so it stays opt-in (BANK_STATEMENT_PDF_ENGINE=pymupdf)."""
    return (os.getenv("BANK_STATEMENT_PDF_ENGINE") or "pdfplumber").strip().lower()


def text_engine() -> str:
    """
    PAGE TEXT: pypdf by default; PyMuPDF opt-in (BANK_STATEMENT_TEXT_ENGINE=pymupdf).

    MEASURED ON 9 REAL STATEMENTS: PyMuPDF text is ~65x faster (0.12 s vs 8.0 s
    for 39 pages) and gave identical rows on 8 -- but its line order broke the
    text-parsed Canara statement (153 of 524 rows, no longer reconciling).
    Correctness wins: it stays opt-in until the row parser is order-independent.
    """
    return (os.getenv("BANK_STATEMENT_TEXT_ENGINE") or "pypdf").strip().lower()


def _fitz():
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24
    except ImportError:
        import fitz  # type: ignore[no-redef]
    return fitz


def available() -> bool:
    try:
        _fitz()
        return True
    except Exception:  # noqa: BLE001
        return False


def page_texts(path: str) -> list[str] | None:
    """Text per page, in reading order. None when PyMuPDF cannot read the file."""
    if text_engine() != "pymupdf" or not available():
        return None
    try:
        with _fitz().open(path) as doc:
            return [page.get_text("text", sort=True) or "" for page in doc]
    except Exception as exc:  # noqa: BLE001 - the caller falls back to pypdf
        logger.debug("PyMuPDF text extraction failed: %s", exc)
        return None


class _Page:
    def __init__(self, page: Any, path: str, index: int):
        self._page, self._path, self._index = page, path, index

    def extract_table(self) -> list[list[str | None]] | None:
        """The page's largest table, pdfplumber-shaped; pdfplumber for this page if none."""
        try:
            found = self._page.find_tables()
            tables = [t.extract() for t in getattr(found, "tables", []) or []]
            tables = [t for t in tables if t and len(t) >= 2]
            if tables:
                best = max(tables, key=lambda t: len(t) * max(len(r) for r in t))
                return [[(c if c not in ("",) else None) if c is not None else None for c in row] for row in best]
        except Exception as exc:  # noqa: BLE001
            logger.debug("PyMuPDF table detection failed on page %d: %s", self._index, exc)
        return self._plumber_table()

    def _plumber_table(self):
        try:
            import pdfplumber

            with pdfplumber.open(self._path, pages=[self._index + 1]) as pdf:
                return pdf.pages[0].extract_table()
        except Exception:  # noqa: BLE001
            return None

    def extract_text(self) -> str:
        return self._page.get_text("text", sort=True) or ""


class _Document:
    def __init__(self, path: str):
        self._doc = _fitz().open(path)
        self.pages = [_Page(p, path, i) for i, p in enumerate(self._doc)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._doc.close()
        return False


def open_pdf(path: str):
    """A pdfplumber-compatible document: PyMuPDF when selected, pdfplumber otherwise."""
    if engine() == "pymupdf" and available():
        try:
            return _Document(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("PyMuPDF could not open %s: %s; using pdfplumber", path, exc)
    import pdfplumber

    return pdfplumber.open(path)


__all__ = ["available", "engine", "open_pdf", "page_texts"]
