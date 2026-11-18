"""
BUILD THE LOCAL IFSC MASTER (2026-10-15) -- app/data/ifsc/ifsc.sqlite, read offline by app/agents/passbook/ifsc.py.

Source: Razorpay's open IFSC dataset (MIT licence), compiled from the RBI's published branch lists:
    https://github.com/razorpay/ifsc/releases  -- IFSC.csv, banks.json, sublet.json of one release

    python scripts/build_ifsc_master.py --source <folder with the three files> --release v2.0.62

Only reference data: bank and branch names, city, district, state, MICR. No customer data. Rebuild when a new
release is published; the release and the file hashes are stored in the `meta` table for audit.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "app" / "data" / "ifsc" / "ifsc.sqlite"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source: Path, release: str, target: Path = TARGET) -> dict:
    banks = json.loads((source / "banks.json").read_text(encoding="utf-8"))
    sublet = json.loads((source / "sublet.json").read_text(encoding="utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""
        CREATE TABLE branch (ifsc TEXT PRIMARY KEY, bank_code TEXT NOT NULL, bank TEXT, branch TEXT, city TEXT,
                             district TEXT, state TEXT, micr TEXT, address TEXT) WITHOUT ROWID;
        CREATE TABLE bank (code TEXT PRIMARY KEY, name TEXT, type TEXT) WITHOUT ROWID;
        CREATE TABLE sublet (ifsc TEXT PRIMARY KEY, bank_code TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
    """)
    names: dict[str, str] = {}
    rows = 0
    with (source / "IFSC.csv").open(encoding="utf-8", newline="") as handle:
        batch = []
        for r in csv.DictReader(handle):
            ifsc = (r["IFSC"] or "").strip().upper()
            if len(ifsc) != 11:
                continue
            code = ifsc[:4]
            bank = (r["BANK"] or "").strip()
            if bank:
                names.setdefault(code, bank)
            batch.append((ifsc, code, bank or None, (r["BRANCH"] or "").strip(), (r["CITY"] or "").strip(),
                          (r["DISTRICT"] or "").strip(), (r["STATE"] or "").strip(), (r["MICR"] or "").strip() or None,
                          (r["ADDRESS"] or "").strip()))
            if len(batch) >= 5000:
                db.executemany("INSERT OR REPLACE INTO branch VALUES (?,?,?,?,?,?,?,?,?)", batch)
                rows += len(batch)
                batch = []
        db.executemany("INSERT OR REPLACE INTO branch VALUES (?,?,?,?,?,?,?,?,?)", batch)
        rows += len(batch)
    db.executemany("INSERT OR REPLACE INTO bank VALUES (?,?,?)",
                   [(code, names.get(code), (info or {}).get("type")) for code, info in banks.items()])
    db.execute("UPDATE branch SET bank = (SELECT name FROM bank WHERE bank.code = branch.bank_code) WHERE bank IS NULL")
    db.executemany("INSERT OR REPLACE INTO sublet VALUES (?,?)", [(k.upper(), v) for k, v in sublet.items()])
    meta = {"source": "https://github.com/razorpay/ifsc (MIT), compiled from RBI branch lists", "release": release,
            "built": date.today().isoformat(), "branches": str(rows), "banks": str(len(banks)),
            **{f"sha256:{f}": _sha256(source / f) for f in ("IFSC.csv", "banks.json", "sublet.json")}}
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", list(meta.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.replace(target)
    return meta


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--release", required=True)
    a = ap.parse_args()
    print(json.dumps(build(a.source, a.release), indent=1))
