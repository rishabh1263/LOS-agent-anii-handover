"""
THE TEN RULES, evaluated deterministically for one case against one resolved Credit document policy.

    RULE-01 Product requirements   the EXISTING document policy engine (app/agents/policy/engine.resolve), through the
                                   policy's `base.through_stage` -- the same requirements every other agent sees.
                                   A conflict inside that policy is UNRESOLVED, never settled here.
    RULE-02 Applicant coverage     every party on the case is checked against its role's requirements; a role the
                                   policy does not cover is UNRESOLVED (never "nothing required")
    RULE-03 Missing documents      a mandatory requirement with nothing accepted on file
    RULE-04 Conditional            a requirement added by a case condition; a condition the case cannot answer
                                   (missing attribute) is UNRESOLVED
    RULE-05 Alternatives           only the policy's `accepts` list satisfies a slot; a same-kind document outside it
                                   is reported, never counted
    RULE-06 Period / freshness     months covered, latest date, count, expiry -- a date that cannot be read is
                                   UNRESOLVED, never satisfied
    RULE-07 Supporting documents   a document that must travel with another
    RULE-08 Cross-stage            earlier-stage documents are reused; types the policy collects AT this stage must be
                                   uploaded after the stage was entered
    RULE-09 Exceptions             services/exception_service.py -- only an APPROVED deviation mapped by the policy
    RULE-10 Audit                  services/audit_service.py

No LLM, no randomness, no clock other than `on` (the evaluation date passed in).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date

from app.agents.docs_verify_credit_agent.config import reasons as T
from app.agents.docs_verify_credit_agent.config.policy_loader import EvaluationError, LoadedPolicy
from app.agents.docs_verify_credit_agent.models import requirement as R
from app.agents.docs_verify_credit_agent.models.application import CaseContext, DocumentRecord, Party
from app.agents.docs_verify_credit_agent.models.policy import CreditDocumentPolicy, PeriodRule
from app.agents.docs_verify_credit_agent.services import dates
from app.agents.docs_verify_credit_agent.services import document_inventory_service as inv
from app.agents.docs_verify_credit_agent.services import exception_service, reconciliation_service
from app.agents.docs_verify_credit_agent.services import file_service

Outcome = R.Outcome


@dataclass(frozen=True)
class Slot:
    slot: str
    accepts: tuple[str, ...]
    rule_id: str                         # RULE-02 / RULE-03 / RULE-04
    blocking: bool
    source_rules: tuple[str, ...] = ()   # the product policy rule ids that require it
    conditions: tuple[str, ...] = ()


@dataclass
class Evaluation:
    outcomes: list[Outcome] = field(default_factory=list)
    base: dict = field(default_factory=dict)
    reused_documents: list[str] = field(default_factory=list)
    waivers_applied: list[str] = field(default_factory=list)
    rules_disabled: list[str] = field(default_factory=list)


_OFF: ContextVar[frozenset[str]] = ContextVar("docs_credit_rules_off", default=frozenset())


def on(rule_id: str) -> bool:
    """Whether a rule is switched on for this evaluation (policy `rules` + DOCS_CREDIT_RULES_OFF)."""
    return rule_id not in _OFF.get()


_NAMES: ContextVar[dict[str, str]] = ContextVar("docs_credit_display_names", default={})


def _label(slot: str) -> str:
    return _NAMES.get().get(slot.upper()) or slot.replace("_", " ").lower()


def _who(party: Party) -> str:
    return T.who(party.role)


def _sev(blocking: bool) -> str:
    return R.BLOCKING if blocking else R.NON_BLOCKING


# ---- requirements per party ---------------------------------------------------------------------------------------

def _base(ctx: CaseContext, policy: CreditDocumentPolicy):
    from app.agents.policy import engine

    resolution = engine.resolve(ctx.product, loan_amount=ctx.loan_amount, attributes=ctx.attributes,
                                stage=policy.base.through_stage)
    if not resolution.requirements:
        raise EvaluationError("POLICY_NOT_CONFIGURED", T.error("no_product_requirements", product=ctx.product), 422)
    return resolution


def _other(ctx: CaseContext, policy: CreditDocumentPolicy):
    """The product's requirements for a person other than the applicant, on THEIR OWN attributes (the
    co-applicant's employment): a self-employed co-applicant gives an ITR and business photos, a salaried one a
    salary slip or ITR; not captured -> the product's general checklist. Never the applicant's attributes."""
    from app.agents.policy import engine

    return engine.resolve(ctx.product, loan_amount=ctx.loan_amount, attributes=dict(ctx.co_attributes),
                          stage=policy.base.through_stage)


def _slots_for(party: Party, policy: CreditDocumentPolicy, resolution) -> list[Slot] | None:
    rule = policy.parties.get(party.role)
    if rule is None:
        return None
    slots = _source_slots(party, rule, policy, resolution)
    if slots is None:
        return None
    excluded = {x.upper() for x in rule.exclude}
    slots = [s for s in slots if s.slot not in excluded]
    have = {s.slot for s in slots}
    return slots + [Slot(r.slot.upper(), tuple(a.upper() for a in r.accepts), "RULE-02", rule.blocking,
                         (f"parties.{party.role}.also",)) for r in rule.also if r.slot.upper() not in have]


def _source_slots(party: Party, rule, policy: CreditDocumentPolicy, resolution) -> list[Slot] | None:
    if rule.source == "base":
        # the product's own requirements; for anyone but the primary applicant they are Applicant Coverage (RULE-02)
        primary = party.role == "PRIMARY_APPLICANT"
        return [Slot(q.slot, tuple(q.accepts), ("RULE-04" if q.requirement == "CONDITIONAL" or q.applicable_conditions
                                                 else "RULE-03") if primary else "RULE-02",
                     rule.blocking and policy.base.blocking, tuple(q.rule_ids), tuple(q.applicable_conditions))
                for q in resolution.requirements if q.mandatory]
    if rule.source == "policy":
        return [Slot(r.slot.upper(), tuple(a.upper() for a in r.accepts), "RULE-02", rule.blocking)
                for r in rule.requirements]
    from app.agents.applicant import config as applicant_config

    entries = applicant_config.co_applicant_documents()
    if not entries:
        return None
    return [Slot(e["slot"], tuple(e["accepts"]), "RULE-02", rule.blocking, ("applicant_agent.readiness",))
            for e in entries]


# ---- RULE-06 -------------------------------------------------------------------------------------------------------

def _period(rule: PeriodRule, ctx: CaseContext, party: Party, doc: DocumentRecord, resolution,
            policy: CreditDocumentPolicy, on: date) -> Outcome:
    base = dict(rule_id="RULE-06", check_id=f"{rule.rule_id}:{doc.document_type}", severity=_sev(rule.blocking),
                party_id=party.party_id, party_role=party.role, slot=doc.document_type,
                evidence=[f"document:{doc.document_id}", f"policy:{rule.rule_id}"])
    evidence = resolution.evidence_for(doc.document_type)
    fields = doc.extracted_fields or {}
    name = _label(doc.document_type)
    who = _who(party)                       # whose document: the applicant's or the co-applicant's

    def unresolved(why: str, action: str = "RECHECK_DOCUMENT") -> Outcome:
        return Outcome(status=R.UNRESOLVED, reason=why, recommended_action=action, **base)

    def stale(why: str, action: str = "REQUEST_FRESH_DOCUMENT") -> Outcome:
        return Outcome(status=R.ACTION_REQUIRED, reason=why, recommended_action=action, **base)

    months = rule.min_months or (evidence.get("min_months") if rule.min_months_from else None)
    if rule.min_months_from and not months:
        return unresolved(T.say("no_min_months", name=name), "RESOLVE_POLICY_CONFIGURATION")
    if months:
        start, end = dates.parse(fields.get("period_start")), dates.parse(fields.get("period_end"))
        if start is None or end is None:
            return unresolved(T.say("period_unreadable", name=name, who=who, months=months))
        if not dates.covers_months(start, end, int(months)):
            return stale(T.say("period_short", name=name, who=who, start=start, end=end, months=months))

    count = rule.min_count or (evidence.get("min_count") if rule.min_count_from else None)
    if rule.min_count_from and not count:
        return unresolved(T.say("no_min_count", name=name), "RESOLVE_POLICY_CONFIGURATION")
    if count:
        types = {t.upper() for t in rule.document_types}
        have = [d for d in ctx.party_documents(party)
                if d.document_type in types and inv.state_of(d, policy.document_states) == inv.SATISFIED]
        if len(have) < int(count):
            return stale(T.say("count_short", have=len(have), name=name, who=who, count=count), "REQUEST_DOCUMENTS")

    if rule.max_age_days is not None and rule.date_field:
        when = dates.parse(fields.get(rule.date_field))
        if when is None:
            return unresolved(T.say("date_unreadable", name=name, who=who, field=rule.date_field))
        age = (on - when).days
        if age > rule.max_age_days:
            return stale(T.say("too_old", name=name, who=who, when=when, age=age, max_age=rule.max_age_days))

    if rule.expiry_from or rule.expiry_field:
        expiry_field = rule.expiry_field or evidence.get("expiry_field")
        if not expiry_field:
            return unresolved(T.say("no_expiry_field", name=name), "RESOLVE_POLICY_CONFIGURATION")
        expiry = dates.parse(fields.get(expiry_field))
        if expiry is None:
            return unresolved(T.say("expiry_unreadable", name=name, who=who))
        if expiry < on:
            return stale(T.say("expired", name=name, who=who, expiry=expiry))

    return Outcome(status=R.SATISFIED, reason=T.say("period_ok", name=name, who=who),
                   recommended_action="PROCEED", **base)


# ---- one slot, one party (RULES 2, 3, 4, 5, 8, 9) -------------------------------------------------------------------

def _slot(ctx: CaseContext, party: Party, slot: Slot, policy: CreditDocumentPolicy, out: Evaluation,
          claimed: frozenset[str] = frozenset()) -> tuple[Outcome, DocumentRecord | None]:
    m = reconciliation_service.match(ctx, party, slot.slot, list(slot.accepts), policy, claimed - set(slot.accepts),
                                     cross_stage=on("RULE-08"))
    waiver = exception_service.for_slot(ctx, party, slot.slot, policy) if on("RULE-09") else \
        exception_service.Waiver(None, ())
    base = dict(check_id=f"{party.role}:{slot.slot}", severity=_sev(slot.blocking), party_id=party.party_id,
                party_role=party.role, slot=slot.slot,
                evidence=[f"policy:{r}" for r in slot.source_rules])

    def kind(name: str) -> dict:
        """The outcome's severity from WHAT is wrong (policy `critical_when`): critical only for the kinds listed --
        by default a document missing, not of an accepted type, rejected or mismatched (possibly fake) -- the rest is
        listed and does not block. No `critical_when` in the policy: every open outcome blocks, as before."""
        crit = policy.critical_when is None or name in policy.critical_when
        return {**base, "severity": _sev(slot.blocking and crit)}
    what, who = _label(slot.slot), _who(party)
    accepted = ", ".join(_label(a) for a in slot.accepts)

    if m.state == inv.SATISFIED and not m.stale_for_stage and not m.stage_entry_unknown:
        ref = {**base, "check_id": f"CROSS_STAGE:{party.role}:{slot.slot}",
               "evidence": [f"document:{m.document.document_id}"]}
        if ctx.stage_entered_at and m.document.uploaded_at and m.document.uploaded_at < ctx.stage_entered_at:
            out.reused_documents.append(m.document.document_id)
            if on("RULE-08"):
                out.outcomes.append(Outcome(rule_id="RULE-08", status=R.SATISFIED, recommended_action="PROCEED",
                                            reason=T.say("reused", what=what, who=who), **ref))
        elif on("RULE-08") and m.document.document_type in {t.upper() for t in policy.cross_stage.collect_at_stage}:
            out.outcomes.append(Outcome(rule_id="RULE-08", status=R.SATISFIED, recommended_action="PROCEED",
                                        reason=T.say("collected_at_stage", what=what, who=who),
                                        **ref))
        return Outcome(rule_id=slot.rule_id, status=R.SATISFIED, recommended_action="PROCEED",
                       reason=T.say("verified", what=what, who=who),
                       **{**base, "evidence": base["evidence"] + [f"document:{m.document.document_id}"]}), m.document

    if waiver.approved is not None:
        out.waivers_applied.append(waiver.approved.deviation_id)
        return Outcome(rule_id="RULE-09", status=R.WAIVED, recommended_action="PROCEED",
                       reason=T.say("waived", what=what, who=who, code=waiver.approved.code),
                       deviation_codes=[waiver.approved.code],
                       **{**base, "evidence": base["evidence"] + [f"deviation:{waiver.approved.deviation_id}",
                                                                  f"policy:{waiver.rule_id}"]}), None
    if waiver.pending is not None:
        return Outcome(rule_id="RULE-09", status=R.EXCEPTION_PENDING, recommended_action="AWAIT_EXCEPTION_APPROVAL",
                       reason=T.say("exception_pending", what=what, who=who, code=waiver.pending.code),
                       deviation_codes=[waiver.pending.code],
                       **{**kind("EXCEPTION_PENDING"),
                          "evidence": base["evidence"] + [f"deviation:{waiver.pending.deviation_id}"]}), None

    codes = list(waiver.codes)
    doc_ev = [f"document:{m.document.document_id}"] if m.document else []
    if m.state is None:
        if m.not_permitted and on("RULE-05"):
            given = ", ".join(sorted({_label(d.document_type) for d in m.not_permitted}))
            return Outcome(rule_id="RULE-05", status=R.ACTION_REQUIRED,
                           recommended_action="RAISE_EXCEPTION" if codes else "REQUEST_DOCUMENTS",
                           reason=T.say("not_accepted", who=who, given=given, what=what, accepted=accepted),
                           missing_documents=list(slot.accepts),
                           deviation_codes=codes,
                           **{**kind("NOT_ACCEPTED"), "evidence": base["evidence"] + [f"document:{d.document_id}"
                                                                                      for d in m.not_permitted]}), None
        return Outcome(rule_id=slot.rule_id, status=R.ACTION_REQUIRED, recommended_action="REQUEST_DOCUMENTS",
                       reason=T.say("missing", what=what, who=who, accepted=accepted),
                       missing_documents=list(slot.accepts), deviation_codes=codes, **kind("MISSING")), None
    if m.stale_for_stage:
        return Outcome(rule_id="RULE-08", status=R.ACTION_REQUIRED, recommended_action="REQUEST_FRESH_DOCUMENT",
                       reason=T.say("stale_for_stage", what=what, who=who), missing_documents=list(slot.accepts),
                       **{**kind("STALE_FOR_STAGE"), "evidence": base["evidence"] + doc_ev}), None
    if m.stage_entry_unknown:
        return Outcome(rule_id="RULE-08", status=R.UNRESOLVED, recommended_action="RECHECK_DOCUMENT",
                       reason=T.say("stage_entry_unknown", what=what, who=who),
                       **{**kind("STAGE_ENTRY_UNKNOWN"), "evidence": base["evidence"] + doc_ev}), None
    by_state = {
        inv.PENDING: (R.PENDING_VERIFICATION, "AWAIT_VERIFICATION", T.say("pending_verification", what=what, who=who)),
        inv.REVIEW: (R.UNDER_REVIEW, "AWAIT_VERIFICATION", T.say("under_review", what=what, who=who)),
        inv.REJECTED: (R.ACTION_REQUIRED, "REQUEST_DOCUMENTS", T.say("rejected", what=what, who=who, accepted=accepted)),
        inv.MISMATCH: (R.MISMATCH, "RECHECK_DOCUMENT", T.say("mismatch", what=what, who=who)),
    }
    status, action, reason = by_state.get(m.state, (R.UNRESOLVED, "RECHECK_DOCUMENT",
                                                    T.say("unknown_state", what=what, who=who)))
    kinds = {inv.PENDING: "PENDING_VERIFICATION", inv.REVIEW: "UNDER_REVIEW", inv.REJECTED: "REJECTED",
             inv.MISMATCH: "MISMATCH"}
    return Outcome(rule_id=slot.rule_id, status=status, recommended_action=action, reason=reason,
                   missing_documents=list(slot.accepts) if m.state == inv.REJECTED else [], deviation_codes=codes,
                   **{**kind(kinds.get(m.state, "UNKNOWN_STATE")), "evidence": base["evidence"] + doc_ev}), None


# ---- RULE-07 -------------------------------------------------------------------------------------------------------

def _supporting(ctx: CaseContext, party: Party, policy: CreditDocumentPolicy) -> list[Outcome]:
    out = []
    docs = ctx.party_documents(party)
    for rule in policy.supporting_documents:
        primary = [d for d in docs if d.document_type == rule.when_present.upper()
                   and inv.state_of(d, policy.document_states) in (inv.SATISFIED, inv.PENDING, inv.REVIEW)]
        if not primary:
            continue
        wanted = {t.upper() for t in rule.requires_one_of}
        doc, state = inv.best([d for d in docs if d.document_type in wanted], policy.document_states)
        base = dict(rule_id="RULE-07", check_id=f"{rule.rule_id}:{party.role}", severity=_sev(rule.blocking),
                    party_id=party.party_id, party_role=party.role, slot=rule.when_present.upper(),
                    evidence=[f"document:{primary[0].document_id}", f"policy:{rule.rule_id}"]
                    + ([f"document:{doc.document_id}"] if doc else []))
        need, what = ", ".join(_label(t) for t in rule.requires_one_of), _label(rule.when_present)
        if state == inv.SATISFIED:
            out.append(Outcome(status=R.SATISFIED, recommended_action="PROCEED",
                               reason=T.say("supporting_ok", what=what, need=need, who=_who(party)), **base))
        elif state in (inv.PENDING, inv.REVIEW):
            out.append(Outcome(status=R.PENDING_VERIFICATION, recommended_action="AWAIT_VERIFICATION",
                               reason=T.say("supporting_pending", what=what, need=need, who=_who(party)), **base))
        else:
            out.append(Outcome(status=R.ACTION_REQUIRED, recommended_action="REQUEST_DOCUMENTS",
                               reason=T.say("supporting_missing", what=what, need=need, who=_who(party)),
                               missing_documents=[t.upper() for t in rule.requires_one_of], **base))
    return out


# ---- the case ------------------------------------------------------------------------------------------------------

def evaluate(ctx: CaseContext, loaded: LoadedPolicy, on_day: date) -> Evaluation:
    from app.agents.docs_verify_credit_agent.config import settings as S

    policy = loaded.policy
    _NAMES.set({k.upper(): v for k, v in policy.display_names.items()})
    off = frozenset({r for r, enabled in policy.rules.items() if enabled is False} | set(S.get().rules_off))
    _OFF.set(off)
    resolution = _base(ctx, policy)
    out = Evaluation(base=resolution.provenance(), rules_disabled=sorted(off))

    if not resolution.conflicts:                                                              # RULE-01
        out.outcomes.append(Outcome(
            rule_id="RULE-01", check_id=f"PRODUCT_POLICY:{resolution.policy_id}", status=R.SATISFIED,
            severity=R.BLOCKING, recommended_action="PROCEED",
            reason=T.say("product_list_found", product=_label(ctx.product or ''),
                         count=sum(1 for q in resolution.requirements if q.mandatory)),
            evidence=[f"policy:{r}" for r in resolution.applied_rules]))
    for conflict in resolution.conflicts:                                                     # RULE-01
        out.outcomes.append(Outcome(
            rule_id="RULE-01", check_id=f"CONFLICT:{conflict.slot}", status=R.UNRESOLVED, severity=R.BLOCKING,
            recommended_action="RESOLVE_POLICY_CONFIGURATION", slot=conflict.slot,
            reason=T.say("product_conflict", slot=_label(conflict.slot), detail=conflict.detail),
            evidence=[f"policy:{r}" for r in conflict.rule_ids]))
    # RULE-04: conditions the case cannot answer, ONE item per missing application detail (every rule it holds up
    # is in the evidence), so the officer reads "fill in X" once rather than once per rule
    pending: dict[tuple[str, ...], list] = {}
    for rule in resolution.unevaluated_rules:
        pending.setdefault(tuple(rule.missing_attributes), []).append(rule)
    for attrs, rules in pending.items():
        needs = list(dict.fromkeys(t for r in rules for t in r.would_require))
        details = " and ".join(_label(a) for a in attrs)
        out.outcomes.append(Outcome(
            rule_id="RULE-04", check_id=f"CONDITION:{'+'.join(attrs)}", status=R.UNRESOLVED,
            severity=_sev(policy.unevaluated_conditions_block), recommended_action="CAPTURE_APPLICATION_DATA",
            reason=T.say("condition_unknown", details=details, needs=", ".join(_label(t) for t in needs)),
            missing_documents=needs, evidence=[f"policy:{r.rule_id}" for r in rules]))

    for party in ctx.parties:                                                                 # RULE-02
        # a co-applicant's checklist is the product's own, WITHOUT the applicant's details (the applicant being
        # self-employed does not make the co-applicant need business photos)
        slots = _slots_for(party, policy, resolution if party.role == "PRIMARY_APPLICANT" else _other(ctx, policy))
        if slots is None:
            out.outcomes.append(Outcome(
                rule_id="RULE-02", check_id=f"{party.role}:COVERAGE", status=R.UNRESOLVED, severity=R.BLOCKING,
                recommended_action="RESOLVE_POLICY_CONFIGURATION", party_id=party.party_id, party_role=party.role,
                reason=T.say("coverage_unknown", who=_who(party))))
            continue
        claimed = frozenset(t for s in slots for t in s.accepts)
        for slot in slots:
            outcome, doc = _slot(ctx, party, slot, policy, out, claimed)
            out.outcomes.append(outcome)
            if doc is None or not on("RULE-06"):
                continue
            for rule in policy.period_and_freshness:                                          # RULE-06
                if doc.document_type in {t.upper() for t in rule.document_types}:
                    out.outcomes.append(_period(rule, ctx, party, doc, resolution, policy, on_day))
        if on("RULE-07"):
            out.outcomes.extend(_supporting(ctx, party, policy))                              # RULE-07
        if on("RULE-11") and policy.file_rules is not None:
            out.outcomes.extend(_files(ctx, party, policy))                                   # RULE-11
    out.outcomes = [o for o in out.outcomes if on(o.rule_id)]
    return out


# ---- RULE-11 -------------------------------------------------------------------------------------------------------

def _files(ctx: CaseContext, party: Party, policy: CreditDocumentPolicy) -> list[Outcome]:
    rules = policy.file_rules
    out = []
    for doc in ctx.party_documents(party):
        if inv.state_of(doc, policy.document_states) == inv.REJECTED:
            continue                                          # already asked for again
        limits = rules.by_type.get(doc.document_type) or rules.default
        formats = [f.upper() for f in (limits.formats or rules.default.formats or [])]
        max_mb = limits.max_mb or rules.default.max_mb
        min_kb = limits.min_kb if limits.min_kb is not None else rules.default.min_kb
        name = _label(doc.document_type)
        base = dict(rule_id="RULE-11", check_id=f"{rules.rule_id}:{doc.document_type}", severity=_sev(rules.blocking),
                    party_id=party.party_id, party_role=party.role, slot=doc.document_type,
                    evidence=[f"document:{doc.document_id}", f"policy:{rules.rule_id}"])
        found = file_service.facts(ctx, doc)
        if found is None:
            if rules.if_file_missing == "unresolved":
                out.append(Outcome(status=R.UNRESOLVED, recommended_action="RECHECK_DOCUMENT",
                                   reason=T.say("file_not_stored", name=name, who=_who(party)), **base))
            continue
        size_mb = found.size_bytes / (1024 * 1024)
        problems = []
        if formats and found.format not in formats:
            problems.append(T.say("file_wrong_format", format=found.format, allowed=", ".join(formats)))
        if max_mb and size_mb > max_mb:
            problems.append(T.say("file_too_large", size_mb=size_mb, max_mb=max_mb))
        if min_kb and found.size_bytes < min_kb * 1024:
            problems.append(T.say("file_too_small", size_kb=found.size_bytes / 1024, min_kb=min_kb))
        if problems:
            out.append(Outcome(status=R.ACTION_REQUIRED, recommended_action="REQUEST_FRESH_DOCUMENT",
                               reason=T.say("file_problems", name=name, who=_who(party), problems="; ".join(problems)),
                               missing_documents=[doc.document_type], **base))
        else:
            out.append(Outcome(status=R.SATISFIED, recommended_action="PROCEED",
                               reason=T.say("file_ok", name=name, format=found.format,
                                            size=T.say("size_mb", mb=size_mb) if found.size_bytes >= 1000 * 1024
                                            else T.say("size_kb", kb=found.size_bytes / 1024)),
                               **base))
    return out


__all__ = ["Evaluation", "evaluate"]
