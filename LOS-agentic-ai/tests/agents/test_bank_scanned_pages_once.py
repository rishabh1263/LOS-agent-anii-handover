"""
A scanned statement is rasterised ONCE and each page OCR'd at most ONCE across the grid strategy and its column
fallback (2026-10-10, measured: 2 rasterisations and 5 reads for 4 pages before -- page 1 read twice).
"""
from __future__ import annotations

from PIL import Image

from app.agents.bank_statement import extract as E


class _Engine:
    def __init__(self):
        self.reads = 0

    def read_array(self, array):
        self.reads += 1
        return [], None


def _run(monkeypatch, tmp_path):
    import pdf2image

    from app.agents.bank_statement import grid as G
    from app.agents.bank_statement import layout as L
    from app.agents.document_agent import ocr

    rasterised = []
    pages = [Image.new("RGB", (200, 260), (255, 255, 255)) for _ in range(4)]

    def convert(path, dpi=200):
        rasterised.append(path)
        return pages

    engine = _Engine()
    monkeypatch.setattr(pdf2image, "convert_from_path", convert)
    monkeypatch.setattr(ocr, "get_engine", lambda: engine)
    # a ruled block on page 1 only (the account summary), no transaction table anywhere
    calls = iter(range(100))
    monkeypatch.setattr(G, "detect_grid", lambda gray: ([1, 2, 3], [1, 2, 3]) if next(calls) == 0 else ([], []))
    monkeypatch.setattr(L, "cells_for_page", lambda tokens: None)
    path = str(tmp_path / "scan.pdf")
    token = E._PAGES.set(E._ScannedPages(path))
    try:
        assert E.extract_via_grid(path, started=E.time.perf_counter()) == []
        assert E.extract_via_columns(path, started=E.time.perf_counter()) == []
    finally:
        E._PAGES.reset(token)
    return rasterised, engine.reads


def test_the_grid_and_its_fallback_share_one_rasterisation_and_one_read_per_page(monkeypatch, tmp_path):
    rasterised, reads = _run(monkeypatch, tmp_path)
    assert len(rasterised) == 1
    assert reads == 4                                    # page 1 read by the grid is not read again


def test_a_direct_caller_still_gets_its_own_reader(monkeypatch, tmp_path):
    """Outside extract_bank_statement each strategy works alone, exactly as before."""
    import pdf2image

    monkeypatch.setattr(pdf2image, "convert_from_path", lambda path, dpi=200: [])
    assert E._PAGES.get() is None
    assert E.extract_via_columns(str(tmp_path / "x.pdf"), started=E.time.perf_counter()) == []
