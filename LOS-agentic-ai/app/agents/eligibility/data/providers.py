"""
Where the applicant data comes from -- behind one interface, so the engine never changes when the source does.

    DataProvider.get(case_id) -> EligibilityInput

  PostgresDataProvider  the `eligibility_cases` table (schema.sql), connection from ELIGIBILITY_DSN (psycopg2)
  LosDataProvider       the LOS case store (applications + applicants) + the credit bureau for the score

Chosen by config `data_provider` / env ELIGIBILITY_DATA_PROVIDER (`get_provider`).

NULL STAYS NONE. A column the row does not have is passed through as None and the engine answers DATA_MISSING for a
mandatory one. Age is derived from `dob` in code (full years on `as_of`, today by default), never stored.

TWO DIFFERENT FAILURES, NEVER CONFUSED WITH MISSING DATA: a case that is not there is `CaseNotFound` (404 at the API);
a database that cannot be reached or read is `ProviderUnavailable` (503) -- neither ever becomes a decision.
"""

from __future__ import annotations

import os
import re
import threading
from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from app.agents.eligibility import config
from app.agents.eligibility.domain.models import EligibilityInput

_COLUMNS = ("case_id", "product", "dob", "net_monthly_income", "existing_emi", "bureau_score", "property_value",
            "requested_amount", "tenure_months", "rate_percent", "max_dpd_12m", "write_offs", "settlements")


class CaseNotFound(LookupError):
    """No applicant data for this case."""


class ProviderUnavailable(RuntimeError):
    """The data source cannot be reached or read (configuration, connection, query)."""


class DataProvider(ABC):
    """The source of one case's structured applicant data."""

    name = ""

    @abstractmethod
    def get(self, case_id: str) -> EligibilityInput:
        """The case's applicant data; CaseNotFound when the source has no such case."""

    def case_options(self) -> list[dict[str, Any]]:
        """The cases the eligibility screen can pick from: [{case_id, label, product}]. [] when the source lists
        none (the LOS provider: the officer opens a case by its id)."""
        return []


def age_on(dob: date | None, as_of: date | None = None) -> int | None:
    """Full years between `dob` and `as_of` (today); None without a date of birth."""
    if dob is None:
        return None
    today = as_of or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _whole(value: Any) -> int | None:
    return None if value is None else int(value)


#: one connection pool per (dsn, timeouts): a new connection per read cost ~65 ms of a ~68 ms assessment (measured)
_POOLS: dict[tuple[str, int, int], Any] = {}
_POOL_LOCK = threading.Lock()


def _waiting_pool_class():
    """A pool that WAITS for a free connection (up to the connect timeout) instead of failing at once: psycopg2's
    pool raises PoolError the moment all `size` connections are out, so 8 parallel requests on a 5-connection pool
    answered 503 for three of them (live, 2026-10-19). Busy is not down."""
    import time

    from psycopg2 import pool

    class WaitingPool(pool.ThreadedConnectionPool):
        def __init__(self, *args, wait_seconds: float = 5.0, **kwargs):
            self._wait_seconds = wait_seconds
            super().__init__(*args, **kwargs)

        def getconn(self, key=None):
            deadline = time.monotonic() + self._wait_seconds
            while True:
                try:
                    return super().getconn(key)
                except pool.PoolError:
                    if self.closed or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)

    return WaitingPool


def _pool(dsn: str, connect_timeout: int, statement_timeout_ms: int, size: int):
    import psycopg2

    key = (dsn, connect_timeout, statement_timeout_ms)
    with _POOL_LOCK:
        found = _POOLS.get(key)
        if found is None or found.closed:
            try:
                from app.agent_db.core import with_options

                # the timeout ADDED to the DSN's options (an options= keyword would drop its schema)
                found = _waiting_pool_class()(1, max(1, size),
                                              with_options(dsn, f"-c statement_timeout={statement_timeout_ms}"),
                                              connect_timeout=connect_timeout, wait_seconds=float(connect_timeout))
            except psycopg2.Error as exc:
                raise ProviderUnavailable(f"cannot connect to the eligibility database ({type(exc).__name__})") from None
            _POOLS[key] = found
        return found


