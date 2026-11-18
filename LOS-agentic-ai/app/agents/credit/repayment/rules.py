"""
The 5 repayment rules (policy.yaml rules): each -> PASS / REFER / DECLINE with its value, its limit and one plain reason.
A figure that could not be measured is never a pass: the rule REFERs and says what is missing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.agents.credit.repayment import config
from app.agents.eligibility.calc.finance import money

PASS, REFER, DECLINE = "PASS", "REFER", "DECLINE"


@dataclass
class Outcome:
    rule: str
    label: str
    result: str
    value: Any
    limit: str
    reason: str
    issues: list[str] = field(default_factory=list)      # what failed, for the next steps (wording next_steps)


def _rs(v: Any) -> str:
    return f"{config.wording().get('currency', '')}{money(v)}"


def num(v: Any) -> str:
    """A figure for a sentence: 100.0 -> "100", 50.25 -> "50.25"."""
    return f"{v:g}" if isinstance(v, float) else str(v)


def _say(rule: str, key: str, **values: Any) -> str:
    """One sentence of wording.yaml rules.<rule>.<key>."""
    return config.wording()["rules"][rule][key].format(**values)


def _limit(rule: str, **values: Any) -> str:
    """The rule's limit in plain words (wording.yaml limits)."""
    return config.wording()["limits"][rule].format(**{k: num(v) for k, v in values.items()})


def _band(value: float, refer: float, decline: float, *, above: bool) -> str:
    """Which side of the two limits a value sits (`above`: higher is worse)."""
    if above:
        return DECLINE if value > decline else REFER if value > refer else PASS
    return DECLINE if value < decline else REFER if value < refer else PASS


def data_complete(m: dict[str, Any]) -> Outcome:
    code, r = "DATA_COMPLETE", config.rule("DATA_COMPLETE")
    found: dict[str, str] = {}
    if m["full_months"] < r["min_full_months"]:
        found["short"] = _say(code, "short", months=m["full_months"], need=r["min_full_months"])
    if m["balance_breaks"] is not None and m["balance_breaks"] > r["max_balance_breaks"]:
        found["breaks"] = _say(code, "breaks", breaks=m["balance_breaks"])
    if r.get("holder_must_match") and m["holder_matches"] is False:
        found["holder"] = _say(code, "holder")
    lines = list(found.values()) or [_say(code, "pass", months=m["full_months"])]
    return Outcome(code, r["label"], REFER if found else PASS, m["full_months"],
                   _limit(code, months=r["min_full_months"]), " ".join(lines), list(found))


def repayment_track(m: dict[str, Any]) -> Outcome:
    code, r = "REPAYMENT_TRACK", config.rule("REPAYMENT_TRACK")
    limit = _limit(code, bounces=r["refer_bounces_window"], missed=r["refer_missed_emis"])
    if not m["window"]:
        return Outcome(code, r["label"], REFER, None, limit, _say(code, "none"))
    b = _band(m["bounces"], r["refer_bounces_window"], r["decline_bounces_window"], above=True)
    e = _band(m["missed_emis"], r["refer_missed_emis"], r["decline_missed_emis"], above=True)
    result = DECLINE if DECLINE in (b, e) else REFER if REFER in (b, e) else PASS
    months = len(m["window"])
    lines = ([_say(code, "bounces", n=m["bounces"], months=months)] if m["bounces"] else []) +             ([_say(code, "missed", n=m["missed_emis"], months=months)] if m["missed_emis"] else [])
    issues = [k for k, n in (("bounces", m["bounces"]), ("missed", m["missed_emis"])) if n]
    return Outcome(code, r["label"], result, {"bounces": m["bounces"], "missed_emis": m["missed_emis"]}, limit,
                   " ".join(lines) if result != PASS else _say(code, "pass", months=months), issues)


def true_foir(m: dict[str, Any], case: dict[str, Any], ob: dict[str, Any]) -> Outcome:
    code, r = "TRUE_FOIR", config.rule("TRUE_FOIR")
    if m["true_foir_pct"] is None:
        return Outcome(code, r["label"], REFER, None, _limit(code, pct=r["refer_above_pct"]), _say(code, "none"))
    result = _band(m["true_foir_pct"], r["refer_above_pct"], r["decline_above_pct"], above=True)
    lines = [_say(code, "ok", pct=num(m["true_foir_pct"]), limit=num(r["refer_above_pct"]))]
    if ob["undeclared_emi"] and result != PASS:          # when it passes, the watch points say it once
        lines.append(_say(code, "hidden", amount=_rs(ob["undeclared_emi"])))
    return Outcome(code, r["label"], result, m["true_foir_pct"], _limit(code, pct=r["refer_above_pct"]),
                   " ".join(lines))


def balance_cover(m: dict[str, Any], case: dict[str, Any]) -> Outcome:
    code, r = "BALANCE_COVER", config.rule("BALANCE_COVER")
    if m["emi_cover_times"] is None:
        return Outcome(code, r["label"], REFER, None, _limit(code, times=r["refer_below_times_emi"]),
                       _say(code, "none"))
    result = _band(m["emi_cover_times"], r["refer_below_times_emi"], r["decline_below_times_emi"], above=False)
    lines = [_say(code, "ok" if result == PASS else "low", abb=_rs(m["abb"]), times=num(m["emi_cover_times"]),
                  emi=_rs(case.get("proposed_emi")), need=num(r["refer_below_times_emi"]))]
    return Outcome(code, r["label"], result, m["emi_cover_times"], _limit(code, times=r["refer_below_times_emi"]),
                   " ".join(lines))


def income_support(m: dict[str, Any], case: dict[str, Any]) -> Outcome:
    code, r = "INCOME_SUPPORT", config.rule("INCOME_SUPPORT")
    if m["income_support_pct"] is None:
        return Outcome(code, r["label"], REFER, None, _limit(code, pct=r["refer_below_pct"]), _say(code, "none"))
    result = _band(m["income_support_pct"], r["refer_below_pct"], r["decline_below_pct"], above=False)
    return Outcome(code, r["label"], result, m["income_support_pct"], _limit(code, pct=r["refer_below_pct"]),
                   _say(code, "ok" if result == PASS else "low", inflow=_rs(m["inflow_median"]),
                        pct=num(m["income_support_pct"]), income=_rs(case.get("declared_income")),
                        need=num(r["refer_below_pct"])))


def evaluate(m: dict[str, Any], case: dict[str, Any], ob: dict[str, Any]) -> list[dict[str, Any]]:
    """All 5 rules, in the policy's order."""
    outcomes = [data_complete(m), repayment_track(m), true_foir(m, case, ob), balance_cover(m, case),
                income_support(m, case)]
    return [asdict(o) for o in outcomes]


def verdict(outcomes: list[dict[str, Any]]) -> str:
    """Any decline -> NOT_RECOMMENDED; any refer -> REFER; all pass -> RECOMMENDED."""
    results = {o["result"] for o in outcomes}
    return "NOT_RECOMMENDED" if DECLINE in results else "REFER" if REFER in results else "RECOMMENDED"


__all__ = ["DECLINE", "PASS", "REFER", "evaluate", "num", "verdict"]
