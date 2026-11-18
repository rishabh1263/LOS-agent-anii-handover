"""
The downloadable Legal Collateral Verification Report (PDF, PyMuPDF from HTML) -- built on request from the latest
validation; every download is audited.

    header      title, application, generated, a recommendation-coloured rule
    sections    application and property · recommendation (findings, missing evidence, next action) · summary in
                points · the ten rules (red, amber, green) · the agency report (CERSAI searches included) ·
                documents on file · the CPA's decision
    every page  footer (what, whose, page x of n); behind the text a soft background: a navy corner wash, a dotted
                grid band and a page frame (config report.background)
"""

from __future__ import annotations

import html
import io
from datetime import datetime, timezone
from typing import Any

from app.agents.credit.legal_verification.config import label, say, section, words
from app.agents.credit.legal_verification.db import database
from app.agents.credit.legal_verification.rules import engine
from app.agents.credit.legal_verification.services import cases, summary
from app.agents.credit.legal_verification.services.access import Caller, require
from app.agents.credit.legal_verification.services.audit import audit
from app.agents.credit.legal_verification.services.errors import ServiceError

NAVY, INK, MUTED, LINE, SOFT = "#0b1437", "#101828", "#5b6475", "#e3e7ee", "#f4f6fb"
SAPPHIRE, AMBER = "#3b57d6", "#f5a524"
TONE = {"FAIL": ("#b42318", "#fdecea"), "REVIEW": ("#b54708", "#fff4e5"), "PASS": ("#067647", "#e8f6ee"),
        "ESCALATE": ("#b42318", "#fdecea"), "REVIEW_REQUIRED": ("#b54708", "#fff4e5"),
        "CLEAR_FOR_REVIEW": ("#067647", "#e8f6ee")}
RECO_ORDER = ["CLEAR_FOR_REVIEW", "REVIEW_REQUIRED", "ESCALATE"]


def _rupees(amount: Any) -> str:
    """Indian grouping: 6000000 -> "Rs 60,00,000" (the currency word from wording report.labels.currency)."""
    digits = str(int(round(float(amount or 0))))
    head, tail = digits[:-3], digits[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return f'{words("report.labels.currency")} {f"{head},{tail}" if head else tail}'


def _e(value: Any) -> str:
    return html.escape(str(value if value not in (None, "", []) else "—"))


def _h2(text: str) -> str:
    """A section heading: an amber bar, the title in midnight, a fine sapphire rule under it."""
    return (f'<p style="margin:14px 0 6px 0;padding:3px 8px;font-size:11pt;font-weight:bold;color:{NAVY};'
            f'border-left:5px solid {AMBER};border-bottom:1px solid #c9d3f5">{_e(text)}</p>')


def _kv(rows: list[tuple[str, Any, str, Any]]) -> str:
    cells = "".join(
        f'<tr><td style="width:17%;color:{MUTED};padding:3px 4px">{_e(a)}</td>'
        f'<td style="width:33%;padding:3px 4px;font-weight:bold">{_e(b) if a else ""}</td>'
        f'<td style="width:17%;color:{MUTED};padding:3px 4px">{_e(c) if c else ""}</td>'
        f'<td style="width:33%;padding:3px 4px;font-weight:bold">{_e(d) if c else ""}</td></tr>' for a, b, c, d in rows)
    return f'<table style="width:100%" cellspacing="0" cellpadding="0">{cells}</table>'


def _grid(head: list[tuple[str, str]], rows: list[list[str]]) -> str:
    th = "".join(f'<td style="width:{w};background-color:{NAVY};color:#ffffff;font-weight:bold;font-size:8pt;'
                 f'padding:5px 6px">{_e(t)}</td>' for t, w in head)
    # one table per row: the PDF Story loses content after a table that runs over a page break; rows banded
    body = "".join(f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr>' + "".join(
        f'<td style="width:{w};padding:4px 6px;border-bottom:1px solid #d5dcf2;border-right:1px solid #d5dcf2;vertical-align:top;'
        f'background-color:{"#ffffff" if n % 2 == 0 else "#f7f9ff"}">{c}</td>'
        for c, (_, w) in zip(row, head)) + "</tr></table>" for n, row in enumerate(rows))
    return f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr>{th}</tr></table>{body}'


