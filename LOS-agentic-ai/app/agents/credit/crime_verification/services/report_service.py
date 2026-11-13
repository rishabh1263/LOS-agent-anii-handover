"""
The downloadable Crime Verification Report -- one per subject result (PDF, PyMuPDF from HTML), and the COMBINED
report of a case (case_download): the applicant and every co-applicant in one PDF -- the application, the summary as
points, one "people at a glance" table, a short section per person, the coverage once (owner 2026-10-29).

    header band      title, application, role, vendor, report number / version / generated, a risk-coloured rule
    subject          the person (PAN masked) and the application
    at a glance      the risk (big, coloured), its meaning, the five-step risk scale, four figures
    summary          the case summary (AI when it passed the check, else written by rules)
    findings         the sources with findings, then by matter type
    details          case, provider reference, vendor, outcome, when, the officer's decision
    coverage         every covered source with its coverage year and cases found
    guide            what each risk level means; limitations
    every page       footer: what, whose, page x of n (+ the demo notice when config report.show_demo_notice);
                     behind the text: security-paper waves, a frame, a faint watermark (config report.background)

The first download generates it; the file is kept under config storage.reports_dir and the database keeps its
reference, hash, version and the case / subject / result it belongs to. A later download serves the same file; it is
regenerated when the stored file is missing, no longer matches its hash, or the summary changed.
"""

from __future__ import annotations

import hashlib
import html
import io
import re
from datetime import datetime, timezone
from typing import Any

from app.agents.credit.crime_verification.config import demo_mode, reports_dir, say, section, words
from app.agents.credit.crime_verification.db import database
from app.agents.credit.crime_verification.services import queries, summary_service
from app.agents.credit.crime_verification.services.access import Caller, require
from app.agents.credit.crime_verification.services.audit import audit, new_id
from app.agents.credit.crime_verification.services.errors import ServiceError

RISK_ORDER = ["NO_RISK", "LOW_RISK", "AVERAGE_RISK", "HIGH_RISK", "VERY_HIGH_RISK"]
RISK_COLOURS = {"NO_RISK": "#12b76a", "LOW_RISK": "#66c61c", "AVERAGE_RISK": "#f5a30a", "HIGH_RISK": "#f97316",
                "VERY_HIGH_RISK": "#e5484d"}
INK, MUTED, LINE, SOFT, BRAND = "#101828", "#667085", "#e4e7ec", "#f5f6fa", "#3b2a8c"


def _notice() -> bool:
    """The demo notice in the PDF: demo mode AND config report.show_demo_notice."""
    return demo_mode() and bool(section("report").get("show_demo_notice", False))