def close_pools() -> None:
    """Close every pooled connection (service shutdown, tests)."""
    with _POOL_LOCK:
        for found in _POOLS.values():
            found.closeall()
        _POOLS.clear()


class PostgresDataProvider(DataProvider):
    """`eligibility_cases` in the database ELIGIBILITY_DSN names (read-only, parameterised, pooled, with timeouts)."""

    name = "postgres"

    def __init__(self, dsn: str | None = None, table: str | None = None, as_of: date | None = None) -> None:
        spec = config.document().get("postgres") or {}
        from app.agents.eligibility.data.migrations import store_dsn

        self.dsn = dsn or store_dsn()
        self.table = str(table or spec.get("table") or "eligibility_cases")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", self.table):
            raise ProviderUnavailable(f"invalid table name in config: {self.table!r}")
        self.connect_timeout = int(config.setting("postgres", "connect_timeout_seconds"))
        self.statement_timeout_ms = int(config.setting("postgres", "statement_timeout_ms"))
        self.pool_size = int(config.setting("postgres", "pool_size"))
        self.as_of = as_of

    def _read(self, query: Any, params: tuple = ()) -> list[tuple]:
        """One read-only query on a pooled connection; a broken connection is discarded, never handed out again."""
        import psycopg2

        if not self.dsn:
            raise ProviderUnavailable("ELIGIBILITY_DSN is not set")
        pool = _pool(self.dsn, self.connect_timeout, self.statement_timeout_ms, self.pool_size)
        try:
            connection = pool.getconn()
        except psycopg2.Error as exc:
            raise ProviderUnavailable(f"cannot connect to the eligibility database ({type(exc).__name__})") from None
        broken = False
        try:
            if not connection.readonly:
                connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
            connection.rollback()                       # read-only: nothing to keep
            return rows
        except psycopg2.Error as exc:
            broken = True
            raise ProviderUnavailable(f"cannot read the eligibility data ({type(exc).__name__})") from None
        finally:
            pool.putconn(connection, close=broken or bool(connection.closed))

    def get(self, case_id: str) -> EligibilityInput:
        from psycopg2 import sql

        query = sql.SQL("SELECT {cols} FROM {table} WHERE case_id = %s").format(
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS), table=sql.Identifier(self.table))
        rows = self._read(query, (str(case_id),))
        row = rows[0] if rows else None
        if row is None:
            raise CaseNotFound(case_id)
        data = dict(zip(_COLUMNS, row))
        return EligibilityInput(
            case_id=str(data["case_id"]), product=(str(data["product"]).upper() if data["product"] else None),
            age=age_on(data["dob"], self.as_of), net_monthly_income=_number(data["net_monthly_income"]),
            existing_emi=_number(data["existing_emi"]), bureau_score=_whole(data["bureau_score"]),
            property_value=_number(data["property_value"]), requested_amount=_number(data["requested_amount"]),
            tenure_months=_whole(data["tenure_months"]), rate_percent=_number(data["rate_percent"]),
            max_dpd_12m=_whole(data["max_dpd_12m"]), write_offs=_whole(data["write_offs"]),
            settlements=_whole(data["settlements"]))

    def case_options(self) -> list[dict[str, Any]]:
        from psycopg2 import sql

        rows = self._read(sql.SQL("SELECT case_id, label, product FROM {table} ORDER BY case_id")
                          .format(table=sql.Identifier(self.table)))
        return [{"case_id": str(r[0]), "label": r[1], "product": r[2]} for r in rows]

    def case_ids(self) -> list[tuple[str, str | None]]:
        """Every (case_id, expected_decision) in the table, for the test suite and demos."""
        from psycopg2 import sql

        rows = self._read(sql.SQL("SELECT case_id, expected_decision FROM {table} ORDER BY case_id")
                          .format(table=sql.Identifier(self.table)))
        return [(str(r[0]), r[1]) for r in rows]


def _amount(value: Any) -> float | None:
    """A figure the LOS stores as text ("5,00,000") as a number; None when absent or unreadable -- never a default."""
    if value is None or not str(value).strip():
        return None
    try:
        return float(str(value).replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10]) if value else None
    except ValueError:
        return None