def _pill(status: str, text: str) -> str:
    ink, bg = TONE.get(status, (MUTED, SOFT))
    return f'<span style="background-color:{bg};color:{ink};font-weight:bold;font-size:8pt">&#160;{_e(text)}&#160;</span>'


def header(kicker: str, title: str, line: str, stamp: tuple[str, str, str] | None, side: list[str]) -> str:
    """The midnight header with an amber edge; on the right a stamp (label, ink, paper) or plain lines."""
    right = (f'<td style="width:30%;background-color:{NAVY};padding:14px 16px;text-align:right">'
             + (f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr><td style="border:2px solid {stamp[1]};'
                f'background-color:{stamp[2]};color:{stamp[1]};text-align:center;padding:7px 4px;font-size:12pt;'
                f'font-weight:bold">{_e(stamp[0]).upper()}</td></tr></table>' if stamp else "")
             + "".join(f'<span style="font-size:7.5pt;color:#c9d6ee">{_e(x)}</span><br/>' for x in side) + "</td>")
    return (f'<table style="width:100%" cellspacing="0" cellpadding="0"><tr>'
            f'<td style="width:70%;background-color:{NAVY};color:#ffffff;padding:16px 18px;border-left:7px solid {AMBER}">'
            f'<span style="font-size:7.5pt;color:{AMBER};font-weight:bold">{_e(kicker.upper())}</span><br/>'
            f'<span style="font-size:18pt;font-weight:bold">{_e(title)}</span><br/>'
            f'<span style="font-size:9pt;color:#c9d6ee">{_e(line)}</span></td>{right}</tr>'
            f'<tr><td colspan="2" style="background-color:{SAPPHIRE};font-size:2pt">&#160;</td></tr></table>')


def _glance(ov: dict[str, Any]) -> str:
    """The recommendation on its scale, and the rules counted."""
    reco = ov["recommendation"]
    scale = "".join(
        f'<td style="width:33%;text-align:center;padding:6px 2px;font-size:8.5pt;'
        f'background-color:{TONE[r][0] if r == reco else "#eef1f8"};color:{"#ffffff" if r == reco else MUTED};'
        f'{"font-weight:bold" if r == reco else ""}">{_e(label("recommendations", r))}</td>' for r in RECO_ORDER)
    c = ov["counts"]
    boxes = "".join(
        f'<td style="width:33%;border:1px solid {LINE};border-top:3px solid {ink};padding:7px 10px;background-color:#ffffff">'
        f'<span style="font-size:16pt;font-weight:bold;color:{ink}">{n}</span><br/>'
        f'<span style="font-size:8pt;color:{MUTED}">{_e(say(f"report.counts.{key}"))}</span></td>'
        for key, n, ink in (("failed", c["failed"], "#b42318"), ("review", c["review"], "#b54708"),
                            ("passed", c["passed"], "#067647")))
    return (f'<table style="width:100%" cellspacing="3" cellpadding="0"><tr>{scale}</tr></table>'
            f'<table style="width:100%" cellspacing="4" cellpadding="0"><tr>{boxes}</tr></table>')


def points_box(items: list[str]) -> str:
    """The summary as points, each coloured by what it says (as on the screen)."""
    def tone(p: str) -> tuple[str, str]:
        low = p.lower()
        if low.startswith(("critical", "mismatch", "agency vs")):
            return "#b42318", "#fff1f1"
        if low.startswith(("needs review", "missing")):
            return "#b54708", "#fff8e6"
        if low.startswith("passed"):
            return "#067647", "#eefbf1"
        return SAPPHIRE, "#f1f4ff"

    rows = []
    for p in items:
        ink, bg = tone(p)
        head, sep, rest = p.partition(":")
        text = f"<b>{_e(head)}:</b>{_e(rest)}" if sep and len(head) <= 28 else _e(p)
        rows.append(f'<p style="margin:0 0 3px 0;padding:5px 9px;background-color:{bg};border-left:4px solid {ink};'
                    f'color:{INK}">{text}</p>')
    return "".join(rows)


