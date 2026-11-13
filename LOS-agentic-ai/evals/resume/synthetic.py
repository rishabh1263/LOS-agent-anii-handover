"""
SYNTHETIC RESUMES WITH GROUND TRUTH (2026-10-16) -- made-up people, real resume layouts.

    python -m evals.resume.synthetic --split dev        # samples/synthetic_resume/dev/
    python -m evals.resume.synthetic --split heldout    # samples/synthetic_resume/heldout/

layouts    experienced (summary, experience, education, skills) | fresher (objective, academic table, projects,
           personal details, declaration) | "CURRICULUM VITAE" with "Name:" and MM/YYYY dates
formats    .docx (Word XML) | native .pdf (text layer) | .jpg (the page photographed / scanned -> OCR)
scenarios  GENUINE, OVERLAP (two full-time jobs at once), FUTURE_DATE, EDU_ORDER (12th before 10th),
           NO_CONTACT, NAME_MISMATCH (HR expects another person) -- and NOT_RESUME documents (offer letter,
           marksheet, article) that must be refused
DEV and HELDOUT use different seeds and different name pools.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "samples" / "synthetic_resume"
FIRST = {"dev": ["RAHUL", "PRIYA", "AMIT", "SNEHA", "VIKAS", "POOJA", "SANJAY", "KAVITA", "ANIL", "MEENA"],
         "heldout": ["KIRAN", "SUNITA", "ARJUN", "DEEPA", "NITIN", "ASHA", "GANESH", "SWATI", "PRAKASH", "REKHA"]}
LAST = {"dev": ["SHARMA", "PATIL", "KULKARNI", "VERMA", "DESHMUKH", "GUPTA"],
        "heldout": ["JOSHI", "PAWAR", "MISHRA", "SHINDE", "CHAVAN", "SINGH"]}
COMPANIES = ["Infosys Ltd", "Tata Consultancy Services", "HDFC Bank", "Bajaj Finserv", "Wipro Ltd", "ICICI Bank",
             "Capgemini", "L&T Finance", "Axis Bank", "Tech Mahindra"]
TITLES = ["Software Engineer", "Credit Analyst", "Sales Officer", "Relationship Manager", "Data Analyst",
          "Operations Executive", "Collection Officer", "Branch Executive"]
DEGREES = [("B.Tech in Computer Science", "graduation"), ("B.Com", "graduation"), ("BBA", "graduation"),
           ("B.Sc in Mathematics", "graduation"), ("MBA (Finance)", "post_graduation"), ("MCA", "post_graduation")]
UNIS = ["Savitribai Phule Pune University", "University of Mumbai", "Shivaji University", "Nagpur University"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SCENARIOS = ["GENUINE", "GENUINE", "GENUINE", "OVERLAP", "FUTURE_DATE", "EDU_ORDER", "NO_CONTACT", "NAME_MISMATCH"]
EXPECTED = {"GENUINE": "VERIFIED", "OVERLAP": "MANUAL_REVIEW", "FUTURE_DATE": "MANUAL_REVIEW",
            "EDU_ORDER": "MANUAL_REVIEW", "NO_CONTACT": "MANUAL_REVIEW", "NAME_MISMATCH": "REJECTED",
            "NOT_RESUME": "UNSUPPORTED"}


def person(rng, split):
    first, last = rng.choice(FIRST[split]), rng.choice(LAST[split])
    y10 = rng.randint(2004, 2014)
    degree, level = rng.choice(DEGREES)
    grad = y10 + 2 + (3 if "B.Tech" not in degree else 4) + (2 if level == "post_graduation" else 0)
    jobs, start = [], (grad, rng.randint(6, 8))
    for _ in range(rng.randint(1, 3)):
        months = rng.randint(14, 40)
        end_m = start[0] * 12 + start[1] - 1 + months
        end = (end_m // 12, end_m % 12 + 1)
        if end[0] * 12 + end[1] >= 2026 * 12 + 9:
            end = None
        jobs.append({"company": rng.choice(COMPANIES), "title": rng.choice(TITLES), "start": start, "end": end})
        if end is None:
            break
        nxt = end[0] * 12 + end[1] - 1 + rng.randint(1, 3)
        start = (nxt // 12, nxt % 12 + 1)
    return {"name": f"{first} {last}", "email": f"{first.lower()}.{last.lower()}{rng.randint(1, 99)}@gmail.com",
            "phone": f"{rng.choice('6789')}{rng.randint(100000000, 999999999)}", "y10": y10, "y12": y10 + 2,
            "degree": degree, "level": level, "grad": grad, "university": rng.choice(UNIS), "jobs": jobs,
            "skills": rng.sample(["Excel", "SQL", "Python", "Tally", "Credit Appraisal", "Communication", "SAP",
                                  "Power BI", "Customer Service", "Java"], 5)}


def _when(point, style):
    if point is None:
        return "Present"
    y, m = point
    return f"{MONTHS[m - 1]} {y}" if style == "word" else f"{m:02d}/{y}"


def lines_for(p, layout, scenario, rng):
    jobs = [dict(j) for j in p["jobs"]]
    if scenario == "OVERLAP":
        a = jobs[0]
        s = a["start"]
        jobs.append({"company": rng.choice(COMPANIES), "title": rng.choice(TITLES),
                     "start": (s[0], min(12, s[1] + 2)), "end": (s[0] + 1, s[1])})
    if scenario == "FUTURE_DATE":
        jobs.append({"company": rng.choice(COMPANIES), "title": rng.choice(TITLES), "start": (2027, 3), "end": None})
    y10, y12 = (p["y12"] + 1, p["y12"]) if scenario == "EDU_ORDER" else (p["y10"], p["y12"])
    style = "num" if layout == "cv" else "word"
    contact = [] if scenario == "NO_CONTACT" else [f"Email: {p['email']}", f"Mobile: +91 {p['phone']}"]
    jobs_lines = []
    for j in jobs:
        jobs_lines += [f"{j['title']} - {j['company']}   {_when(j['start'], style)} - {_when(j['end'], style)}",
                       "Handled customer accounts and daily reporting."]
    edu = [f"{p['degree']}, {p['university']}, {p['grad']}", f"12th (HSC), Maharashtra State Board, {y12}",
           f"10th (SSC), Maharashtra State Board, {y10}"]
    if layout == "experienced":
        return [p["name"], " | ".join(contact) or "Pune, Maharashtra", "PROFESSIONAL SUMMARY",
                "Professional with experience in banking and finance operations.", "WORK EXPERIENCE", *jobs_lines,
                "EDUCATION", *edu, "SKILLS", ", ".join(p["skills"])]
    if layout == "fresher":
        return ["RESUME", p["name"], *contact, "CAREER OBJECTIVE",
                "To work in a challenging environment and grow with the organisation.", "ACADEMIC QUALIFICATIONS",
                *edu, "INTERNSHIP" if jobs_lines else "PROJECTS", *(jobs_lines or ["Loan management system"]),
                "PERSONAL DETAILS", f"Name: {p['name']}", "Languages: English, Hindi, Marathi", "DECLARATION",
                "I hereby declare that the above information is true to the best of my knowledge."]
    return ["CURRICULUM VITAE", f"Name: {p['name']}", *contact, "PROFESSIONAL EXPERIENCE", *jobs_lines,
            "EDUCATIONAL QUALIFICATION", *edu, "KEY SKILLS", " | ".join(p["skills"])]


NOT_RESUMES = {
    "offer_letter": ["OFFER LETTER", "Date: 12/03/2025", "Dear Rahul,", "We are pleased to offer you the position of",
                     "Credit Analyst at our Pune office. Your annual CTC will be Rs 6,00,000.",
                     "Please sign and return this letter of appointment.", "For HR Department"],
    "marksheet": ["MAHARASHTRA STATE BOARD OF SECONDARY AND HIGHER SECONDARY EDUCATION", "STATEMENT OF MARKS",
                  "Roll No 123456", "Subject  Marks Obtained  Maximum Marks", "English 78 100", "Mathematics 88 100",
                  "Grand Total 512 600", "Result PASS"],
    "article": ["The monsoon arrived early this year across the western coast.",
                "Farmers welcomed the rain, which is expected to help the kharif crop.",
                "Meteorologists said the season would remain normal through September.",
                "Reservoir levels in the region have risen by twelve percent this week."] * 2,
}


def docx_bytes(lines) -> bytes:
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(line)}</w:t></w:r></w:p>" for line in lines)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats'
           f'.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-'
          'types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType='
          '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buffer.getvalue()


def pdf_bytes(lines) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 50
    for line in lines:
        bold = line.isupper() and len(line.split()) <= 4
        page.insert_text((50, y), line[:95], fontsize=12 if bold else 10.5, fontname="hebo" if bold else "helv")
        y += 22 if bold else 17
    return doc.tobytes()


def jpg_bytes(lines, rng, quality) -> bytes:
    import pymupdf
    from PIL import Image

    from evals.documents.synthetic import degrade

    doc = pymupdf.open(stream=pdf_bytes(lines), filetype="pdf")
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72), alpha=False)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    im, q = degrade(im, quality, rng)
    buffer = io.BytesIO()
    im.convert("RGB").save(buffer, "JPEG", quality=q)
    return buffer.getvalue()


def generate(split: str, per: int) -> Path:
    rng = random.Random({"dev": 20261016, "heldout": 99991016}[split])
    folder = OUT / split
    folder.mkdir(parents=True, exist_ok=True)
    truth: dict = {"_note": "SYNTHETIC resumes: made-up people; values are the truth by construction."}
    n = 0
    for _ in range(per):
        for layout in ("experienced", "fresher", "cv"):
            for scenario in SCENARIOS:
                p = person(rng, split)
                fmt = ["docx", "pdf", "jpg"][n % 3]
                n += 1
                lines = lines_for(p, layout, scenario, rng)
                quality = rng.choice(["clean", "phone", "skew"]) if fmt == "jpg" else "native"
                data = docx_bytes(lines) if fmt == "docx" else pdf_bytes(lines) if fmt == "pdf" else \
                    jpg_bytes(lines, rng, quality)
                name = f"resume_{layout}_{scenario.lower()}_{n}.{fmt}"
                (folder / name).write_bytes(data)
                # someone else's resume = a DIFFERENT FIRST NAME. Same first name with another surname is often
                # the same person after marriage -- that is a review, never a rejection (see verify.py).
                other = None
                if scenario == "NAME_MISMATCH":
                    first = p["name"].split()[0]
                    other = f"{rng.choice([f for f in FIRST[split] if f != first])} {rng.choice(LAST[split])}"
                truth[name] = {"document_type": "RESUME", "layout": layout, "format": fmt, "quality": quality,
                               "scenario": scenario, "expected_verdict": EXPECTED[scenario],
                               "expected_input": {"name": other or p["name"]},
                               "fields": {"name": p["name"],
                                          "email": None if scenario == "NO_CONTACT" else p["email"],
                                          "phone": None if scenario == "NO_CONTACT" else p["phone"],
                                          "highest_qualification": p["level"],
                                          "year_10th": p["y12"] + 1 if scenario == "EDU_ORDER" else p["y10"],
                                          "year_12th": p["y12"], "graduation_year": p["grad"]
                                          if p["level"] == "graduation" else None}}
    for kind, lines in NOT_RESUMES.items():
        for fmt in ("docx", "pdf", "jpg"):
            n += 1
            data = docx_bytes(lines) if fmt == "docx" else pdf_bytes(lines) if fmt == "pdf" else \
                jpg_bytes(lines, rng, "clean")
            name = f"not_resume_{kind}_{n}.{fmt}"
            (folder / name).write_bytes(data)
            truth[name] = {"document_type": "NOT_RESUME", "kind": kind, "format": fmt, "quality": "clean",
                           "scenario": "NOT_RESUME", "expected_verdict": "UNSUPPORTED", "expected_input": {},
                           "fields": {}}
    (folder / "ground_truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")
    return folder


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "heldout"], default="dev")
    ap.add_argument("--per", type=int, default=2)
    a = ap.parse_args()
    folder = generate(a.split, a.per)
    print(folder, len(json.loads((folder / "ground_truth.json").read_text(encoding="utf-8"))) - 1, "files")


if __name__ == "__main__":
    main()
