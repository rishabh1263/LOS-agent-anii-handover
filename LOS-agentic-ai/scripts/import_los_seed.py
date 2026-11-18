"""
IMPORT THE DUMMY LOS SEED AS APP CASES (config app/config/los_seed_import.yaml).

The seed tables in the LOS store's database (customers, loan_applications, application_participants, ...) are read
only; each seed loan application becomes an ordinary case through fos_api.create_case -- the app's one create path --
so validation, encryption, access grants and case events are exactly those of a form-made case. Re-runnable: a case
whose id already exists is skipped. Prints counts only, never a name, mobile or address.

    python scripts/import_los_seed.py --dry-run            # map everything, write nothing
    python scripts/import_los_seed.py --limit 10           # import the first 10
    python scripts/import_los_seed.py                      # import all
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
CONFIG = ROOT / "app" / "config" / "los_seed_import.yaml"


def config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def _name(*parts: object) -> str | None:
    text = " ".join(str(p).strip() for p in parts if p and str(p).strip())
    return text or None


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def read_seed(dsn: str, limit: int | None) -> list[dict[str, Any]]:
    """Every seed application with its primary applicant, address, collateral and co-applicants (read only)."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        apps = conn.execute(
            "SELECT la.*, c.full_name AS c_name, c.mobile_number AS c_mobile, c.date_of_birth AS c_dob "
            "FROM loan_applications la JOIN customers c ON c.customer_id = la.customer_id "
            "ORDER BY la.application_id" + (" LIMIT %s" if limit else ""), (limit,) if limit else ()).fetchall()
        people = conn.execute(
            "SELECT p.application_id, p.participant_id, p.is_primary, i.first_name, i.middle_name, i.last_name, i.date_of_birth, "
            "i.personal_email, i.relation_with_applicant, i.is_aadhaar_linked_mobile, a.full_address "
            "FROM application_participants p "
            "LEFT JOIN individual_applicant_details i ON i.participant_id = p.participant_id "
            "LEFT JOIN applicant_addresses a ON a.participant_id = p.participant_id "
            "ORDER BY p.application_id, p.is_primary DESC, p.participant_id").fetchall()
        collateral = {r["application_id"]: r for r in conn.execute(
            "SELECT DISTINCT ON (application_id) application_id, property_type FROM collateral_properties "
            "ORDER BY application_id, collateral_property_id")}
    by_app: dict[Any, list[dict[str, Any]]] = {}
    for p in people:
        by_app.setdefault(p["application_id"], []).append(p)
    for a in apps:
        a["people"] = by_app.get(a["application_id"], [])
        a["collateral"] = collateral.get(a["application_id"])
    return apps


def synthetic_pan(participant_id: object, surname: str | None, cfg: dict[str, Any]) -> str:
    """A format-valid, meaningless PAN from a keyed hash of the seed participant id (los_seed_import.yaml)."""
    import hashlib
    import hmac

    spec = cfg.get("synthetic_pan") or {}
    digest = hmac.new(str(spec.get("key", "")).encode(), str(participant_id).encode(), hashlib.sha256).digest()
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    initial = next((ch for ch in str(surname or "").upper() if ch in letters), "X")
    head = "".join(letters[b % 26] for b in digest[:3])
    digits = f"{int.from_bytes(digest[3:6], 'big') % 10000:04d}"
    return f"{head}{str(spec.get('holder_type') or 'P')[:1]}{initial}{digits}{letters[digest[6] % 26]}"


