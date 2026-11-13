"""
REAL-WORLD-STYLE RESUMES WITH GROUND TRUTH FOR EVERY HR FIELD (2026-10-16) -- made-up people, the layouts and file
types Indian HR actually receives.

    python -m evals.resume.realworld --split dev       # samples/resume_realworld/dev/
    python -m evals.resume.realworld --split heldout   # samples/resume_realworld/heldout/

TEMPLATES  naukri_biodata (objective, education TABLE, "Company: / Designation: / Duration:" blocks, personal details,
           declaration + Place) | modern (one line per job "Designation - Company  Mon YYYY - Present", bullets,
           CTC / notice line) | two_column (sidebar: contact, skills, languages, DOB; main: experience, education) |
           table_cv (personal-details table, employment table with FROM and TO cells) | fresher (internships,
           languages table, immediate joiner) | banking_ops (HELD-OUT ONLY: other headings and label spellings)
FORMATS    docx, docx_table, docx_twocol (layout table), docx_header (name + contact in the page header), doc (Word
           97-2003, written here -- validated with antiword), rtf, odt, txt, html_as_doc (a portal's "Word" export),
           pdf, pdf_twocol, pdf_scan (image only), jpg_photo (degraded), png_screen
TRAPS      father's name, gender, PAN, Aadhaar printed (must never come back unmasked); a referee's phone; a
           "Preferred location" that is not where the person lives; employment gaps
DEV and HELDOUT use different seeds, name pools, cities and companies; banking_ops and some label spellings appear
only in HELDOUT, so held-out accuracy measures generalisation, not memory.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import struct
import zipfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "samples" / "resume_realworld"
TODAY = date(2026, 10, 16)

POOLS = {
    "dev": {
        "first": ["Rahul", "Priya", "Amit", "Sneha", "Vikas", "Pooja", "Sanjay", "Kavita", "Anil", "Meena", "Rohit",
                  "Neha"],
        "last": ["Sharma", "Patil", "Kulkarni", "Verma", "Deshmukh", "Gupta", "Jadhav", "Reddy"],
        "fathers": ["Ramesh", "Suresh", "Mahesh", "Dinesh", "Prakash"],
        "cities": [("Pune", "Maharashtra", "411"), ("Nagpur", "Maharashtra", "440"), ("Indore", "Madhya Pradesh", "452"),
                   ("Jaipur", "Rajasthan", "302"), ("Nashik", "Maharashtra", "422"), ("Surat", "Gujarat", "395")],
        "companies": ["HDFC Bank", "Bajaj Finserv Ltd", "Infosys", "Shriram Finance", "Tata Capital Ltd",
                      "Axis Bank", "Muthoot Finance", "Capgemini India Pvt Ltd", "IndusInd Bank",
                      "Wipro Technologies"],
        "titles": ["Sales Officer", "Credit Analyst", "Relationship Manager", "Branch Executive", "Collection Officer",
                   "Operations Executive", "Customer Service Executive", "Field Officer"],
        "skills": ["MS Excel", "Tally ERP", "Credit Appraisal", "Customer Handling", "Field Sales", "SAP", "Loan Processing",
                   "KYC Verification", "Communication", "Team Management"],
        "templates": ["naukri_biodata", "modern", "two_column", "table_cv", "fresher"],
    },
    "heldout": {
        "first": ["Kiran", "Sunita", "Arjun", "Deepa", "Nitin", "Asha", "Ganesh", "Swati", "Prakash", "Rekha",
                  "Manoj", "Divya"],
        "last": ["Joshi", "Pawar", "Mishra", "Shinde", "Chavan", "Singh", "Yadav", "Nair"],
        "fathers": ["Vijay", "Ashok", "Santosh", "Rajendra", "Mohan"],
        "cities": [("Lucknow", "Uttar Pradesh", "226"), ("Aurangabad", "Maharashtra", "431"),
                   ("Bhopal", "Madhya Pradesh", "462"), ("Vadodara", "Gujarat", "390"), ("Kolhapur", "Maharashtra", "416"),
                   ("Coimbatore", "Tamil Nadu", "641")],
        "companies": ["ICICI Bank", "L&T Finance Ltd", "Tech Mahindra", "Manappuram Finance", "Aditya Birla Capital",
                      "Kotak Mahindra Bank", "Cholamandalam Investment and Finance Company", "Genpact",
                      "Ujjivan Small Finance Bank", "Mahindra Finance"],
        "titles": ["Loan Officer", "Credit Manager", "Sales Executive", "Branch Manager", "Recovery Officer",
                   "Business Development Executive", "Customer Relationship Officer", "Underwriter"],
        "skills": ["Advanced Excel", "Underwriting", "Portfolio Management", "Cross Selling", "CIBIL Analysis", "Tally",
                   "Documentation", "Negotiation", "MIS Reporting", "Lead Generation"],
        "templates": ["naukri_biodata", "modern", "two_column", "table_cv", "fresher", "banking_ops"],
    },
}
LANGS = ["English", "Hindi", "Marathi", "Gujarati", "Tamil", "Telugu", "Kannada", "Bengali", "Punjabi", "Malayalam"]
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
         "December"]
FORMATS = ["docx", "docx_table", "docx_twocol", "docx_header", "doc", "rtf", "odt", "txt", "html_as_doc", "pdf",
           "pdf_twocol", "pdf_scan", "jpg_photo", "png_screen"]


# ============================================================================== the person (the truth)
def person(rng: random.Random, split: str, template: str) -> dict:
    pool = POOLS[split]
    first, last = rng.choice(pool["first"]), rng.choice(pool["last"])
    fresher = template == "fresher"
    born = date(rng.randint(1985, 2001) if not fresher else rng.randint(2000, 2003), rng.randint(1, 12),
                rng.randint(1, 28))
    y10 = born.year + 16
    grad_level = rng.choice(["graduation", "graduation", "post_graduation"])
    degree = rng.choice(["B.Com", "BBA", "B.Sc", "B.A."]) if grad_level == "graduation" else rng.choice(
        ["MBA (Finance)", "M.Com", "MCA"])
    ug = rng.choice(["B.Com", "BBA", "B.Sc"])
    grad = y10 + 5 if grad_level == "graduation" else y10 + 7
    city, state, pin3 = rng.choice(pool["cities"])
    pin = f"{pin3}{rng.randint(0, 999):03d}"
    jobs, gaps = [], []
    if fresher:
        if rng.random() < 0.7:
            s = (grad, rng.randint(5, 7))
            jobs.append({"employer": rng.choice(pool["companies"]), "designation": "Intern", "start": s,
                         "end": (s[0], s[1] + 2), "internship": True})
    else:
        start = (grad, rng.randint(6, 9))
        companies = rng.sample(pool["companies"], 4)
        count = rng.randint(1, 3)
        pending_gap = 0
        for k in range(count):
            months = rng.randint(14, 36)
            e = start[0] * 12 + start[1] - 1 + months
            end = (e // 12, e % 12 + 1)
            if end[0] * 12 + end[1] >= TODAY.year * 12 + TODAY.month - 1:
                end = None
            jobs.append({"employer": companies[k], "designation": rng.choice(pool["titles"]), "start": start,
                         "end": end, "internship": False})
            if pending_gap >= 6:                       # a gap counts only once the NEXT job exists
                gaps.append(pending_gap)
            if end is None or k == count - 1:
                break
            gap = rng.choice([1, 1, 2, 3, 8, 10])
            nxt = end[0] * 12 + end[1] - 1 + gap
            if nxt >= TODAY.year * 12 + TODAY.month - 2:
                jobs[-1]["end"] = None
                break
            start = (nxt // 12, nxt % 12 + 1)
            pending_gap = gap - 1
        if jobs and jobs[-1]["end"] is not None and rng.random() < 0.6:
            jobs[-1]["end"] = None
    langs = ["English", "Hindi"] + rng.sample([x for x in LANGS if x not in ("English", "Hindi")], rng.randint(0, 2))
    rng.shuffle(langs)
    ctc = None if fresher else rng.choice([
        ("{:.1f} LPA", "annual_lakh"), ("Rs. {:,} p.a.", "annual_rupees"), ("{:,} per month", "monthly"),
        ("{:.2f} Lakhs per annum", "annual_lakh")])
    lakhs = round(rng.uniform(2.4, 9.5), 1)
    if ctc:
        style = ctc[1]
        if style == "monthly":
            monthly = int(round(lakhs * 1e5 / 12 / 500) * 500)
            ctc_text, ctc_annual = ctc[0].format(monthly), monthly * 12
        elif style == "annual_rupees":
            rupees = int(round(lakhs * 1e5 / 1000) * 1000)
            ctc_text, ctc_annual = ctc[0].format(rupees).replace(",", "X"), rupees
            ctc_text = _indian_commas(rupees, "Rs. {} p.a.")
        else:
            ctc_text, ctc_annual = ctc[0].format(lakhs), round(lakhs * 1e5)
    else:
        ctc_text = ctc_annual = None
    exp_choice = rng.choice(["range", "single", "negotiable"]) if not fresher else rng.choice(["single", "negotiable"])
    if exp_choice == "range":
        lo = round((lakhs or 3) + rng.choice([0.5, 1.0, 1.5]), 1)
        exp_text, exp_truth = f"{lo:.1f} - {lo + 1:.1f} LPA", {"min": round(lo * 1e5), "max": round((lo + 1) * 1e5)}
    elif exp_choice == "single":
        v = round((lakhs or 2.5) * rng.choice([1.2, 1.3]), 1)
        exp_text, exp_truth = f"{v:.1f} LPA", {"annual": round(v * 1e5)}
    else:
        exp_text, exp_truth = "Negotiable", {"negotiable": True}
    notice_text, notice_days = rng.choice([("30 days", 30), ("1 Month", 30), ("2 Months", 60), ("15 Days", 15),
                                           ("Immediate Joiner", 0), ("60 days", 60), ("90 Days", 90)]) \
        if not fresher else ("Immediate Joiner", 0)
    return {"name": f"{first} {last}", "first": first, "father": f"{rng.choice(pool['fathers'])} {last}",
            "email": f"{first.lower()}.{last.lower()}{rng.randint(1, 98)}@{rng.choice(['gmail.com', 'yahoo.co.in', 'outlook.com'])}",
            "phone": f"{rng.choice('6789')}{rng.randint(100000000, 999999999)}",
            "ref_phone": f"{rng.choice('6789')}{rng.randint(100000000, 999999999)}",
            "dob": born, "gender": rng.choice(["Male", "Female"]),
            "pan": "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(3)) + "P" + last[0].upper()
                   + f"{rng.randint(1000, 9999)}" + rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ"),
            "aadhaar": f"{rng.randint(2, 9)}{rng.randint(100, 999)} {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
            "city": city, "state": state, "pin": pin, "street": f"Flat {rng.randint(1, 40)}, {rng.choice(['Shivaji Nagar', 'Gandhi Road', 'Station Road', 'MG Road', 'Ram Nagar'])}",
            "preferred": rng.choice([c for c, _, _ in POOLS[split]["cities"] if c != city]),
            "y10": y10, "y12": y10 + 2, "grad": grad, "degree": degree, "ug": ug, "level": grad_level,
            "university": rng.choice(["Savitribai Phule Pune University", "Rashtrasant Tukadoji Maharaj Nagpur University",
                                      "University of Mumbai", "Gujarat University", "Shivaji University",
                                      "Devi Ahilya Vishwavidyalaya"]),
            "jobs": jobs, "gaps": gaps, "languages": langs, "skills": rng.sample(POOLS[split]["skills"], 5),
            "ctc_text": ctc_text, "ctc_annual": ctc_annual, "exp_text": exp_text, "exp_truth": exp_truth,
            "notice_text": notice_text, "notice_days": notice_days, "fresher": fresher}


def _indian_commas(n: int, fmt: str) -> str:
    s = str(n)
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return fmt.format(",".join(groups + [tail]) if groups else tail)


def _when(point, style, rng=None):
    if point is None:
        return "Present" if style != "num" else "Till Date"
    y, m = point
    return {"word": f"{MON[m - 1]} {y}", "full": f"{MONTH[m - 1]} {y}", "num": f"{m:02d}/{y}",
            "apos": f"{MON[m - 1]}'{str(y)[2:]}"}[style]


def _dob(d: date, style: str) -> str:
    return {"slash": f"{d.day:02d}/{d.month:02d}/{d.year}", "dash": f"{d.day:02d}-{d.month:02d}-{d.year}",
            "dot": f"{d.day:02d}.{d.month:02d}.{d.year}", "words": f"{d.day} {MONTH[d.month - 1]} {d.year}",
            "ordinal": f"{d.day}{'th' if 11 <= d.day <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d.day % 10, 'th')} "
                       f"{MONTH[d.month - 1]}, {d.year}",
            "us": f"{MONTH[d.month - 1]} {d.day}, {d.year}", "mon": f"{d.day:02d}-{MON[d.month - 1]}-{d.year}"}[style]


# ============================================================================== the document model
# blocks: ("title", text) ("heading", text) ("line", text) ("bullet", text) ("table", rows) ("gap",)
# model: {"header": [lines], "body": [blocks], "left": [blocks] | None}   left = a sidebar (two-column templates)
def build(p: dict, template: str, rng: random.Random, split: str) -> dict:
    held = split == "heldout"
    dob_style = rng.choice(["slash", "dash", "dot", "words", "ordinal"] + (["us", "mon"] if held else []))
    dob_label = rng.choice(["Date of Birth", "DOB", "D.O.B."] + (["Birth Date", "Date Of Birth"] if held else []))
    style = rng.choice(["word", "full", "num"] + (["apos"] if held else []))
    contact = [f"Mobile: +91 {p['phone'][:5]} {p['phone'][5:]}", f"Email: {p['email']}"]
    personal_rows = [("Father's Name", p["father"]), (dob_label, _dob(p["dob"], dob_style)), ("Gender", p["gender"]),
                     ("Marital Status", rng.choice(["Single", "Married"])), ("Nationality", "Indian"),
                     ("Languages Known", ", ".join(p["languages"])), ("PAN No", p["pan"])]
    address = [f"{p['street']},", f"{p['city']}, {p['state']} - {p['pin']}"]
    edu_rows = [("Qualification", "Board / University", "Year of Passing", "Percentage"),
                (p["degree"], p["university"], str(p["grad"]), f"{rng.randint(55, 85)}%")]
    if p["level"] == "post_graduation":
        edu_rows.append((p["ug"], p["university"], str(p["grad"] - 2), f"{rng.randint(55, 85)}%"))
    edu_rows += [("HSC (12th)", "Maharashtra State Board" if rng.random() < .5 else "CBSE", str(p["y12"]),
                  f"{rng.randint(55, 90)}%"),
                 ("SSC (10th)", "Maharashtra State Board" if rng.random() < .5 else "CBSE", str(p["y10"]),
                  f"{rng.randint(60, 95)}%")]
    edu_lines = [f"{r[0]} - {r[1]} - {r[2]} ({r[3]})" for r in edu_rows[1:]]
    jobs = list(reversed(p["jobs"]))                    # latest first, as resumes are written
    offer = []
    if p["ctc_text"]:
        offer.append(("Current CTC", p["ctc_text"]))
    offer += [("Expected CTC", p["exp_text"]), ("Notice Period", p["notice_text"])]
    body: list = []
    left = None
    header: list[str] = []
    if template == "naukri_biodata":
        body += [("title", "RESUME"), ("line", p["name"].upper()), *[("line", c) for c in contact],
                 ("heading", "CAREER OBJECTIVE"),
                 ("line", "To work with an organisation where I can use my skills and grow along with it."),
                 ("heading", "EDUCATIONAL QUALIFICATION"), ("table", edu_rows)]
        if jobs:
            body.append(("heading", "WORK EXPERIENCE"))
            for j in jobs:
                body += [("line", f"Company: {j['employer']}"), ("line", f"Designation: {j['designation']}"),
                         ("line", f"Duration: {_when(j['start'], style)} to {_when(j['end'], style)}"),
                         ("bullet", "Handled customer queries and daily MIS reports."), ("gap",)]
        body += [("heading", "SKILLS"), ("line", ", ".join(p["skills"])),
                 ("heading", "PERSONAL DETAILS"), *[("line", f"{k}: {v}") for k, v in personal_rows],
                 ("line", "Address: " + address[0]), ("line", address[1]),
                 *[("line", f"{k}: {v}") for k, v in offer],
                 ("heading", "DECLARATION"),
                 ("line", "I hereby declare that the above information is true to the best of my knowledge."),
                 ("line", f"Place: {p['city']}"), ("line", "Date:"), ("line", f"({p['name']})")]
    elif template == "modern":
        header = [p["name"], f"{p['city']}, {p['state']} | +91-{p['phone']} | {p['email']}"]
        body += [("heading", "PROFESSIONAL SUMMARY"),
                 ("line", "Banking professional with experience in retail lending, sales and customer service.")]
        body.append(("heading", "WORK EXPERIENCE"))
        for j in jobs:
            body += [("line", f"{j['designation']} - {j['employer']}   {_when(j['start'], style)} - {_when(j['end'], style)}"),
                     ("bullet", "Sourced and processed retail loan applications within TAT."),
                     ("bullet", "Achieved monthly disbursement targets.")]
        body += [("heading", "EDUCATION"), *[("line", x) for x in edu_lines],
                 ("heading", "KEY SKILLS"), ("line", " | ".join(p["skills"])),
                 ("heading", "LANGUAGES"), ("line", ", ".join(p["languages"])),
                 ("heading", "OTHER DETAILS"), ("line", " | ".join(f"{k}: {v}" for k, v in offer)),
                 ("line", f"{dob_label}: {_dob(p['dob'], dob_style)}"),
                 ("line", f"Preferred Location: {p['preferred']}"),
                 ("heading", "REFERENCES"), ("line", f"Mr. S. K. Rao, Branch Head - Mobile: {p['ref_phone']}")]
    elif template == "two_column":
        left = [("heading", "CONTACT"), ("line", f"+91 {p['phone']}"), ("line", p["email"]),
                ("line", f"Location: {p['city']}, {p['state']}"),
                ("heading", "SKILLS"), *[("line", s) for s in p["skills"]],
                ("heading", "LANGUAGES"), *[("line", x) for x in p["languages"]],
                ("heading", "PERSONAL"), ("line", f"{dob_label}: {_dob(p['dob'], dob_style)}"),
                ("line", f"Gender: {p['gender']}"), ("line", f"Notice Period: {p['notice_text']}")]
        body += [("title", p["name"].upper()), ("heading", "PROFILE"),
                 ("line", "Result oriented professional in lending operations and sales."),
                 ("heading", "EXPERIENCE")]
        for j in jobs:
            body += [("line", f"{j['employer']}, {p['city']}"), ("line", j["designation"]),
                     ("line", f"{_when(j['start'], 'num')} - {_when(j['end'], 'num')}"),
                     ("bullet", "Managed a portfolio of retail customers.")]
        body += [("heading", "EDUCATION"), *[("line", x) for x in edu_lines]]
        if p["ctc_text"]:
            body += [("heading", "COMPENSATION"), ("line", f"Current CTC: {p['ctc_text']}"),
                     ("line", f"Expected CTC: {p['exp_text']}")]
        else:
            body += [("line", f"Expected CTC: {p['exp_text']}")]
    elif template == "table_cv":
        body += [("title", "CURRICULUM VITAE"), ("line", f"Name: {p['name']}"), *[("line", c) for c in contact],
                 ("heading", "PERSONAL DETAILS"),
                 ("table", [(k, v) for k, v in personal_rows] + [("Address", " ".join(address))])]
        if jobs:
            body += [("heading", "EMPLOYMENT HISTORY"),
                     ("table", [("Organisation", "Designation", "From", "To")] +
                      [(j["employer"], j["designation"], _when(j["start"], "word"), _when(j["end"], "word"))
                       for j in jobs])]
        body += [("heading", "ACADEMIC QUALIFICATIONS"), ("table", edu_rows),
                 ("heading", "OTHER DETAILS"), ("table", offer)]
    elif template == "fresher":
        body += [("title", "RESUME"), ("line", p["name"]), *[("line", c) for c in contact],
                 ("line", f"Address: {p['street']}, {p['city']} - {p['pin']}"),
                 ("heading", "CAREER OBJECTIVE"), ("line", "Seeking an entry level role in banking and finance."),
                 ("heading", "EDUCATION"), ("table", edu_rows)]
        if jobs:
            j = jobs[0]
            body += [("heading", "INTERNSHIP"),
                     ("line", f"Intern at {j['employer']} ({_when(j['start'], 'word')} - {_when(j['end'], 'word')})"),
                     ("bullet", "Assisted the branch team with KYC documentation.")]
        body += [("heading", "LANGUAGES KNOWN"), ("table", [("Language", "Read", "Write", "Speak")] +
                                                 [(x, "Yes", "Yes", "Yes") for x in p["languages"]]),
                 ("heading", "PERSONAL DETAILS"), ("line", f"{dob_label}: {_dob(p['dob'], dob_style)}"),
                 ("line", f"Father's Name: {p['father']}"), ("line", f"Aadhaar No: {p['aadhaar']}"),
                 ("line", f"Expected Salary: {p['exp_text']}"), ("line", "Availability: Immediate Joiner"),
                 ("heading", "DECLARATION"), ("line", "I hereby declare that the details above are correct."),
                 ("line", f"Place: {p['city']}")]
    else:  # banking_ops -- HELD-OUT ONLY
        body += [("line", p["name"].upper()), ("line", f"Present Address: {p['street']}, {p['city']}, {p['state']} "
                                                        f"PIN - {p['pin']}"),
                 ("line", f"Contact No.: {p['phone']}   E-mail ID: {p['email']}"),
                 ("heading", "PROFESSIONAL JOURNEY")]
        for j in jobs:
            body += [("line", f"{j['employer']} | {j['designation']} | {_when(j['start'], style)} - "
                               f"{_when(j['end'], style)}"),
                     ("bullet", "Responsible for sourcing, documentation and disbursement.")]
        body += [("heading", "ACADEMIC CREDENTIALS"), *[("line", x) for x in edu_lines],
                 ("heading", "PERSONAL PARTICULARS"), ("line", f"Birth Date: {_dob(p['dob'], dob_style)}"),
                 ("line", f"Languages: {', '.join(p['languages'])}"),
                 ("line", f"Last Drawn Salary: {p['ctc_text']}" if p["ctc_text"] else "Last Drawn Salary: NA"),
                 ("line", f"Expected Package: {p['exp_text']}"), ("line", f"Joining Time: {p['notice_text']}")]
    return {"header": header, "body": body, "left": left}


# ============================================================================== flatten (txt, doc, pdf)
def flat_lines(model: dict, *, table_sep: str = " | ") -> list[str]:
    out = list(model["header"])
    blocks = (model["left"] or []) + model["body"] if model["left"] else model["body"]
    for b in blocks:
        if b[0] == "table":
            out += [table_sep.join(r) for r in b[1]]
        elif b[0] == "bullet":
            out.append("• " + b[1])
        elif b[0] == "gap":
            out.append("")
        else:
            out.append(b[1])
    return out


# ============================================================================== .docx
_DOCX_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _w_p(text: str, bold=False) -> str:
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:p><w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _w_table(rows) -> str:
    out = ["<w:tbl>"]
    for r in rows:
        out.append("<w:tr>" + "".join(f"<w:tc>{_w_p(c)}</w:tc>" for c in r) + "</w:tr>")
    return "".join(out) + "</w:tbl>"


def _w_blocks(blocks, use_tables=True) -> str:
    out = []
    for b in blocks:
        if b[0] == "table":
            out.append(_w_table(b[1]) if use_tables else "".join(_w_p("   ".join(r)) for r in b[1]))
        elif b[0] == "gap":
            out.append("<w:p/>")
        elif b[0] == "bullet":
            out.append(_w_p("• " + b[1]))
        else:
            out.append(_w_p(b[1], bold=b[0] in ("heading", "title")))
    return "".join(out)


def docx(model: dict, variant: str) -> bytes:
    header_xml = None
    if variant == "docx_twocol" and model["left"]:
        cell_l = _w_blocks(model["left"])
        cell_r = _w_blocks(model["body"])
        body = "".join(_w_p(h) for h in model["header"]) + \
            f"<w:tbl><w:tr><w:tc>{cell_l}</w:tc><w:tc>{cell_r}</w:tc></w:tr></w:tbl>"
    elif variant == "docx_header":
        lines = model["header"] or [b[1] for b in model["body"][:3] if b[0] in ("line", "title")]
        header_xml = f'<w:hdr {_DOCX_NS}>' + "".join(_w_p(x) for x in lines) + "</w:hdr>"
        skip = 0 if model["header"] else 3
        rest = model["body"][skip:]
        body = _w_blocks((model["left"] or []) + rest, use_tables=True)
    else:
        body = "".join(_w_p(h) for h in model["header"]) + \
            _w_blocks((model["left"] or []) + model["body"], use_tables=variant != "docx")
    sect = '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/></w:sectPr>' if header_xml else ""
    doc = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_DOCX_NS}><w:body>{body}{sect}' \
          f'</w:body></w:document>'
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-'
          'types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType='
          '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          + ('<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
             'wordprocessingml.header+xml"/>' if header_xml else "") + '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
                '2006/relationships"><Relationship Id="rIdH" Type="http://schemas.openxmlformats.org/officeDocument/'
                '2006/relationships/header" Target="header1.xml"/></Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        if header_xml:
            z.writestr("word/header1.xml", header_xml)
            z.writestr("word/_rels/document.xml.rels", doc_rels)
    return buffer.getvalue()


# ============================================================================== .odt
def odt(model: dict) -> bytes:
    ns = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"')
    parts = [f"<text:p>{escape(h)}</text:p>" for h in model["header"]]
    for b in (model["left"] or []) + model["body"]:
        if b[0] == "table":
            rows = "".join("<table:table-row>" + "".join(f"<table:table-cell><text:p>{escape(c)}</text:p>"
                                                         "</table:table-cell>" for c in r) + "</table:table-row>"
                           for r in b[1])
            parts.append(f"<table:table>{rows}</table:table>")
        elif b[0] == "gap":
            parts.append("<text:p/>")
        elif b[0] == "heading":
            parts.append(f'<text:h text:outline-level="2">{escape(b[1])}</text:h>')
        else:
            parts.append(f"<text:p>{escape(('• ' if b[0] == 'bullet' else '') + b[1])}</text:p>")
    content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ns}><office:body><office:text>'
               + "".join(parts) + "</office:text></office:body></office:document-content>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content)
        z.writestr("META-INF/manifest.xml", '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest='
                   '"urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="/" '
                   'manifest:media-type="application/vnd.oasis.opendocument.text"/></manifest:manifest>')
    return buffer.getvalue()


# ============================================================================== .rtf
def _rtf_escape(text: str) -> str:
    out = []
    for ch in text:
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ord(ch) > 127:
            out.append(f"\\u{ord(ch) if ord(ch) < 32768 else ord(ch) - 65536}?")
        else:
            out.append(ch)
    return "".join(out)


def rtf(model: dict) -> bytes:
    parts = [r"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0 Calibri;}}{\*\generator Sample;}\f0\fs22 "]
    for h in model["header"]:
        parts.append(_rtf_escape(h) + r"\par ")
    for b in (model["left"] or []) + model["body"]:
        if b[0] == "table":
            for r in b[1]:
                cells = "".join(rf"\cellx{2400 * (i + 1)}" for i in range(len(r)))
                parts.append(r"\trowd" + cells + " " + "".join(r"\intbl " + _rtf_escape(c) + r"\cell " for c in r)
                             + r"\row ")
        elif b[0] == "gap":
            parts.append(r"\par ")
        elif b[0] in ("heading", "title"):
            parts.append(r"{\b " + _rtf_escape(b[1]) + r"}\par ")
        else:
            parts.append(_rtf_escape(("• " if b[0] == "bullet" else "") + b[1]) + r"\par ")
    parts.append("}")
    return "".join(parts).encode("ascii")


# ============================================================================== .html saved as .doc
def html_doc(model: dict) -> bytes:
    rows = [f"<p>{escape(h)}</p>" for h in model["header"]]
    for b in (model["left"] or []) + model["body"]:
        if b[0] == "table":
            rows.append("<table border=1>" + "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>"
                                                     for r in b[1]) + "</table>")
        elif b[0] == "heading":
            rows.append(f"<h3>{escape(b[1])}</h3>")
        elif b[0] == "bullet":
            rows.append(f"<ul><li>{escape(b[1])}</li></ul>")
        elif b[0] == "gap":
            rows.append("<br>")
        else:
            rows.append(f"<p>{escape(b[1])}</p>")
    page = ('<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">'
            '<head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"><title>Resume</title>'
            '<style>p{margin:0}</style></head><body>' + "".join(rows) + "</body></html>")
    return page.encode("utf-8")


# ============================================================================== .doc (Word 97-2003)
def doc97(lines: list[str]) -> bytes:
    """A minimal valid Word 97 binary: compound file (Root, WordDocument, 1Table), FIB with ccpText and the Clx
    piece table, the text stored 8-bit. Checked with antiword (an independent reader)."""
    text = ("\r".join(lines) + "\r").encode("cp1252", errors="replace")
    fc_text = 0x800
    word = bytearray(fc_text + len(text))
    struct.pack_into("<HHHHH", word, 0, 0xA5EC, 0x00C1, 0, 0x0409, 0)
    struct.pack_into("<H", word, 0x0A, 0x0200 | 0x0004)          # fWhichTblStm = 1Table; fComplex off
    struct.pack_into("<H", word, 0x0C, 0x00BF)                   # nFibBack
    struct.pack_into("<II", word, 0x18, fc_text, fc_text + len(text))   # fcMin, fcMac
    struct.pack_into("<H", word, 0x20, 14)                       # csw
    struct.pack_into("<H", word, 0x3E, 22)                       # cslw
    struct.pack_into("<I", word, 0x40, len(word))                # cbMac
    struct.pack_into("<i", word, 0x4C, len(text))                # ccpText
    struct.pack_into("<H", word, 0x98, 0x005D)                   # cbRgFcLcb (Word 97)
    clx = bytearray(b"\x02")
    plc = struct.pack("<II", 0, len(text)) + struct.pack("<HIH", 0, (fc_text * 2) | 0x40000000, 0)
    clx += struct.pack("<I", len(plc)) + plc
    table = bytearray(clx) + bytearray(max(0, 4096 - len(clx)))
    struct.pack_into("<II", word, 0x1A2, 0, len(clx))            # fcClx, lcbClx
    word[fc_text:] = text
    word += bytearray((-len(word)) % 512 + (4096 - len(word) if len(word) < 4096 else 0))
    return _cfb({"WordDocument": bytes(word), "1Table": bytes(table)})


def _cfb(streams: dict[str, bytes]) -> bytes:
    ss = 512
    names = list(streams)
    data_sectors: dict[str, list[int]] = {}
    sectors: list[bytes] = []
    for name in names:
        blob = streams[name] + b"\x00" * ((-len(streams[name])) % ss)
        start = len(sectors) + 2                         # sector 0 = FAT, 1 = directory
        data_sectors[name] = list(range(start, start + len(blob) // ss))
        sectors += [blob[i:i + ss] for i in range(0, len(blob), ss)]
    total = 2 + len(sectors)
    assert total <= ss // 4, "test file too large for one FAT sector"
    fat = [0xFFFFFFFF] * (ss // 4)
    fat[0] = 0xFFFFFFFD
    fat[1] = 0xFFFFFFFE
    for name, secs in data_sectors.items():
        for a, b in zip(secs, secs[1:]):
            fat[a] = b
        fat[secs[-1]] = 0xFFFFFFFE

    def entry(name, etype, start, size, child=0xFFFFFFFF, right=0xFFFFFFFF):
        raw = name.encode("utf-16-le") + b"\x00\x00"
        e = bytearray(128)
        e[:len(raw)] = raw
        struct.pack_into("<H", e, 64, len(raw))
        e[66], e[67] = etype, 1                         # type, black
        struct.pack_into("<III", e, 68, 0xFFFFFFFF, right, child)
        struct.pack_into("<II", e, 116, start, size)
        return bytes(e)

    directory = entry("Root Entry", 5, 0xFFFFFFFE, 0, child=1)
    for i, name in enumerate(names):
        directory += entry(name, 2, data_sectors[name][0], len(streams[name]),
                           right=(i + 2) if i + 1 < len(names) else 0xFFFFFFFF)
    directory += b"\x00" * (ss - len(directory))
    header = bytearray(512)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHH", header, 24, 0x003E, 0x0003, 0xFFFE, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<IIIIIIII", header, 44, 1, 1, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    difat = [0] + [0xFFFFFFFF] * 108
    struct.pack_into("<109I", header, 76, *difat)
    return bytes(header) + struct.pack(f"<{ss // 4}I", *fat) + directory + b"".join(sectors)


# ============================================================================== PDF / images
def pdf(model: dict, two_col: bool) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 46

    def write(x, yy, text, bold=False, size=10):
        page.insert_text((x, yy), text[:110], fontsize=size, fontname="hebo" if bold else "helv")

    def blocks_out(blocks, x, width_chars, y0):
        yy = y0
        for b in blocks:
            nonlocal page
            if yy > 815:
                page = doc.new_page(width=595, height=842)
                yy = 46
            if b[0] == "table":
                # AUTOFIT, as Word sizes a table: each column as wide as its longest cell (+ padding), then scaled
                # into the available width -- never a cell cut off or running into the next one
                cols = len(b[1][0])
                need = [max(pymupdf.get_text_length(r[k], fontname="helv", fontsize=10) for r in b[1]) + 14
                        for k in range(cols)]
                avail = width_chars * 5.0
                scale = min(1.0, avail / sum(need))
                size = 10 * scale if scale < 1 else 10
                widths = [w * scale for w in need]
                for r in b[1]:
                    xx = x
                    for k, cell in enumerate(r):
                        page.insert_text((xx, yy), cell, fontsize=max(6.5, size), fontname="helv")
                        xx += widths[k]
                    yy += 15
            elif b[0] == "gap":
                yy += 8
            elif b[0] in ("heading", "title"):
                yy += 6
                write(x, yy, b[1], bold=True, size=11.5 if b[0] == "heading" else 15)
                yy += 18
            else:
                write(x, yy, ("• " if b[0] == "bullet" else "") + b[1].replace("₹", "Rs."))
                yy += 14.5
        return yy

    for h in model["header"]:
        write(40, y, h, bold=h == model["header"][0], size=15 if h == model["header"][0] else 10)
        y += 20
    if two_col and model["left"]:
        blocks_out(model["left"], 30, 34, y)
        blocks_out(model["body"], 215, 70, y)
    else:
        blocks_out((model["left"] or []) + model["body"], 40, 100, y)
    return doc.tobytes()


def raster(pdf_bytes: bytes, rng: random.Random, kind: str) -> bytes:
    import pymupdf
    from PIL import Image

    from evals.documents.synthetic import degrade

    d = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pix = d[0].get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72), alpha=False)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buffer = io.BytesIO()
    if kind == "jpg_photo":
        im, q = degrade(im, rng.choice(["phone", "skew", "clean"]), rng)
        im.convert("RGB").save(buffer, "JPEG", quality=q)
    elif kind == "png_screen":
        im.save(buffer, "PNG")
    else:  # pdf_scan: the page image inside a PDF, no text layer
        im, q = degrade(im, "clean", rng)
        jb = io.BytesIO()
        im.convert("RGB").save(jb, "JPEG", quality=q)
        out = pymupdf.open()
        pg = out.new_page(width=595, height=842)
        pg.insert_image(pg.rect, stream=jb.getvalue())
        return out.tobytes()
    return buffer.getvalue()


def render(model: dict, fmt: str, rng: random.Random) -> tuple[bytes, str]:
    if fmt.startswith("docx"):
        return docx(model, fmt), ".docx"
    if fmt == "doc":
        return doc97(flat_lines(model, table_sep="\t")), ".doc"
    if fmt == "rtf":
        return rtf(model), ".rtf"
    if fmt == "odt":
        return odt(model), ".odt"
    if fmt == "txt":
        return "\r\n".join(flat_lines(model, table_sep="    ")).encode("utf-8"), ".txt"
    if fmt == "html_as_doc":
        return html_doc(model), ".doc"
    two = fmt == "pdf_twocol"
    base = pdf(model, two_col=two or (fmt in ("jpg_photo", "png_screen", "pdf_scan") and bool(model["left"])))
    if fmt in ("pdf", "pdf_twocol"):
        return base, ".pdf"
    return raster(base, rng, fmt), {"jpg_photo": ".jpg", "png_screen": ".png", "pdf_scan": ".pdf"}[fmt]


# ============================================================================== truth
def truth(p: dict, template: str = "") -> dict:
    jobs = [{"employer": j["employer"], "designation": j["designation"],
             "start": f"{j['start'][0]:04d}-{j['start'][1]:02d}",
             "end": "PRESENT" if j["end"] is None else f"{j['end'][0]:04d}-{j['end'][1]:02d}",
             "internship": j["internship"]} for j in p["jobs"]]
    full = [j for j in p["jobs"] if not j["internship"]]
    cur = next((j for j in full if j["end"] is None), full[-1] if full else None)
    return {"name": p["name"].upper(), "email": p["email"], "phone": p["phone"],
            "date_of_birth": p["dob"].isoformat(), "city": p["city"],
            # the state only where the resume PRINTS it (a fresher's address line here has city + PIN only)
            "state": None if template == "fresher" else p["state"],
            "highest_qualification": p["level"], "year_10th": p["y10"], "year_12th": p["y12"],
            "graduation_year": p["grad"] if p["level"] == "graduation" else None,
            "jobs": jobs, "current_employer": cur["employer"] if cur else None,
            "current_designation": cur["designation"] if cur else None,
            "gap_count": len(p["gaps"]), "current_ctc_annual": p["ctc_annual"],
            "expected_ctc": p["exp_truth"], "notice_days": p["notice_days"], "languages": sorted(p["languages"]),
            # never in the output, unmasked
            "_forbidden": [p["father"].split()[0], p["pan"], p["aadhaar"].replace(" ", ""), p["ref_phone"]],
            "_gender": p["gender"]}


def generate(split: str, per: int) -> Path:
    rng = random.Random({"dev": 1610_2026, "heldout": 6102_9999}[split])
    folder = OUT / split
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*"):
        old.unlink()
    labels: dict = {"_note": "SYNTHETIC resumes: made-up people; values are the truth by construction."}
    n = 0
    for _ in range(per):
        for template in POOLS[split]["templates"]:
            for fmt in FORMATS:
                if fmt in ("docx_twocol", "pdf_twocol") and template != "two_column" and rng.random() < 0.7:
                    continue
                p = person(rng, split, template)
                model = build(p, template, rng, split)
                data, ext = render(model, fmt, rng)
                n += 1
                name = f"{template}_{fmt}_{n}{ext}"
                (folder / name).write_bytes(data)
                labels[name] = {"template": template, "format": fmt, **truth(p, template)}
    (folder / "ground_truth.json").write_text(json.dumps(labels, indent=1), encoding="utf-8")
    return folder


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "heldout"], default="dev")
    ap.add_argument("--per", type=int, default=1)
    a = ap.parse_args()
    folder = generate(a.split, a.per)
    print(folder, len(json.loads((folder / "ground_truth.json").read_text(encoding="utf-8"))) - 1, "files")


if __name__ == "__main__":
    main()
