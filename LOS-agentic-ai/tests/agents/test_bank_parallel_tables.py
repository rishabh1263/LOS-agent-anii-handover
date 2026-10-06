"""PARALLEL PAGE TABLES (2026-10-05): a 104-page native statement took 200 s read
one page at a time and was deferred from every upload; read in parallel it
completes in ~12 s. The rows must be the SAME rows the in-line path reads --
parallelism changes when a table is read, never what is read from it."""

from pathlib import Path

import pytest

from app.agents.bank_statement import extract as X

SAMPLE = Path("samples/real_batch/bank_amit.pdf")
pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="real sample not present")


def _rows(result):
    return [(t.date, t.debit, t.credit, t.balance) for t in result.transactions]


def test_parallel_and_in_line_reads_are_identical(monkeypatch):
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "600000")
    monkeypatch.setenv("BANK_STATEMENT_PROJECTION_SAMPLE_PAGES", "1")

    monkeypatch.setenv("BANK_STATEMENT_TABLE_WORKERS", "1")
    in_line = X.extract_bank_statement(str(SAMPLE))

    monkeypatch.setenv("BANK_STATEMENT_TABLE_WORKERS", "2")
    monkeypatch.setattr(X, "_PARALLEL_MIN_PAGES", 1)
    calls = []
    real = X._prefetch_tables
    monkeypatch.setattr(X, "_prefetch_tables", lambda *a: calls.append(a) or real(*a))
    try:
        parallel = X.extract_bank_statement(str(SAMPLE))
    finally:
        X._drop_pool()

    assert calls, "the parallel path was not taken"
    assert parallel.status == in_line.status
    assert parallel.balance_reconciles is in_line.balance_reconciles is True
    assert _rows(parallel) == _rows(in_line)


def test_a_broken_pool_falls_back_to_reading_in_line(monkeypatch):
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "600000")
    monkeypatch.setenv("BANK_STATEMENT_PROJECTION_SAMPLE_PAGES", "1")
    monkeypatch.setenv("BANK_STATEMENT_TABLE_WORKERS", "2")
    monkeypatch.setattr(X, "_PARALLEL_MIN_PAGES", 1)

    def broken():
        raise RuntimeError("no processes on this host")

    monkeypatch.setattr(X, "_table_pool", broken)
    result = X.extract_bank_statement(str(SAMPLE))

    assert result.status.value == "SUCCESS"
    assert result.balance_reconciles is True
