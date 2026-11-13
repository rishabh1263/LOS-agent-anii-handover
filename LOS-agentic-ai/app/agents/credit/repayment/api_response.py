"""
THE ONE API'S ANSWER -- POST /api/v1/credit/assess/{case_id}. MID-SHORT BY DEFAULT, NOTHING LOST: every point the
officer needs is in the default answer, once; the long lists (month by month, top payers, the dry run's months,
the vendor's figures next to ours, the internal sections) are in `detail` (detail=true).

    decision, can_repay, summary, story, emi            the answer, and the case told in five plain lines; (the eligibility data is used in the backend
                                                        calculations -- declared income, EMIs, the new EMI -- and
                                                        is not repeated here; it is in `detail`)
    report        the vendor report, section by section in the Excel's order: one line, key figures, flags
    insights      our two additions: EMI dry run, money left each month (one line each)
    checks        the 5 checks: label, pass / review / fail, value, limit
    problems, watch, next_steps, working
    people        only with a co-applicant          loans     the loan map, one row per loan
    note, detail (on request), ai_summary (last)
    (the credit agent runs only on run_underwriting=true; its result is in `detail`, not in the answer)

Field names are the contract; every sentence comes from wording.yaml; key figures per section: wording.yaml
compact_keys.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.credit.repayment import config
from app.agents.eligibility.calc.finance import money


# ---- the contract ----------------------------------------------------------------------------------------------------
class Story(BaseModel):
    """The answer as a credit officer would tell it, in five plain lines."""
    customer: str
    found: str
    because: str
    fine: str
    in_one_line: str


class Emi(BaseModel):
    new: float | None = Field(None, description="The EMI of the loan checked (eligibility's).")
    affordable_up_to: float | None = Field(None, description="The most EMI the income carries, all EMIs counted.")
    loan_up_to: float | None = Field(None, description="The loan that EMI repays at the same rate and tenure.")
    note: str | None = None


class Section(BaseModel):
    meaning: str = Field(description="What this part of the vendor report says, in one plain line.")
    key: dict[str, Any] = Field(description="The section's key figures.")
    flags: list[str] | None = Field(None, description="Worth a look (left out when nothing is).")


class Report(BaseModel):
    """The vendor's bank report, section by section (the Excel's own order)."""
    account: Section
    monthly: Section
    balances: Section
    income: Section
    spending: Section
    loans: Section
    bounces: Section
    vendor: Section


class Insights(BaseModel):
    """Not in the vendor report: worked out from its transactions and daily balances (one plain line each)."""
    emi_dry_run: str | None = None
    monthly_surplus: str | None = None


class CheckRow(BaseModel):
    label: str
    result: Literal["pass", "review", "fail"]
    value: str | None = None
    limit: str


class Person(BaseModel):
    role: str
    relationship: str | None = None
    statement: str = Field(description="received / sample data / not received")
    income_in_bank: float | None = None
    emis_in_bank: float | None = None
    bounces: int | None = None


class LoanRow(BaseModel):
    lender: str
    emi: float | None = None
    status: str
    found_by: str
    owner: str | None = None
    missed_months: list[str] | None = None
    taken_on: str | None = None
    note: str | None = Field(None, description="Only when there is something to know about the loan.")


class Underwriting(BaseModel):
    ran: bool
    status: str | None = Field(None, description="READY_FOR_DECISION / REVIEW_REQUIRED / DATA_INSUFFICIENT.")
    message: str
    findings: list[str] | None = Field(None, description="Findings that need review, most severe first.")
    run_id: str | None = Field(None, exclude=True)


class AiSummary(BaseModel):
    text: str
    pending: bool | None = Field(None, description="true: the AI note is being written now; ask again in a few "
                                                   "seconds and it is there (this text is the facts-built note).")
    source: Literal["ai", "rules"] = Field(description="ai: the local model's note, checked; rules: built from the "
                                                       "facts (the model was off, slow, or its text was refused).")


class AssessResponse(BaseModel):
    request_id: str
    case_id: str
    decision: Literal["RECOMMENDED", "REFER", "NOT_RECOMMENDED"]
    can_repay: Literal["YES", "NEEDS_REVIEW", "NO"]
    summary: str
    story: Story
    emi: Emi | None = None
    report: Report
    insights: Insights | None = None
    checks: list[CheckRow]
    problems: list[str] | None = None
    watch: list[str] | None = None
    next_steps: list[str]
    working: list[str] | None = None
    people: list[Person] | None = None
    loans: list[LoanRow] | None = None
    note: str
    detail: dict[str, Any] | None = Field(None, description="Everything behind the answer (detail=true).")
    ai_summary: AiSummary | None = None


# ---- building it -----------------------------------------------------------------------------------------------------
_SEVERITY = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


def _rs(value: Any) -> str:
    return f"{config.wording().get('currency', '')}{money(value)}"


def _num(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def underwriting_block(run: Any | None, *, requested: bool, in_los: bool = True) -> Underwriting:
    """The credit agent's run (None when not asked for, or the case is not a LOS case) in plain words."""
    say = config.wording()["underwriting"]
    if not requested:
        return Underwriting(ran=False, message=say["not_requested"])
    if not in_los:
        return Underwriting(ran=False, message=say["refused"]["NOT_IN_LOS"])
    if run is None or run.status != "SUCCEEDED":
        code = run.error.code if run is not None and run.error else "other"
        return Underwriting(ran=False, run_id=getattr(run, "run_id", None),
                            message=say["refused"].get(code, say["refused"]["other"]))
    out = run.output or {}
    status = out.get("status")
    assessment = out.get("assessment") or {}
    message = say.get(status, status or "")
    if status == "DATA_INSUFFICIENT":
        steps = [r.split(":", 1)[1] for r in assessment.get("status_reasons") or []
                 if r.startswith("REQUIRED_EVIDENCE_UNAVAILABLE:")]
        names = list(dict.fromkeys(say["evidence"].get(s.split(":")[0], s.split(":")[0]) for s in steps))
        if names:
            message += " " + say["missing"].format(items=", ".join(names))
    findings = sorted((f for f in assessment.get("findings") or [] if f.get("status") in ("NEGATIVE", "REVIEW")),
                      key=lambda f: _SEVERITY.get(str(f.get("severity")), 9))
    return Underwriting(ran=True, status=status, message=message, run_id=run.run_id,
                        findings=[str(f.get("description") or "") for f in findings] or None)


