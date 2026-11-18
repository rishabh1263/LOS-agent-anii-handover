"""
Where each case's vendor bank analysis is kept (settings.yaml store):
    files     one JSON per case under files_dir (development); a case with no upload falls back to demo_dir
    postgres  table postgres_table in the DATABASE_URL database (production), created when missing
Only the normalized four blocks are stored (vendor.py) -- never the raw upload.

ONE STATEMENT PER PERSON: the applicant's is kept under the case id; a co-applicant's under "<case id>--<party>"
(party: the co-applicant's id, e.g. CO-1A2B3C or CO1). `parties(case_id)` lists the co-applicants who have one.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.credit.repayment import config

ROOT = Path(__file__).resolve().parents[4]
_CASE_ID = re.compile(r"^CASE-[0-9A-F]{12}$")
_PARTY = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SEP = "--"


class StoreUnavailable(RuntimeError):
    """The configured store cannot be read or written."""


def _spec() -> dict[str, Any]:
    return config.settings()["store"]


def valid_case_id(case_id: str) -> bool:
    return bool(_CASE_ID.fullmatch(case_id or ""))


def valid_party(party: str | None) -> bool:
    return party is None or bool(_PARTY.fullmatch(party))


def _key(case_id: str, party: str | None = None) -> str:
    if not valid_case_id(case_id) or not valid_party(party):
        raise ValueError("invalid case id or party")
    return case_id if party is None else f"{case_id}{_SEP}{party}"


def _path(folder: str, case_id: str, party: str | None = None) -> Path:
    return ROOT / folder / f"{_key(case_id, party)}.json"


# ---- postgres --------------------------------------------------------------------------------------------------------
def _pg():
    import psycopg

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise StoreUnavailable("DATABASE_URL is not set")
    try:
        conn = psycopg.connect(dsn, connect_timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise StoreUnavailable(f"database unreachable ({type(exc).__name__})") from exc
    table = _spec()["postgres_table"]
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (case_id TEXT PRIMARY KEY, report JSONB NOT NULL, "
                 "received_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    return conn, table


# ---- the store -------------------------------------------------------------------------------------------------------
def save(case_id: str, data: dict[str, Any], party: str | None = None) -> None:
    """Keep (replace) one person's statement analysis (the applicant's when `party` is None)."""
    data = {**data, "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if _spec()["backend"] == "postgres":
        conn, table = _pg()
        with conn:
            conn.execute(f"INSERT INTO {table} (case_id, report) VALUES (%s, %s::jsonb) ON CONFLICT (case_id) "
                         "DO UPDATE SET report = EXCLUDED.report, received_at = now()",
                         (_key(case_id, party), json.dumps(data)))
        return
    path = _path(_spec()["files_dir"], case_id, party)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)                       # written whole, never half a file


def load(case_id: str, party: str | None = None) -> dict[str, Any] | None:
    """One person's analysis (the upload, else the demo report), or None."""
    if not valid_case_id(case_id) or not valid_party(party):
        return None
    spec = _spec()
    if spec["backend"] == "postgres":
        conn, table = _pg()
        with conn:
            row = conn.execute(f"SELECT report FROM {table} WHERE case_id = %s", (_key(case_id, party),)).fetchone()
        if row:
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    else:
        path = _path(spec["files_dir"], case_id, party)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    demo = spec.get("demo_dir")
    if demo and _path(demo, case_id, party).exists():
        return {**json.loads(_path(demo, case_id, party).read_text(encoding="utf-8")), "demo": True}
    return None


def parties(case_id: str) -> list[str]:
    """The co-applicants of a case who have a statement (uploads first, then demo files), in a stable order."""
    if not valid_case_id(case_id):
        return []
    spec, prefix = _spec(), f"{case_id}{_SEP}"
    found: list[str] = []
    if spec["backend"] == "postgres":
        conn, table = _pg()
        with conn:
            rows = conn.execute(f"SELECT case_id FROM {table} WHERE case_id LIKE %s ORDER BY case_id",
                                (prefix + "%",)).fetchall()
        found += [r[0][len(prefix):] for r in rows]
    for folder in (spec.get("files_dir") if spec["backend"] != "postgres" else None, spec.get("demo_dir")):
        if folder and (ROOT / folder).is_dir():
            found += sorted(p.stem[len(prefix):] for p in (ROOT / folder).glob(f"{prefix}*.json"))
    return [p for p in dict.fromkeys(found) if valid_party(p)]


__all__ = ["StoreUnavailable", "load", "parties", "save", "valid_case_id", "valid_party"]
