"""
The CLOSED set of signals an underwriting rule may read.

A signal is one named value taken from an OBSERVATION the graph collected,
carried with the evidence references it came from. A rule can read only a
signal listed in SIGNALS; the policy loader refuses any other input.

EVERY VALUE HERE IS SELECTED, NOT COMPUTED -- with exactly two exceptions,
both DECLARED-vs-RECORDED comparisons the approved design allows:

  income.declared_vs_recorded_diff_pct
      the applicant's DECLARED monthly income (application) against the
      salary-slip amount the income-consistency check RECORDED. This is not
      the slip-vs-bank income-consistency check, which stays the LOS flow's.
  obligations.bureau_vs_declared_diff_pct
      the bureau-reported monthly obligation against the DECLARED
      obligations on the application. Primary applicant only: the
      application records one declared figure and does not split it by party.

Both use one symmetric, bounded difference: |a - b| / max(|a|, |b|) * 100
(0 when both are 0). A DEMO metric, like every threshold it is compared with.

UNAVAILABLE IS A RESULT, never a default. A signal whose observation is
missing, failed, or lacks the key is returned with available=False and the
reason -- never as 0, False or "clean".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.agents.credit.schemas import EvidenceRef, Observation, Quality, UnderwritingContext


@dataclass
class SignalValue:
    signal: str
    party_id: str | None
    available: bool
    value: Any = None
    refs: list[EvidenceRef] = field(default_factory=list)
    quality: str | None = None
    reason: str | None = None
    is_demo: bool = False
    step: str | None = None                # the plan category that supplies it


@dataclass(frozen=True)
class SignalDef:
    name: str
    scope: str                             # "case" | "party" | "primary"
    step: str                              # plan category supplying it
    resolve: Callable[["Observations", UnderwritingContext, str | None], SignalValue]


class Observations:
    """Read access to what the graph collected: observation per (tool, party)."""

    def __init__(self, observations: list[Observation], executed: dict[str, int]) -> None:
        self._observations = observations
        self._executed = executed

    def get(self, tool: str, party_id: str | None = None) -> Observation | None:
        index = self._executed.get(f"{tool}|{party_id or ''}")
        return None if index is None else self._observations[index]


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def symmetric_diff_pct(a: Any, b: Any) -> float | None:
    x, y = _number(a), _number(b)
    if x is None or y is None:
        return None
    scale = max(abs(x), abs(y))
    if scale == 0:
        return 0.0
    return round(float(abs(x - y) / scale * 100), 2)


def _unavailable(name: str, party_id: str | None, step: str, reason: str,
                 observation: Observation | None = None) -> SignalValue:
    return SignalValue(signal=name, party_id=party_id, available=False, reason=reason,
                       quality=observation.quality.value if observation else None,
                       is_demo=bool(observation and observation.is_demo), step=step)


def _why(observation: Observation | None, tool: str) -> str:
    if observation is None:
        return f"{tool} was not executed"
    if observation.quality is not Quality.PRESENT and observation.error:
        return f"{tool}: {observation.error}"
    return f"{tool}: {observation.quality.value}"


def _refs(observation: Observation, *fields: str, party_id: str | None = None) -> list[EvidenceRef]:
    return [e for e in observation.evidence
            if e.field in fields and (party_id is None or e.party_id in (None, party_id))]


def _field_signal(name: str, tool: str, key: str, ref_field: str, step: str,
                  *, party_scoped: bool = False) -> SignalDef:
    """A value read verbatim from one observation's data."""

    def resolve(obs: Observations, ctx: UnderwritingContext, party_id: str | None) -> SignalValue:
        observation = obs.get(tool, party_id if party_scoped else None)
        if observation is None or observation.quality not in (Quality.PRESENT,
                                                              Quality.LOW_CONFIDENCE):
            return _unavailable(name, party_id, step, _why(observation, tool), observation)
        data = observation.data or {}
        if data.get(key) is None:
            return _unavailable(name, party_id, step, f"{tool}: {key} not recorded", observation)
        refs = _refs(observation, ref_field, party_id=party_id if party_scoped else None)
        if not refs:
            return _unavailable(name, party_id, step, f"{tool}: no evidence reference for {key}",
                                observation)
        return SignalValue(signal=name, party_id=party_id, available=True, value=data[key],
                           refs=refs, quality=observation.quality.value,
                           is_demo=observation.is_demo, step=step)

    return SignalDef(name, "party" if party_scoped else "case", step, resolve)


def _bureau(field_name: str) -> SignalDef:
    return _field_signal(f"bureau.{field_name}", "bureau.get", field_name, field_name,
                         "bureau", party_scoped=True)


def _bank(key: str) -> SignalDef:
    return _field_signal(f"bank.{key}", "bank_behaviour.get", key, f"evidence.{key}", "banking")


def _kyc_status() -> SignalDef:
    name, step = "kyc.status", "kyc"

    def resolve(obs: Observations, ctx: UnderwritingContext, party_id: str | None) -> SignalValue:
        observation = obs.get("kyc.get")
        if observation is None or observation.quality is Quality.UNAVAILABLE:
            return _unavailable(name, party_id, step, _why(observation, "kyc.get"), observation)
        party = (observation.data.get("parties") or {}).get(party_id)
        if not party or not party.get("status"):
            return _unavailable(name, party_id, step, "kyc.get: no KYC recorded for this party",
                                observation)
        refs = _refs(observation, "status", party_id=party_id)
        refs = [r for r in refs if r.party_id == party_id]
        if not refs:
            return _unavailable(name, party_id, step, "kyc.get: no evidence reference",
                                observation)
        return SignalValue(signal=name, party_id=party_id, available=True,
                           value=party["status"], refs=refs, quality="PRESENT", step=step)

    return SignalDef(name, "party", step, resolve)