def _value(rule: dict[str, Any]) -> str | None:
    value, template = rule.get("value"), config.wording()["check_values"].get(rule["rule"])
    if value is None or not template:
        return None
    if isinstance(value, dict):
        return template.format(bounces=value.get("bounces"), missed=value.get("missed_emis"))
    return template.format(value=_num(value))


def _next_steps(report: dict[str, Any]) -> list[str]:
    say = config.wording()["next_steps"]
    steps: list[str] = []
    if report.get("gated"):
        steps.append(say["eligibility"])
    said = config.wording()["insights"]
    for key, name in (("emi_dry_run", "dry_run"), ("monthly_surplus", "surplus")):
        if ((report.get("insights") or {}).get(key) or {}).get("concern"):
            steps.append(said[name]["step"])
    for o in report["rules"]:
        if o["result"] == "PASS":
            continue
        code = o["rule"]
        if code in ("DATA_COMPLETE", "REPAYMENT_TRACK"):
            need = config.rule("DATA_COMPLETE")["min_full_months"]
            steps += [say[code][i].format(need=need) for i in o.get("issues") or [] if i in say[code]]
        elif code == "TRUE_FOIR":
            s = report.get("suggestion") or {}
            steps.append(say[code]["with_amount"].format(max_loan=_rs(s["max_loan"]), max_emi=_rs(s["max_emi"]))
                         if s.get("max_loan") else say[code]["plain"])
        elif code in say and isinstance(say[code], str):
            steps.append(say[code])
    obligations = report["sections"]["obligations"]
    if obligations.get("undeclared_emi"):
        steps.append(say["undeclared_emi"])
    if any(not p.get("statement_received") for p in report.get("people") or []):
        steps.append(say["co_statement"])
    if any(c.get("agree") is False for c in report["sections"]["vendor_vs_ours"]):
        steps.append(say["vendor_differs"])
    if report["sections"]["data_check"].get("holder_matches") is None:
        steps.append(say["name_not_compared"])
    return list(dict.fromkeys(steps)) or [say["none"]]