class LosDataProvider(DataProvider):
    """
    The LOS case store itself (the same repository every LOS route uses -- app.store.get_repository):

      applications  product, loan_amount -> requested_amount, tenure_months, interest_rate_pct -> rate_percent,
                    declared_monthly_income -> net_monthly_income, declared_monthly_obligations -> existing_emi,
                    property value = the LOWER of valuation_value / purchase_price, else the declared property_value
      applicants    date_of_birth -> age (in code)
      bureau        the credit agent's bureau provider (app/agents/credit/bureau -- the demo bureau today, a real one
                    in production): the score; its total monthly obligation fills existing_emi only when the
                    application declares none

    No bureau record for the party -> score None -> DATA_MISSING. A bureau that times out / fails, or a store that
    cannot be read -> ProviderUnavailable (503) -- never a decision.
    """

    name = "los"

    def __init__(self, as_of: date | None = None) -> None:
        self.as_of = as_of
        self.bureau_timeout = float(config.setting("los", "bureau_timeout_seconds"))

    def _bureau(self, party_id: str) -> tuple[int | None, float | None]:
        """(score, total monthly obligation) from the bureau; (None, None) when it holds no record for the party.
        The repayment history (Rule 6) of the same report is kept on `self.history`."""
        self.history = {"max_dpd_12m": None, "write_offs": None, "settlements": None}
        import asyncio

        from app.agents.credit.bureau import BureauError, get_provider as bureau_provider

        try:
            provider = bureau_provider()
            report = asyncio.run(asyncio.wait_for(provider.fetch(party_id, timeout=self.bureau_timeout),
                                                  self.bureau_timeout + 1))
        except (BureauError, asyncio.TimeoutError) as exc:
            raise ProviderUnavailable(f"the bureau could not be read ({type(exc).__name__})") from None
        except Exception as exc:  # noqa: BLE001 - a misconfigured bureau (e.g. demo in production): unavailable
            raise ProviderUnavailable(f"the bureau is not available ({type(exc).__name__})") from None
        if report is None:
            return None, None
        self.history = {"max_dpd_12m": report.max_dpd_12m if report.accounts else 0,
                        "write_offs": report.write_offs, "settlements": report.settlements}
        return (int(report.score) if report.score is not None else None), report.total_monthly_obligation

    def get(self, case_id: str) -> EligibilityInput:
        from app.store import get_repository

        try:
            repository = get_repository()
            application = repository.get_application(case_id)
            applicant = repository.get_applicant(application.applicant_id) if application else None
        except Exception as exc:  # noqa: BLE001 - the case store down: unavailable, never a decision
            raise ProviderUnavailable(f"the LOS case store could not be read ({type(exc).__name__})") from None
        if application is None:
            raise CaseNotFound(case_id)
        score, bureau_emi = self._bureau(application.applicant_id)
        declared_emi = _amount(application.declared_monthly_obligations)
        firm = [v for v in (_amount(application.valuation_value), _amount(application.purchase_price)) if v]
        tenure = _amount(application.tenure_months)
        return EligibilityInput(
            case_id=case_id, product=(str(application.product).upper() if application.product else None),
            age=age_on(_date(getattr(applicant, "date_of_birth", None)), self.as_of),
            net_monthly_income=_amount(application.declared_monthly_income),
            existing_emi=declared_emi if declared_emi is not None else bureau_emi,
            bureau_score=score,
            property_value=min(firm) if firm else _amount(application.property_value),
            requested_amount=_amount(application.loan_amount),
            tenure_months=int(tenure) if tenure is not None else None,
            rate_percent=_amount(application.interest_rate_pct), **self.history)


_PROVIDERS: dict[str, type[DataProvider]] = {"postgres": PostgresDataProvider, "los": LosDataProvider}


def get_provider() -> DataProvider:
    """The configured provider (config `data_provider` / env ELIGIBILITY_DATA_PROVIDER)."""
    name = config.provider_name()
    if name not in _PROVIDERS:
        raise ProviderUnavailable(f"unknown eligibility data provider {name!r} (use: {', '.join(_PROVIDERS)})")
    return _PROVIDERS[name]()


__all__ = ["CaseNotFound", "DataProvider", "LosDataProvider", "PostgresDataProvider", "ProviderUnavailable",
           "age_on", "close_pools", "get_provider"]
