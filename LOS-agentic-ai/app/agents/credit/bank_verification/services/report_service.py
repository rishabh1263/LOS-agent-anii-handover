"""
The verification report: the summary in easy words, rule-wise results, discrepancies, risk flags, missing evidence,
the human actions still open, ownership, the final decision and its rationale, and the case history. One HTML
document; the PDF is the same HTML laid out by PyMuPDF (no browser, no extra dependency). Every heading, label and
colour is in the wording config (`report`). Account numbers stay masked.
"""

from __future__ import annotations

import html
import io
from typing import Any

from app.agents.credit.bank_verification.config import cfg, say, words
from app.agents.credit.bank_verification.repository import Repository
from app.agents.credit.bank_verification.schemas import rupees
from app.agents.credit.bank_verification.services import summary_service, verification_service
from app.agents.credit.bank_verification.services.verification_service import ServiceError


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _badge(status: str | None, label: str | None = None) -> str:
    colours = words("report.colours")
    colour = colours.get(str(status), colours["default"])
    return f'<b style="color:{colour}">{_e(label or str(status or "—").replace("_", " "))}</b>'


def _items(values: list[str]) -> str:
    return "".join(f"<li>{_e(v)}</li>" for v in values) or f"<li>{_e(say('report.none'))}</li>"


def build(repo: Repository, case_id: str) -> dict[str, Any]:
    """Everything the report shows, as data (the screen and the files read the same thing)."""
    case = repo.case(case_id)
    if case is None or case.get("historical"):
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=case_id)
    found = verification_service.latest(repo, case_id)
    if found is None:
        raise ServiceError(409, "NOT_PROCESSED")
    run, _ = found
    decisions = repo.decisions(case_id)
    decision = decisions[-1] if decisions else None
    agencies = {a["id"]: a["name"] for a in cfg().get("agencies") or []}
    open_actions = []
    if run["recommended_action"] and run["recommended_action"] != "READY_FOR_FINALIZATION" \
            and case["status"] not in verification_service.FINAL:
        open_actions.append((cfg().get("actions") or {}).get(run["recommended_action"], run["recommended_action"]))
    if decision and decision["outcome"].get("blocked_reasons") and case["status"] not in verification_service.FINAL:
        open_actions += decision["outcome"]["blocked_reasons"]
    return {"case": case, "run": run, "decision": decision, "agency": agencies.get(case.get("assigned_agency") or ""),
            "history": repo.history(case_id), "open_actions": open_actions,
            "summary": summary_service.summarise(repo, case_id, start=False)}


def html_report(repo: Repository, case_id: str) -> str:
    data = build(repo, case_id)
    case, run, decision, plain = data["case"], data["run"], data["decision"], data["summary"]
    sec, col = words("report.sections"), words("report.columns")
    rows = "".join(f"<tr><td>{_e(r['rule_name'])}</td><td>{_badge(r['status'])}</td>"
                   f"<td>{_e(r['reason'])}</td></tr>" for r in run["rules"])
    easy = "".join(f"<li><b>{_e(i['name'])}</b> ({_e(i['status_label'])}): {_e(i['text'])}</li>"
                   for i in plain["checklist"])
    flags = [f"[{f['severity'].title()}] {f['message']}" for f in run.get("risk_flags") or []]
    if decision:
        p = decision["payload"]
        checklist = "".join(f"<li>{_e(k.replace('_', ' '))}: {_e(v)}</li>" for k, v in p["checklist"].items())
        decided = (f"<p>{_badge(decision['final_status'])} ({_e(decision['requested'])}) · {_e(decision['decided_by'])}"
                   f" · {_e(decision['created_at'])}</p>"
                   f"<p>{_badge(p['ownership'])} · {_e(p['branch_visit'])}</p><p>{_e(p['rationale'])}</p>"
                   + (f"<p>{_e(p['remarks'])}</p>" if p.get("remarks") else "")
                   + f"<ul>{checklist}</ul>"
                   + "".join(f"<p>{_e(b)}</p>" for b in decision["outcome"].get("blocked_reasons") or []))
    else:
        decided = f"<p>{_e(say('report.no_decision'))}</p>"
    history = "".join(f"<tr><td>{_e(h['at'])}</td><td>{_e(h['actor'])}</td><td>{_e(h['event'])}</td></tr>"
                      for h in data["history"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_e(say('report.title', case_id=case_id))}</title>
<style>body{{font-family:Helvetica,Arial,sans-serif;font-size:10pt;color:#1d2433;margin:24px}}
h1{{font-size:15pt;margin:0 0 4px}} h2{{font-size:11.5pt;margin:16px 0 6px;border-bottom:1px solid #d0d5dd}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #d0d5dd;padding:4px 6px;vertical-align:top;text-align:left}}
th{{background:#f2f4f7}}</style></head><body>
<h1>{_e(say('report.title', case_id=case_id))}</h1>
<p>{_e(case['applicant_name'])} · {_e(case['application_ref'])} · {_e(case['product'].replace('_', ' ').title())} ·
{_e(rupees(case['loan_amount']))} · {_e(case['stage'])} · {_e(words('report.modes').get(run['mode'], ''))}<br>
{_badge(case['status'], words('case_status.labels').get(case['status']))} ·
{_badge(run['overall_status'], words('plain.status').get(run['overall_status']))} · {_e(data['agency'] or '—')}</p>
<h2>{_e(plain['label'])}</h2><p>{_e(plain['text'])}</p><p style="color:#667085;font-size:8.5pt">{_e(plain['note'])}</p>
<ul>{easy}</ul>
<h2>{_e(sec['assessment'])}</h2><p>{_e(run['explanation'])}</p><p>{_e(run['checks']['score_note'])}</p>
<h2>{_e(sec['rules'])}</h2><table><tr><th>{_e(col['name'])}</th><th>{_e(col['status'])}</th>
<th>{_e(col['explanation'])}</th></tr>{rows}</table>
<h2>{_e(sec['discrepancies'])}</h2><ul>{_items(run.get('mismatches') or [])}</ul>
<h2>{_e(sec['flags'])}</h2><ul>{_items(flags)}</ul>
<h2>{_e(sec['missing'])}</h2><ul>{_items(run.get('missing_evidence') or [])}</ul>
<h2>{_e(sec['actions'])}</h2><ul>{_items(data['open_actions'])}</ul>
<h2>{_e(sec['decision'])}</h2>{decided}
<h2>{_e(sec['history'])}</h2><table><tr><th>{_e(col['when'])}</th><th>{_e(col['who'])}</th><th>{_e(col['what'])}</th></tr>{history}</table>
<p style="color:#667085;font-size:8.5pt">{_e(say('report.footer', run_id=run['run_id']))}</p>
</body></html>"""


def pdf_report(repo: Repository, case_id: str) -> bytes:
    import pymupdf

    story = pymupdf.Story(html=html_report(repo, case_id))
    out = io.BytesIO()
    writer = pymupdf.DocumentWriter(out)
    page = pymupdf.paper_rect("a4")
    where = page + (36, 36, -36, -36)
    more = True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    return out.getvalue()