def _html(ws: dict[str, Any], points: dict[str, Any], generated: str) -> str:
    lab, sec = words("report.labels"), words("report.sections")
    app, case, run = ws["application"], ws["case"], ws["run"]
    ov = run["overview"]
    ink, bg = TONE.get(ov["recommendation"], (NAVY, SOFT))
    top = header(say("report.subtitle"), say("report.title"), f'{app["applicant_name"]} · {app["application_id"]}',
                 (ov["recommendation_label"], ink, bg),
                 [f'{lab["status"]} {case["status_label"]}', f'{lab["generated"]} {generated}'])
    application = _kv([
        (lab["applicant"], app["applicant_name"], lab["application"], app["application_id"]),
        (lab["loan"], _rupees(app["loan_amount"]), lab["property_type"], app["property_type_label"]),
        (lab["owners"], ", ".join(app["owner_names"]), lab["agency"], case["agency_name"]),
        (lab["address"], app["property_address"], "", ""),
    ])
    findings = "".join(f"<li>{_e(f)}</li>" for f in ov["findings"][:8]) or f"<li>{_e(lab['none'])}</li>"
    missing = "".join(f"<li>{_e(m)}</li>" for m in ov["missing"][:8]) or f"<li>{_e(lab['none'])}</li>"
    recommendation = (
        f'<table style="width:100%;background-color:{bg};border-left:5px solid {ink}" cellspacing="0" cellpadding="0">'
        f'<tr><td style="padding:10px 12px"><span style="color:{MUTED}">{_e(ov["recommendation_note"])}</span>'
        f'<p style="margin:8px 0 2px 0;font-weight:bold">{_e(lab["findings"])}</p><ul style="margin:0">{findings}</ul>'
        f'<p style="margin:8px 0 2px 0;font-weight:bold">{_e(lab["missing"])}</p><ul style="margin:0">{missing}</ul>'
        f'<p style="margin:8px 0 0 0"><b>{_e(lab["next"])}:</b> {_e(ov["next_action_text"])}</p></td></tr></table>')
    rules = _grid([(lab["rule"], "31%"), (lab["result"], "14%"), (lab["reason"], "55%")],
                  [[f'<b>{_e(r["name"])}</b><br/><span style="color:{MUTED};font-size:7.5pt">'
                    f'{_e(r["explain"])}</span>', _pill(r["status"], r["status_label"]), _e(r["reason"])]
                   for r in engine.ordered(run["rules"]) if r["id"] != "R10"])     # rule 10 is the recommendation
    agency = ""
    rep = ws.get("agency_report")
    if rep:                                           # the agency's report in its own fields, with its analysis
        from app.agents.credit.legal_verification.services import agency_report

        agency = _h2(sec["agency"]) + agency_report.analysis_block(rep.get("analysis") or []) + "".join(
            agency_report.section_block(sid, list(fields), rep["answers"])
            for sid, fields in (section("agency_report").get("sections") or {}).items())
    docs = _grid([(lab["document"], "40%"), (lab["read"], "25%"), (lab["uploaded"], "35%")],
                 [[_e(d["kind_label"]), _e(d["read_label"]), _e(f'{d["uploaded_by"]}, {str(d["uploaded_at"])[:10]}')]
                  for d in ws["documents"]])
    decision = _kv([(lab["decision"], case["decision_label"], lab["by"], case["decided_by"]),
                    (lab["remarks"], case["decision_remarks"], "", "")])
    return f"""<html><body style="font-family:sans-serif;font-size:9pt;color:{INK}">
{top}
{_h2(sec["glance"])}{_glance(ov)}
{_h2(points["label"])}{points_box(points["points"])}
{_h2(sec["recommendation"])}{recommendation}
{_h2(sec["application"])}{application}
{_h2(sec["rules"])}{rules}
{agency}
{_h2(sec["documents"])}{docs}
{_h2(sec["decision"])}{decision}
</body></html>"""


