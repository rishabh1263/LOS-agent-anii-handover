"""
SYNTHETIC 10th / 12th MARKSHEETS (and the wrong documents) for the marksheet API -- every name, number and board line
is invented; layouts follow the public look of each board's marksheet.

    python scripts/make_marksheet_samples.py      # -> samples/marksheet/{dev,heldout}/ + labels.json

dev and heldout use different seeds AND different name pools, so the held-out names were never seen while tuning.
"""

from __future__ import annotations

import io
import json
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "marksheet"
FONTS = [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\calibri.ttf",
         r"C:\Windows\Fonts\verdana.ttf", r"C:\Windows\Fonts\georgia.ttf"]
BOLD = [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\timesbd.ttf", r"C:\Windows\Fonts\calibrib.ttf"]

FIRST = {"dev": ["RAHUL", "PRIYA", "ANIKET", "SNEHA", "ROHIT", "KAVYA", "ARJUN", "NEHA", "VIKRAM", "POOJA", "AMIT",
                 "DIVYA", "SURESH", "ANJALI", "KARAN"],
         "heldout": ["SIDDHARTH", "MEERA", "TANMAY", "RITU", "HARSH", "ISHA", "NIKHIL", "SHREYA", "GAURAV", "AARTI",
                     "MANOJ", "LAKSHMI", "DEEPAK", "SWATI", "YASH"]}
LAST = {"dev": ["SHARMA", "PATIL", "NAIR", "GUPTA", "REDDY", "SINGH", "IYER", "JOSHI", "KUMAR", "DAS"],
        "heldout": ["DESHMUKH", "MENON", "AGARWAL", "CHOUDHARY", "PILLAI", "MISHRA", "KULKARNI", "BOSE", "YADAV",
                    "SAXENA"]}
SUBJECTS_10 = ["ENGLISH", "HINDI", "MATHEMATICS", "SCIENCE", "SOCIAL SCIENCE"]
SUBJECTS_12 = ["ENGLISH CORE", "PHYSICS", "CHEMISTRY", "MATHEMATICS", "COMPUTER SCIENCE"]

# board -> (header lines, 10th title, 12th title, name label style)
BOARDS = {
    "CBSE": (["CENTRAL BOARD OF SECONDARY EDUCATION"], "SECONDARY SCHOOL EXAMINATION", "SENIOR SCHOOL CERTIFICATE EXAMINATION", "certify"),
    "CISCE": (["COUNCIL FOR THE INDIAN SCHOOL CERTIFICATE EXAMINATIONS"], "INDIAN CERTIFICATE OF SECONDARY EDUCATION EXAMINATION",
              "INDIAN SCHOOL CERTIFICATE EXAMINATION", "name"),
    "MAHARASHTRA": (["MAHARASHTRA STATE BOARD OF SECONDARY AND HIGHER SECONDARY EDUCATION, PUNE"],
                    "SECONDARY SCHOOL CERTIFICATE EXAMINATION", "HIGHER SECONDARY CERTIFICATE EXAMINATION", "full_name"),
    "UP": (["BOARD OF HIGH SCHOOL AND INTERMEDIATE EDUCATION UTTAR PRADESH"], "HIGH SCHOOL EXAMINATION",
           "INTERMEDIATE EXAMINATION", "name_of_candidate"),
    "KARNATAKA": (["KARNATAKA SECONDARY EDUCATION EXAMINATION BOARD"], "S.S.L.C EXAMINATION", None, "candidate_name"),
    "KARNATAKA_PU": (["DEPARTMENT OF PRE-UNIVERSITY EDUCATION, KARNATAKA"], None, "II PRE-UNIVERSITY EXAMINATION", "candidate_name"),
    "TAMIL_NADU": (["GOVERNMENT EXAMINATIONS, TAMIL NADU"], "SECONDARY SCHOOL LEAVING CERTIFICATE", "HIGHER SECONDARY COURSE EXAMINATION", "name_of_candidate"),
    "BIHAR": (["BIHAR SCHOOL EXAMINATION BOARD, PATNA"], "ANNUAL SECONDARY SCHOOL EXAMINATION", "ANNUAL INTERMEDIATE EXAMINATION", "student_name"),
}


def font(size: int, rnd: random.Random, bold: bool = False):
    path = rnd.choice(BOLD if bold else FONTS)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _name(rnd, split) -> str:
    return f"{rnd.choice(FIRST[split])} {rnd.choice(FIRST[split]) if rnd.random() < 0.3 else ''} {rnd.choice(LAST[split])}".replace("  ", " ")


def marksheet(rnd: random.Random, split: str, board: str, cls: str) -> tuple[Image.Image, str]:
    headers, t10, t12, style = BOARDS[board]
    title = t10 if cls == "10TH" else t12
    W, H = 1240, 1754
    im = Image.new("RGB", (W, H), (250, 248, 240) if rnd.random() < 0.5 else (255, 255, 255))
    d = ImageDraw.Draw(im)
    y = 80
    for line in headers:
        f = font(34, rnd, bold=True)
        d.text(((W - d.textlength(line, font=f)) / 2, y), line, fill=(20, 20, 90), font=f)
        y += 60
    year = rnd.randint(2012, 2024)
    f = font(30, rnd, bold=True)
    line = f"{title} {year}"
    d.text(((W - d.textlength(line, font=f)) / 2, y), line, fill=(10, 10, 10), font=f)
    y += 55
    caption = rnd.choice(["STATEMENT OF MARKS", "MARKSHEET", "MARKS STATEMENT CUM CERTIFICATE", "MARK SHEET"])
    d.text(((W - d.textlength(caption, font=f)) / 2, y), caption, fill=(10, 10, 10), font=f)
    y += 90
    name = _name(rnd, split)
    mother = f"{rnd.choice(['SUNITA', 'KAMALA', 'REKHA', 'GEETA', 'USHA'])} {name.split()[-1]}"
    father = f"{rnd.choice(['RAMESH', 'SURESH', 'MAHESH', 'PRAKASH', 'VIJAY'])} {name.split()[-1]}"
    body = font(28, rnd)
    roll = f"{rnd.randint(1000000, 9999999)}"
    lines = []
    if style == "certify":
        lines = [f"Roll No. {roll}", f"This is to certify that {name}", f"Mother's Name {mother}",
                 f"Father's/Guardian's Name {father}", f"Date of Birth {rnd.randint(1, 28):02d}-{rnd.randint(1, 12):02d}-{year - 16}"]
    elif style == "name":
        lines = [f"Name: {name}", f"Unique ID: {roll}", f"Father's Name: {father}", f"Mother's Name: {mother}"]
    elif style == "full_name":
        surname, *rest = name.split()[::-1]
        lines = [f"SEAT NO {roll}", f"CANDIDATE'S FULL NAME (SURNAME FIRST) {name.split()[-1]} {' '.join(name.split()[:-1])}",
                 f"CANDIDATE'S MOTHER'S NAME {mother.split()[0]}"]
        name = f"{name.split()[-1]} {' '.join(name.split()[:-1])}"
    elif style == "name_of_candidate":
        lines = [f"Roll Number : {roll}", f"Name of Candidate : {name}", f"Father's Name : {father}",
                 f"Mother's Name : {mother}"]
    elif style == "candidate_name":
        lines = [f"Register No : {roll}", f"Candidate Name : {name}", f"Father Name : {father}"]
    else:
        lines = [f"Roll Code {roll[:4]} Roll No {roll[4:]}", f"Student Name {name}", f"Father's Name {father}"]
    if rnd.random() < 0.3 and style not in ("certify",):
        rnd.shuffle(lines)
    for line in lines:
        d.text((110, y), line, fill=(15, 15, 15), font=body)
        y += 52
    y += 30
    d.rectangle((100, y, W - 100, y + 60), outline=(40, 40, 40), width=2)
    d.text((120, y + 15), "SUBJECT", fill=(0, 0, 0), font=body)
    d.text((W - 520, y + 15), "MAXIMUM", fill=(0, 0, 0), font=body)
    d.text((W - 300, y + 15), "OBTAINED", fill=(0, 0, 0), font=body)
    y += 60
    total = 0
    for sub in (SUBJECTS_10 if cls == "10TH" else SUBJECTS_12):
        marks = rnd.randint(45, 99)
        total += marks
        d.rectangle((100, y, W - 100, y + 52), outline=(90, 90, 90), width=1)
        d.text((120, y + 12), sub, fill=(0, 0, 0), font=body)
        d.text((W - 480, y + 12), "100", fill=(0, 0, 0), font=body)
        d.text((W - 270, y + 12), str(marks), fill=(0, 0, 0), font=body)
        y += 52
    y += 30
    d.text((110, y), f"TOTAL {total} / 500        RESULT : PASS", fill=(0, 0, 0), font=body)
    y += 60
    d.text((110, y), rnd.choice(["DIVISION : FIRST", "GRADE : A1", "RESULT : PASSED"]), fill=(0, 0, 0), font=body)
    d.text((W - 450, H - 200), "Controller of Examinations", fill=(0, 0, 0), font=font(24, rnd))
    return im, name


def other(rnd: random.Random, kind: str) -> Image.Image:
    W, H = 1240, 900 if kind in ("PAN", "PASSBOOK") else 1600
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    b, f = font(36, rnd, bold=True), font(28, rnd)
    texts = {
        "PAN": ["INCOME TAX DEPARTMENT", "GOVT. OF INDIA", "Permanent Account Number Card", "ABCPS1234K",
                "Name", "RAVI SHARMA", "Father's Name", "MOHAN SHARMA", "Date of Birth 12/04/1990"],
        "PASSBOOK": ["STATE BANK OF INDIA", "PASSBOOK", "Account Holder : RAVI SHARMA", "Account No : 12345678901",
                     "IFSC : SBIN0001234", "Branch : ANDHERI EAST", "CIF No : 8812345678"],
        "SALARY_SLIP": ["ACME TECHNOLOGIES PVT LTD", "SALARY SLIP FOR MARCH 2024", "Employee Name : RAVI SHARMA",
                        "Basic 40000", "HRA 16000", "Deductions 4200", "NET PAY 51800"],
        "DEGREE": ["UNIVERSITY OF MUMBAI", "This is to certify that RAVI SHARMA", "has been admitted to the degree of",
                   "BACHELOR OF COMMERCE", "SEMESTER VI EXAMINATION", "GRADE A", "Vice Chancellor"],
        "RESUME": ["RAVI SHARMA", "CURRICULUM VITAE", "WORK EXPERIENCE", "Software Engineer, Acme 2019-2024",
                   "EDUCATION", "B.Tech Computer Science 2019", "SKILLS Python, SQL"],
        "INVOICE": ["TAX INVOICE", "GSTIN 27ABCDE1234F1Z5", "Invoice No INV-2024-118", "Item  Qty  Rate  Amount",
                    "Laptop 1 54000 54000", "CGST 9%  SGST 9%", "Total 63720"],
        "LETTER": ["Dear Sir,", "I am writing to request leave for two days", "due to a family function.",
                   "Kindly grant me leave from Monday to Tuesday.", "Thanking you,", "Yours faithfully"],
    }[kind]
    y = 100
    for i, line in enumerate(texts):
        d.text((100, y), line, fill=(10, 10, 10), font=b if i == 0 else f)
        y += 70
    return im


def photo(im: Image.Image, rnd: random.Random) -> Image.Image:
    """A phone photo of the page: turned a little, on a table, noise, JPEG."""
    angle = rnd.uniform(-4, 4)
    im = im.rotate(angle, expand=True, fillcolor=(rnd.randint(60, 120),) * 3, resample=Image.BICUBIC)
    if rnd.random() < 0.5:
        bg = Image.new("RGB", (int(im.width * 1.12), int(im.height * 1.08)), (rnd.randint(70, 130),) * 3)
        bg.paste(im, ((bg.width - im.width) // 2, (bg.height - im.height) // 2))
        im = bg
    scale = rnd.uniform(0.6, 1.0)
    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.BILINEAR)
    if rnd.random() < 0.4:
        im = im.filter(ImageFilter.GaussianBlur(rnd.uniform(0.3, 0.9)))
    return im


def save(im: Image.Image, path: Path, rnd: random.Random) -> None:
    if path.suffix == ".pdf":
        im.save(path, "PDF", resolution=150)
    else:
        im.save(path, "JPEG", quality=rnd.randint(60, 90))


def native_pdf(path: Path, lines: list[str]) -> None:
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((60, y), line, fontsize=12)
        y += 22
    doc.save(path)


def build(split: str, seed: int) -> dict:
    rnd = random.Random(seed)
    folder = OUT / split
    folder.mkdir(parents=True, exist_ok=True)
    labels: dict[str, dict] = {}
    n = 0
    for board, (_, t10, t12, _) in BOARDS.items():
        for cls, title in (("10TH", t10), ("12TH", t12)):
            if not title:
                continue
            for k in range(3):
                im, name = marksheet(rnd, split, board, cls)
                kind = rnd.choice(["photo", "photo", "scan", "pdf"])
                if kind == "photo":
                    im = photo(im, rnd)
                ext = ".pdf" if kind == "pdf" else ".jpg"
                file = f"ms_{board.lower()}_{cls.lower()}_{k}{ext}"
                save(im, folder / file, rnd)
                labels[file] = {"status": "VERIFIED", "class": cls, "name": name, "board": board}
                n += 1
    for kind in ("PAN", "PASSBOOK", "SALARY_SLIP", "DEGREE", "RESUME", "INVOICE", "LETTER"):
        for k in range(2):
            im = other(rnd, kind)
            if k:
                im = photo(im, rnd)
            file = f"other_{kind.lower()}_{k}.jpg"
            save(im, folder / file, rnd)
            labels[file] = {"status": "NOT_MARKSHEET", "detected_as": None if kind == "LETTER" else kind}
    Image.new("RGB", (1240, 1754), (255, 255, 255)).save(folder / "blank_white.jpg", "JPEG")
    labels["blank_white.jpg"] = {"status": "BLANK"}
    noisy = Image.effect_noise((1240, 1754), 4).convert("RGB").point(lambda v: 235 + v // 25)
    noisy.save(folder / "blank_paper.jpg", "JPEG")
    labels["blank_paper.jpg"] = {"status": "BLANK"}
    im, _ = marksheet(rnd, split, "CBSE", "12TH")
    im.filter(ImageFilter.GaussianBlur(9)).save(folder / "blurry_marksheet.jpg", "JPEG")
    labels["blurry_marksheet.jpg"] = {"status": "UNREADABLE"}
    scene = Image.new("RGB", (1200, 900), (90, 140, 200))
    ImageDraw.Draw(scene).ellipse((300, 200, 900, 700), fill=(240, 200, 60))
    scene.save(folder / "photo_no_text.jpg", "JPEG")
    labels["photo_no_text.jpg"] = {"status": "BLANK"}
    name = _name(rnd, split)
    native_pdf(folder / "digital_cbse_12th.pdf", [
        "CENTRAL BOARD OF SECONDARY EDUCATION", "SENIOR SCHOOL CERTIFICATE EXAMINATION 2022", "MARKS STATEMENT",
        f"This is to certify that {name}", "Roll No. 2234567", "Mother's Name GEETA DEVI",
        "SUBJECT MAXIMUM OBTAINED", "ENGLISH CORE 100 88", "PHYSICS 100 79", "TOTAL 412 RESULT PASS"])
    labels["digital_cbse_12th.pdf"] = {"status": "VERIFIED", "class": "12TH", "name": name, "board": "CBSE"}
    (folder / "labels.json").write_text(json.dumps(labels, indent=1), encoding="utf-8")
    return labels


def main() -> int:
    for split, seed in (("dev", 11), ("heldout", 97)):
        labels = build(split, seed)
        print(f"{split}: {len(labels)} files -> {OUT / split}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
