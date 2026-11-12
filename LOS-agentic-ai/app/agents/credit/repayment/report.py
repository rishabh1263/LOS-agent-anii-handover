"""
THE REPAYMENT REPORT: can this applicant repay the proposed EMI? -- the eligibility case data and the vendor's bank
analysis read together, the way a credit officer does, in one clean answer:

    verdict + one line + reasons (problems | watch points | good points)   RECOMMENDED / REFER / NOT_RECOMMENDED
    5 rules                                                                policy.yaml
    sections   data check | declared vs bank | obligations (karz ka naksha) | capacity | behaviour |
               vendor vs ours | vendor report (its own sheets and field names)
    suggestion the EMI and loan this account supports

This is the internal record (the credit agent reads it through MCP); api_response.py shapes what the API returns.
Every sentence comes from wording.yaml. An assessment for the officer, never the credit decision itself.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.credit.repayment import config, crosscheck, household, insights, rules, sections
from app.agents.credit.repayment.rules import num
from app.agents.eligibility.calc import finance

CAN_REPAY = {"RECOMMENDED": "YES", "REFER": "NEEDS_REVIEW", "NOT_RECOMMENDED": "NO"}


def _rs(v: Any) -> str:
    return f"{config.wording().get('currency', '')}{finance.money(v)}"


def case_facts(eligibility: dict[str, Any], applicant_name: str | None = None) -> dict[str, Any]:
    """What the case declares (eligibility inputs) and the EMI proposed (eligibility's, else from the asked loan)."""
    inp = eligibility.get("inputs") or {}
    proposed = eligibility.get("emi")
    amount, months, rate = inp.get("loan_amount"), inp.get("tenure_months"), inp.get("interest_rate_pct")
    if proposed is None and amount and months and rate is not None:
        proposed = round(finance.emi(float(amount), float(rate), int(months)), 2)
    return {"declared_income": inp.get("monthly_income"), "declared_emi": inp.get("monthly_obligations"),
            "proposed_emi": proposed, "loan_amount": amount, "tenure_months": months, "rate_pct": rate,
            "applicant_name": applicant_name}


def _watch(m: dict[str, Any], ob: dict[str, Any]) -> list[str]:
    """Points to look at (policy.yaml observations decides when; wording.yaml watch says what)."""
    o, say = config.policy()["observations"], config.wording()["watch"]
    out = []
    if ob["new_loans"]:
        out.append(say["new_loans"].format(n=ob["new_loans"], months=o["new_loans_months"]))
    if ob["undeclared_emi"]:
        out.append(say["undeclared_emi"].format(amount=_rs(ob["undeclared_emi"])))
    if m["cash_share_pct"] is not None and m["cash_share_pct"] > o["cash_deposit_share_pct"]:
        out.append(say["cash"].format(pct=num(m["cash_share_pct"])))
    if m["top_payer_share_pct"] is not None and m["top_payer_share_pct"] > o["top_payer_share_pct"]:
        out.append(say["top_payer"].format(pct=num(m["top_payer_share_pct"])))
    if m["spike_times_median"] is not None and m["spike_times_median"] > o["spike_times_median"]:
        out.append(say["spike"].format(times=num(m["spike_times_median"])))
    if m["abb_trend_pct"] is not None and m["abb_trend_pct"] <= -o["falling_balance_pct"]:
        out.append(say["falling_balance"].format(pct=num(abs(m["abb_trend_pct"]))))
    return out


_ORDER = {"RECOMMENDED": 0, "REFER": 1, "NOT_RECOMMENDED": 2}


def _gate(verdict: str, eligibility: dict[str, Any]) -> str:
    """The worse of the bank verdict and the most the eligibility decision allows (policy.yaml eligibility_gate)."""
    cap = (config.policy().get("eligibility_gate") or {}).get(str(eligibility.get("decision")), "REFER")
    return max(verdict, cap, key=_ORDER.__getitem__)


def _eligibility_reason(eligibility: dict[str, Any]) -> str:
    say = config.wording()["eligibility"]
    decision = str(eligibility.get("decision"))
    missing = eligibility.get("missing_fields") or []
    if missing:
        # field codes in the eligibility screen's own plain words ("bureau score is missing")
        from app.agents.eligibility.output.summary import _missing_words

        detail = "; ".join(_missing_words(missing) or [])
    else:
        detail = "; ".join(str(r).rstrip(".") for r in eligibility.get("reasons") or [] if r)
    # a product or rule code left in a reason reads as words: CAR_LOAN -> Car Loan
    detail = re.sub(r"\b[A-Z]+(?:_[A-Z]+)+\b", lambda m: m.group(0).replace("_", " ").title(), detail)
    if detail[:2].istitle():                                    # "No terms ..." inside the line reads "no terms ..."
        detail = detail[0].lower() + detail[1:]
    return say["line"].format(decision=say["decisions"].get(decision, decision.lower()),
                              detail=f" ({detail})." if detail else ".")


def _vendor_report(bank: dict[str, Any], window: list[str]) -> dict[str, Any]:
    """The vendor's own sheets, under its own names: the summary sheets, its EMI lists, and Monthly Details for the
    months the rules read (vendor_fields.yaml vendor_view.in_report)."""
    spec = config.vendor()["vendor_view"]
    view = bank.get("vendor_view") or {}
    out = {name: view[name] for name in spec.get("in_report") or [] if view.get(name)}
    grid = view.get(spec["month_grid"]) or {}
    if grid:
        out[spec["month_grid"]] = {k: grid[k] for k in window if k in grid}
    return out


def _sourced(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each rule with the vendor sheet / fields it read (vendor_fields.yaml sources)."""
    sources = config.vendor().get("sources") or {}
    return [{**o, "vendor_fields": sources.get(o["rule"], [])} for o in outcomes]


def _suggestion(m: dict[str, Any], case: dict[str, Any]) -> dict[str, Any] | None:
    """The EMI the income carries at the FOIR limit (all bank EMIs counted), and the loan that EMI repays."""
    income, proposed = case.get("declared_income"), case.get("proposed_emi")
    if not income or proposed is None:
        return None
    say = config.wording()["suggestion"]
    limit = config.rule("TRUE_FOIR")["refer_above_pct"]
    max_emi = round(max(0.0, income * limit / 100 - m["existing_emi"]), 2)
    loan = None
    if max_emi and case.get("tenure_months") and case.get("rate_pct") is not None:
        loan = round(finance.principal_for_emi(max_emi, float(case["rate_pct"]), int(case["tenure_months"])), -3)
    fits = proposed <= max_emi
    if fits:
        note = say["fits"].format(max_emi=_rs(max_emi))
    elif not max_emi:
        note = say["none"]
    else:
        note = say["lower"].format(max_emi=_rs(max_emi), loan=say["loan"].format(amount=_rs(loan)) if loan else "")
    return {"max_emi": max_emi, "max_loan": loan, "fits": fits, "note": note}


def _people(people: list[dict[str, Any]], co_applicants: list[dict[str, Any]],
            applicant_name: str | None) -> list[dict[str, Any]]:
    """Each person on the case: who they are, whether their statement arrived, and what it shows."""
    shown = {"applicant": {"name": applicant_name, "relationship": None, "declared_income": None}}
    out = []
    for p in people:
        acc, m, ob = p["data"]["account"], p["m"], p["ob"]
        info = p.get("info") or shown["applicant"]
        out.append({"role": p["role"], "party": p.get("party"), "name": info.get("name"),
                    "relationship": info.get("relationship"), "statement_received": True,
                    "demo": bool(p["data"].get("demo")), "bank": acc.get("bank"),
                    "account": acc.get("account_number_masked"), "full_months": m["full_months"],
                    "declared_income": info.get("declared_income"), "income_in_bank": m["inflow_median"],
                    "emi_in_bank": ob["monthly_emi_in_bank"], "bounces": m["bounces"],
                    "missed_emis": ob["missed_emis"], "name_matches": m["holder_matches"]})
    for co in co_applicants:
        if not co.get("data"):
            out.append({"role": "co_applicant", "party": co.get("party"), "name": co.get("name"),
                        "relationship": co.get("relationship"), "statement_received": False,
                        "declared_income": co.get("declared_income")})
    return out


def _missing_statements(co_applicants: list[dict[str, Any]]) -> list[str]:
    """A co-applicant on the case (or whose income is clubbed) with no bank statement: their income is unverified."""
    say = config.wording()["watch"]
    out = []
    for co in co_applicants:
        if co.get("data"):
            continue
        who = co.get("relationship") or co.get("name")
        who = f" ({who})" if who else ""
        income = co.get("declared_income")
        out.append(say["co_no_statement_income"].format(who=who, income=_rs(income)) if income
                   else say["co_no_statement"].format(who=who))
    return out


def build(case_id: str, eligibility: dict[str, Any], bank: dict[str, Any], *,
          applicant_name: str | None = None, co_applicants: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The full report for one case: the applicant's statement, and each co-applicant's when it arrived
    (`co_applicants`: [{party, name, relationship, declared_income, data | None}])."""
    pol, say = config.policy(), config.wording()
    case = case_facts(eligibility, applicant_name)
    co_applicants = list(co_applicants or [])
    people = [household.person("applicant", bank, case, declared_income=case.get("declared_income"),
                               declared_emi=case.get("declared_emi"), name=applicant_name)]
    clubbed = sum(float(co.get("declared_income") or 0) for co in co_applicants)
    total = case.get("declared_income")
    people[0]["info"] = {"name": applicant_name, "relationship": None,
                         "declared_income": round(float(total) - clubbed, 2) if total else None}
    for co in co_applicants:
        if co.get("data"):
            people.append({**household.person("co_applicant", co["data"], case,
                                              declared_income=co.get("declared_income"), declared_emi=0,
                                              name=co.get("name")), "party": co.get("party"), "info": co})
    m, ob = household.combine(people, case)
    outcomes = rules.evaluate(m, case, ob)
    verdict = rules.verdict(outcomes)
    problems = [o["reason"] for o in outcomes if o["result"] != rules.PASS]
    gated = _gate(verdict, eligibility)
    gated_by_eligibility = gated != verdict
    if gated_by_eligibility:
        verdict = gated
        problems.insert(0, _eligibility_reason(eligibility))
    good = [o["reason"] for o in outcomes if o["result"] == rules.PASS]
    me = people[0]
    checks = crosscheck.build(bank, me["m"], me["ob"])
    for p in people[1:]:
        checks += [{**c, "party": p.get("party")} for c in crosscheck.build(p["data"], p["m"], p["ob"])]
    proposed_emi = case.get("proposed_emi")
    ours = insights.build(people, m, ob, proposed_emi, case.get("declared_income"), case.get("tenure_months"))
    said = say["insights"]
    extra = []
    if (ours["emi_dry_run"] or {}).get("concern"):
        extra.append(said["dry_run"]["watch"].format(n=ours["emi_dry_run"]["of"] - ours["emi_dry_run"]["cleared"]))
    if (ours["monthly_surplus"] or {}).get("concern"):
        extra.append(said["surplus"]["watch"])
    watch = _missing_statements(co_applicants) + _watch(m, ob) + extra + crosscheck.differences(checks)
    foir_failed = any(o["rule"] == "TRUE_FOIR" and o["result"] != rules.PASS for o in outcomes)
    if foir_failed and ob["undeclared_emi"]:
        # the failed FOIR check already says the undeclared EMIs: said once, as the problem
        hidden = say["watch"]["undeclared_emi"].format(amount=_rs(ob["undeclared_emi"]))
        watch = [w for w in watch if w != hidden]
    proposed = case.get("proposed_emi")
    account = bank["account"]
    return {
        "case_id": case_id,
        "verdict": verdict,
        "verdict_label": say["verdict_labels"][verdict],
        "can_repay": CAN_REPAY[verdict],
        "headline": say["headline"][verdict].format(
            emi=_rs(proposed) if proposed is not None else say["headline"]["no_emi"]),
        "reasons": problems + watch + good,
        "reason_groups": {"problems": problems, "watch": watch, "good": good},
        "gated": gated_by_eligibility,
        "rules": _sourced(outcomes),
        "suggestion": _suggestion(m, case),
        "case": case,
        "people": _people(people, co_applicants, applicant_name),
        "vendor_sections": sections.build(people, m, ob, checks, proposed_emi),
        "insights": ours,
        "sections": {
            "data_check": {
                "vendor": bank.get("vendor"), "bank": account.get("bank"), "account_type": account.get("account_type"),
                "account": account.get("account_number_masked"),
                "period": [account.get("period_start"), account.get("period_end")],
                "months": len(bank["monthly"]), "full_months": m["full_months"], "window": m["window"],
                "balance_breaks": m["balance_breaks"], "holder_matches": m["holder_matches"]},
            "declared_vs_bank": [
                {"item": "Monthly income", "declared": case.get("declared_income"), "bank": m["inflow_median"],
                 "match_pct": m["income_support_pct"]},
                {"item": "Monthly EMIs", "declared": case.get("declared_emi"), "bank": ob["monthly_emi_in_bank"],
                 "undeclared": ob["undeclared_emi"]},
                {"item": "Account holder", "declared": applicant_name, "bank": account.get("holder_name"),
                 "match": m["holder_matches"]}],
            "obligations": ob,
            "capacity": {
                "proposed_emi": proposed, "existing_emi": m["existing_emi"], "true_foir_pct": m["true_foir_pct"],
                "eligibility_foir_pct": (eligibility.get("foir") or {}).get("value_pct"), "abb": m["abb"],
                "emi_cover_times": m["emi_cover_times"], "abb_trend_pct": m["abb_trend_pct"],
                "lowest_balance": m["lowest_balance"]},
            "behaviour": {
                "bounces": m["bounces"], "missed_emis": m["missed_emis"], "cash_share_pct": m["cash_share_pct"],
                "top_payer_share_pct": m["top_payer_share_pct"], "spike_times_median": m["spike_times_median"],
                "monthly_inflow": m["monthly_inflow"], "income_support_pct": m["income_support_pct"],
                "inflow_median": m["inflow_median"]},
            "vendor_vs_ours": checks,
            "vendor_report": _vendor_report(bank, m["window"]),
        },
        "eligibility": {"decision": eligibility.get("decision"), "eligible_amount": eligibility.get("eligible_amount"),
                        "requested_amount": eligibility.get("requested_amount"), "emi": eligibility.get("emi"),
                        "source": eligibility.get("source") or "eligibility"},
        "policy": {"version": pol.get("version"), "signed_off": bool(pol.get("signed_off"))},
        "note": say["note"],
    }


__all__ = ["CAN_REPAY", "build", "case_facts"]