def co_applicant_pans(seeds: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[Any, str]:
    """participant_id -> synthetic PAN for every seed co-applicant; a `same_person` pair shares the first one's."""
    spec = cfg.get("synthetic_pan") or {}
    if not spec.get("enabled"):
        return {}
    out = {p["participant_id"]: synthetic_pan(p["participant_id"], p.get("last_name"), cfg)
           for s in seeds for p in s["people"] if not p["is_primary"] and p.get("first_name")}
    first_co = {s["application_number"]: next((p["participant_id"] for p in s["people"]
                                               if not p["is_primary"] and p.get("first_name")), None) for s in seeds}
    for a, b in spec.get("same_person") or []:
        if first_co.get(a) and first_co.get(b):
            out[first_co[b]] = out[first_co[a]]
    return out


def to_request(seed: dict[str, Any], cfg: dict[str, Any], pans: dict[Any, str] | None = None) -> dict[str, Any]:
    """The create-case body for one seed application (no Aadhaar, no PAN, no stage)."""
    primary = next((p for p in seed["people"] if p["is_primary"]), {})
    product = (cfg.get("product_by_purpose") or {}).get(seed.get("loan_purpose")) or cfg["default_product"]
    application: dict[str, Any] = {
        "product": product,
        "loan_amount": float(seed["loan_amount_requested"]) if seed.get("loan_amount_requested") is not None else None,
        "employment_type": (cfg.get("employment_by_category") or {}).get(seed.get("applicant_category")),
        "tenure_months": int(seed["loan_tenure_years"]) * 12 if seed.get("loan_tenure_years") else None,
        "loan_purpose": seed.get("loan_purpose"),
        "applicant_type": seed.get("applicant_type"),
        "document_received_date": _iso(seed.get("document_received_date")),
        "co_origination": seed.get("co_origination"),
    }
    if product in ("HOME_LOAN", "LOAN_AGAINST_PROPERTY") and seed.get("collateral"):
        kind = (cfg.get("property_type_by_seed") or {}).get(seed["collateral"].get("property_type"))
        if kind:
            application["property_type"] = kind
    linked = seed.get("is_aadhaar_linked_mobile")
    body: dict[str, Any] = {
        "applicant": {
            "full_name": _name(primary.get("first_name"), primary.get("middle_name"), primary.get("last_name"))
            or seed.get("c_name"),
            "mobile": seed.get("applicant_mobile_number") or seed.get("c_mobile"),
            "email": primary.get("personal_email"),
            "date_of_birth": _iso(primary.get("date_of_birth") or seed.get("c_dob")),
            "address": (primary.get("full_address") or None) and str(primary["full_address"])[:500],
            "mobile_aadhaar_linked": linked if isinstance(linked, bool) else None,
        },
        "application": {k: v for k, v in application.items() if v is not None},
        "case_id": f"{cfg['case_id_prefix']}{seed['application_number']}",
        "applicant_id": f"{cfg['applicant_id_prefix']}{seed['customer_id']}",
    }
    if cfg.get("import_co_applicants", True):
        body["co_applicants"] = [
            {k: v for k, v in {"full_name": _name(p.get("first_name"), p.get("middle_name"), p.get("last_name")),
                               "date_of_birth": _iso(p.get("date_of_birth")),
                               "pan": (pans or {}).get(p["participant_id"]),
                               "relationship": p.get("relation_with_applicant")}.items() if v}
            for p in seed["people"] if not p["is_primary"] and p.get("first_name")]
    return body


async def run(args: argparse.Namespace) -> None:
    cfg = config()
    dsn = os.environ.get("LOS_STORE_DSN") or ""
    if not dsn:
        sys.exit("LOS_STORE_DSN is not set")
    seeds = read_seed(dsn, args.limit)
    print(f"seed applications read: {len(seeds)}")
    from fastapi import HTTPException

    from app.api.routes import fos_api
    from app.store import get_repository

    repo = get_repository()
    claims = {"sub": cfg["officer_subject"], "scope": cfg["officer_scopes"]}
    outcome: Counter[str] = Counter()
    products: Counter[str] = Counter()
    known_applicants: set[str] = set()
    pans = co_applicant_pans(seeds, cfg)
    print(f"synthetic co-applicant PANs: {len(pans)} ({len(set(pans.values()))} distinct)")
    from app.agents.los import co_applicants

    for seed in seeds:
        body = to_request(seed, cfg, pans)
        products[body["application"].get("product", "?")] += 1
        if repo.get_application(body["case_id"]) is not None:
            outcome["skipped: already imported"] += 1
            known_applicants.add(body["applicant_id"])
            if not args.dry_run and pans:
                outcome[_backfill_pan(co_applicants, repo, body, seed, pans)] += 1
            continue
        if args.dry_run:
            fos_api.CreateCaseRequest.model_validate(body)       # the body is valid for the create path
            outcome["would create"] += 1
            continue
        # one applicant may hold several cases: the second one names the applicant the first one created, and
        # create_case opens a new case for that applicant without re-writing them
        try:
            await fos_api.create_case(fos_api.CreateCaseRequest.model_validate(body), claims, channel="seed_import")
            outcome["created"] += 1
            known_applicants.add(body["applicant_id"])
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            outcome[f"refused {exc.status_code} {detail.get('error')} {detail.get('field') or ''}".strip()] += 1
        except Exception as exc:  # noqa: BLE001 - one bad row never stops the rest; counted by kind
            outcome[f"error {type(exc).__name__}"] += 1
    print("products:", dict(products))
    for key, n in outcome.most_common():
        print(f"  {key}: {n}")


def _backfill_pan(co_applicants, repo, body: dict[str, Any], seed: dict[str, Any], pans: dict[Any, str]) -> str:
    """An already-imported case: each of its co-applicants gets the synthetic PAN of the seed participant with the
    same name (co_applicants.record_intake, the intake's own update -- sealed; never overwrites another value)."""
    stored = co_applicants.list_for_case(body["case_id"], repo)
    if not stored:
        return "pan backfill: no co-applicant"
    want = {_name(p.get("first_name"), p.get("middle_name"), p.get("last_name")): pans.get(p["participant_id"])
            for p in seed["people"] if not p["is_primary"]}
    done = 0
    for co in stored:
        pan = want.get(co.get("name"))
        if not pan or co.get("pan") == pan:
            continue
        co_applicants.record_intake(body["case_id"], body["applicant_id"], co["co_applicant_id"],
                                    {"pan_number": pan}, repository=repo)
        done += 1
    return "pan backfill: co-applicant PAN set" if done else "pan backfill: already set / no name match"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--dry-run", action="store_true", help="map and validate, write nothing")
    parser.add_argument("--limit", type=int, default=None, help="only the first N seed applications")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