def _background(page) -> None:
    """A LAND SURVEY SHEET behind the text (a property's title is being verified): survey graph paper, contour lines in
    a corner, a dashed plot boundary with corner marks and a dimension line, a compass rose, microtext along the left
    margin like a cheque's security line, a midnight band at the foot. (Not the crime report's waves or watermark.)"""
    import math

    import pymupdf

    w, h = page.rect.width, page.rect.height
    navy, sapphire, amber = (0.04, 0.08, 0.22), (0.23, 0.34, 0.84), (0.96, 0.65, 0.14)
    shape = page.new_shape()
    shape.draw_rect(page.rect)                                          # the survey sheet
    shape.finish(fill=(0.968, 0.976, 1.0), width=0)
    for x in range(0, int(w) + 1, 10):                                  # graph paper: fine every 10, strong every 50
        shape.draw_line(pymupdf.Point(x, 0), pymupdf.Point(x, h))
    for y in range(0, int(h) + 1, 10):
        shape.draw_line(pymupdf.Point(0, y), pymupdf.Point(w, y))
    shape.finish(color=sapphire, width=0.25, stroke_opacity=0.07)
    for x in range(0, int(w) + 1, 50):
        shape.draw_line(pymupdf.Point(x, 0), pymupdf.Point(x, h))
    for y in range(0, int(h) + 1, 50):
        shape.draw_line(pymupdf.Point(0, y), pymupdf.Point(w, y))
    shape.finish(color=sapphire, width=0.5, stroke_opacity=0.11)

    cx, cy = 40, h - 70                                                  # contour lines, bottom left
    for k in range(9):
        r = 40 + k * 22
        pts = [pymupdf.Point(cx + (r + 9 * math.sin(3 * t + k)) * math.cos(t), cy + (r * 0.7 + 7 * math.cos(2 * t + k)) * math.sin(t))
               for t in [i * math.pi / 60 for i in range(121)]]
        shape.draw_polyline(pts)
    shape.finish(color=(0.15, 0.55, 0.45), width=0.6, stroke_opacity=0.16, closePath=False)

    plot = [pymupdf.Point(w - 230, h - 190), pymupdf.Point(w - 70, h - 214), pymupdf.Point(w - 52, h - 92),
            pymupdf.Point(w - 205, h - 70)]                              # the plot boundary, bottom right
    shape.draw_polyline(plot + [plot[0]])
    shape.finish(color=amber, width=1.4, stroke_opacity=0.5, dashes="[6 4] 0", closePath=False)
    for p in plot:                                                      # its corner marks
        shape.draw_circle(p, 3.2)
    shape.finish(color=amber, fill=(1, 1, 1), width=1.1, stroke_opacity=0.65, fill_opacity=0.9)
    a1, a2 = pymupdf.Point(w - 230, h - 202), pymupdf.Point(w - 70, h - 226)   # a dimension line along the top side
    shape.draw_line(a1, a2)
    for p in (a1, a2):
        shape.draw_line(p + (0, -4), p + (0, 4))
    shape.finish(color=navy, width=0.6, stroke_opacity=0.35)

    rx, ry, rr = 92, h - 150, 30                                        # a compass rose, over the contours
    shape.draw_circle(pymupdf.Point(rx, ry), rr)
    shape.draw_circle(pymupdf.Point(rx, ry), rr - 6)
    shape.finish(color=navy, width=0.6, stroke_opacity=0.28)
    for k in range(16):
        t = math.pi * k / 8
        r1 = rr - 6 if k % 2 else rr - 10
        shape.draw_line(pymupdf.Point(rx + r1 * math.cos(t), ry + r1 * math.sin(t)),
                        pymupdf.Point(rx + rr * math.cos(t), ry + rr * math.sin(t)))
    shape.finish(color=navy, width=0.5, stroke_opacity=0.28)
    for t, colour in ((-math.pi / 2, amber), (math.pi / 2, sapphire), (0, sapphire), (math.pi, sapphire)):
        tip = pymupdf.Point(rx + (rr - 7) * math.cos(t), ry + (rr - 7) * math.sin(t))
        left = pymupdf.Point(rx + 5 * math.cos(t + math.pi / 2), ry + 5 * math.sin(t + math.pi / 2))
        right = pymupdf.Point(rx + 5 * math.cos(t - math.pi / 2), ry + 5 * math.sin(t - math.pi / 2))
        shape.draw_polyline([tip, left, pymupdf.Point(rx, ry), right, tip])
        shape.finish(color=colour, fill=colour, width=0.4, stroke_opacity=0.55, fill_opacity=0.45)

    shape.draw_rect(pymupdf.Rect(0, h - 12, w, h))                      # the foot band
    shape.finish(fill=navy, width=0, fill_opacity=0.95)
    shape.draw_rect(pymupdf.Rect(0, h - 15, w, h - 12))
    shape.finish(fill=amber, width=0, fill_opacity=0.95)
    shape.commit(overlay=False)

    page.insert_text(pymupdf.Point(rx - 3.2, ry - rr - 3), say("report.compass_north"), fontsize=8, fontname="hebo",
                     color=navy, fill_opacity=0.55, overlay=False)
    micro = (say("report.microtext") + "  ·  ") * 40                    # the security line along the left margin
    page.insert_text(pymupdf.Point(22, h - 30), micro, fontsize=4.6, fontname="helv", color=sapphire, fill_opacity=0.8,
                     rotate=90, overlay=False)
    page.insert_text(pymupdf.Point(w - 196, h - 207), say("report.plot_label"), fontsize=6, fontname="hebo",
                     color=navy, fill_opacity=0.35, overlay=False)