def _documents_not_passed() -> SignalDef:
    name, step = "documents.not_passed_count", "documents"

    def resolve(obs: Observations, ctx: UnderwritingContext, party_id: str | None) -> SignalValue:
        observation = obs.get("documents.get")
        if observation is None or observation.quality is not Quality.PRESENT:
            return _unavailable(name, None, step, _why(observation, "documents.get"), observation)
        count = observation.data.get("not_passed_count")
        if count is None:
            return _unavailable(name, None, step, "documents.get: count not recorded", observation)
        not_passed = [e for e in observation.evidence
                      if (e.value_summary or "").upper() not in ("PASS", "NOT_RECORDED")]
        # With nothing failing, the evidence for "0" is the verdicts that passed.
        refs = not_passed if count else [e for e in observation.evidence
                                         if (e.value_summary or "").upper() == "PASS"]
        if not refs:
            return _unavailable(name, None, step, "documents.get: no evidence reference",
                                observation)
        return SignalValue(signal=name, party_id=None, available=True, value=count, refs=refs,
                           quality="PRESENT", step=step)

    return SignalDef(name, "case", step, resolve)


def _declared_vs_recorded_income() -> SignalDef:
    name, step = "income.declared_vs_recorded_diff_pct", "income"

    def resolve(obs: Observations, ctx: UnderwritingContext, party_id: str | None) -> SignalValue:
        application, income = obs.get("application.get"), obs.get("income.get")
        if application is None or application.quality is not Quality.PRESENT:
            return _unavailable(name, None, step, _why(application, "application.get"), application)
        if ctx.declared_monthly_income is None:
            return _unavailable(name, None, step, "declared monthly income not captured",
                                application)
        if income is None or income.quality is not Quality.PRESENT:
            return _unavailable(name, None, step, _why(income, "income.get"), income)
        recorded = income.data.get("documented_monthly_income")
        diff = symmetric_diff_pct(ctx.declared_monthly_income, recorded)
        if diff is None:
            return _unavailable(name, None, step, "no recorded salary-slip amount to compare",
                                income)
        refs = _refs(application, "declared_monthly_income") + _refs(income, "salary_slip.amount")
        if len(refs) < 2:
            return _unavailable(name, None, step, "missing evidence reference", income)
        return SignalValue(signal=name, party_id=None, available=True, value=diff, refs=refs,
                           quality="PRESENT", step=step)

    return SignalDef(name, "case", step, resolve)


def _bureau_vs_declared_obligations() -> SignalDef:
    name, step = "obligations.bureau_vs_declared_diff_pct", "bureau"

    def resolve(obs: Observations, ctx: UnderwritingContext, party_id: str | None) -> SignalValue:
        application, bureau = obs.get("application.get"), obs.get("bureau.get", party_id)
        if bureau is None or bureau.quality is not Quality.PRESENT:
            return _unavailable(name, party_id, step, _why(bureau, "bureau.get"), bureau)
        if ctx.declared_monthly_obligations is None or application is None:
            return _unavailable(name, party_id, step, "declared obligations not captured",
                                application)
        diff = symmetric_diff_pct(bureau.data.get("total_monthly_obligation"),
                                  ctx.declared_monthly_obligations)
        if diff is None:
            return _unavailable(name, party_id, step, "bureau obligation not reported", bureau)
        refs = _refs(bureau, "total_monthly_obligation") + \
            _refs(application, "declared_monthly_obligations")
        if len(refs) < 2:
            return _unavailable(name, party_id, step, "missing evidence reference", bureau)
        return SignalValue(signal=name, party_id=party_id, available=True, value=diff,
                           refs=refs, quality="PRESENT", is_demo=bureau.is_demo, step=step)

    return SignalDef(name, "primary", step, resolve)


SIGNALS: dict[str, SignalDef] = {d.name: d for d in (
    _bureau("score"), _bureau("history_months"), _bureau("max_dpd_12m"),
    _bureau("settlements"), _bureau("write_offs"), _bureau("enquiries_6m"),
    _bank("returned_transaction_count"), _bank("mandate_debit_count"),
    _field_signal("income.consistency_status", "income.get", "consistency_status", "status",
                  "income"),
    _declared_vs_recorded_income(),
    _field_signal("eligibility.status", "eligibility.get", "status", "status", "eligibility"),
    _field_signal("risk.final_outcome", "risk.get", "final_outcome", "final_outcome", "risk"),
    _kyc_status(),
    _documents_not_passed(),
    _bureau_vs_declared_obligations(),
)}


def subjects_for(definition: SignalDef, ctx: UnderwritingContext) -> list[str | None]:
    if definition.scope == "party":
        return [p.party_id for p in ctx.parties]
    if definition.scope == "primary":
        return [ctx.applicant_id]
    return [None]


def resolve_all(names: list[str], observations: Observations,
                ctx: UnderwritingContext) -> dict[tuple[str, str | None], SignalValue]:
    """Every named signal for every subject it applies to."""
    resolved: dict[tuple[str, str | None], SignalValue] = {}
    for name in names:
        definition = SIGNALS[name]
        for party_id in subjects_for(definition, ctx):
            resolved[(name, party_id)] = definition.resolve(observations, ctx, party_id)
    return resolved


__all__ = ["Observations", "SIGNALS", "SignalDef", "SignalValue", "resolve_all",
           "subjects_for", "symmetric_diff_pct"]
