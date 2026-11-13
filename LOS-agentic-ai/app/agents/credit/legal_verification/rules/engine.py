"""
THE TEN RULES -- deterministic; no model decides anything. Each source is kept apart in what a rule shows:

    APPLICATION   what the LOS sent (owners, property address, property type)
    AGENCY        the agency's Verification Report answers -- also the Sale Deed's details (owners, deed number, date,
                  Sub-Registrar office, instrument): the agency reads the deed, the CPA does not re-key it
    AGENT         what the agent itself found about a file (read status, metadata)

A rule says FAIL (a mismatch: red), REVIEW (evidence missing or uncertain: amber -- never a pass by default) or PASS
(green). Missing evidence is never read as "nothing found" (rule 6). Rule 10 sums up and recommends; the CPA decides.
Severities, periods, required documents and checks come from config (legal_verification.yaml).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.agents.credit.legal_verification.config import cfg, label, say, section, words
from app.agents.credit.legal_verification.rules import fold

FAIL, REVIEW, PASS = "FAIL", "REVIEW", "PASS"
ORDER = {FAIL: 0, REVIEW: 1, PASS: 2}
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}


@dataclass
class Evidence:
    application: dict[str, Any]                       # application_id, owner_names, property_address, property_pin, ...
    deed: dict[str, Any] | None = None                # the Sale Deed fields (after corrections); None = no Sale Deed
    deed_sources: dict[str, str] = field(default_factory=dict)   # field -> SALE_DEED | CORRECTED
    deed_file: dict[str, Any] | None = None           # read_status, pages, metadata, subtype, notes
    documents: list[str] = field(default_factory=list)            # the kinds on file
    agency: dict[str, Any] | None = None              # the agency's latest answers; None = no report yet
    today: date = field(default_factory=date.today)


def _w(rule: str, key: str, **values: Any) -> str:
    return say(f"rules.{rule}.{key}", **values)


def _row(label_: str, match: bool | None, note: str | None = None, **values: Any) -> dict[str, Any]:
    """One comparison: the values by source (APPLICATION / SALE_DEED / CORRECTED / AGENCY / AGENT)."""
    # every source the row was meant to compare stays in it: a value not found is None (the screen says so, cleanly)
    shown = {k: (None if v in (None, "", []) else v) for k, v in values.items()}
    return {"label": label_, "values": shown, "match": match, "note": note}


def _result(rule: str, status: str, reason: str, *, rows: list[dict] | None = None, missing: list[str] | None = None,
            action: str | None = None) -> dict[str, Any]:
    spec = (section("rules").get(rule) or {})
    act = None if status == PASS else (action or spec.get("action"))
    return {"id": rule, "number": int(rule[1:]), "name": _w(rule, "name"), "explain": _w(rule, "explain"),
            "why": _w(rule, "why") if status != PASS else None, "status": status,
            "status_label": label("rule_statuses", status),
            "severity": spec.get("severity") if status == FAIL else ("REVIEW" if status == REVIEW else None),
            "severity_label": label("severities", spec.get("severity")) if status == FAIL else None,
            "reason": reason, "comparisons": rows or [], "missing": missing or [],
            "action": act, "action_text": label("actions", act) if act else None}


def _f(key: str) -> str:
    """A report field's label ({years}: the configured title search period)."""
    return label("agency_form.fields", key).replace("{years}", str(section("title_search").get("years", 13)))


def _yn(value: Any) -> str:
    """yes / no / na from an answer (anything else: unanswered)."""
    v = str(value or "").strip().lower()
    return {"true": "yes", "false": "no", "not applicable": "na"}.get(v, v) if v in ("yes", "no", "na", "true", "false",
                                                                                   "not applicable") else ""


def _clause(text: Any) -> str:
    """Free text put inside a sentence that ends with its own full stop."""
    return str(text or "—").strip().rstrip(".") or "—"


