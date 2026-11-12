"""
R3 -- STATEMENT INTEGRITY: is the file readable and complete, and does anything about it look edited?

Signals only, never proof: an editing tool in the PDF metadata (config integrity.suspicious_producers), a file
modified long after it was created (integrity.modified_after_created_hours), transactions dated outside the printed
statement period. Any of them -> REVIEW_REQUIRED. FAIL only when the file is not a statement at all. (The balance
arithmetic is rule 5's.)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.agents.credit.bank_verification.config import section
from app.agents.credit.bank_verification.rules.base import Evidence, Words, flag, result
from app.agents.credit.bank_verification.schemas import ExtractionStatus, RuleResult, RuleStatus

ID = "R3"
w = Words(ID)


def _pdf_date(value: str | None) -> datetime | None:
    """'D:20261001090000+05'30'' -> an aware datetime (None when absent / unparseable)."""
    found = re.match(r"^D:(\d{14})(?:([+\-])(\d{2})'?(\d{2})'?|Z)?", str(value or ""))
    if not found:
        return None
    moment = datetime.strptime(found.group(1), "%Y%m%d%H%M%S")
    if found.group(2):
        offset = timedelta(hours=int(found.group(3)), minutes=int(found.group(4)))
        return moment.replace(tzinfo=timezone(offset if found.group(2) == "+" else -offset))
    return moment.replace(tzinfo=timezone.utc)


def run(ev: Evidence) -> RuleResult:
    facts = ev.facts
    if facts is None and ev.aa is not None:
        return result(ID, RuleStatus.NOT_APPLICABLE, w("aa_only"))
    if facts is None:
        return result(ID, RuleStatus.REVIEW_REQUIRED, w("no_file"), missing_evidence=[w("no_file_missing")],
                      recommended_action="REQUEST_READABLE_STATEMENT")
    notes = "; ".join(facts.notes)
    file_evidence = w("file_evidence", kind=facts.kind, pages=facts.pages, with_text=facts.pages_with_text)
    if facts.extraction_status is ExtractionStatus.INVALID:
        return result(ID, RuleStatus.FAIL, w("invalid", notes=notes), evidence=[file_evidence],
                      risk_flags=[flag(ID, "NOT_A_STATEMENT")], recommended_action="REQUEST_READABLE_STATEMENT",
                      details={"notes": facts.notes})
    if facts.extraction_status is ExtractionStatus.UNREADABLE:
        return result(ID, RuleStatus.REVIEW_REQUIRED, w("unreadable", notes=notes),
                      missing_evidence=[w("unreadable_missing")], evidence=[file_evidence],
                      recommended_action="REQUEST_READABLE_STATEMENT", details={"notes": facts.notes})
    spec = section("integrity")
    checks = [w("readable", with_text=facts.pages_with_text, pages=facts.pages)]
    flags = []
    if facts.extraction_status is ExtractionStatus.PARTIAL:
        flags.append(flag(ID, "INCOMPLETE_EXTRACTION"))
    if facts.scanned:
        flags.append(flag(ID, "READ_FROM_SCAN"))
    meta = facts.metadata
    made_by = " ".join(meta.get(k, "") for k in ("producer", "creator")).lower()
    tool = next((t for t in (str(x).lower() for x in spec.get("suspicious_producers") or []) if t in made_by), None)
    if tool:
        flags.append(flag(ID, "EDITING_TOOL_IN_METADATA", tool=tool))
    created, modified = _pdf_date(meta.get("creationDate")), _pdf_date(meta.get("modDate"))
    if created and modified and modified - created > timedelta(hours=float(spec.get("modified_after_created_hours", 24))):
        flags.append(flag(ID, "MODIFIED_AFTER_CREATION", days=(modified - created).days))
    if facts.kind == "PDF":
        checks.append(w("producer", producer=meta.get("producer", "—")))
    data = ev.statement
    if data is not None and data.period_start and data.period_end:
        grace = timedelta(days=int(spec.get("period_grace_days", 0)))
        outside = [t for t in data.transactions
                   if not (data.period_start - grace <= t.date <= data.period_end + grace)]
        if outside:
            flags.append(flag(ID, "TRANSACTION_OUTSIDE_PERIOD", n=len(outside)))
        else:
            checks.append(w("in_period"))
    checks += [w("reader_note", note=n) for n in facts.notes]
    status = RuleStatus.REVIEW_REQUIRED if flags else RuleStatus.PASS
    reason = w("review", signals="; ".join(f.message for f in flags)) if flags else w("pass")
    return result(ID, status, reason, risk_flags=flags, evidence=checks,
                  recommended_action="AGENCY_REVIEW" if flags else None,
                  details={"metadata": {k: meta[k] for k in ("producer", "creator", "creationDate", "modDate")
                                        if k in meta}, "pages": facts.pages})