def _e(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def _when(value: Any) -> str:
    """ISO time -> "27 Oct 2026, 13:36 UTC"."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value or "—")
    return moment.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _dob(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%d %b %Y")
    except ValueError:
        return str(value or "—")


def _rupees(amount: Any) -> str:
    """Indian grouping: 4500000 -> "Rs 45,00,000"."""
    digits = str(int(round(float(amount or 0))))
    head, tail = digits[:-3], digits[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return "Rs " + (f"{head},{tail}" if head else tail)


def _h2(text: str) -> str:
    return (f'<p style="font-size:11.5pt;font-weight:bold;color:{BRAND};margin:14px 0 6px 0;'
            f'border-bottom:1px solid {LINE};padding-bottom:3px">{_e(text)}</p>')


def _kv4(rows: list[tuple[str, Any, str, Any]], wide: list[tuple[str, Any]] | None = None) -> str:
    """Two label / value pairs per row (explicit cell widths: the PDF engine needs them to keep columns apart); `wide`
    rows put one long value across the remaining three columns (an address)."""
    cells = "".join(
        f'<tr><td style="width:18%;color:{MUTED};padding:3px 4px">{_e(k1)}</td>'
        f'<td style="width:32%;padding:3px 4px;font-weight:bold">{_e(v1) if k1 else ""}</td>'
        f'<td style="width:18%;color:{MUTED};padding:3px 4px">{_e(k2) if k2 else ""}</td>'
        f'<td style="width:32%;padding:3px 4px;font-weight:bold">{_e(v2) if k2 else ""}</td></tr>'
        for k1, v1, k2, v2 in rows)
    cells += "".join(f'<tr><td style="width:18%;color:{MUTED};padding:3px 4px">{_e(k)}</td>'
                     f'<td colspan="3" style="padding:3px 4px;font-weight:bold">{_e(v)}</td></tr>' for k, v in wide or [])
    return f'<table style="width:100%" cellspacing="0" cellpadding="0">{cells}</table>'


def _grid(head: list[tuple[str, str, str]], rows: list[str]) -> str:
    """A ruled table: head = [(label, width, align)], rows = ready <tr> strings."""
    th = "".join(f'<td style="width:{w};background-color:{SOFT};color:{MUTED};font-weight:bold;font-size:8.5pt;'
                 f'padding:5px 6px;text-align:{al};border-bottom:1px solid {LINE}">{_e(lb)}</td>' for lb, w, al in head)
    return f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr>{th}</tr>{"".join(rows)}</table>'


def _td(value: str, align: str = "left", style: str = "") -> str:
    return f'<td style="padding:4px 6px;text-align:{align};border-bottom:1px solid {LINE};{style}">{value}</td>'


def _header(app: dict[str, Any], subject: dict[str, Any], vendor: str, colour: str, report_id: str, version: str,
            generated: str) -> str:
    lab = words("report.labels")
    return (
        f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr>'
        f'<td style="width:62%;background-color:{BRAND};color:#ffffff;padding:14px 16px">'
        f'<span style="font-size:8pt;color:#c7c2f0">{_e(say("report.subtitle").upper())}</span><br/>'
        f'<span style="font-size:18pt;font-weight:bold">{_e(say("report.title"))}</span><br/>'
        f'<span style="font-size:9pt;color:#e0dcfa">{_e(app["application_id"])} · {_e(subject["role_label"])} · '
        f'{_e(vendor)}</span></td>'
        f'<td style="width:38%;background-color:{BRAND};color:#e0dcfa;padding:14px 16px;text-align:right;'
        f'font-size:8pt">{_e(lab["report_no"])} {_e(report_id)}<br/>{_e(lab["version"])} {_e(version)}<br/>'
        f'{_e(lab["generated"])} {_e(generated)}</td></tr>'
        f'<tr><td colspan="2" style="background-color:{colour};font-size:3pt">&#160;</td></tr></table>')


def _glance(result: dict[str, Any], subject: dict[str, Any], hits: int, covered: int) -> str:
    lab, stat, labels = words("report.labels"), words("report.stats"), words("risk.labels")
    level = result["risk_level"]
    colour = RISK_COLOURS.get(level, INK)
    scale = "".join(
        f'<td style="width:20%;background-color:{RISK_COLOURS[r] if r == level else "#eef0f4"};'
        f'color:{"#ffffff" if r == level else MUTED};text-align:center;padding:5px 2px;font-size:8.5pt;'
        f'{"font-weight:bold" if r == level else ""}">{_e(labels[r])}</td>' for r in RISK_ORDER)
    figures = [(stat["cases"], result["total_cases"]), (stat["hits"], hits), (stat["covered"], covered),
               (stat["status"], subject["status_label"])]
    boxes = "".join(
        f'<td style="width:25%;border:1px solid {LINE};padding:7px 9px"><span style="font-size:'
        f'{"14pt" if isinstance(v, int) else "11pt"};'
        f'font-weight:bold">{_e(v)}</span><br/><span style="font-size:8pt;color:{MUTED}">{_e(k)}</span></td>'
        for k, v in figures)
    return (
        f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr>'
        f'<td style="width:30%;padding:4px 0"><span style="font-size:8.5pt;color:{MUTED}">{_e(lab["risk"])}</span>'
        f'<br/><span style="font-size:16pt;font-weight:bold;color:{colour};white-space:nowrap">{_e(result["risk_label"])}</span></td>'
        f'<td style="width:70%;padding:4px 0 4px 10px">{_e(result["risk_meaning"])}</td></tr></table>'
        f'<table style="width:100%" cellspacing="2" cellpadding="0"><tr>{scale}</tr></table>'
        f'<table style="width:100%" cellspacing="4" cellpadding="0"><tr>{boxes}</tr></table>')


def _findings(result: dict[str, Any], hits: list[tuple[str, dict[str, Any]]]) -> str:
    sec, col = words("report.sections"), words("report.columns")
    if not hits:
        body = (f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr><td style="background-color:#ecfdf3;'
                f'color:#067647;padding:8px 10px">{_e(say("report.no_findings"))}</td></tr></table>')
    else:
        rows = [f'<tr>{_td(_e(i["name"]))}{_td(_e(g), style=f"color:{MUTED}")}'
                f'{_td(_e(i["cases_found"]), "right", "font-weight:bold;color:#b42318")}</tr>' for g, i in hits]
        body = (f'<p style="margin:0 0 6px 0">{_e(result["summary"])}</p>'
                + _grid([(col["source"], "55%", "left"), (col["group"], "30%", "left"), (col["found"], "15%", "right")],
                        rows))
    if result["clusters"]:
        rows = [f'<tr>{_td(_e(c["label"]))}{_td(_e(c["count"]), "right", "font-weight:bold")}</tr>'
                for c in result["clusters"]]
        body += _h2(sec["clusters"]) + _grid([(col["matter"], "85%", "left"), (col["count"], "15%", "right")], rows)
    return body


def _coverage(result: dict[str, Any]) -> str:
    col = words("report.columns")
    rows = []
    for group in result["groups"]:
        rows.append(f'<tr><td colspan="3" style="background-color:#f9fafb;font-weight:bold;padding:4px 6px;'
                    f'font-size:8.5pt;border-bottom:1px solid {LINE}">{_e(group["label"])}</td></tr>')
        for i in group["items"]:
            hit = "font-weight:bold;color:#b42318" if i["cases_found"] else f"color:{MUTED}"
            rows.append(f'<tr>{_td(_e(i["name"]))}{_td(_e(i["coverage_since"]), "center")}'
                        f'{_td(_e(i["cases_found"]), "right", hit)}</tr>')
    return _grid([(col["source"], "62%", "left"), (col["since"], "20%", "center"), (col["found"], "18%", "right")],
                 rows)


def _html(app: dict[str, Any], case: dict[str, Any], subject: dict[str, Any], generated: str, version: str,
          summary: dict[str, Any], report_id: str) -> str:
    result = subject["result"]
    lab, sec, col, fields = (words("report.labels"), words("report.sections"), words("report.columns"),
                             words("fields"))
    colour = RISK_COLOURS.get(result["risk_level"], INK)
    covered = sum(len(g["items"]) for g in result["groups"])
    hits = [(g["label"], i) for g in result["groups"] for i in g["items"] if i["cases_found"]]
    vendor = case["vendor_name"] + (f' ({say("report.provider_demo")})' if _notice() else "")
    notice = (f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr><td style="background-color:#fffaeb;'
              f'color:#93370d;border:1px solid #fedf89;padding:6px 8px;font-size:8.5pt;font-weight:bold">'
              f'{_e(say("report.disclaimer"))}</td></tr></table>') if _notice() else ""

    summary_box = _summary_box(summary)
    subject_block = _kv4([
        (fields["full_name"], subject["full_name"], lab["role"], subject["role_label"]),
        (fields["father_name"], subject["father_name"], fields["pan"], subject["pan_masked"]),
        (fields["date_of_birth"], _dob(subject["date_of_birth"]), lab["application"], app["application_id"]),
        (lab["product"], app["product"], lab["loan"], _rupees(app["loan_amount"])),
    ], wide=[(fields["permanent_address"], subject["permanent_address"])])
    decision = (f'{case["final_decision_label"]} — {case["finalized_by"]}, {_when(case["finalized_at"])}'
                if case.get("final_decision") else "—")
    details = _kv4([
        (lab["case"], case["case_id"], lab["provider_ref"], result["provider_reference"]),
        (lab["vendor"], vendor, lab["outcome"], result["outcome_label"]),
        (lab["initiated"], f'{case["initiated_by"]}, {_when(case["initiated_at"])}', lab["result_at"],
         _when(result["result_at"])),
        (lab["decision"], decision, "", ""),
    ])
    guide = _grid([(col["level"], "22%", "left"), (col["meaning"], "78%", "left")],
                  [f'<tr>{_td(_e(words("risk.labels")[k]), style=f"font-weight:bold;color:{RISK_COLOURS[k]}")}'
                   f'{_td(_e(v))}</tr>' for k, v in words("risk.meaning").items()])

    return f"""<html><body style="font-family:sans-serif;font-size:9.5pt;color:{INK}">
{_header(app, subject, vendor, colour, report_id, version, generated)}
{notice}
{_h2(sec["subject"])}{subject_block}
{_h2(sec["glance"])}{_glance(result, subject, len(hits), covered)}
{summary_box}
{_h2(sec["findings"])}{_findings(result, hits)}
{_h2(sec["case"])}{details}
{_h2(sec["coverage"])}{_coverage(result)}
{_h2(sec["meaning"])}{guide}
{_h2(sec["limitations"])}<p style="color:{MUTED}">{_e(say("report.limitations"))}</p>
</body></html>"""


def points(text: str) -> list[str]:
    """The summary as points: one per sentence (every sentence names its person -- summary_service)."""
    # numbered points one per line (2026-11-02, app/core/summary_points.py); an older text: one per sentence
    parts = (text or "").split("\n") if "\n" in (text or "") else re.split(r"(?<=[.!?])\s+", text or "")
    return [s.strip() for s in parts if s.strip()]


def _point(text: str) -> str:
    """One point; a leading "Role:" (the rules-written summary) in bold."""
    head, sep, rest = text.partition(":")
    if sep and len(head) <= 30:
        return f"<b>{_e(head)}:</b>{_e(rest)}"
    return _e(text)


def _summary_box(summary: dict[str, Any]) -> str:
    if not summary.get("text"):
        return ""
    title = summary["label"] if summary["source"] == "ai" else words("report.sections")["summary"]
    rows = "".join(f'<tr><td style="width:14px;color:{BRAND};font-weight:bold;padding:2px 0 2px 10px;vertical-align:top">'
                   f'&#8226;</td><td style="padding:2px 10px 2px 4px">{_point(p)}</td></tr>'
                   for p in points(summary["text"]))
    return (f'{_h2(title)}<table style="width:100%;background-color:#f4f2ff;border-left:3px solid {BRAND}" '
            f'cellspacing="0" cellpadding="0"><tr><td colspan="2" style="font-size:3pt">&#160;</td></tr>{rows}'
            f'<tr><td colspan="2" style="font-size:3pt">&#160;</td></tr></table>')


def _risk_cell(subject: dict[str, Any]) -> str:
    r = subject.get("result")
    if not r:
        return _td(_e(subject["status_label"]), style=f"color:{MUTED}")
    return _td(_e(r["risk_label"]), style=f'font-weight:bold;color:{RISK_COLOURS.get(r["risk_level"], INK)}')


def _case_html(app: dict[str, Any], case: dict[str, Any], generated: str, version: str, summary: dict[str, Any],
               report_id: str) -> str:
    """Everyone on one report: compact -- one table for all, then a short block per person, the coverage once."""
    lab, sec, col, fields = (words("report.labels"), words("report.sections"), words("report.columns"),
                             words("fields"))
    people = case["subjects"]
    results = [s["result"] for s in people if s.get("result")]
    worst = max((r["risk_level"] for r in results), key=lambda x: RISK_ORDER.index(x) if x in RISK_ORDER else -1,
                default="")
    vendor = case["vendor_name"] + (f' ({say("report.provider_demo")})' if _notice() else "")
    everyone = {"role_label": say("report.everyone", n=len(people))}
    decision = (f'{case["final_decision_label"]} — {case["finalized_by"]}, {_when(case["finalized_at"])}'
                if case.get("final_decision") else "—")
    application = _kv4([
        (lab["application"], app["application_id"], lab["product"], app["product"]),
        (lab["loan"], _rupees(app["loan_amount"]), lab["branch"], app.get("branch")),
        (lab["case"], case["case_id"], lab["vendor"], vendor),
        (lab["initiated"], f'{case["initiated_by"]}, {_when(case["initiated_at"])}', lab["decision"], decision),
    ])
    table = _grid(
        [("#", "4%", "center"), (col["role"], "20%", "left"), (col["name"], "22%", "left"), (col["pan"], "13%", "left"),
         (col["risk"], "15%", "left"), (col["found"], "9%", "right"), (col["hits"], "9%", "right"),
         (col["status"], "8%", "left")],
        [f'<tr>{_td(str(n), "center", f"color:{MUTED}")}{_td(_e(s["role_label"]), style="font-weight:bold")}'
         f'{_td(_e(s["full_name"]))}{_td(_e(s["pan_masked"]), style="font-size:8.5pt")}{_risk_cell(s)}'
         f'{_td(_e(s["result"]["total_cases"]) if s.get("result") else "—", "right", "font-weight:bold")}'
         f'{_td(_e(sum(1 for g in s["result"]["groups"] for i in g["items"] if i["cases_found"])) if s.get("result") else "—", "right")}'
         f'{_td(_e(s["status_label"]), style="font-size:8pt")}</tr>'
         for n, s in enumerate(people, start=1)])
    blocks = []
    for s in people:
        r = s.get("result")
        block = _h2(say("report.sections.person", role=s["role_label"], name=s["full_name"]))
        block += _kv4([(fields["father_name"], s["father_name"], fields["pan"], s["pan_masked"]),
                       (fields["date_of_birth"], _dob(s["date_of_birth"]), lab["status"], s["status_label"])],
                      wide=[(fields["permanent_address"], s["permanent_address"])])
        if r:
            colour = RISK_COLOURS.get(r["risk_level"], INK)
            hits = [(g["label"], i) for g in r["groups"] for i in g["items"] if i["cases_found"]]
            block += (f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr>'
                      f'<td style="width:24%;padding:4px 4px"><span style="font-size:8pt;color:{MUTED}">{_e(lab["risk"])}'
                      f'</span><br/><span style="font-size:12pt;font-weight:bold;color:{colour}">{_e(r["risk_label"])}'
                      f'</span></td><td style="width:76%;padding:4px 4px;color:{MUTED}">{_e(r["risk_meaning"])}</td>'
                      f'</tr></table>')
            block += _findings(r, hits)
        blocks.append(block)
    # the coverage once: every source with, per source, who had cases there
    coverage = ""
    if results:
        rows = []
        for group in results[0]["groups"]:
            rows.append(f'<tr><td colspan="3" style="background-color:#f9fafb;font-weight:bold;padding:4px 6px;'
                        f'font-size:8.5pt;border-bottom:1px solid {LINE}">{_e(group["label"])}</td></tr>')
            for item in group["items"]:
                found = [say("report.found_for", role=s["role_label"], n=i["cases_found"]) for s in people if s.get("result")
                         for g in s["result"]["groups"] for i in g["items"]
                         if i["code"] == item["code"] and i["cases_found"]]
                rows.append(f'<tr>{_td(_e(item["name"]))}{_td(_e(item["coverage_since"]), "center")}'
                            f'{_td(_e(", ".join(found)) if found else "—", "left", "font-weight:bold;color:#b42318" if found else f"color:{MUTED}")}</tr>')
        coverage = _h2(sec["coverage"]) + _grid([(col["source"], "48%", "left"), (col["since"], "16%", "center"),
                                                 (col["found_for"], "36%", "left")], rows)
    guide = _grid([(col["level"], "22%", "left"), (col["meaning"], "78%", "left")],
                  [f'<tr>{_td(_e(words("risk.labels")[k]), style=f"font-weight:bold;color:{RISK_COLOURS[k]}")}'
                   f'{_td(_e(v))}</tr>' for k, v in words("risk.meaning").items()])
    notice = (f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr><td style="background-color:#fffaeb;'
              f'color:#93370d;border:1px solid #fedf89;padding:6px 8px;font-size:8.5pt;font-weight:bold">'
              f'{_e(say("report.disclaimer"))}</td></tr></table>') if _notice() else ""
    return f"""<html><body style="font-family:sans-serif;font-size:9.5pt;color:{INK}">
{_header(app, everyone, vendor, RISK_COLOURS.get(worst, BRAND), report_id, version, generated)}
{notice}
{_h2(sec["application"])}{application}
{_summary_box(summary)}
{_h2(sec["people"])}{table}
{"".join(blocks)}
{coverage}
{_h2(sec["meaning"])}{guide}
{_h2(sec["limitations"])}<p style="color:{MUTED}">{_e(say("report.limitations"))}</p>
</body></html>"""


def _background(page) -> None:
    """Behind the text (config report.background): bands of security-paper waves, a page frame, a diagonal watermark."""
    import math

    import pymupdf

    spec = section("report").get("background") or {}
    width, height = page.rect.width, page.rect.height
    lavender = (0.80, 0.76, 0.96)
    if spec.get("waves", True):
        shape = page.new_shape()
        bands = max(1, int(spec.get("wave_lines", 9)))
        for band in range(bands):
            base = height * (band + 0.5) / bands
            for k in range(4):                                   # four interlaced waves per band: a guilloche
                amp, freq, phase = 7 + 2.5 * k, 2 * math.pi / (70 + 9 * k), k * 0.9 + band * 0.6
                points = [pymupdf.Point(x, base + amp * math.sin(freq * x + phase)) for x in range(0, int(width) + 6, 6)]
                shape.draw_polyline(points)
        shape.finish(color=lavender, width=0.35, stroke_opacity=0.45, closePath=False)
        shape.commit(overlay=False)
    if spec.get("frame", True):
        shape = page.new_shape()
        shape.draw_rect(page.rect + (16, 16, -16, -16))
        shape.finish(color=(0.36, 0.24, 0.96), width=0.8, stroke_opacity=0.35)
        shape.draw_rect(page.rect + (20, 20, -20, -20))
        shape.finish(color=(0.88, 0.27, 0.56), width=0.4, stroke_opacity=0.25)
        shape.commit(overlay=False)
    if spec.get("watermark", True):
        text = say("report.watermark")
        size = 64
        length = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
        centre = pymupdf.Point(width / 2, height / 2)
        start = pymupdf.Point(centre.x - length / 2, centre.y + size / 3)
        page.insert_text(start, text, fontsize=size, fontname="helv", color=(0.36, 0.24, 0.96), fill_opacity=0.05,
                         morph=(centre, pymupdf.Matrix(-35)), overlay=False)


def _pdf(markup: str, footer: dict[str, str]) -> bytes:
    import pymupdf

    story = pymupdf.Story(html=markup)
    out = io.BytesIO()
    writer = pymupdf.DocumentWriter(out)
    page = pymupdf.paper_rect("a4")
    where = page + (36, 32, -36, -50)
    more = True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    # the footer on every page: what this is, whose, page x of n (and the demo notice in demo mode)
    doc = pymupdf.open(stream=out.getvalue(), filetype="pdf")
    for number, pg in enumerate(doc, start=1):
        _background(pg)
        width, height = pg.rect.width, pg.rect.height
        pg.draw_line((36, height - 40), (width - 36, height - 40), color=(0.89, 0.91, 0.93), width=0.6)
        line = say("report.footer", page=number, pages=doc.page_count, **footer)
        pg.insert_text((36, height - 28), line, fontsize=7.5, color=(0.4, 0.44, 0.52))
        if _notice():
            pg.insert_text((36, height - 17), say("banner"), fontsize=7, color=(0.58, 0.22, 0.05))
    return doc.tobytes(garbage=3, deflate=True)


def download(who: Caller, subject_id: str) -> tuple[bytes, str]:
    """The subject's report (generated on first request). Returns (pdf bytes, file name)."""
    require(who, "download_report")
    subject = queries.subject_result(who, subject_id)
    if not subject["result"]:
        raise ServiceError(409, "REPORT_NOT_AVAILABLE")
    version = str(section("report").get("version", "1.0"))
    folder = reports_dir()
    case = queries.case_detail(who, subject["case_id"])
    summary = summary_service.summary_of(case, start=False)
    content_key = hashlib.sha256(str(summary.get("text") or "").encode()).hexdigest()
    with database().tx() as conn:
        app = conn.execute("SELECT * FROM crime_verification_applications WHERE application_id = %s",
                           (case["application_id"],)).fetchone()
        stored = conn.execute("SELECT * FROM crime_verification_reports WHERE result_id = %s AND version = %s",
                              (subject["result"]["result_id"], version)).fetchone()
        content = None
        if stored and stored["content_key"] == content_key:
            path = folder / stored["file_ref"]
            if path.exists():
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() == stored["sha256"]:
                    content = data
        if content is None:
            generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
            report_id = stored["report_id"] if stored else new_id("RPT")
            content = _pdf(_html(app, case, subject, generated, version, summary, report_id),
                           {"title": say("report.title"), "application": app["application_id"],
                            "role": subject["role_label"]})
            file_ref = f"{report_id}.pdf"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / file_ref).write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            if stored:
                conn.execute("UPDATE crime_verification_reports SET sha256 = %s, size_bytes = %s, content_key = %s, "
                             "generated_at = now(), generated_by = %s WHERE report_id = %s",
                             (digest, len(content), content_key, who.user, report_id))
            else:
                conn.execute("INSERT INTO crime_verification_reports (report_id, case_id, subject_id, result_id, "
                             "report_type, version, file_ref, sha256, size_bytes, content_key, generated_by) VALUES "
                             "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                             (report_id, case["case_id"], subject_id, subject["result"]["result_id"],
                              str(section("report").get("type", "CRIME_VERIFICATION_REPORT")), version, file_ref,
                              digest, len(content), content_key, who.user))
            audit(conn, case["application_id"], case["case_id"], who.user, "REPORT_GENERATED", role=subject["role"],
                  report_id=report_id, version=version)
        audit(conn, case["application_id"], case["case_id"], who.user, "REPORT_DOWNLOADED", role=subject["role"])
    name = f"crime-verification-{case['application_id']}-{subject['role'].lower()}.pdf"
    return content, name


def case_download(who: Caller, case_id: str) -> tuple[bytes, str]:
    """The combined report of a case: everyone on the application in one PDF. Built on request from the stored
    results (nothing new is decided here), so it always shows every person's latest result; every download is
    audited. Returns (pdf bytes, file name)."""
    require(who, "download_report")
    case = queries.case_detail(who, case_id)
    if not any(s.get("result") for s in case["subjects"]):
        raise ServiceError(409, "REPORT_NOT_AVAILABLE")
    version = str(section("report").get("version", "1.0"))
    summary = summary_service.summary_of(case, start=False)
    with database().tx() as conn:
        app = conn.execute("SELECT * FROM crime_verification_applications WHERE application_id = %s",
                           (case["application_id"],)).fetchone()
        report_id = new_id("RPT")
        generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        content = _pdf(_case_html(app, case, generated, version, summary, report_id),
                       {"title": say("report.title"), "application": app["application_id"],
                        "role": say("report.everyone", n=len(case["subjects"]))})
        audit(conn, case["application_id"], case["case_id"], who.user, "REPORT_DOWNLOADED",
              labels=[s["role_label"] for s in case["subjects"]], report_id=report_id, version=version)
    return content, f"crime-verification-{case['application_id']}-all.pdf"