# ---------------------------------------------------------------------------------------------------- R1 owners
def owners(ev: Evidence) -> dict[str, Any]:
    app = fold.names_list(ev.application.get("owner_names"))
    if not app:
        return _result("R1", REVIEW, _w("R1", "review_no_app"))
    deed = fold.names_list((ev.agency or {}).get("owner_names"))       # the owners as the agency read them off the deed
    if not deed:
        rows = [_row(_w("R1", "owner", n=i), None, APPLICATION=n) for i, n in enumerate(app, 1)]
        return _result("R1", REVIEW, _w("R1", "review_no_deed"), rows=rows,
                       missing=[label("documents.fields", "owner_names")], action="SEND_TO_AGENCY" if ev.agency is None else None)
    folded, used, pairs = False, set(), {}
    for i, name in enumerate(app):                    # first every owner that matches someone on the deed
        hit = next(((j, fold.name_match(name, d)) for j, d in enumerate(deed) if j not in used and fold.name_match(name, d)),
                   None)
        if hit:
            used.add(hit[0])
            pairs[i] = hit[0]
            folded |= hit[1] == "folded"
    spare = [j for j in range(len(deed)) if j not in used]
    unmatched = [n for i, n in enumerate(app) if i not in pairs]
    rows = []
    for i, name in enumerate(app):                    # an unmatched owner faces the next unmatched name on the deed
        j = pairs.get(i, spare.pop(0) if spare else None)
        rows.append(_row(_w("R1", "owner", n=i + 1), i in pairs, None, APPLICATION=name,
                         AGENCY=deed[j] if j is not None else None))
    for j in spare:                                   # an owner on the deed the application does not name
        rows.append(_row(_w("R1", "owner", n=len(rows) + 1), False, None, AGENCY=deed[j]))
    extra = [deed[j] for j in range(len(deed)) if j not in used]
    if unmatched or extra:
        return _result("R1", FAIL, _w("R1", "fail", names=", ".join(unmatched + extra)), rows=rows)
    return _result("R1", PASS, _w("R1", "pass_folded" if folded else "pass"), rows=rows)


# ---------------------------------------------------------------------------------------------------- R2 the file
def deed_consistency(ev: Evidence) -> dict[str, Any]:
    if ev.deed_file is None:
        return _result("R2", REVIEW, _w("R2", "no_deed"), missing=[label("documents.kinds", "SALE_DEED")],
                       action="UPLOAD_SALE_DEED")
    f, spec = ev.deed_file, section("documents")
    signals, rows = [], []
    meta = f.get("metadata") or {}
    opened = not meta.get("unopenable")
    rows.append(_row(_w("R2", "file"), opened, None, AGENT=_w("R2", "pages", n=f.get("pages")) if opened and f.get("pages")
                     else None))
    if not opened:
        signals.append(_w("R2", "signals.unreadable"))
    made_by = " ".join(str(meta.get(k) or "") for k in ("producer", "creator")).lower()
    tool = next((t for t in (str(x).lower() for x in spec.get("suspicious_producers") or []) if t in made_by), None)
    if made_by.strip():
        rows.append(_row(_w("R2", "producer"), tool is None, None, AGENT=meta.get("producer") or meta.get("creator")))
    if tool:
        signals.append(_w("R2", "signals.editing_tool", tool=tool))
    created, modified = fold.to_date(meta.get("created")), fold.to_date(meta.get("modified"))
    if created and modified:
        late = (modified - created).days * 24 > float(spec.get("modified_after_created_hours", 72))
        if late:
            signals.append(_w("R2", "signals.modified_late", days=(modified - created).days))
    deed = ev.agency or {}                            # the deed's details, as the agency read them
    subtype = deed.get("document_type")
    if subtype and subtype not in ("SALE_DEED", "UNKNOWN"):
        signals.append(_w("R2", "signals.other_instrument", subtype=label("agency_form.options", subtype)))
    if ev.agency is not None and not deed.get("sale_deed_number") and not deed.get("sale_deed_date"):
        signals.append(_w("R2", "signals.no_registration"))
    if signals:
        return _result("R2", REVIEW, _w("R2", "review", signals="; ".join(signals)), rows=rows,
                       missing=[s for s in signals])
    return _result("R2", PASS, _w("R2", "pass"), rows=rows)


