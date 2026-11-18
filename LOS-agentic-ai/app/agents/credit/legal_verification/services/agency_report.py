"""
The agency's Verification Report as a document (owner 2026-10-29: "sab manually fill hone ke baad agency ka auto report
generate karo, auto attach, then submit -- report random nahi, related analysis ke baad hi").

Built ONLY from what the agency entered and the application it was sent -- nothing invented, nothing sampled:
    1. the analysis: one line per finding, each traceable to the answers it reads (owners against the application,
       registration, location, the title chain and its span, the search period, encumbrance, CERSAI, the legal opinion,
       the agency's overall opinion against its own findings); an unanswered item says so
    2. the answers, section by section in the Leviosa form's order
The PDF is stored like any upload (config storage.documents_dir) and attached to the case as the agency's report
(kind AGENCY_REPORT, metadata.generated); the report row keeps its document and the analysis (migration 0005).
Every word comes from the wording yaml (agency_pdf.*, agency_form.*).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from app.agents.credit.legal_verification.config import documents_dir, label, say, section, words
from app.agents.credit.legal_verification.rules import fold
from app.agents.credit.legal_verification.services import report as pdfkit
from app.agents.credit.legal_verification.services.audit import new_id

OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"
_MARK = {OK: ("#067647", "#e8f6ee"), WARN: ("#b54708", "#fff4e5"), BAD: ("#b42318", "#fdecea"), INFO: ("#1e3a8a", "#eef3ff")}


def _a(key: str, **values: Any) -> str:
    return say(f"agency_pdf.analysis.{key}", **values)


def _f(key: str) -> str:
    return label("agency_form.fields", key).replace("{years}", str(section("title_search").get("years", 13)))


def _yn(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in ("yes", "no", "na") else ""


def _clause(text: Any) -> str:
    return str(text or "").strip().rstrip(".")


def _same_party(a: Any, b: Any) -> bool:
    """The same person or institution, as two entries write it (titles, Ltd., spacing aside)."""
    if not a or not b:
        return False
    if fold.name_match(str(a), str(b)):
        return True
    x, y = fold.address_words(str(a)), fold.address_words(str(b))
    return len(x & y) >= min(2, len(x), len(y)) > 0


def _rows(answers: dict[str, Any], fid: str) -> list[dict[str, Any]]:
    return [r for r in (answers.get(fid) or []) if isinstance(r, dict)]


def _same_place(a: Any, b: Any) -> bool | None:
    if not a or not b:
        return None
    return str(a).strip().lower() == str(b).strip().lower() or bool(fold.address_words(a) & fold.address_words(b))


# ------------------------------------------------------------------------------------------------- the analysis
def analysis(app: dict[str, Any], answers: dict[str, Any], today: date | None = None) -> list[dict[str, str]]:
    """The findings, in order: [{tone: ok | warn | bad | info, text}]. Deterministic: the same answers, the same lines."""
    today = today or date.today()
    out: list[dict[str, str]] = []
    add = lambda tone, key, **v: out.append({"tone": tone, "text": _a(key, **v)})  # noqa: E731

    # 0. the report itself: who signed, when, under which reference
    gone = [_f(k).lower() for k in ("report_date", "report_reference", "advocate_name") if not answers.get(k)]
    if gone:
        add(WARN, "header_missing", fields=", ".join(gone))

    # 1. the owners on the deed against the application
    mine, theirs = fold.names_list(app.get("owner_names")), fold.names_list(answers.get("owner_names"))
    if not theirs:
        add(WARN, "owners_none")
    elif mine and len(mine) == len(theirs) and all(any(fold.name_match(x, y) for y in theirs) for x in mine):
        add(OK, "owners_match", names=", ".join(theirs))
    else:
        add(BAD, "owners_differ", agency=", ".join(theirs), application=", ".join(mine) or "—")

    # 1b. whose property it is, and its type against the application
    if answers.get("property_belongs_to") == "THIRD_PARTY":
        add(WARN, "third_party")
    ptype, app_type = answers.get("property_type"), app.get("property_type")
    if ptype and app_type and ptype != app_type:
        add(BAD, "type_differ", agency=label("property_types", ptype), application=label("property_types", app_type))
    elif ptype and app_type:
        add(OK, "type_match", type=label("property_types", ptype))

    # 2. the instrument and its registration
    doc = answers.get("document_type")
    if doc and doc != "SALE_DEED":
        add(WARN, "other_instrument", type=label("agency_form.options", doc))
    reg = {k: answers.get(k) for k in ("sale_deed_number", "sale_deed_date", "sub_registrar_office")}
    gone = [_f(k) for k, v in reg.items() if not str(v or "").strip()]
    when = fold.to_date(reg["sale_deed_date"])
    if gone:
        add(WARN, "registration_missing", fields=", ".join(gone).lower())
    elif not when or when > today:
        add(BAD, "registration_date", date=reg["sale_deed_date"])
    else:
        add(INFO, "registration", number=reg["sale_deed_number"], date=when.strftime("%d %b %Y"),
            office=reg["sub_registrar_office"])

    # 2b. what the agency examined: how many, how many originals
    examined = _rows(answers, "documents_examined")
    if examined:
        originals = sum(1 for r in examined if r.get("copy") == "ORIGINAL")
        if originals:
            add(INFO, "examined", n=len(examined), originals=originals)
        else:
            add(WARN, "examined_copies", n=len(examined))

    # 2c. how the property is identified on the land records, and its boundaries
    ids = [f"{_f(k)} {answers[k]}" for k in ("revenue_survey_no", "city_survey_no", "city_survey_ward", "sheet_no",
                                             "tp_scheme_no", "final_plot_no") if answers.get(k)]
    if ids:
        add(INFO, "identity", ids="; ".join(ids))
    elif any(answers.get(k) for k in ("project_scheme", "unit_no", "area")):
        add(WARN, "identity_none")
    sides = [_f(f"boundary_{s}") for s in ("north", "south", "east", "west")]
    missing_sides = [sides[i] for i, s in enumerate(("north", "south", "east", "west")) if not answers.get(f"boundary_{s}")]
    if len(missing_sides) < 4:
        if missing_sides:
            add(WARN, "boundaries_missing", sides=", ".join(m.split(":")[-1].strip() for m in missing_sides))
        else:
            add(OK, "boundaries_ok")

    # 3. the location against the application
    details = dict(app.get("details") or {})
    differ = [_f(k) for k in ("district", "state") if _same_place(details.get(k), answers.get(k)) is False]
    unknown = [_f(k) for k in ("district", "state") if _same_place(details.get(k), answers.get(k)) is None]
    if differ:
        add(BAD, "location_differ", fields=", ".join(differ).lower())
    elif unknown:
        add(WARN, "location_unknown", fields=", ".join(unknown).lower())
    else:
        add(OK, "location_match", district=answers.get("district"), state=answers.get("state"))

    # 4. the title chain: continuous, ending with the owners, and how far back
    chain = sorted([c for c in (answers.get("title_history") or []) if isinstance(c, dict)],
                   key=lambda c: fold.to_date(c.get("date")) or date.max)
    kinds = section("title_instruments") or {}
    fixes = [c for c in chain if kinds.get(str(c.get("instrument") or "")) == "CORRECTION"]
    if fixes:
        add(INFO, "corrections", deeds="; ".join(
            f'{label("agency_form.options", c.get("instrument"))} {c.get("reg_no") or ""} ({c.get("date") or "—"})'.replace("  ", " ")
            for c in fixes))
    chain = [c for c in chain if c not in fixes] or chain
    need = int(section("title_search").get("years", 13))
    if not chain:
        add(WARN, "chain_none")
    else:
        breaks = [f'{a.get("to_owner") or "—"} → {b.get("from_owner") or "—"}' for a, b in zip(chain, chain[1:])
                  if not fold.name_match(a.get("to_owner"), b.get("from_owner"))]
        span = int(fold.years_between(fold.to_date(chain[0].get("date")), today) or 0)
        last = str(chain[-1].get("to_owner") or "")
        ends = not theirs or any(fold.name_match(n, last) or n.lower() in last.lower() for n in theirs)
        if breaks:
            add(BAD, "chain_break", broken="; ".join(breaks))
        elif not ends:
            add(BAD, "chain_last", last=last or "—")
        else:
            add(OK, "chain_ok", n=len(chain), first=chain[0].get("from_owner") or "—", years=span)
        if span < need:
            add(WARN, "chain_short", years=span, need=need)
        # each registered transfer inside the searched period should be in the EC under its number
        entries = _rows(answers, "ec_entries")
        start = fold.to_date(answers.get("ec_from")) or (date(int(answers["search_from_year"]), 1, 1)
                                                         if str(answers.get("search_from_year") or "").isdigit() else None)
        if entries and start:
            due = [c for c in chain if c.get("reg_no") and (fold.to_date(c.get("date")) or date.min) >= start]
            numbers = {fold.reference(e.get("doc_no")) for e in entries if e.get("doc_no")}
            absent = [c for c in due if fold.reference(c.get("reg_no")) not in numbers]
            if due:
                add(OK if not absent else WARN, "chain_in_ec", found=len(due) - len(absent), total=len(due))
            if absent:
                add(WARN, "chain_not_in_ec", deeds="; ".join(f'{c.get("reg_no")} ({c.get("date") or "—"})' for c in absent))

    # 4b. the title documents examined, and how far back
    years_need = int(section("title_search").get("years", 13))
    checked = _yn(answers.get("title_docs_checked"))
    if checked == "no":
        add(BAD, "docs_not_checked", years=years_need)
    elif checked == "yes":
        add(OK, "docs_checked", years=years_need)
    doc_years = answers.get("property_doc_years")
    try:
        doc_years = int(float(doc_years)) if doc_years not in (None, "") else None
    except (TypeError, ValueError):
        doc_years = None
    if doc_years is not None and doc_years < years_need:
        add(WARN, "doc_years_short", years=doc_years, need=years_need)

    # 4c. the search period as reported, against the EC's own period and the policy
    y_from, y_to = answers.get("search_from_year"), answers.get("search_to_year")
    if y_from and y_to:
        period = f"{int(y_from)}–{int(y_to)}"
        ec_f, ec_t = fold.to_date(answers.get("ec_from")), fold.to_date(answers.get("ec_to"))
        if ec_f and ec_t and (ec_f.year, ec_t.year) != (int(y_from), int(y_to)):
            add(WARN, "period_mismatch", period=period, ec=f"{ec_f.year}–{ec_t.year}")
        if int(y_to) - int(y_from) < years_need:
            add(WARN, "period_short", years=int(y_to) - int(y_from), need=years_need)

    # 5. the encumbrance search
    ec_from, ec_to = fold.to_date(answers.get("ec_from")), fold.to_date(answers.get("ec_to"))
    ec_years = int(fold.years_between(ec_from, ec_to) or 0) if ec_from and ec_to else None
    ec_need = int(section("encumbrance").get("years", 13))
    found = _yn(answers.get("encumbrance_found"))
    if found == "yes":
        add(BAD, "encumbrance", details=answers.get("encumbrance_details") or "—")
    elif ec_years is None:
        add(WARN, "ec_none")
    elif found == "no":
        add(OK if ec_years >= ec_need else WARN, "ec_clear" if ec_years >= ec_need else "ec_short", years=ec_years,
            need=ec_need)
    else:
        add(WARN, "ec_unanswered")

    # 5b. each charge in the EC: released later by the same lender, or still open -- evidence to assess, never a verdict
    natures = section("ec_natures") or {}
    entries = sorted(_rows(answers, "ec_entries"), key=lambda e: fold.to_date(e.get("date")) or date.max)
    wanted = " ".join([str(r.get("document") or "") for r in _rows(answers, "documents_required")]
                      + [str(answers.get("documents_required_prior") or "")]
                      + [str(r.get("condition") or "") for r in _rows(answers, "opinion_conditions")]).lower()
    open_charges = []
    for i, e in enumerate(entries):
        if natures.get(str(e.get("nature") or "")) != "CHARGE":
            continue
        lender = e.get("claimant") or "—"
        release = next((r for r in entries[i + 1:] if natures.get(str(r.get("nature") or "")) == "RELEASE"
                        and _same_party(r.get("executant"), lender)), None)
        if release:
            add(INFO, "ec_released", lender=lender, date=release.get("date") or "—")
            continue
        if any(_same_party(x.get("claimant"), lender) for x in open_charges):
            continue                                  # a further charge of a lender already open
        open_charges.append(e)
        add(WARN, "ec_open_charge", lender=lender, date=e.get("date") or "—", doc=e.get("doc_no") or "—")
        if any(w in wanted for w in fold.address_words(str(lender))) and ("release" in wanted or "no due" in wanted):
            add(WARN, "ec_open_required", lender=lender)

    # 5c. what the bank must take, before and after
    required = _rows(answers, "documents_required")
    if required:
        add(INFO, "required_docs", n=len(required), prior=sum(1 for r in required if r.get("stage") == "PRIOR_TO_MORTGAGE"))

    # 6. CERSAI
    searches = [s for s in (answers.get("cersai_searches") or []) if isinstance(s, dict)]
    cersai = _yn(answers.get("cersai_records_found"))
    if cersai == "yes":
        add(BAD, "cersai_found", details=answers.get("cersai_details") or "—")
    elif not searches:
        add(WARN, "cersai_none")
    elif cersai == "no":
        add(OK, "cersai_clear", n=len(searches))
    else:
        add(WARN, "cersai_unanswered")

    # 7. the legal opinion questions
    asked = list((section("agency_report").get("sections") or {}).get("legal_opinion") or [])
    said = {q: _yn(answers.get(q)) for q in asked}
    noes = [_f(q) for q, v in said.items() if v == "no" and q != "title_clear_marketable"]
    open_ = [_f(q) for q, v in said.items() if not v]
    if noes:
        add(BAD, "opinion_no", items="; ".join(noes))
    if open_:
        add(WARN, "opinion_open", n=len(open_))
    if not noes and not open_:
        add(OK, "opinion_ok", n=len(asked))

    # 8. the mortgage: registered or equitable, and what must come before disbursal
    if _yn(answers.get("registered_mortgage")) == "yes":
        add(INFO, "registered_mortgage")
    if str(answers.get("documents_required_prior") or "").strip():
        add(INFO, "prior_documents", documents=_clause(answers.get("documents_required_prior")))

    # 9. the agency's own overall opinion against its findings and its conditions
    conditions = _rows(answers, "opinion_conditions")
    if conditions:
        add(WARN, "opinion_conditional", n=len(conditions))
    elif open_charges and _yn(answers.get("title_clear_marketable")) == "yes":
        add(BAD, "opinion_unconditional_open")
    problems = sum(1 for x in out if x["tone"] == BAD)
    clear = _yn(answers.get("title_clear_marketable"))
    if clear == "yes" and problems:
        add(BAD, "overall_conflict", n=problems)
    elif clear == "yes":
        add(OK, "overall_clear")
    elif clear == "no":
        add(BAD, "overall_not_clear")
    else:
        add(WARN, "overall_open")
    return out


# ------------------------------------------------------------------------------------------------- the document
def _value(fid: str, value: Any) -> str:
    """One answer as the report prints it (escaped)."""
    if value in (None, "", [], {}):
        return pdfkit._e(say("agency_pdf.not_answered"))
    if fid == "owners" and isinstance(value, list):
        return pdfkit._e(", ".join(" ".join(x for x in (r.get("first"), r.get("middle"), r.get("last")) if x)
                                   for r in value if isinstance(r, dict)))
    if fid == "property_type":
        return pdfkit._e(label("property_types", value))
    if isinstance(value, str) and value in words("agency_form.options"):
        return pdfkit._e(label("agency_form.options", value))
    if isinstance(value, list):
        return pdfkit._e(", ".join(map(str, value)))
    return pdfkit._e(value)


def section_block(sid: str, fields: list[str], answers: dict[str, Any]) -> str:
    """One section of the agency's answers (also used in the CPA's PDF)."""
    specs = section("agency_report").get("fields") or {}
    tables = [f for f in fields if (specs.get(f) or {}).get("type") == "table"]
    plain = [f for f in fields if f not in tables and f != "cersai_searches"]
    # one paragraph per answer (a table row that does not fit the page makes the PDF Story overflow the page and lose
    # what follows; paragraphs break cleanly)
    rows = "".join(
        f'<p style="margin:0;padding:3px 4px;border-bottom:0.5px solid {pdfkit.LINE}">'
        f'<span style="color:{pdfkit.MUTED}">{pdfkit._e(_f(f))}</span>&#160;&#160;<b>{_value(f, answers.get(f))}</b></p>'
        for f in plain)
    out = pdfkit._h2(label("agency_form.sections", sid)) + rows
    for fid in tables:
        rows = _rows(answers, fid)
        if not rows:
            continue
        cols = specs[fid].get("columns") or []
        width = f"{100 // max(1, len(cols))}%"
        cell = lambda c, v: pdfkit._e(label("agency_form.options", v) if c.get("type") == "select" and v else v)  # noqa: E731
        out += (f'<p style="margin:8px 0 3px 0;font-weight:bold">{pdfkit._e(_f(fid))}</p>'
                + pdfkit._grid([(label(f"agency_form.columns.{fid}", c["id"]), width) for c in cols],
                               [[cell(c, r.get(c["id"])) for c in cols] for r in rows]))
    if "cersai_searches" in fields and answers.get("cersai_searches"):
        s = words("agency_form.searches")
        out += f'<p style="margin:8px 0 3px 0;font-weight:bold">{pdfkit._e(_f("cersai_searches"))}</p>' + pdfkit._grid(
            [(s["search_type"], "22%"), (s["searched_at"], "18%"), (s["reference"], "20%"), (s["result"], "40%")],
            [[pdfkit._e(label("agency_form.search_types", r.get("search_type"))), pdfkit._e(r.get("searched_at")),
              pdfkit._e(r.get("reference")), pdfkit._e(r.get("result"))] for r in answers["cersai_searches"]])
    return out


def analysis_block(lines: list[dict[str, str]]) -> str:
    """The findings with their tone tags and the tally (also used in the CPA's PDF)."""
    tags = words("agency_pdf.labels.tone")
    counts = {t: sum(1 for x in lines if x["tone"] == t) for t in (OK, WARN, BAD)}
    tally = say("agency_pdf.tally", ok=counts[OK], warn=counts[WARN], bad=counts[BAD])
    items = "".join(                                  # one paragraph per line, coloured by tone (see section_block)
        f'<p style="margin:0 0 3px 0;padding:5px 9px;background-color:{_MARK[x["tone"]][1]};'
        f'border-left:4px solid {_MARK[x["tone"]][0]}"><b style="color:{_MARK[x["tone"]][0]};font-size:7.5pt">'
        f'{pdfkit._e(tags[x["tone"]])}</b>&#160;&#160;{pdfkit._e(x["text"])}</p>' for x in lines)
    return (f'<p style="color:{pdfkit.MUTED};margin:0 0 4px 0">{pdfkit._e(say("agency_pdf.analysis_note"))} {pdfkit._e(tally)}</p>'
            + items)


def _html(app: dict[str, Any], agency_name: str, by: str, answers: dict[str, Any], lines: list[dict[str, str]],
          generated: str) -> str:
    lab = words("agency_pdf.labels")
    counts = {t: sum(1 for x in lines if x["tone"] == t) for t in (OK, WARN, BAD)}
    stamp = ((say("agency_pdf.stamp.adverse"), "#b42318", "#fdecea") if counts[BAD]
             else (say("agency_pdf.stamp.confirm"), "#b54708", "#fff4e5") if counts[WARN]
             else (say("agency_pdf.stamp.in_order"), "#067647", "#e8f6ee"))
    header = pdfkit.header(agency_name, say("agency_pdf.title"), f'{app["applicant_name"]} · {app["application_id"]}',
                           stamp, [f'{lab["by"]} {by}', f'{lab["generated"]} {generated}'])
    facts = pdfkit._kv([
        (lab["applicant"], app["applicant_name"], lab["application"], app["application_id"]),
        (lab["property_type"], label("property_types", app.get("property_type")), lab["agency"], agency_name),
        (lab["address"], app.get("property_address"), "", ""),
    ])
    findings = pdfkit._h2(say("agency_pdf.analysis_title")) + analysis_block(lines)
    body = "".join(section_block(sid, list(fields), answers)
                   for sid, fields in (section("agency_report").get("sections") or {}).items())
    sign = (f'<p style="margin:18px 0 0 0;color:{pdfkit.MUTED}">{pdfkit._e(say("agency_pdf.declaration", agency=agency_name))}</p>')
    return (f'<html><body style="font-family:sans-serif;font-size:9pt;color:{pdfkit.INK}">{header}'
            f'{pdfkit._h2(say("agency_pdf.case_title"))}{facts}{findings}{body}{sign}</body></html>')


def build(app: dict[str, Any], agency: str, by: str, answers: dict[str, Any]) -> tuple[bytes, list[dict[str, str]]]:
    """(the PDF, the analysis) for these answers."""
    agency_name = (section("agencies").get(agency) or {}).get("name") or agency
    lines = analysis(app, answers)
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    pdf = pdfkit._pdf(_html(app, agency_name, by, answers, lines, generated),
                      {"title": say("agency_pdf.title"), "application": app["application_id"]})
    return pdf, lines


def attach(conn, app: dict[str, Any], case: dict[str, Any], who_user: str, report_id: str,
           answers: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """Generate the report, store the file, attach it to the case as the agency's report; (document id, analysis)."""
    from app.agents.credit.legal_verification.services.documents import pdf_facts

    data, lines = build(app, case["agency"], who_user, answers)
    kind = (section("documents").get("agency_kinds") or ["AGENCY_REPORT"])[0]
    document_id = new_id("DOC")
    folder = documents_dir()
    folder.mkdir(parents=True, exist_ok=True)
    file_ref = f"{document_id}.pdf"
    (folder / file_ref).write_bytes(data)
    pages, meta = pdf_facts(data)
    meta = {**meta, "generated": True, "report_id": report_id,
            "answers_sha256": hashlib.sha256(json.dumps(answers, sort_keys=True, default=str).encode()).hexdigest()}
    name = say("agency_pdf.file_name", application=app["application_id"])
    conn.execute("INSERT INTO legal_verification_documents (document_id, case_id, kind, file_name, file_ref, content_type, "
                 "sha256, size_bytes, pages, metadata, read_status, extracted, uploaded_by) "
                 "VALUES (%s, %s, %s, %s, %s, 'application/pdf', %s, %s, %s, %s, 'STORED', %s, %s)",
                 (document_id, case["case_id"], kind, Path(name).name[:200], file_ref, hashlib.sha256(data).hexdigest(),
                  len(data), pages, Jsonb(meta), Jsonb({}), who_user))
    conn.execute("UPDATE legal_verification_agency_reports SET document_id = %s, analysis = %s WHERE report_id = %s",
                 (document_id, Jsonb(lines), report_id))
    return document_id, lines


__all__ = ["analysis", "analysis_block", "attach", "build", "section_block"]