def _pdf(markup: str, footer: dict[str, str]) -> bytes:
    import pymupdf

    story = pymupdf.Story(html=markup)
    out = io.BytesIO()
    writer = pymupdf.DocumentWriter(out)
    page = pymupdf.paper_rect("a4")
    more = True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(page + (36, 32, -36, -50))
        story.draw(device)
        writer.end_page()
    writer.close()
    doc = pymupdf.open(stream=out.getvalue(), filetype="pdf")
    for n, pg in enumerate(doc, start=1):
        _background(pg)
        w, h = pg.rect.width, pg.rect.height
        pg.draw_line((36, h - 40), (w - 36, h - 40), color=(0.79, 0.83, 0.96), width=0.6)
        pg.insert_text((36, h - 27), say("report.footer", page=n, pages=doc.page_count, **footer), fontsize=7.5,
                       color=(0.23, 0.29, 0.55))
    return doc.tobytes(garbage=3, deflate=True)


def download(who: Caller, application_id: str) -> tuple[bytes, str]:
    require(who, "download_report")
    ws = cases.workspace(who, application_id)
    if ws["run"] is None:
        raise ServiceError(409, "NOT_VALIDATED")
    with database().tx() as conn:
        run = cases.latest_run(conn, ws["case"]["case_id"])
    points = summary.of_run(run)
    generated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    content = _pdf(_html(ws, points, generated), {"title": say("report.title"), "application": application_id})
    with database().tx() as conn:
        audit(conn, application_id, ws["case"]["case_id"], who.user, "REPORT_DOWNLOADED", run_id=run["run_id"])
    return content, f"legal-verification-{application_id}.pdf"


__all__ = ["download", "section"]