# ---------------------------------------------------------------------------------------------------- R3 address
def address(ev: Evidence) -> dict[str, Any]:
    if ev.agency is None:
        return _result("R3", REVIEW, _w("R3", "review_no_report"), missing=[_w("R3", "review_no_report")],
                       action="SEND_TO_AGENCY")
    details = dict(ev.application.get("details") or {})
    rows, differ, missing = [], [], []
    for key in ("district", "state"):
        mine, theirs = details.get(key), ev.agency.get(key)
        same = None if not mine or not theirs else (str(mine).strip().lower() == str(theirs).strip().lower()
                                                    or bool(fold.address_words(mine) & fold.address_words(theirs)))
        name = label("agency_form.fields", key)
        rows.append(_row(name, same, None, APPLICATION=mine, AGENCY=theirs))
        (differ if same is False else missing if same is None else []).append(name)
    if ev.agency.get("taluka"):
        rows.append(_row(label("agency_form.fields", "taluka"), None, None, APPLICATION=ev.application.get("property_address"),
                         AGENCY=ev.agency.get("taluka")))
    if differ:
        return _result("R3", FAIL, _w("R3", "fail", fields=", ".join(differ).lower()), rows=rows)
    if missing:
        return _result("R3", REVIEW, _w("R3", "review", missing=", ".join(missing).lower()), rows=rows, missing=missing)
    return _result("R3", PASS, _w("R3", "pass"), rows=rows)


# ---------------------------------------------------------------------------------------------------- R4 the chain
def history(ev: Evidence) -> dict[str, Any]:
    chain = [c for c in ((ev.agency or {}).get("title_history") or []) if isinstance(c, dict)]
    if not chain:
        a = ev.agency or {}
        told = a.get("property_history")
        traced, kept = _yn(a.get("title_traced_adequately")), _yn(a.get("chain_maintained"))
        rows = [_row(_f("title_traced_adequately"), None if not traced else traced == "yes", None,
                     AGENCY=label("agency_form.options", traced) if traced else None),
                _row(_f("chain_maintained"), None if not kept else kept == "yes", None,
                     AGENCY=label("agency_form.options", kept) if kept else None)]
        if told:
            rows.append(_row(_f("property_history"), None, None, AGENCY=told))
        if "no" in (traced, kept):
            return _result("R4", FAIL, _w("R4", "fail_agency"), rows=rows)
        if traced == kept == "yes" and told:
            return _result("R4", PASS, _w("R4", "pass_agency"), rows=rows)
        return _result("R4", REVIEW, _w("R4", "review_text_only" if told else "review_none"), rows=rows,
                       missing=[_f("title_history")])
    chain = sorted(chain, key=lambda c: fold.to_date(c.get("date")) or date.max)
    # a declaration / rectification fixes a deed and moves no title: it is shown, never a link of the chain
    kinds = section("title_instruments") or {}
    corrections = [c for c in chain if kinds.get(str(c.get("instrument") or "")) == "CORRECTION"]
    chain = [c for c in chain if c not in corrections] or chain
    rows, breaks = [], []
    for i, link in enumerate(chain, 1):
        ok = True
        if i > 1 and not fold.name_match(chain[i - 2].get("to_owner"), link.get("from_owner")):
            ok = False
            breaks.append(f'{chain[i - 2].get("to_owner") or "—"} → {link.get("from_owner") or "—"}')
        rows.append(_row(_w("R4", "link", n=i), ok, link.get("document"),
                         AGENCY=f'{link.get("date") or "—"}: {link.get("from_owner") or "—"} → {link.get("to_owner") or "—"}'))
    current = fold.names_list((ev.agency or {}).get("owner_names")) or fold.names_list(ev.application.get("owner_names"))
    last_ok = any(fold.name_match(n, chain[-1].get("to_owner")) for n in current) or any(
        fold.name_match(n, part) for n in current for part in fold.names_list(str(chain[-1].get("to_owner") or "")
                                                                             .replace(" and ", "\n").replace(",", "\n")))
    if breaks:
        return _result("R4", FAIL, _w("R4", "fail_break", broken="; ".join(breaks)), rows=rows)
    if "no" in (_yn((ev.agency or {}).get("title_traced_adequately")), _yn((ev.agency or {}).get("chain_maintained"))):
        return _result("R4", FAIL, _w("R4", "fail_agency"), rows=rows)
    if current and not last_ok:
        rows[-1]["match"] = False
        return _result("R4", FAIL, _w("R4", "fail_last"), rows=rows)
    first = fold.to_date(chain[0].get("date"))
    years = fold.years_between(first, ev.today) or 0
    need = float(section("title_search").get("years", 30))
    if years < need:
        return _result("R4", REVIEW, _w("R4", "review_short", years=years, need=int(need)), rows=rows,
                       missing=[_w("R4", "review_short", years=years, need=int(need))])
    return _result("R4", PASS, _w("R4", "pass", first=chain[0].get("from_owner") or "—", years=years), rows=rows)


