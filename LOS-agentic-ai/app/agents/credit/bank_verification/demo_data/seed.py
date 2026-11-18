"""
Reset the demo: wipe the bank verification store, then seed the twelve scenarios and the historical cases, and run
verification once for each scenario (as the CPA would) so the dashboard starts with real results.

    python -m app.agents.credit.bank_verification.demo_data.seed
"""

from __future__ import annotations

import copy
from typing import Any

from app.agents.credit.bank_verification.config import demo, words
from app.agents.credit.bank_verification.demo_data import scenarios, statements
from app.agents.credit.bank_verification.repository import Repository
from app.agents.credit.bank_verification.schemas import EnteredDetails, Provenance, StatementData
from app.agents.credit.bank_verification.services import statement_service, transaction_service


def actor() -> str:
    return str(demo().get("seed_actor") or "system:demo-seed")


def _render(statement: dict[str, Any]) -> tuple[bytes, str]:
    spec = statement["spec"]
    if statement.get("render") == "scanned":
        return statements.build_scanned_pdf(spec, statement.get("dpi")), "statement_scan.pdf"
    if statement.get("render") == "csv":
        return statements.build_csv(spec), "statement.csv"
    return statements.build_pdf(spec, producer=statement.get("producer"), modified=statement.get("modified")), \
        "statement.pdf"


def _source_json(spec: dict[str, Any]) -> dict[str, Any]:
    """A statement spec as the AA response the simulated aggregator returns."""
    data = StatementData.model_validate({**spec, "transactions": [{**t, "seq": i} for i, t in enumerate(spec["transactions"])]})
    return {"provider": "SIMULATED ACCOUNT AGGREGATOR (demo)", "simulated": True,
            "data": data.model_dump(mode="json")}


def form_for(scenario: dict[str, Any]) -> dict[str, Any] | None:
    """The CPA's form for a scenario: AA prefill (AA_SUCCESS) or typed from the statement (manual), plus its tweaks."""
    tweaks = scenario.get("form") or {}
    if scenario["mode"] == "AA_SUCCESS" and not tweaks:
        return None                                   # the service fills it from AA
    source = scenario["aa"] if scenario["mode"] == "AA_SUCCESS" else scenario["statement"]["spec"]
    data = StatementData.model_validate({**source, "transactions": [{**t, "seq": i} for i, t in enumerate(source["transactions"])]})
    form = transaction_service.prefill(data, Provenance.AA if scenario["mode"] == "AA_SUCCESS" else Provenance.MANUAL)
    values = form.model_dump(mode="json")
    for key in ("account_number", "account_type", "bank_name"):
        if key in tweaks:
            values[key] = tweaks[key]
            values["provenance"][key] = Provenance.MANUAL.value
    for index, change in (tweaks.get("transactions") or {}).items():
        values["transactions"][index].update(change)
        values["provenance"][f"transaction_{index + 1}"] = Provenance.MANUAL.value
    return EnteredDetails.model_validate(values).model_dump(mode="json")


def application_account(scenario: dict[str, Any]) -> dict[str, Any]:
    """The bank account on the scenario's loan application: the one its form carries (a scenario whose form differs
    from the statement means the customer gave another account), else the statement's own."""
    source = scenario["aa"] if scenario.get("aa") is not None else scenario["statement"]["spec"]
    tweaks = scenario.get("form") or {}
    return {"bank_name": tweaks.get("bank_name") or source["details"]["bank_name"],
            "account_number": tweaks.get("account_number") or source["details"]["account_number"]}


def reset(repo: Repository | None = None, *, start_state: bool = True) -> dict[str, Any]:
    """`start_state` (the screen's start state, owner 2026-10-26): nothing is verified yet, and an AA-failure case has NO
    statement on file -- its statement is a file in `<home>/sample_statements/` for the officer to upload. AA-success
    cases keep theirs (the AA brings it). `start_state=False` (the tests): every statement on file and every case processed
    with its scenario form, so the expected results can be checked."""
    from app.agents.credit.bank_verification.services import verification_service

    repo = repo or Repository()
    repo.purge_demo()          # ONLY the demo cases and their files: a real case and its audit trail are never touched
    samples = repo.root / "sample_statements"
    samples.mkdir(parents=True, exist_ok=True)
    for old in samples.glob("*"):
        old.unlink()
    stored: dict[str, bytes] = {}
    for item in scenarios.historical() + scenarios.SCENARIOS:
        case = {**item["case"], "mode": item["mode"], "status": str(words("case_status.seeded")),
                "scenario": item.get("code")}
        repo.put_case({**case, "is_demo": True})              # a demo case: --purge-demo removes it
        if "code" in item:
            repo.add_source(case["case_id"], "APPLICATION", application_account(item))
        same = item["statement"].get("same_file_as")
        content, name = (stored[same], "statement.pdf") if same else _render(item["statement"])
        stored[case["case_id"]] = content
        if start_state and "code" in item and item["mode"] == "AA_FAILURE":
            # uploaded by the officer, never on file before
            (samples / f"{case['case_id']}_{case['applicant_name'].replace(' ', '_')}_{name}").write_bytes(content)
            repo.audit(case["case_id"], actor(), "CASE_SEEDED", {"scenario": item.get("code"), "mode": item["mode"]})
            continue
        data, facts = statement_service.read(content, name, seeded=True)
        keys = [f"{t.date.isoformat()}|{t.amount}|{t.direction.value}" for t in (data.transactions if data else [])]
        repo.add_document(case["case_id"], "STATEMENT", content, file_name=name, kind=facts.kind, sha256=facts.sha256,
                          fingerprint=facts.fingerprint, account_number=data.details.account_number if data else None,
                          transaction_keys=keys, seeded=True)
        if item.get("aa") is not None:
            repo.add_source(case["case_id"], "AA", _source_json(copy.deepcopy(item["aa"])))
        form = form_for(item) if "code" in item else None
        if form is not None:
            repo.add_source(case["case_id"], "DEMO_FORM", form)
        repo.audit(case["case_id"], actor(), "CASE_SEEDED",
                   {"scenario": item.get("code"), "mode": item["mode"], "synthetic": True})
        if item["case"].get("historical"):
            repo.update_case(case["case_id"], status=str(demo()["historical_status"]), risk_level="NONE",
                             assigned_agency=str(demo()["historical_agency"]))
    if start_state:
        from app.agents.credit.bank_verification.demo_data import reports

        folder = repo.root / "sample_reports"
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*"):
            old.unlink()
        for item in scenarios.SCENARIOS:
            agency = item.get("agency") or {}
            name = f"{item['case']['case_id']}_{item['case']['applicant_name'].replace(' ', '_')}_agency_report.pdf"
            (folder / name).write_bytes(reports.for_case(item["case"]["case_id"],
                                                         ownership=agency.get("ownership") or "CONFIRMED",
                                                         agency_id=agency.get("agency_id")))
    processed = 0
    if not start_state:
        for item in scenarios.SCENARIOS:
            form = repo.latest_source(item["case"]["case_id"], "DEMO_FORM")
            verification_service.process(
                repo, {"case_id": item["case"]["case_id"], "mode": item["mode"], "statement_source": "SEEDED",
                       "entered": form}, upload=None, actor=actor(), idempotency_key=None)
            processed += 1
    return {"cases": len(scenarios.SCENARIOS), "historical": len(scenarios.historical()), "processed": processed}


if __name__ == "__main__":
    print(reset())
