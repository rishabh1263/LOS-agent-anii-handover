"""
THE CPA HANDOFF NOTE (FOS plan 7.1; flag COPILOT_HANDOFF_NOTE, config chatbot.handoff_note -- ON in dev).

"handoff note banao" / "CPA note" -> ONLY when the case passes the live FOS gate (readiness_report, which is the
gate's verdict): one page -- case and parties (masked), loan details, documents with their verification status, the
KYC table per party, signature, exceptions and overrides with reasons (the recorded maker-checker approvals),
generated time, officer. In chat (markdown) and as a file (`GET /api/v1/fos/handoff-note?case_id=..&format=md|html|
pdf`; PDF through the installed PyMuPDF -- no new dependency). Every note made or downloaded is audited. Not ready ->
refused, with what is still blocking. The note never moves the case: a person confirms the move.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

FLAG = "COPILOT_HANDOFF_NOTE"
_ON = {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("handoff_note")


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg().get("enabled", False))


def asked(message: str) -> bool:
    said = " " + " ".join(re.findall(r"[\wऀ-ॿ]+", str(message or "").lower())) + " "
    return any(f" {' '.join(str(p).lower().split())} " in said for p in _cfg().get("phrases") or [])


def _label(key: str, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    defaults = {"title": "CPA Handoff Note -- {case_id}", "not_ready": "A handoff note is made once the case is "
                "ready for CPA. Not ready yet: {passed} of {total} checks passed.",
                "confirm": "Case is ready for CPA. A person must confirm the move.",
                "download": "Download the handoff note (PDF)"}
    return language_lock.pick((_cfg().get("labels") or {}).get(key, defaults.get(key, key))).format(**values)


def _readable(value: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(value)


def build(case_id: str, officer: str | None = None, repository: Any = None) -> dict[str, Any]:
    """{"ready", "markdown", "generated_at"} -- the note, or ready=False with the report that blocks it."""
    from app.agents.applicant.copilot.answering import kyc_table, readiness_report
    from app.agents.applicant.copilot.answering.profile import _shown
    from app.store import get_repository

    repository = repository or get_repository()
    report = readiness_report.build(case_id, repository)
    if not report.get("ready"):
        return {"ready": False, "report": report}
    application = repository.get_application(case_id)
    applicant = repository.get_applicant(application.applicant_id)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# {_label('title', case_id=case_id)}", "",
             f"**Generated:** {generated}  ", f"**Officer:** {officer or '--'}", "", _label("confirm"), "",
             "## Case and parties",
             f"- Case: {case_id} -- stage FOS, {_readable(getattr(application, 'status', ''))}",
             f"- Applicant: {_shown('full_name', getattr(applicant, 'full_name', None)) or '--'} "
             f"({application.applicant_id})"]
    try:
        from app.agents.los import co_applicants

        for co in co_applicants.list_for_case(case_id, repository):
            lines.append(f"- Co-applicant: {_shown('full_name', co.get('name')) or '--'} ({co.get('co_applicant_id')})")
    except Exception:  # noqa: BLE001 - no co-applicant table: none listed
        pass
    lines += ["", "## Loan details"]
    for field in ("product", "loan_amount", "tenure_months", "interest_rate_pct", "employment_type"):
        value = getattr(application, field, None)
        if value not in (None, ""):
            lines.append(f"- {_readable(field)}: {_shown(field, value)}")
    lines += ["", "## Documents", "| Party | Document | Status |", "|---|---|---|"]
    for d in repository.list_documents(case_id) or []:
        status = str(getattr(getattr(d, "status", ""), "value", getattr(d, "status", ""))).upper()
        if status == "SUPERSEDED":
            continue
        role = str(getattr(getattr(d, "party_role", None), "value", getattr(d, "party_role", None)) or "")
        lines.append(f"| {'Co-applicant' if role == 'CO_APPLICANT' else 'Applicant'} | "
                     f"{_readable(d.document_type)} | {_readable(status)} |")
    table = kyc_table.build(case_id, repository)
    lines += ["", "## KYC", kyc_table.render(table)]
    exceptions = [i for g in report.get("groups") or [] if not g.get("counted") for i in g["items"]
                  if i["status"] != "PASS"]
    approvals = []
    try:
        approvals = repository.list_approvals(case_id) or []
    except Exception:  # noqa: BLE001
        approvals = []
    lines += ["", "## Exceptions and overrides"]
    if not exceptions and not approvals:
        lines.append("- None recorded.")
    for i in exceptions:
        lines.append(f"- {i['label']}: {i['status']} -- {i['reason']} (not required for CPA by the current policy)")
    for a in approvals:
        lines.append(f"- {_readable(a.get('action') or a.get('type') or 'Approval')}: {_readable(a.get('status') or '')}"
                     f" -- {a.get('reason') or 'no reason recorded'}")
    return {"ready": True, "markdown": "\n".join(lines).strip(), "generated_at": generated, "report": report}


def to_html(markdown: str) -> str:
    from markdown_it import MarkdownIt

    body = MarkdownIt("commonmark").enable("table").render(markdown)
    style = ("body{font-family:Helvetica,Arial,sans-serif;font-size:10pt;color:#111}"
             "table{border-collapse:collapse;width:100%}td,th{border:1px solid #999;padding:3px 5px;text-align:left}"
             "h1{font-size:15pt}h2{font-size:12pt;margin-top:12px}")
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{style}</style></head><body>{body}</body></html>"


def to_pdf(markdown: str) -> bytes:
    """The note as PDF through PyMuPDF's HTML story (already installed)."""
    import io

    import fitz

    html = to_html(markdown)
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    story = fitz.Story(html=html)
    rect = fitz.paper_rect("a4")
    where = rect + (36, 36, -36, -36)
    more = True
    while more:
        device = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buffer.getvalue()