# ---------------------------------------------------------------------------------------------------- R5 registration
def registration(ev: Evidence) -> dict[str, Any]:
    if ev.agency is None:
        return _result("R5", REVIEW, _w("R5", "review_no_report"), missing=[_w("R5", "review_no_report")],
                       action="SEND_TO_AGENCY")
    a = ev.agency
    rows, wrong, unknown = [], [], []
    for key in ("sale_deed_number", "sale_deed_date", "sub_registrar_office"):
        value, name = a.get(key), label("agency_form.fields", key)
        ok: bool | None = None if not str(value or "").strip() else True
        if ok and key == "sale_deed_date":
            when = fold.to_date(value)
            ok = bool(when) and when <= ev.today
        rows.append(_row(name, ok, None, AGENCY=value))
        (wrong if ok is False else unknown if ok is None else []).append(name)
    if wrong:
        return _result("R5", FAIL, _w("R5", "fail", fields=", ".join(wrong).lower()), rows=rows)
    if unknown:
        return _result("R5", REVIEW, _w("R5", "review", fields=", ".join(unknown).lower()), rows=rows, missing=unknown)
    return _result("R5", PASS, _w("R5", "pass"), rows=rows)


# ---------------------------------------------------------------------------------------------------- R6 encumbrance
def encumbrance(ev: Evidence) -> dict[str, Any]:
    a = ev.agency
    if a is None:
        return _result("R6", REVIEW, _w("R6", "review_no_report"), missing=[label("documents.kinds", "ENCUMBRANCE_CERTIFICATE")])
    f = lambda k: label("agency_form.fields", k)  # noqa: E731
    yes = lambda v: str(v).lower() in ("yes", "true", "1")  # noqa: E731
    years = fold.years_between(fold.to_date(a.get("ec_from")), fold.to_date(a.get("ec_to")))
    ec = _yn(a.get("ec_verified"))
    rows = [_row(_f("ec_verified"), None if not ec else ec == "yes", None,
                 AGENCY=label("agency_form.options", ec) if ec else None),
            _row(f("encumbrance_found"), not yes(a.get("encumbrance_found")), a.get("encumbrance_details"),
                 AGENCY=label("agency_form.options", "yes" if yes(a.get("encumbrance_found")) else "no"))]
    period = _row(f"{f('ec_from')} – {f('ec_to')}", None, None,
                  AGENCY=f'{a.get("ec_from")} – {a.get("ec_to")}' if years is not None else None)
    rows.insert(1, period)
    # the CERSAI search (the central registry of charges): an active charge is a finding; no search is missing evidence
    searches = [x for x in (a.get("cersai_searches") or []) if isinstance(x, dict) and any(x.values())]
    cersai_hit = yes(a.get("cersai_records_found"))
    rows.append(_row(_w("R6", "cersai"), not cersai_hit if searches or cersai_hit else None, a.get("cersai_details"),
                     AGENCY=label("agency_form.options", "yes" if cersai_hit else "no") if searches or cersai_hit else None))
    rows.append(_row(_w("R6", "cersai_searches"), True if searches else None,
                     "; ".join(f'{label("agency_form.search_types", x.get("search_type"))}: {x.get("result") or "—"}'
                               for x in searches) or None,
                     AGENCY=_w("R6", "searches_n", n=len(searches)) if searches else None))
    if yes(a.get("encumbrance_found")):
        return _result("R6", FAIL, _w("R6", "fail", details=_clause(a.get("encumbrance_details"))), rows=rows)
    if cersai_hit:
        return _result("R6", FAIL, _w("R6", "fail_cersai", details=_clause(a.get("cersai_details"))), rows=rows)
    reasons, missing = [], []
    if ec != "yes":
        reasons.append(_w("R6", "review_no_ec"))
        missing.append(label("documents.kinds", "ENCUMBRANCE_CERTIFICATE"))
    elif years is not None:                       # a period given: it must cover the configured years
        need = float(section("encumbrance").get("years", 13))
        if years < need:
            reason = _w("R6", "review_short", years=years if years is not None else 0, need=int(need))
            period["match"] = False if years is not None else None
            reasons.append(reason)
            missing.append(reason)
        else:
            period["match"] = True
    if not searches:
        reasons.append(_w("R6", "review_no_cersai"))
        missing.append(_w("R6", "review_no_cersai"))
    if reasons:
        return _result("R6", REVIEW, " ".join(reasons), rows=rows, missing=missing)
    return _result("R6", PASS, _w("R6", "pass", years=years if years is not None else int(section("encumbrance").get("years", 13))),
                   rows=rows)


