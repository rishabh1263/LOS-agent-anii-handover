"""
A field agency's verification report, built for a scenario (config demo agency_report_layout) -- the tests and the
screen's sample files. `build_pdf(fields)` prints the given fields; `for_case(case_id, ...)` fills them from the
scenario (its applicant, the account on its application, its bank) with any field overridden or left out (None).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pymupdf

from app.agents.credit.bank_verification.config import cfg, demo


def _layout() -> dict[str, Any]:
    return demo()["agency_report_layout"]


def build_pdf(fields: dict[str, str | None]) -> bytes:
    """A one-page report with a text layer: the title, then one "Label: value" line per field given."""
    layout = _layout()
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 60), layout["title"], fontsize=15)
    y = 100
    for key, value in fields.items():
        if value is None:
            continue
        lines = [(layout["labels"]["transaction"].format(n=n), v) for n, v in enumerate(value, start=1)] \
            if key == "transactions" else [(layout["labels"][key], value)]
        for label, text in lines:
            page.insert_text((50, y), f"{label}: {text}", fontsize=11)
            y += 24
    data = doc.tobytes()
    doc.close()
    return data


def build_image(fields: dict[str, str | None], dpi: int = 150) -> bytes:
    """The same report as a photo (PNG, no text layer): read by OCR."""
    with pymupdf.open(stream=build_pdf(fields), filetype="pdf") as doc:
        return doc[0].get_pixmap(dpi=dpi).tobytes("png")


def fields_for(case_id: str, *, ownership: str = "CONFIRMED", agency_id: str | None = None,
               when: date | None = None, **override: str | None) -> dict[str, str | None]:
    """A correct report for a scenario's case; `override` changes or (None) leaves out a field."""
    from app.agents.credit.bank_verification.demo_data import scenarios, seed

    scenario = scenarios.by_case(case_id)
    if scenario is None:                               # not a demo case: only what is known about it
        return {"case_ref": case_id, "ownership": _layout()["ownership_words"][ownership],
                "report_date": (when or date.today()).strftime("%d-%m-%Y"), **override}
    account = seed.application_account(scenario)
    source = scenario["aa"] if scenario.get("aa") is not None else scenario["statement"]["spec"]
    latest = _latest(source)
    agency = agency_id or (scenario.get("agency") or {}).get("agency_id")
    names = {a["id"]: a["name"] for a in cfg().get("agencies") or []}
    fields = {"case_ref": case_id, "application_ref": scenario["case"]["application_ref"],
              "applicant_name": scenario["case"]["applicant_name"], "account_number": account["account_number"],
              "bank_name": account["bank_name"], "account_type": source["details"]["account_type"],
              "transactions": latest, "ownership": _layout()["ownership_words"][ownership],
              "report_date": (when or date.today()).strftime("%d-%m-%Y"),
              "agency": names.get(agency or "", next(iter(names.values()), "")), "remarks": _layout()["remarks"]}
    return {**fields, **override}


def _latest(source: dict[str, Any]) -> list[str]:
    """The statement's latest three as a report writes them: "21-09-2026 / 1,240.50 / Debit"."""
    from app.agents.credit.bank_verification.schemas import StatementData
    from app.agents.credit.bank_verification.services import transaction_service

    data = StatementData.model_validate({**source, "transactions": [{**t, "seq": i} for i, t in
                                                                    enumerate(source["transactions"])]})
    return [f"{t.date.strftime('%d-%m-%Y')} / {t.amount:,.2f} / {t.direction.value.title()}"
            for t in transaction_service.latest(data.transactions).rows]


def for_case(case_id: str, **kw: Any) -> bytes:
    return build_pdf(fields_for(case_id, **kw))
