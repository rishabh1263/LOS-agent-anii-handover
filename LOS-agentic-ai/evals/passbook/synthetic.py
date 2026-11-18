"""
SYNTHETIC BANK PASSBOOK FRONT PAGES WITH GROUND TRUTH (2026-10-15).

    python -m evals.passbook.synthetic --split dev --per 3        # samples/synthetic_passbook/dev/
    python -m evals.passbook.synthetic --split heldout --per 3    # samples/synthetic_passbook/heldout/

WHY SYNTHETIC: there are no consented real passbooks, and real ones from the internet carry real people's account
numbers. Every holder name and account number here is generated; the label IS the truth. Bank, branch and IFSC are
REAL public reference data (app/data/ifsc/ifsc.sqlite), so the IFSC master checks are exercised for real.

WHAT VARIES
  bank      SBI, HDFC, ICICI, Bank of Baroda, PNB, Canara, Union Bank, Axis -- each with its own label wording, label
            order, bilingual (Hindi) captions for the public-sector banks, and account-number length
  scenario  GENUINE, plus the negatives the verifier must catch:
              MASKED          account number printed masked (XXXXXXX1234)
              BRANCH_MISMATCH the IFSC of another branch of the same bank
              IFSC_UNKNOWN    a well-formed IFSC that is in no master
              BANK_CONFLICT   the IFSC of a different bank than the one printed
              NO_ACCOUNT      the account number line missing
  quality   clean, phone, skew, low_light, jpeg, small (evals/documents/synthetic.degrade)
DEV and HELDOUT use different seeds AND different name pools.

ground_truth.json per file: document_type PASSBOOK, bank code, scenario, quality, expected verdict (and the verdicts
also acceptable for a degraded image), and the fields as printed / as the master resolves them.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from evals.documents.synthetic import FIRST, FONT_DEVA, LAYOUT, SURNAME, MALE_FATHERS, degrade

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "samples" / "synthetic_passbook"
IFSC_DB = ROOT / "app" / "data" / "ifsc" / "ifsc.sqlite"
W, H = 1400, 980                                   # a passbook front page photographed / scanned at ~200 dpi
FONTS = {"sans": "C:/Windows/Fonts/arial.ttf", "sans_bold": "C:/Windows/Fonts/arialbd.ttf",
         "serif": "C:/Windows/Fonts/times.ttf", "serif_bold": "C:/Windows/Fonts/timesbd.ttf",
         "mono": "C:/Windows/Fonts/cour.ttf", "mono_bold": "C:/Windows/Fonts/courbd.ttf"}

# code: (printed name, Hindi name or None, account length, label set, body font, accent colour)
BANKS = {
    "SBIN": ("STATE BANK OF INDIA", "भारतीय स्टेट बैंक", 11, "psb", "mono", (16, 82, 160)),
    "HDFC": ("HDFC BANK", None, 14, "pvt", "sans", (0, 64, 128)),
    "ICIC": ("ICICI Bank", None, 12, "pvt2", "sans", (174, 40, 46)),
    "BARB": ("BANK OF BARODA", "बैंक ऑफ़ बड़ौदा", 14, "psb", "serif", (240, 103, 34)),
    "PUNB": ("PUNJAB NATIONAL BANK", "पंजाब नैशनल बैंक", 16, "psb2", "mono", (162, 26, 48)),
    "CNRB": ("CANARA BANK", "केनरा बैंक", 13, "psb2", "serif", (0, 110, 180)),
    "UBIN": ("UNION BANK OF INDIA", "यूनियन बैंक ऑफ़ इंडिया", 15, "psb", "sans", (200, 30, 40)),
    "UTIB": ("AXIS BANK", None, 15, "pvt", "sans", (151, 20, 77)),
}
# label wording per family: (account, holder, ifsc, branch, customer id, type, opened)
LABELS = {
    "psb": ("Account No.", "Name", "IFSC", "Branch", "CIF No.", "Account Type", "Date of Opening"),
    "psb2": ("A/c No.", "Customer Name", "IFS Code", "Branch Name", "Customer ID", "Type of A/c", "Opened On"),
    "pvt": ("Account Number", "Account Holder", "IFSC Code", "Branch", "Customer ID", "Product", "A/c Open Date"),
    "pvt2": ("Account No", "Name of Account Holder", "IFSC", "Branch Name", "Customer ID", "Account Type", "Open Date"),
}
HINDI = {"Account No.": "खाता संख्या", "A/c No.": "खाता सं.", "Name": "नाम", "Customer Name": "ग्राहक का नाम",
         "IFSC": "आईएफएससी", "IFS Code": "आईएफएस कोड", "Branch": "शाखा", "Branch Name": "शाखा का नाम",
         "CIF No.": "सीआईएफ सं.", "Customer ID": "ग्राहक आईडी", "Account Type": "खाते का प्रकार",
         "Type of A/c": "खाते का प्रकार", "Date of Opening": "खोलने की तिथि", "Opened On": "खोलने की तिथि"}
SCENARIOS = ["GENUINE", "GENUINE", "GENUINE", "MASKED", "BRANCH_MISMATCH", "IFSC_UNKNOWN", "BANK_CONFLICT",
             "NO_ACCOUNT"]
QUALITIES = ["clean", "phone", "skew", "low_light", "jpeg", "small"]
EXPECTED = {"GENUINE": "VERIFIED", "MASKED": "MANUAL_REVIEW", "BRANCH_MISMATCH": "MANUAL_REVIEW",
            "IFSC_UNKNOWN": "MANUAL_REVIEW", "BANK_CONFLICT": "REJECTED", "NO_ACCOUNT": "MANUAL_REVIEW"}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONTS[kind], size, layout_engine=LAYOUT)


def deva(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_DEVA, size, layout_engine=LAYOUT)


def branches(db: sqlite3.Connection, code: str, rng: random.Random, n: int = 2) -> list[tuple]:
    """n real branches of bank `code` with a usable, short-enough branch name."""
    rows = db.execute("SELECT ifsc, bank, branch, city, state, address FROM branch WHERE bank_code = ? "
                      "AND length(branch) BETWEEN 4 AND 30 AND branch NOT LIKE '%RTGS%' AND branch NOT LIKE '%HO%' "
                      "ORDER BY ifsc LIMIT 4000", (code,)).fetchall()
    return rng.sample(rows, n)


def unknown_ifsc(db: sqlite3.Connection, code: str, rng: random.Random) -> str:
    while True:
        candidate = f"{code}0{rng.randrange(900000, 999999)}"
        if db.execute("SELECT 1 FROM branch WHERE ifsc = ?", (candidate,)).fetchone() is None:
            return candidate


def make(rng: random.Random, db: sqlite3.Connection, split: str, code: str, scenario: str):
    printed, hindi, length, family, body, accent = BANKS[code]
    labels = LABELS[family]
    first, _ = rng.choice(FIRST[split])
    surname, _ = rng.choice(SURNAME[split])
    father, _ = rng.choice(MALE_FATHERS[split])
    holder = f"{first} {father if rng.random() < 0.3 else ''} {surname}".replace("  ", " ").strip()
    account = str(rng.randrange(1, 10)) + "".join(str(rng.randrange(10)) for _ in range(length - 1))
    home, other = branches(db, code, rng, 2)
    ifsc, branch_name = home[0], home[2]
    if scenario == "BRANCH_MISMATCH":
        ifsc = other[0]                                   # another branch's IFSC under this branch's name
    elif scenario == "IFSC_UNKNOWN":
        ifsc = unknown_ifsc(db, code, rng)
    elif scenario == "BANK_CONFLICT":
        foreign = rng.choice([c for c in BANKS if c != code])
        ifsc = branches(db, foreign, rng, 1)[0][0]
    opened = date(2005, 1, 1) + timedelta(days=rng.randrange(0, 7000))

    im = Image.new("RGB", (W, H), (250, 249, 244))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 150), fill=accent)
    d.text((60, 30), printed, font=font("sans_bold", 54), fill=(255, 255, 255))
    if hindi:
        d.text((60, 96), hindi, font=deva(34), fill=(255, 255, 255))
    d.text((W - 330, 50), "PASSBOOK", font=font("sans_bold", 46), fill=(255, 255, 255))
    d.rectangle((W - 300, 200, W - 70, 470), outline=(120, 120, 120), width=3)
    d.text((W - 245, 320), "PHOTO", font=font("sans", 30), fill=(170, 170, 170))

    acct_text = None if scenario == "NO_ACCOUNT" else ("X" * (length - 4) + account[-4:] if scenario == "MASKED"
                                                       else account)
    rows = [(labels[3], branch_name), (labels[2], ifsc), (labels[4], str(rng.randrange(10**10, 10**11))),
            (labels[0], acct_text), (labels[1], f"{'MR. ' if rng.random() < 0.4 else ''}{holder}"),
            (labels[5], rng.choice(["SAVINGS", "SB-GENERAL", "SAVINGS BANK", "SALARY ACCOUNT"])),
            (labels[6], opened.strftime("%d/%m/%Y")),
            ("Address", f"{rng.randrange(1, 300)}, {home[3].title()}, {home[4].title()}"),
            ("MICR", "".join(str(rng.randrange(10)) for _ in range(9)))]
    if family == "pvt2":
        rows.insert(0, rows.pop(4))                      # ICICI-style: the holder first
    y = 200
    label_font, value_font = font(body, 34), font(body + "_bold", 36)
    for label, value in rows:
        if value is None:
            continue
        d.text((60, y), label, font=label_font, fill=(40, 40, 40))
        if hindi and label in HINDI:
            d.text((60, y + 40), HINDI[label], font=deva(24), fill=(90, 90, 90))
        d.text((430, y), ":", font=label_font, fill=(40, 40, 40))
        d.text((460, y), str(value), font=value_font, fill=(10, 10, 10))
        y += 78 if hindi else 66
    d.text((60, H - 70), "Authorised Signatory", font=font("sans", 26), fill=(90, 90, 90))
    d.line((60, H - 80, 380, H - 80), fill=(90, 90, 90), width=2)

    master_bank = db.execute("SELECT name FROM bank WHERE code = ?", (code,)).fetchone()[0]
    truth = {"bank_name": master_bank, "account_holder_name": holder,
             "account_number": account if scenario in ("GENUINE", "BRANCH_MISMATCH", "IFSC_UNKNOWN",
                                                       "BANK_CONFLICT") else None,
             "account_number_status": {"MASKED": "MASKED", "NO_ACCOUNT": "MISSING"}.get(scenario, "PRESENT"),
             "ifsc_code": ifsc, "branch_name": branch_name}
    return im, truth


def generate(split: str, per: int) -> Path:
    rng = random.Random({"dev": 20261015, "heldout": 99991015}[split] + 7)
    db = sqlite3.connect(f"file:{IFSC_DB}?mode=ro", uri=True)
    folder = OUT / split
    folder.mkdir(parents=True, exist_ok=True)
    truth: dict = {"_note": "SYNTHETIC passbooks: generated holders and account numbers; real public IFSC data."}
    for code in BANKS:
        for i in range(per):
            for scenario_i, scenario in enumerate(SCENARIOS):
                # GENUINE pages (3 per bank per round) cycle through all six qualities over two rounds; the
                # negatives are clean in round 0 (the check itself is tested) and degraded after
                quality = (QUALITIES[(i * 3 + scenario_i) % len(QUALITIES)] if scenario == "GENUINE"
                           else QUALITIES[(i + scenario_i) % len(QUALITIES)] if i else "clean")
                im, fields = make(rng, db, split, code, scenario)
                im, jpeg_q = degrade(im, quality, rng)
                name = f"passbook_{code}_{scenario.lower()}_{quality}_{i}_{scenario_i}.jpg"
                im.convert("RGB").save(folder / name, "JPEG", quality=jpeg_q)
                expected = EXPECTED[scenario]
                acceptable = [expected] + (["MANUAL_REVIEW", "UNREADABLE"] if quality in ("low_light", "small",
                                                                                         "jpeg") else [])
                truth[name] = {"document_type": "PASSBOOK", "bank": code, "scenario": scenario, "quality": quality,
                               "expected_verdict": expected, "acceptable_verdicts": sorted(set(acceptable)),
                               "fields": fields}
    (folder / "ground_truth.json").write_text(json.dumps(truth, indent=1, ensure_ascii=False), encoding="utf-8")
    return folder


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "heldout"], default="dev")
    ap.add_argument("--per", type=int, default=3)
    a = ap.parse_args()
    folder = generate(a.split, a.per)
    print(folder, len(json.loads((folder / "ground_truth.json").read_text(encoding="utf-8"))) - 1, "passbooks")


if __name__ == "__main__":
    main()
