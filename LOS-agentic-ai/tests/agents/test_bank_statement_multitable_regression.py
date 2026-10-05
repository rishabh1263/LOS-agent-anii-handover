"""
BANK STATEMENT REGRESSION FROM THE REAL-SAMPLE SWEEP (2026-10-03).

A page's transactions can be split into two tables of the same width (real Kotak
statement: a 35-row table and a 2-row tail). `extract_table()` returned only the
largest, so the tail was dropped on every such page: 104 rows unread, 11 breaks in
the balance chain, no reconciliation. Every same-width table is now joined in page
order; a table of another width (a summary box) is still left out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.bank_statement import extract as bank

KOTAK = Path("samples/real_batch/bank_kotak.pdf")


class _Table:
    def __init__(self, top, rows):
        self.bbox = (0, top, 100, top + 10)
        self._rows = rows

    def extract(self):
        return self._rows


class _Page:
    def __init__(self, tables):
        self._tables = tables

    def find_tables(self):
        return list(self._tables)


def test_same_width_tables_are_joined_top_to_bottom_and_other_widths_left_out():
    main = _Table(10, [["d", "n", "dr", "cr", "bal"], ["01", "a", "1", "", "9"]])
    tail = _Table(80, [["02", "b", "2", "", "7"]])
    summary = _Table(50, [["Opening", "x"], ["Closing", "y"]])
    joined = bank._page_table(_Page([tail, summary, main]))       # order of discovery is not page order
    assert joined == main.extract() + tail.extract()


def test_an_engine_without_table_geometry_falls_back_to_extract_table():
    class Plain:
        def extract_table(self):
            return [["only"]]

    assert bank._page_table(Plain()) == [["only"]]


@pytest.mark.skipif(not KOTAK.exists(), reason="real Kotak statement not present")
def test_the_real_kotak_statement_is_read_completely_and_reconciles(monkeypatch):
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "600000")     # the full parse, not the upload budget
    result = bank.extract_bank_statement(str(KOTAK))
    assert len(result.transactions) == 1339
    assert result.balance_reconciles is True and result.status.value == "SUCCESS"
