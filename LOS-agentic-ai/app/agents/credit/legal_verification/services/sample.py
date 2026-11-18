"""
"Fill sample" (DEMO MODE, config ui.show_fill_sample): random realistic values for the Sale Deed details and the
agency report, consistent with the application -- its owners and its address -- and with each other (the report
describes the same deed), so a demo case can be filled in a click. Every pool and range is in
legal_verification_demo.yaml `samples`; nothing is invented here.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from app.agents.credit.legal_verification.config import cfg, demo, demo_mode, section
from app.agents.credit.legal_verification.db import database
from app.agents.credit.legal_verification.rules import fold
from app.agents.credit.legal_verification.services import cases
from app.agents.credit.legal_verification.services.access import Caller, require
from app.agents.credit.legal_verification.services.errors import ServiceError


def _day(rng: random.Random, year_from: int, year_to: int) -> date:
    start = date(year_from, 1, 1)
    return start + timedelta(days=rng.randrange((date(year_to, 12, 31) - start).days))


def deed(app: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    pool = demo().get("samples") or {}
    office = rng.choice(pool["offices"])
    n = rng.randint(*pool["office_numbers"])
    when = _day(rng, *pool["deed_years"])
    address = str(app["property_address"])
    pin = app.get("property_pin") or fold.pin_of(address) or ""
    return {"owner_names": list(app["owner_names"] or []), "seller_names": [rng.choice(pool["sellers"])],
            "document_type": "SALE_DEED", "sale_deed_number": f"{office['code']}-{n}/{rng.randint(1000, 9999)}/{when.year}",
            "sale_deed_date": when.isoformat(), "sub_registrar_office": f"{office['name']} {n}",
            "property_address": address.replace(pin, "").strip(" ,"), "pin_code": pin,
            "area": rng.choice(pool["areas"]),
            "consideration": str(rng.randrange(*pool["consideration"], 50000))}


def report(app: dict[str, Any], details: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    pool = demo().get("samples") or {}
    today = date.today()
    owners = list(details.get("owner_names") or app["owner_names"] or [])
    seller = (details.get("seller_names") or [rng.choice(pool["sellers"])])[0]
    deed_day = fold.to_date(details.get("sale_deed_date")) or _day(rng, *pool["deed_years"])
    first = today.replace(year=today.year - rng.randint(*pool["first_transfer_years_ago"]), month=1, day=1)
    middle = first + timedelta(days=rng.randrange(max(1, (deed_day - first).days - 400)))
    earlier = rng.choice(pool["earlier_owners"])
    office_n = rng.randint(1000, 9999)
    chain = [{"date": first.isoformat(), "instrument": "ALLOTMENT", "reg_no": "", "from_owner": rng.choice(pool["first_holders"]),
              "to_owner": earlier, "document": rng.choice(pool["first_documents"])},
             {"date": middle.isoformat(), "instrument": "SALE_DEED", "reg_no": str(office_n - rng.randint(100, 900)),
              "from_owner": earlier, "to_owner": seller, "document": rng.choice(pool["middle_documents"])},
             {"date": deed_day.isoformat(), "instrument": "SALE_DEED", "reg_no": str(details.get("sale_deed_number") or office_n),
              "from_owner": seller, "to_owner": " and ".join(owners), "document": "Sale Deed"}]
    ec_from = today.replace(year=today.year - rng.randint(*pool["ec_years"]), month=1, day=1)
    pin = details.get("pin_code") or app.get("property_pin") or ""
    stamp = today - timedelta(days=rng.randint(1, 20))
    searches = [{"search_type": t, "searched_at": f"{stamp.isoformat()} {rng.randint(10, 17)}:{rng.randint(10, 59)}",
                 "reference": f"CS-{rng.randint(10000000, 99999999)}",
                 "parameters": rng.choice(pool["search_parameters"]).format(pin=pin), "result": pool["search_result"]}
                for t in (section("cersai").get("search_types") or [])[:2]]
    checks = ((section("property_types").get(app["property_type"]) or {}).get("legal_checks")) or []
    pool_b = demo().get("samples") or {}
    years = int(section("title_search").get("years", 13))
    d = dict(app.get("details") or {})
    place = next((p for p in ((pool.get("application") or {}).get("places") or []) if p.get("city") == d.get("city")), {})
    taluka = place.get("taluka") or d.get("city")
    ptype = app["property_type"]
    flat = ptype == "RESIDENTIAL_FLAT"
    adv = pool["advocate_report"]
    lender = rng.choice(adv["lenders"])
    charged = deed_day + timedelta(days=rng.randint(30, 400))
    released = charged + timedelta(days=rng.randint(700, 1500))
    ec_entries = [{"date": c["date"], "nature": "TRANSFER", "doc_no": c["reg_no"], "executant": c["from_owner"],
                   "claimant": c["to_owner"], "amount": "", "page": str(i + 1)}
                  for i, c in enumerate(chain) if c["reg_no"] and c["date"] >= ec_from.isoformat()]
    if released < today:                              # an earlier home loan, charged and released (the lender's No Due asked)
        ec_entries += [{"date": charged.isoformat(), "nature": "MORTGAGE", "doc_no": str(rng.randint(100, 9999)),
                        "executant": " and ".join(owners), "claimant": lender, "amount": str(rng.randrange(1000000, 4000000, 1000)),
                        "page": str(len(ec_entries) + 1)},
                       {"date": released.isoformat(), "nature": "RELEASE", "doc_no": str(rng.randint(100, 9999)),
                        "executant": lender, "claimant": " and ".join(owners), "amount": "", "page": str(len(ec_entries) + 2)}]
    required = [{"document": f'{c["document"]} no. {c["reg_no"]} dated {c["date"]}', "stage": "PRIOR_TO_MORTGAGE",
                 "copy": "ORIGINAL"} for c in chain if c["reg_no"]]
    if released < today:
        required.append({"document": f"No Due Certificate from {lender}", "stage": "PRIOR_TO_MORTGAGE", "copy": "ORIGINAL"})
    required.append({"document": rng.choice(adv["post_documents"]), "stage": "POST_DISBURSAL", "copy": "ORIGINAL"})
    sides = rng.sample(adv["boundaries"], 4)
    unit = f'{rng.choice(adv["unit_prefixes"])}/{rng.randint(1, 240)}'
    extra = {
        "report_date": (today - timedelta(days=rng.randint(1, 6))).isoformat(),
        "report_reference": f'{rng.choice(adv["reference_prefixes"])}/{today.year}/{rng.randint(1000, 9999)}',
        "advocate_name": rng.choice(adv["advocates"]), "addressed_to": rng.choice(adv["addressed_to"]),
        "documents_examined": [{"instrument": c["instrument"], "reg_no": c["reg_no"], "date": c["date"],
                                "executed_by": c["from_owner"], "in_favour_of": c["to_owner"],
                                "copy": "ORIGINAL" if c is chain[-1] else rng.choice(["ORIGINAL", "CERTIFIED_COPY"]),
                                "page": str(i + 1)} for i, c in enumerate(chain)],
        "project_scheme": rng.choice(adv["schemes"]), "unit_no": unit, "area": rng.choice(pool["areas"]),
        "revenue_survey_no": str(rng.randint(10, 999)), "city_survey_no": f"{rng.randint(100, 9999)}/{rng.randint(1, 9)}",
        "city_survey_ward": str(rng.randint(1, 20)), "sheet_no": str(rng.randint(10, 999)),
        "tp_scheme_no": str(rng.randint(1, 40)), "final_plot_no": str(rng.randint(1, 300)), "village": place.get("area") or d.get("city"),
        "boundary_north": sides[0], "boundary_south": sides[1], "boundary_east": sides[2], "boundary_west": sides[3],
        "mortgage_type": "EQUITABLE", "documents_required": required,
        "opinion_text": rng.choice(adv["opinions"]).format(owners=", ".join(owners)),
        "opinion_conditions": [{"condition": c} for c in rng.sample(adv["conditions"], 2)],
        "sarfaesi_enforceable": "yes",
        "search_from_year": ec_from.year, "search_to_year": today.year,
        "search_sro": str(details.get("sub_registrar_office") or ""), "search_receipt_no": str(rng.randint(10 ** 9, 10 ** 10 - 1)),
        "search_date": stamp.isoformat(), "ec_entries": ec_entries,
    }
    return extra | {"owners": [{"first": n.split()[0], "middle": " ".join(n.split()[1:-1]), "last": n.split()[-1] if len(n.split()) > 1 else ""}
                       for n in owners],
            "property_belongs_to": "APPLICANT" if len(owners) < 2 else "APPLICANT_AND_CO_APPLICANT",
            "documents_provided": ", ".join(pool_b["documents_provided"]),
            "property_history": " ".join(f'{c["date"][:4]}: {c["from_owner"]} to {c["to_owner"]} ({c["document"]}).' for c in chain),
            "document_type": details.get("document_type") or "SALE_DEED",
            "sale_deed_number": details.get("sale_deed_number"), "sale_deed_date": deed_day.isoformat(),
            "sub_registrar_office": details.get("sub_registrar_office"),
            "property_doc_years": max(years, today.year - first.year), "property_type": ptype,
            "title_search": rng.choice(pool_b["title_search"]),
            "registered_mortgage": "no", "signing_authority": rng.choice(pool_b["signing_authorities"]),
            "documents_required_prior": ", ".join(rng.sample(pool["documents_to_deposit"], 3)),
            "documents_post_disbursal": rng.choice(pool_b["post_disbursal"]),
            "title_docs_checked": "yes", "conclusion": rng.choice(pool["observations"]),
            "state": d.get("state"), "district": d.get("district"), "taluka": taluka,
            "title_traced_adequately": "yes", "chain_maintained": "yes", "ec_verified": "yes",
            "flat_title_certified": "yes" if flat else "na", "flat_undivided_share": "yes" if flat else "na",
            "equitable_mortgage_possible": "yes", "title_clear_marketable": "yes", "roc_search": "na",
            "title_history": chain, "ec_from": ec_from.isoformat(), "ec_to": today.isoformat(), "encumbrance_found": "no",
            "cersai_records_found": "no", "cersai_searches": searches}


def application(app: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Random realistic Application Details (pools in the demo file), keeping the owner when one is on file."""
    pool = (demo().get("samples") or {}).get("application") or {}
    d = dict(app.get("details") or {})
    place = rng.choice(pool["places"])
    owner = (app.get("owner_names") or [app["applicant_name"]])[0].split()
    return {"property_usage": rng.choice(pool["usages"]), "property_nature": rng.choice(pool["natures"]),
            "document": "Sale Deed", "owner_first_name": d.get("owner_first_name") or (owner[0] if owner else ""),
            "owner_middle_name": d.get("owner_middle_name") or " ".join(owner[1:-1]),
            "owner_last_name": d.get("owner_last_name") or (owner[-1] if len(owner) > 1 else ""),
            "flat_no": f'{rng.choice(pool["unit_prefixes"])} {rng.randint(101, 1804)}',
            "address_line1": rng.choice(pool["buildings"]), "address_line2": place["area"],
            "project_name": rng.choice(pool["buildings"]), "landmark": rng.choice(pool["landmarks"]),
            "city": place["city"], "district": place["district"], "state": place["state"], "pin_code": place["pin"],
            "owner_pan": d.get("owner_pan") or "", "applicant_pan": d.get("applicant_pan") or ""}


def sample(who: Caller, application_id: str, part: str) -> dict[str, Any]:
    require(who, "view")
    if not (demo_mode() and section("ui").get("show_fill_sample", False)):
        raise ServiceError(403, "DEMO_ONLY")
    with database().tx() as conn:
        app = cases.application(conn, application_id, who)
        case = cases.case_of(conn, application_id, who)
        fields, _, _ = cases.deed_fields(conn, case["case_id"])
    rng = random.Random()
    if part == "deed":
        return {"values": deed(app, rng)}
    if part == "application":
        return {"values": application(app, rng)}
    details = {k: v for k, v in (fields or {}).items() if v} or deed(app, rng)
    return {"values": report(app, details, rng)}


__all__ = ["cfg", "deed", "report", "sample"]