def _working(report: dict[str, Any]) -> list[str]:
    say, roles = config.wording()["working"], config.wording()["roles"]
    case, cap, beh = report.get("case") or {}, report["sections"]["capacity"], report["sections"]["behaviour"]
    ob = report["sections"]["obligations"]
    out = []
    income, total = case.get("declared_income"), beh.get("inflow_median")
    with_statement = [p for p in report.get("people") or [] if p.get("statement_received")]
    if income and total is not None:
        pct = _num(beh.get("income_support_pct"))
        if len(with_statement) > 1:
            parts = " + ".join(say["part"].format(who=roles.get(p["role"], p["role"]), amount=_rs(p["income_in_bank"]))
                               for p in with_statement)
            out.append(say["income_many"].format(parts=parts, total=_rs(total), declared=_rs(income), pct=pct))
        else:
            out.append(say["income_one"].format(total=_rs(total), declared=_rs(income), pct=pct))
    out.append(say["emis"].format(declared=_rs(case.get("declared_emi") or 0), bank=_rs(ob["monthly_emi_in_bank"]),
                                  used=_rs(cap["existing_emi"])))
    if cap.get("true_foir_pct") is not None:
        out.append(say["foir"].format(existing=_rs(cap["existing_emi"]), new=_rs(cap["proposed_emi"]),
                                      income=_rs(income), pct=_num(cap["true_foir_pct"]),
                                      limit=_num(config.rule("TRUE_FOIR")["refer_above_pct"])))
    if cap.get("emi_cover_times") is not None:
        days = ", ".join(map(str, config.rule("BALANCE_COVER")["eod_days"]))
        out.append(say["balance"].format(days=days, abb=_rs(cap["abb"]), times=_num(cap["emi_cover_times"]),
                                         new=_rs(cap["proposed_emi"])))
    return out


def _people(report: dict[str, Any]) -> list[Person] | None:
    """Shown only when the case has a co-applicant: alone, the applicant is the account section."""
    people = report.get("people") or []
    if len(people) < 2:
        return None
    roles, status = config.wording()["roles"], config.wording()["statement_status"]
    out = []
    for p in people:
        received = p.get("statement_received")
        shown = status["missing"] if not received else status["sample"] if p.get("demo") else status["received"]
        out.append(Person(role=roles.get(p["role"], p["role"]), relationship=p.get("relationship"), statement=shown,
                          income_in_bank=p.get("income_in_bank"), emis_in_bank=p.get("emi_in_bank"),
                          bounces=p.get("bounces")))
    return out


def _undeclared(loans: list[dict[str, Any]], ob: dict[str, Any]) -> set[int]:
    """The loans that make up the undeclared EMI: the running ones the declared amount does not cover, our check's
    finds first (the customer declared what the vendor also lists)."""
    left = float(ob.get("declared_emi") or 0)
    out: set[int] = set()
    running = [(i, l) for i, l in enumerate(loans) if l.get("running") and l.get("emi")]
    for i, l in sorted(running, key=lambda x: str(x[1].get("found_by", "")).startswith("ours")):
        if left >= l["emi"] * 0.95:
            left -= l["emi"]
        else:
            out.add(i)
    return out


def _loan_note(l: dict[str, Any], undeclared: bool) -> str | None:
    from app.agents.credit.repayment.sections import _day, _label

    say = config.wording()["loan_notes"]
    lines = []
    if str(l.get("found_by", "")).startswith("ours"):
        lines.append(say["our_check"])
    if undeclared:
        lines.append(say["undeclared"])
    if l.get("missed_months"):
        lines.append(say["missed"].format(months=", ".join(_label(m) for m in l["missed_months"])))
    if l.get("new") and (l.get("disbursed") or {}).get("date"):
        lines.append(say["new"].format(date=_day(l["disbursed"]["date"])))
    return " ".join(lines) or None