def attach(published: dict[str, Any], message: str, claims: dict[str, Any] | None = None) -> dict[str, Any]:
    """A handoff-note request: the note (ready) or the refusal with what blocks it. Shared by both routes."""
    if not isinstance(published, dict) or not enabled() or not published.get("case_id") or not asked(message):
        return published
    from app.agents.applicant import audit
    from app.security.auth import get_subject

    case_id = published["case_id"]
    officer = str(get_subject(claims) or "") if claims else None
    note = build(case_id, officer)
    from app.agents.applicant.copilot.answering.counts import keep_case_header

    if not note["ready"]:
        report = note["report"]
        text = _label("not_ready", passed=report.get("passed", 0), total=report.get("total", 0))
        if report.get("next_fix"):
            text += f"\n👉 {report['next_fix']}."
        published.update(intent="HANDOFF_NOTE", answer=keep_case_header(published.get("answer"), text, case_id))
        audit.record(request_id=str(published.get("request_id") or ""), subject=officer,
                     applicant_id=published.get("applicant_id"), case_id=case_id,
                     intent="HANDOFF_NOTE", tools=[], status="REFUSED_NOT_READY", message=message)
        return published
    published.update(intent="HANDOFF_NOTE", answer=keep_case_header(published.get("answer"), note["markdown"], case_id),
                     handoff_note={"generated_at": note["generated_at"]})
    published.pop("answer_markdown", None)
    published["actions"] = list(published.get("actions") or []) + [
        {"type": "OPEN_URL", "label": _label("download"),
         "url": f"/api/v1/fos/handoff-note?case_id={case_id}&format=pdf"}]
    audit.record(request_id=str(published.get("request_id") or ""), subject=officer,
                     applicant_id=published.get("applicant_id"), case_id=case_id,
                 intent="HANDOFF_NOTE", tools=["handoff.note"], status="OK", message=message)
    return published


__all__ = ["FLAG", "asked", "attach", "build", "enabled", "to_html", "to_pdf"]