# ---------------------------------------------------------------------------------------------------- R7 documents
def required_documents(ev: Evidence) -> dict[str, Any]:
    ptype = ev.application.get("property_type")
    need = list(((section("property_types").get(ptype) or {}).get("required_documents")) or [])
    have = set(ev.documents)
    rows = [_row(label("documents.kinds", k), k in have, None,
                 AGENT=label("documents.read", "STORED") if k in have else _w("R7", "not_uploaded"))
            for k in need]
    missing_docs = [label("documents.kinds", k) for k in need if k not in have]
    years_need = int(section("title_search").get("years", 13))
    a = ev.agency or {}
    checked = str(a.get("title_docs_checked") or "").lower()          # the Leviosa form's "checked for last N years"
    rows.append(_row(label("agency_form.fields", "title_docs_checked").format(years=years_need),
                     None if not checked else checked == "yes", None,
                     AGENCY=label("agency_form.options", checked) if checked else None))
    reasons, missing = [], list(missing_docs)
    if missing_docs:
        reasons.append(_w("R7", "review_docs", docs=", ".join(missing_docs)))
    if not checked:
        reasons.append(_w("R7", "review_no_period", need=years_need))
        missing.append(_w("R7", "review_no_period", need=years_need))
    elif checked != "yes":
        reasons.append(_w("R7", "review_not_checked", need=years_need))
        missing.append(_w("R7", "review_not_checked", need=years_need))
    try:
        doc_years = float(a.get("property_doc_years")) if a.get("property_doc_years") not in (None, "") else None
    except (TypeError, ValueError):
        doc_years = None
    rows.append(_row(_f("property_doc_years"), None if doc_years is None else doc_years >= years_need, None,
                     AGENCY=str(a.get("property_doc_years")) if doc_years is not None else None))
    if doc_years is not None and doc_years < years_need:
        reasons.append(_w("R7", "review_years", years=a.get("property_doc_years"), need=years_need))
        missing.append(_w("R7", "review_years", years=a.get("property_doc_years"), need=years_need))
    if reasons:
        return _result("R7", REVIEW, " ".join(reasons), rows=rows, missing=missing)
    return _result("R7", PASS, _w("R7", "pass", n=len(need), years=years_need), rows=rows)


# ---------------------------------------------------------------------------------------------------- R8 legal checks
def legal_checks(ev: Evidence) -> dict[str, Any]:
    ptype = ev.application.get("property_type")
    spec = section("property_types").get(ptype)
    if not spec:
        return _result("R8", REVIEW, _w("R8", "no_type"))
    answers = ev.agency or {}
    rows, adverse, pending = [], [], []
    for code in spec.get("legal_checks") or []:
        state = _yn(answers.get(code))               # yes = done, na = not applicable, no = adverse
        name = _f(code)
        rows.append(_row(name, {"yes": True, "na": True, "no": False}.get(state), None,
                         AGENCY=label("agency_form.options", state) if state else None))
        if state == "no":
            adverse.append(name)
        elif state not in ("yes", "na"):
            pending.append(name)
    if adverse:
        return _result("R8", FAIL, _w("R8", "fail", checks=", ".join(adverse)), rows=rows)
    if pending:
        return _result("R8", REVIEW, _w("R8", "review", checks=", ".join(pending)), rows=rows, missing=pending)
    return _result("R8", PASS, _w("R8", "pass", n=len(rows), type=label("property_types", ptype)), rows=rows)


