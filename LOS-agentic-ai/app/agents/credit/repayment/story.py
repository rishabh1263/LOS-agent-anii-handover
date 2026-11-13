"""
THE ANSWER AS A SHORT STORY -- how a credit officer would explain the case to a colleague, in five plain lines:

    customer      who they are: what comes in, what EMI they declared, the new EMI
    found         what the bank statement showed about their loans (an undeclared EMI first)
    because       why the answer is what it is: the check that decided it, else "all checks pass"
    fine          what is in order
    in_one_line   all of it in one sentence

Built from the report's own figures (no model); every sentence is in wording.yaml story.
"""

from __future__ import annotations

from typing import Any

from app.agents.credit.repayment import config
from app.agents.eligibility.calc.finance import money


def _rs(v: Any) -> str:
    return f"{config.wording().get('currency', '')}{money(v)}"


def _num(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def _customer(report: dict[str, Any], say: dict[str, Any]) -> str:
    case, income = report.get("case") or {}, report["vendor_sections"]["income"]
    salary = (income.get("figures") or {}).get("salary_each_month")
    payer = (income.get("top_payers") or [{}])[0].get("name")
    money_in = say["salary"].format(amount=_rs(salary), payer=payer) if salary and payer else \
        say["money_in"].format(amount=_rs(report["sections"]["behaviour"].get("inflow_median")))
    co = [p for p in report.get("people") or [] if p["role"] == "co_applicant" and p.get("statement_received")]
    if co:
        money_in += say["with_co"].format(amount=_rs(sum(p.get("income_in_bank") or 0 for p in co)))
    new = case.get("proposed_emi")
    return say["customer"].format(money_in=money_in, declared=_rs(case.get("declared_emi") or 0),
                                  new=_rs(new) if new is not None else say["unknown"])


def _found(report: dict[str, Any], say: dict[str, Any]) -> str:
    ob = report["sections"]["obligations"]
    hidden = [l for l in ob.get("loans") or [] if str(l.get("found_by", "")).startswith("ours") and l.get("running")]
    if ob.get("undeclared_emi"):
        who = f" ({', '.join(l['lender'] for l in hidden)})" if hidden else ""
        return say["found_undeclared"].format(bank=_rs(ob["monthly_emi_in_bank"]), hidden=_rs(ob["undeclared_emi"]),
                                              who=who)
    if ob.get("monthly_emi_in_bank"):
        return say["found_match"].format(bank=_rs(ob["monthly_emi_in_bank"]))
    return say["found_none"]


def _problem(o: dict[str, Any], report: dict[str, Any], say: dict[str, Any]) -> str:
    """The deciding check, in a few words (wording.yaml story.problems)."""
    words, cap = say["problems"], report["sections"]["capacity"]
    code, value = o["rule"], o.get("value")
    if code == "TRUE_FOIR" and value is not None:
        hidden = report["sections"]["obligations"].get("undeclared_emi")
        limit = _num(config.rule("TRUE_FOIR")["refer_above_pct"])
        return words["TRUE_FOIR_hidden"].format(amount=_rs(hidden), pct=_num(value), limit=limit) if hidden \
            else words["TRUE_FOIR"].format(pct=_num(value), limit=limit)
    if code == "REPAYMENT_TRACK" and isinstance(value, dict):
        parts = [words[k].format(n=n) for k, n in (("bounces", value.get("bounces")), ("missed", value.get("missed_emis")))
                 if n]
        return " and ".join(parts) or words["REPAYMENT_TRACK_none"]
    if code == "DATA_COMPLETE":
        need = config.rule("DATA_COMPLETE")["min_full_months"]
        parts = [words[i].format(months=value, need=need) for i in o.get("issues") or [] if i in words]
        return " and ".join(parts) or words["holder"]
    if code == "BALANCE_COVER" and value is not None:
        return words[code].format(times=_num(value), abb=_rs(cap.get("abb")))
    if code == "INCOME_SUPPORT" and value is not None:
        return words[code].format(pct=_num(value))
    return words.get(f"{code}_none") or words.get(code, o["reason"]).format(pct="-", times="-", bounces="-", missed="-")


def build(report: dict[str, Any]) -> dict[str, str]:
    say = config.wording()["story"]
    order = {"DECLINE": 0, "REFER": 1}
    open_ = sorted((o for o in report["rules"] if o["result"] != "PASS"), key=lambda o: order[o["result"]])
    passed = [say["fine_words"][o["rule"]] for o in report["rules"] if o["result"] == "PASS"]
    rank = {code: i for i, code in enumerate(say["fine_order"])}
    best = [say["fine_nouns"][o["rule"]] for o in sorted((o for o in report["rules"] if o["result"] == "PASS"),
                                                         key=lambda o: rank.get(o["rule"], 9))][:2]
    if report.get("gated"):
        because = report["reason_groups"]["problems"][0]
        problem = say["eligibility_problem"]
    elif open_:
        problem = _problem(open_[0], report, say)
        because = problem[0].upper() + problem[1:] + "."
    else:
        problem, because = "", say["all_pass"]
    fine = say["fine"].format(items=", ".join(passed)) if passed else say["nothing_fine"]
    verdict = say["verdict"][report["can_repay"]]
    if problem:
        line = (say["one_line_mixed"] if best else say["one_line_bad"]).format(
            good=" and the ".join(best), problem=problem, verdict=verdict)
    else:
        line = say["one_line_good"].format(verdict=verdict)
    line = line[0].upper() + line[1:]
    return {"customer": _customer(report, say), "found": _found(report, say), "because": because, "fine": fine,
            "in_one_line": line}


__all__ = ["build"]
