"""
RESUME VERDICT (2026-10-16) -- what the document IS, then whether the resume holds together. Deterministic.

    VERIFIED        a resume: readable, a name, a valid email or mobile, and a timeline with no contradiction
    MANUAL_REVIEW   a resume with something a person should look at (an overlap, a future date, education out of
                    order, contact missing or malformed, a near-but-not-equal name)
    REJECTED        a resume of someone else (the name clearly differs from the one HR expects)
    UNSUPPORTED     NOT a resume -- and which document it looks like instead (PAN, passbook, marksheet, ...)
    UNREADABLE      too little text to judge

WHAT VERIFIED MEANS, AND DOES NOT: the file is a resume and is internally consistent (and consistent with the name /
years HR supplied). Employers, dates and claims are NOT checked with anyone -- that needs background verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

from app.agents.resume.parse import LEVEL_ORDER, Resume

VERIFIED, MANUAL_REVIEW, REJECTED, UNSUPPORTED, UNREADABLE = (
    "VERIFIED", "MANUAL_REVIEW", "REJECTED", "UNSUPPORTED", "UNREADABLE")

# Another document, recognised by what only it prints. Checked BEFORE resume signals: a resume never carries these.
OTHER_DOCUMENTS: list[tuple[str, tuple[str, ...], int]] = [
    # (type, markers in compact upper text, how many must appear)
    ("PAN_CARD", ("INCOMETAXDEPARTMENT", "PERMANENTACCOUNTNUMBER", "GOVTOFINDIA"), 2),
    ("AADHAAR", ("UNIQUEIDENTIFICATIONAUTHORITY", "AADHAAR", "MERAAADHAAR", "UIDAI"), 2),
    ("VOTER_ID", ("ELECTIONCOMMISSION", "ELECTORSPHOTOIDENTITY", "ELECTOR"), 2),
    ("DRIVING_LICENCE", ("DRIVINGLICENCE", "DRIVINGLICENSE", "TRANSPORTDEPARTMENT", "VALIDTILL"), 2),
    ("PASSPORT", ("REPUBLICOFINDIA", "PASSPORTNO", "PLACEOFISSUE", "P<IND"), 2),
    ("BANK_PASSBOOK", ("PASSBOOK", "IFSC", "CIFNO", "MICR"), 2),
    ("BANK_STATEMENT", ("STATEMENTOFACCOUNT", "OPENINGBALANCE", "CLOSINGBALANCE", "WITHDRAWAL", "IFSC"), 3),
    ("SALARY_SLIP", ("SALARYSLIP", "PAYSLIP", "NETPAY", "GROSSEARNINGS", "TOTALDEDUCTIONS", "BASICSALARY"), 2),
    ("MARKSHEET", ("STATEMENTOFMARKS", "MARKSHEET", "MARKSOBTAINED", "MAXIMUMMARKS", "ROLLNO", "BOARDOF",
                   "GRANDTOTAL", "RESULTPASS"), 3),
    ("DEGREE_CERTIFICATE", ("THISISTOCERTIFY", "DEGREEOF", "CONFERRED", "CONVOCATION", "VICECHANCELLOR"), 2),
    ("OFFER_OR_EXPERIENCE_LETTER", ("OFFERLETTER", "LETTEROFAPPOINTMENT", "APPOINTMENTLETTER", "TOWHOMSOEVER",
                                    "RELIEVINGLETTER", "EXPERIENCECERTIFICATE", "WEAREPLEASEDTOOFFER"), 1),
    ("ITR", ("INDIANINCOMETAXRETURN", "ACKNOWLEDGEMENTNUMBER", "ASSESSMENTYEAR", "ITRV"), 2),
    ("INVOICE", ("TAXINVOICE", "GSTIN", "INVOICENO", "HSN"), 2),
]
RESUME_TITLES = ("RESUME", "CURRICULUMVITAE", "BIODATA")
MIN_TEXT_CHARS = 120


@dataclass
class Check:
    name: str
    result: str                 # PASS | FAIL | REVIEW | NOT_APPLICABLE
    reason: str | None = None
    detail: str | None = None


@dataclass
class Verdict:
    status: str
    reasons: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    looks_like: str | None = None


def identify(text: str, resume: Resume) -> tuple[str | None, int]:
    """(another document's type or None, resume evidence score). A resume with its own title or 2+ resume sections
    and a contact is a resume even if it mentions one of another document's words (a CA's resume says GSTIN)."""
    compact = re.sub(r"[^A-Z0-9<]", "", (text or "").upper())
    sections = {s for s in resume.sections if s in ("education", "experience", "skills", "summary", "projects",
                                                     "certifications", "personal", "declaration")}
    score = len(sections) + (1 if resume.email or resume.phone else 0) + \
        (1 if any(t in compact[:200] for t in RESUME_TITLES) else 0)
    for kind, markers, need in OTHER_DOCUMENTS:
        if sum(m in compact for m in markers) >= need and score < 4:
            return kind, score
    return None, score


def _name_similarity(a: str | None, b: str | None) -> float:
    """1.0 for the same name (one may carry a middle name the other omits); a DIFFERENT FIRST NAME is a different
    person whatever the surname -- "PRIYA SHARMA" is not "RAHUL SHARMA" (they scored 0.6 as text and went to review,
    2026-10-16), so it scores 0."""
    if not a or not b:
        return 0.0
    la, lb = re.sub(r"[^A-Z ]", " ", a.upper()).split(), re.sub(r"[^A-Z ]", " ", b.upper()).split()
    ta, tb = set(la), set(lb)
    if ta and tb and (ta <= tb or tb <= ta):
        return 1.0
    if la and lb and len(la[0]) > 1 and len(lb[0]) > 1 and SequenceMatcher(None, la[0], lb[0]).ratio() < 0.75:
        return 0.0
    return SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()


def decide(text: str, resume: Resume, *, expected: dict | None = None, today: date | None = None) -> Verdict:
    today = today or date.today()
    checks: list[Check] = []
    reasons: list[str] = []

    def add(name, result, reason=None, detail=None):
        checks.append(Check(name, result, reason, detail))
        if reason and reason not in reasons:
            reasons.append(reason)

    if len(re.sub(r"\s", "", text or "")) < MIN_TEXT_CHARS:
        add("readable", "FAIL", "TOO_LITTLE_TEXT", f"{len(text or '')} characters")
        return Verdict(UNREADABLE, reasons, checks)
    other, score = identify(text, resume)
    if other:
        add("is_resume", "FAIL", "NOT_A_RESUME", f"reads as {other}")
        return Verdict(UNSUPPORTED, reasons, checks, looks_like=other)
    if score < 2:
        add("is_resume", "FAIL", "NOT_A_RESUME", f"resume evidence {score} (sections + contact + title)")
        return Verdict(UNSUPPORTED, reasons, checks)
    add("is_resume", "PASS", None, f"sections: {', '.join(resume.sections) or 'none'}")

    add("name_present", "PASS" if resume.name else "REVIEW", None if resume.name else "NAME_NOT_FOUND")
    if resume.email or resume.phone:
        add("contact_present", "PASS")
    else:
        add("contact_present", "REVIEW", "CONTACT_MISSING", "no email and no 10-digit Indian mobile")

    # timeline
    now = today.year * 12 + today.month
    future = [j for j in resume.experience if j.start[0] * 12 + j.start[1] > now + 1
              or (j.end and j.end[0] * 12 + j.end[1] > now + 1)]
    future_edu = [e for e in resume.education if e.year and e.year > today.year and not e.pursuing]
    if future or future_edu:
        add("dates_not_in_future", "REVIEW", "FUTURE_DATE",
            f"{len(future)} job period(s), {len(future_edu)} completed qualification(s) after today")
    else:
        add("dates_not_in_future", "PASS")
    backwards = [j for j in resume.experience if j.end and (j.end[0] * 12 + j.end[1]) < (j.start[0] * 12 + j.start[1])]
    if backwards:
        add("job_dates_in_order", "REVIEW", "JOB_ENDS_BEFORE_IT_STARTS", f"{len(backwards)} period(s)")
    jobs = sorted((j for j in resume.experience if not j.internship), key=lambda j: j.start)

    def span(j):
        end = j.end or (today.year, today.month)
        return j.start[0] * 12 + j.start[1], end[0] * 12 + end[1]

    overlap = 0
    for i, a in enumerate(jobs):
        for b in jobs[i + 1:]:
            (a0, a1), (b0, b1) = span(a), span(b)
            # A PROMOTION, NOT A SECOND JOB: an employer's whole period with each role inside it, the inner one
            # starting or ending with the outer ("EMG 11/2018-Present", then "Coordinator 11/2018-01/2022" and
            # "Portfolio Coordinator 01/2022-Present" -- real sample resume, 2026-10-16).
            nested = (a0 <= b0 and b1 <= a1) or (b0 <= a0 and a1 <= b1)
            aligned = a0 == b0 or a1 == b1
            if nested and aligned:
                continue
            overlap = max(overlap, min(a1, b1) - max(a0, b0))
    if overlap > 6:
        add("jobs_not_overlapping", "REVIEW", "OVERLAPPING_JOBS", f"{overlap} months of two full-time jobs at once")
    elif len(jobs) > 1:
        add("jobs_not_overlapping", "PASS")
    years = {}
    for e in resume.education:
        if e.year and e.level in LEVEL_ORDER:
            years.setdefault(e.level, e.year)
    order = [("10th", "12th", 1), ("12th", "graduation", 2), ("10th", "graduation", 3),
             ("graduation", "post_graduation", 1)]
    bad = [f"{a} {years[a]} -> {b} {years[b]}" for a, b, gap in order if a in years and b in years
           and years[b] - years[a] < gap]
    if bad:
        add("education_in_order", "REVIEW", "EDUCATION_TIMELINE_INCONSISTENT", "; ".join(bad))
    elif len(years) > 1:
        add("education_in_order", "PASS")
    if jobs and "graduation" in years and jobs[0].start[0] < years.get("10th", 0):
        add("work_after_schooling", "REVIEW", "WORK_BEFORE_SCHOOLING", "a job starts before the 10th was passed")

    # what HR already holds (optional)
    expected = {k: v for k, v in (expected or {}).items() if v not in (None, "")}
    if expected.get("name"):
        sim = _name_similarity(resume.name, expected["name"])
        if not resume.name:
            add("name_matches_records", "REVIEW", "NAME_NOT_FOUND")
        elif sim >= 0.85:
            add("name_matches_records", "PASS", None, f"similarity {sim:.2f}")
        elif sim >= 0.5:
            add("name_matches_records", "REVIEW", "NAME_PARTLY_MATCHES", f"similarity {sim:.2f}")
        else:
            add("name_matches_records", "FAIL", "NAME_MISMATCH", f"similarity {sim:.2f}")
    for level, key in (("10th", "year_10th"), ("12th", "year_12th"), ("graduation", "graduation_year")):
        if expected.get(key):
            want = int(expected[key])
            got = years.get(level)
            add(f"{level}_year_matches_records", "PASS" if got == want else "REVIEW",
                None if got == want else f"{level.upper()}_YEAR_MISMATCH" if got else f"{level.upper()}_YEAR_NOT_FOUND",
                f"resume {got}, records {want}")

    if any(c.result == "FAIL" for c in checks):
        return Verdict(REJECTED, reasons, checks)
    if any(c.result == "REVIEW" for c in checks):
        return Verdict(MANUAL_REVIEW, reasons, checks)
    return Verdict(VERIFIED, reasons, checks)


__all__ = ["Check", "MANUAL_REVIEW", "OTHER_DOCUMENTS", "REJECTED", "UNREADABLE", "UNSUPPORTED", "VERIFIED",
           "Verdict", "decide", "identify"]