def _loans(ob: dict[str, Any], household: bool) -> list[LoanRow]:
    from app.agents.credit.repayment.sections import _label

    say, roles = config.wording(), config.wording()["roles"]
    rows = []
    loans = ob.get("loans") or []
    undeclared = _undeclared(loans, ob) if ob.get("undeclared_emi") else set()
    for i, l in enumerate(loans):
        status = "NEW" if l.get("new") else "RUNNING" if l.get("running") else "CLOSED"
        found = "OUR_CHECK" if str(l.get("found_by", "")).startswith("ours") else "VENDOR"
        rows.append(LoanRow(lender=l["lender"], emi=l.get("emi"), status=say["loan_status"][status],
                            found_by=say["found_by"][found],
                            owner=roles.get(l.get("party") or "applicant") if household else None,
                            missed_months=[_label(m) for m in l.get("missed_months") or []] or None,
                            taken_on=(l.get("disbursed") or {}).get("date"),
                            note=_loan_note(l, i in undeclared)))
    return rows


def _compact(name: str, section: dict[str, Any]) -> Section:
    """One section, short: the line, its key figures (wording.yaml compact_keys), the flags."""
    keys = config.wording()["compact_keys"].get(name) or []
    figures = section.get("figures") or {}
    key = {k: figures[k] for k in keys if figures.get(k) is not None}
    for lead, field in (("top_payers", "top_payer"), ("top_payees", "top_payee")):
        if section.get(lead) and field in keys:
            top = section[lead][0]
            key[field] = f"{top['name']} ({_num(top['share_pct'])}%)"
    return Section(meaning=section["meaning"], key=key, flags=section.get("flags") or None)


def build(report: dict[str, Any], *, request_id: str, statement_status: str, underwriting: Underwriting,
          detail: bool = False, explain: bool = True) -> AssessResponse:
    """The internal report (report.py) + the credit agent's result -> the API's answer (blocking: the AI summary)."""
    from app.agents.credit.repayment import ai_summary, story

    say = config.wording()
    groups, s = report["reason_groups"], report["sections"]
    suggestion = report.get("suggestion") or {}
    emi = Emi(new=s["capacity"].get("proposed_emi"), affordable_up_to=suggestion.get("max_emi"),
              loan_up_to=suggestion.get("max_loan"), note=suggestion.get("note"))
    found = report.get("insights") or {}
    dry, surplus = found.get("emi_dry_run"), found.get("monthly_surplus")
    household = len(report.get("people") or []) > 1
    answer = AssessResponse(
        request_id=request_id, case_id=report["case_id"], decision=report["verdict"], can_repay=report["can_repay"],
        summary=report["headline"], story=Story(**story.build(report)),
        emi=emi if any(v is not None for v in emi.model_dump().values()) else None,
        report=Report(**{k: _compact(k, v) for k, v in report["vendor_sections"].items()}),
        insights=Insights(emi_dry_run=(dry or {}).get("line"), monthly_surplus=(surplus or {}).get("line"))
        if dry or surplus else None,
        checks=[CheckRow(label=say["checks"].get(o["rule"], o["label"]), result=say["check_results"][o["result"]],
                         value=_value(o), limit=o["limit"]) for o in report["rules"]],
        problems=groups["problems"] or None, watch=groups["watch"] or None,
        next_steps=_next_steps(report), working=_working(report) or None,
        people=_people(report), loans=_loans(s["obligations"], household) or None,
        note=report["note"],
        detail={
            "statement": {"status": statement_status, **(report.get("bank_data") or {})},
            "report": report["vendor_sections"], "insights": found,
            "checks": [{"rule": o["rule"], "result": o["result"], "finding": o["reason"], "value": o["value"],
                        "limit": o["limit"], "vendor_fields": o.get("vendor_fields")} for o in report["rules"]],
            "good": groups["good"], "people": report.get("people"),
            "vendor_differences": [c for c in s["vendor_vs_ours"] if c["agree"] is False],
            "eligibility": report["eligibility"], "policy": report["policy"],
            "underwriting": {**underwriting.model_dump(exclude_none=True), "run_id": underwriting.run_id},
            "sections": s,
        } if detail else None)
    if explain:
        shown = answer.model_dump()
        shown["_good"] = [o["reason"] for o in report["rules"] if o["result"] == "PASS"]   # the passed checks' findings
        answer.ai_summary = AiSummary(**ai_summary.summarise(shown))
    return answer


__all__ = ["AssessResponse", "build", "underwriting_block"]