# ---------------------------------------------------------------------------------------------------- R9 the agency
def agency_cross_check(ev: Evidence, earlier: list[dict[str, Any]]) -> dict[str, Any]:
    a = ev.agency
    if a is None:
        return _result("R9", REVIEW, _w("R9", "no_report"), missing=[_w("R9", "no_report")], action="SEND_TO_AGENCY")
    f = lambda k: label("agency_form.fields", k)  # noqa: E731
    rows, differ, unknown = [], [], []

    def compare(name: str, match: bool | None, **values: Any) -> None:
        rows.append(_row(name, match, None, **values))
        (differ if match is False else unknown if match is None else []).append(name)

    compare(f("property_type"), None if not a.get("property_type") else a.get("property_type") == ev.application.get("property_type"),
            AGENCY=label("property_types", a.get("property_type")),
            APPLICATION=label("property_types", ev.application.get("property_type")))
    problems = [r for r in earlier if r["status"] == FAIL]
    opinion = _yn(a.get("title_clear_marketable"))
    opinion_clear = opinion == "yes"
    rows.append(_row(_f("title_clear_marketable"), False if opinion_clear and problems else (None if not opinion else True),
                     _w("R9", "opinion_note", n=len(problems)) if problems else None,
                     AGENCY=label("agency_form.options", opinion) if opinion else None))
    if differ:
        return _result("R9", FAIL, _w("R9", "fail", fields=", ".join(differ).lower()), rows=rows)
    if opinion_clear and problems:
        return _result("R9", FAIL, _w("R9", "fail_opinion", n=len(problems)), rows=rows)
    if unknown:
        return _result("R9", REVIEW, _w("R9", "review", fields=", ".join(unknown).lower()), rows=rows, missing=unknown)
    return _result("R9", PASS, _w("R9", "pass"), rows=rows)


# ---------------------------------------------------------------------------------------------------- R10 overall
def overall(results: list[dict[str, Any]], ev: Evidence) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rule 10 and the recommendation: (rule 10's result, the overview the screen and the report show)."""
    failed = [r for r in results if r["status"] == FAIL]
    review = [r for r in results if r["status"] == REVIEW]
    passed = [r for r in results if r["status"] == PASS]
    spec = section("recommendation")
    critical = [r for r in failed if r["severity"] in (spec.get("escalate_on_severity") or ["CRITICAL"])]
    if critical or len(failed) >= int(spec.get("escalate_min_failed", 2)):
        recommendation = "ESCALATE"
    elif failed or review:
        recommendation = "REVIEW_REQUIRED"
    else:
        recommendation = "CLEAR_FOR_REVIEW"
    ranked = sorted(failed, key=lambda r: (SEVERITY_ORDER.get(r["severity"] or "", 9), r["number"])) + review
    findings = [f'{r["name"]}: {r["reason"]}' for r in ranked]
    missing = list(dict.fromkeys(m for r in results for m in r["missing"]))
    if ranked:
        action = ranked[0]["action"]
    elif ev.agency is None:
        action = "SEND_TO_AGENCY"
    else:
        action = "READY_FOR_DECISION"
    status = FAIL if failed else REVIEW if review else PASS
    rule = _result("R10", status, _w("R10", "reason", failed=len(failed), review=len(review), passed=len(passed)),
                   action=action)
    rule["severity"] = None if status != FAIL else ("CRITICAL" if critical else "HIGH")
    rule["severity_label"] = label("severities", rule["severity"]) if rule["severity"] else None
    rule["action"], rule["action_text"] = action, label("actions", action)
    overview = {"recommendation": recommendation, "recommendation_label": label("recommendations", recommendation),
                "recommendation_note": label("recommendation_notes", recommendation),
                "counts": {"critical": len(critical), "failed": len(failed), "review": len(review), "passed": len(passed)},
                "findings": findings, "missing": missing, "next_action": action,
                "next_action_text": label("actions", action)}
    return rule, overview


def run(ev: Evidence) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The ten results (in rule order) and the overview."""
    first = [owners(ev), deed_consistency(ev), address(ev), history(ev), registration(ev), encumbrance(ev),
             required_documents(ev), legal_checks(ev)]
    results = first + [agency_cross_check(ev, first)]
    rule10, overview = overall(results, ev)
    return results + [rule10], overview


def ordered(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Red first (critical before high before medium), then amber, then green; rule number within each."""
    return sorted(results, key=lambda r: (ORDER.get(r["status"], 3), SEVERITY_ORDER.get(r.get("severity") or "", 9),
                                          r["number"]))


__all__ = ["Evidence", "FAIL", "PASS", "REVIEW", "ordered", "run", "words", "cfg"]
