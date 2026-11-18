"""
SYNTHETIC ITR ACKNOWLEDGEMENTS AND SALARY SLIPS WITH GROUND TRUTH (2026-11-12) -- fake people, fake numbers, the
real documents' wording and layout.

    python -m evals.documents.financial_synthetic --split dev --per 4       # samples/synthetic_financial/dev/
    python -m evals.documents.financial_synthetic --split heldout --per 4   # samples/synthetic_financial/heldout/

WHY: the ITR / salary-slip extractors had no labelled set, so "more accurate" could not be measured. Real customer
documents carry no labels and must not be copied into a test set. Every value here is generated, so the label IS the
truth -- never copied from an extractor.

WHAT VARIES
  ITR-V        the current acknowledgement (rows 1-15) and the ITR-U (updated return) acknowledgement (rows 1-10)
  SALARY       stacked tables, earnings | deductions side by side, statutory FORM XIX wage slip
  QUALITY      scan (clean, slightly soft), photo (tilt, uneven light, JPEG), lowres (a small, soft scan)
DEV and HELDOUT use different seeds AND different name / employer pools.

Writes <split>/<file> (.pdf for scans, .jpg for photos) and <split>/ground_truth.json:
  {"file": ..., "kind": "ITR"|"SALARY", "layout": ..., "quality": ..., "fields": {field: value}}
Amounts are integers (rupees), dates ISO, names as printed.
"""

from __future__ import annotations

import argparse
import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "samples" / "synthetic_financial"
FONT = "C:/Windows/Fonts/arial.ttf"
FONT_B = "C:/Windows/Fonts/arialbd.ttf"
W, H = 1654, 2339                                         # A4 at 200 dpi

POOLS = {
    "dev": {
        "first": ["RAHUL", "PRIYA", "AMIT", "SNEHA", "VIKRAM", "ANJALI", "SURESH", "KAVITA", "ROHAN", "MEERA"],
        "last": ["SHARMA", "PATIL", "IYER", "DESHMUKH", "VERMA", "NAIR", "KULKARNI", "REDDY", "JOSHI", "GUPTA"],
        "employer": ["ASHVA TECHNOLOGIES PRIVATE LIMITED", "NIRMAN INFRA LIMITED", "SURYA FOODS PVT LTD",
                     "KAVERI TEXTILES LIMITED", "PRAGATI LOGISTICS PRIVATE LIMITED"],
        "city": [("PUNE", "411038"), ("NAGPUR", "440010"), ("CHENNAI", "600040"), ("JAIPUR", "302017")],
        "seed": 1101,
    },
    "heldout": {
        "first": ["KARAN", "POOJA", "NIKHIL", "DEEPA", "ARJUN", "LATA", "MANOJ", "RITU", "SAMEER", "USHA"],
        "last": ["MEHTA", "KADAM", "PILLAI", "BANERJEE", "SINGH", "MENON", "GAIKWAD", "RAO", "CHOUDHARY", "SHAH"],
        "employer": ["VRINDA SOFTWARE PRIVATE LIMITED", "SAHYADRI CEMENTS LIMITED", "GANGA AGRO PVT LTD",
                     "NEELKANTH MOTORS LIMITED", "TARANG ENERGY PRIVATE LIMITED"],
        "city": [("NASHIK", "422005"), ("KOCHI", "682020"), ("INDORE", "452010"), ("SURAT", "395007")],
        "seed": 2202,
    },
}
QUALITIES = ("scan", "photo", "lowres")


def inr(n: int) -> str:
    """12,34,567 -- Indian grouping."""
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + s


def pan(rng: random.Random) -> str:
    L = "ABCDEFGHJKLMNPRSTUVWXYZ"
    return "".join(rng.choice(L) for _ in range(3)) + "P" + rng.choice(L) + f"{rng.randint(1000, 9999)}" + rng.choice(L)


class Page:
    def __init__(self) -> None:
        self.img = Image.new("RGB", (W, H), "white")
        self.d = ImageDraw.Draw(self.img)
        self.f = {s: ImageFont.truetype(FONT, s) for s in (22, 24, 26, 30, 34, 38)}
        self.b = {s: ImageFont.truetype(FONT_B, s) for s in (22, 24, 26, 30, 34, 38)}

    def text(self, xy, s, size=26, bold=False, anchor="la"):
        self.d.text(xy, s, fill=(20, 20, 20), font=(self.b if bold else self.f)[size], anchor=anchor)

    def box(self, x0, y0, x1, y1):
        self.d.rectangle([x0, y0, x1, y1], outline=(60, 60, 60), width=2)


# ---------------------------------------------------------------- ITR-V
def itr(rng: random.Random, pool: dict, layout: str) -> tuple[Image.Image, dict]:
    name = f"{rng.choice(pool['first'])} {rng.choice(pool['last'])}"
    p = pan(rng)
    ay_start = rng.choice([2022, 2023, 2024, 2025])
    ay = f"{ay_start}-{str(ay_start + 1)[-2:]}"
    filed = date(ay_start, 7, 1) + timedelta(days=rng.randint(0, 120))
    ack = "".join(str(rng.randint(0, 9)) for _ in range(15))
    form = rng.choice(["ITR-1", "ITR-2", "ITR-3", "ITR-4"])
    city, pin = rng.choice(pool["city"])
    income = rng.randrange(300000, 4500000, 10)
    pg = Page()
    pg.text((W // 2, 90), "INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT", 38, True, "ma")
    pg.text((W - 120, 90), "Assessment Year", 26, False, "ra")
    pg.text((W - 120, 125), ay, 30, True, "ra")
    pg.text((W // 2, 150), "[Where the data of the Return of Income in Form ITR-1(SAHAJ), ITR-2, ITR-3, ITR-4(SUGAM)",
            22, False, "ma")
    pg.text((W // 2, 180), "filed and verified]", 22, False, "ma")
    pg.text((120, 240), f"Acknowledgement Number : {ack}", 26)
    pg.text((W - 120, 240), f"Date of filing : {filed.strftime('%d-%b-%Y')}", 26, False, "ra")
    rows_head = [("PAN", p), ("Name", name), ("Address", f"FLAT {rng.randint(1, 99)}, SHANTI NAGAR, {city}, {pin}"),
                 ("Status", "Individual"), ("Form Number", form), ("Filed u/s", "139(1)-On or before due date"),
                 ("e-Filing Acknowledgement Number", ack)]
    y = 300
    for cap, val in rows_head:
        pg.box(100, y, 700, y + 56)
        pg.box(700, y, W - 100, y + 56)
        pg.text((120, y + 14), cap, 26)
        pg.text((720, y + 14), val, 26)
        y += 56
    fields = {"acknowledgement_number": ack, "assessment_year": ay, "pan": p, "name": name, "form_number": form,
              "filing_date": filed.isoformat(), "total_income": income}
    if layout == "itr_v":
        net_tax = max(0, int(income * rng.uniform(0.0, 0.12)) // 10 * 10)
        interest = rng.choice([0, 0, rng.randint(100, 5000)])
        total = net_tax + interest
        paid = max(0, total + rng.choice([0, rng.randint(-20000, 20000)]))
        due = total - paid
        due = (abs(due) // 10 * 10) * (1 if due >= 0 else -1)
        paid = total - due
        rows = [("Current Year business loss, if any", 0), ("Total Income", income),
                ("Book Profit under MAT, where applicable", 0), ("Adjusted Total Income under AMT, where applicable", 0),
                ("Net tax payable", net_tax), ("Interest and Fee Payable", interest),
                ("Total tax, interest and Fee payable", total), ("Taxes Paid", paid),
                ("(+) Tax Payable /(-) Refundable (7-8)", due), ("Accreted Income as per section 115TD", 0),
                ("Additional Tax payable u/s 115TD", 0), ("Interest payable u/s 115TE", 0),
                ("Additional Tax and interest payable", 0), ("Tax and interest paid", 0),
                ("(+) Tax Payable /(-) Refundable (13-14)", 0)]
        fields.update(net_tax_payable=net_tax, total_tax_and_interest_payable=total, taxes_paid=paid,
                      tax_payable_or_refundable=due)
    else:  # ITR-U
        earlier = int(income * rng.uniform(0.6, 0.95)) // 10 * 10
        earlier_tax = int(earlier * 0.05) // 10 * 10
        extra = max(0, int((income - earlier) * rng.uniform(0.2, 0.35)) // 10 * 10)
        net_payable = extra
        paid = net_payable
        rows = [("Current Year business loss, if any", 0), ("Total Income as per Updated return", income),
                ("Total income as per earlier return", earlier),
                ("Book Profit under MAT, where applicable as per Updated Return", 0),
                ("Deemed Total Income under AMT, where applicable as per Updated Return", 0),
                ("Tax payable (+) / Refundable (-) as per earlier return", earlier_tax),
                ("Additional income-tax liability on updated income", extra), ("Net amount payable", net_payable),
                ("Tax paid u/s 140B", paid), ("Tax due (8 - 9)", net_payable - paid)]
        fields["form_number"] = form
        fields.update(net_tax_payable=extra, total_tax_and_interest_payable=net_payable, taxes_paid=paid,
                      tax_payable_or_refundable=net_payable - paid)
    y += 40
    pg.text((120, y), "Taxable Income and Tax details", 30, True)
    y += 50
    for i, (cap, val) in enumerate(rows, 1):
        pg.box(100, y, 1150, y + 52)
        pg.box(1150, y, 1240, y + 52)
        pg.box(1240, y, W - 100, y + 52)
        pg.text((120, y + 12), cap, 24 if len(cap) > 50 else 26)
        pg.text((1195, y + 12), str(i), 26, False, "ma")
        sval = inr(val)
        if val < 0:
            sval = "(-) " + inr(-val)
        pg.text((W - 120, y + 12), sval, 26, False, "ra")
        y += 52
    y += 40
    pg.text((120, y), f"Income Tax Return submitted electronically on {filed.strftime('%d-%b-%Y')} 11:24:05 from IP",
            24)
    pg.text((120, y + 34), f"address 10.12.34.56 and verified by {name} having PAN {p} on", 24)
    pg.text((120, y + 68), f"{filed.strftime('%d-%b-%Y')} using Electronic Verification Code generated through", 24)
    pg.text((120, y + 102), "Aadhaar OTP mode", 24)
    pg.text((W // 2, H - 120), "DO NOT SEND THIS ACKNOWLEDGEMENT TO CPC, BENGALURU", 26, True, "ma")
    return pg.img, fields


# ---------------------------------------------------------------- salary slip
def salary(rng: random.Random, pool: dict, layout: str) -> tuple[Image.Image, dict]:
    name = f"{rng.choice(pool['first'])} {rng.choice(pool['last'])}"
    employer = rng.choice(pool["employer"])
    city, pin = rng.choice(pool["city"])
    month = rng.choice(["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER",
                        "OCTOBER", "NOVEMBER", "DECEMBER"])
    year = rng.choice([2024, 2025, 2026])
    code = f"EMP{rng.randint(1000, 99999)}"
    desig = rng.choice(["SENIOR EXECUTIVE", "ACCOUNTS OFFICER", "SALES MANAGER", "SOFTWARE ENGINEER", "TECHNICIAN"])
    doj = date(rng.randint(2012, 2023), rng.randint(1, 12), rng.randint(1, 28))
    p = pan(rng)
    uan = str(rng.randint(100000000000, 999999999999))
    basic = rng.randrange(12000, 90000, 10)
    hra = basic * rng.choice([40, 50]) // 100
    conv = rng.choice([1600, 1920, 2400])
    special = rng.randrange(1000, 40000, 10)
    gross = basic + hra + conv + special
    pf = min(1800, basic * 12 // 100)
    ptax = 200
    tds = rng.choice([0, rng.randrange(500, 9000, 10)])
    ded = pf + ptax + tds
    net = gross - ded
    pg = Page()
    if layout == "form_xix":
        pg.text((W // 2, 100), "FORM XIX", 34, True, "ma")
        pg.text((W // 2, 150), "[See Rule 78(1)(b)]  WAGE SLIP", 26, False, "ma")
        pg.text((W // 2, 210), employer, 30, True, "ma")
        pg.text((W // 2, 252), f"PLOT {rng.randint(1, 80)}, MIDC AREA, {city} - {pin}", 24, False, "ma")
        lines = [f"Name of Workman :- {name}", f"Employee Code :- {code}", f"Designation :- {desig}",
                 f"Wage period : {month} {year}", f"Basic Wages : {inr(basic)}", f"HRA : {inr(hra)}",
                 f"Conveyance : {inr(conv)}", f"Other Allowance : {inr(special)}", f"Gross Wages : {inr(gross)}",
                 f"Deductions, if any : {inr(ded)}", f"Net paid amount : {inr(net)}"]
        y = 340
        for ln in lines:
            pg.text((160, y), ln, 30)
            y += 70
        fields = {"employer_name": employer, "employee_name": name, "employee_code": code, "designation": desig,
                  "pay_period": f"{month} {year}", "gross_earnings": gross, "total_deductions": ded,
                  "net_pay": net, "basic_salary": basic}
        return pg.img, fields
    pg.text((W // 2, 90), employer, 38, True, "ma")
    pg.text((W // 2, 145), f"{rng.randint(1, 9)}th Floor, Trade Centre, {city} - {pin}", 24, False, "ma")
    pg.text((W // 2, 200), f"Payslip for the month of {month} {year}", 30, True, "ma")
    head = [("Employee Name", name), ("Employee Code", code), ("Designation", desig),
            ("Date of Joining", doj.strftime("%d-%m-%Y")), ("PAN No.", p), ("UAN No.", uan)]
    y = 270
    for i in range(0, len(head), 2):
        for j, (cap, val) in enumerate(head[i:i + 2]):
            x = 100 + j * 727
            pg.box(x, y, x + 300, y + 56)
            pg.box(x + 300, y, x + 727, y + 56)
            pg.text((x + 16, y + 14), cap, 26)
            pg.text((x + 316, y + 14), val, 26)
        y += 56
    earn = [("Basic Salary", basic), ("House Rent Allowance", hra), ("Conveyance", conv), ("Special Allowance", special)]
    dedu = [("Provident Fund", pf), ("Professional Tax", ptax), ("Income Tax (TDS)", tds)]
    y += 40
    if layout == "side_by_side":
        pg.text((120, y), "Earnings", 30, True)
        pg.text((W - 120, y), "Amount", 30, True, "ra")
        pg.text((900, y), "Deductions", 30, True)
        pg.text((760, y), "Amount", 30, True, "ra")
        y += 54
        for k in range(max(len(earn), len(dedu))):
            if k < len(earn):
                pg.text((120, y), earn[k][0], 26)
                pg.text((760, y), inr(earn[k][1]) + ".00", 26, False, "ra")
            if k < len(dedu):
                pg.text((900, y), dedu[k][0], 26)
                pg.text((W - 120, y), inr(dedu[k][1]) + ".00", 26, False, "ra")
            y += 48
        pg.text((120, y + 10), "Total Earnings", 26, True)
        pg.text((760, y + 10), inr(gross) + ".00", 26, True, "ra")
        pg.text((900, y + 10), "Total Deductions", 26, True)
        pg.text((W - 120, y + 10), inr(ded) + ".00", 26, True, "ra")
        y += 90
    else:  # stacked
        for title, rows, total_cap, total in (("Earnings", earn, "Total Earnings", gross),
                                              ("Deductions", dedu, "Total Deductions", ded)):
            pg.text((120, y), title, 30, True)
            y += 50
            for cap, val in rows:
                pg.box(100, y, 1100, y + 50)
                pg.box(1100, y, W - 100, y + 50)
                pg.text((120, y + 12), cap, 26)
                pg.text((W - 120, y + 12), inr(val) + ".00", 26, False, "ra")
                y += 50
            pg.box(100, y, 1100, y + 50)
            pg.box(1100, y, W - 100, y + 50)
            pg.text((120, y + 12), total_cap, 26, True)
            pg.text((W - 120, y + 12), inr(total) + ".00", 26, True, "ra")
            y += 90
    pg.text((120, y), "Net Pay", 30, True)
    pg.text((W - 120, y), inr(net) + ".00", 30, True, "ra")
    pg.text((W // 2, y + 120), "This is a computer generated payslip and does not require a signature.", 22, False, "ma")
    fields = {"employer_name": employer, "employee_name": name, "employee_code": code, "designation": desig,
              "pay_period": f"{month} {year}", "date_of_joining": doj.strftime("%d-%m-%Y"), "pan_masked": p,
              "uan_number": uan, "gross_earnings": gross, "total_deductions": ded, "net_pay": net,
              "basic_salary": basic}
    return pg.img, fields


# ---------------------------------------------------------------- quality
def degrade(img: Image.Image, quality: str, rng: random.Random) -> Image.Image:
    if quality == "scan":
        out = img.convert("L").filter(ImageFilter.GaussianBlur(0.6)).convert("RGB")
        arr = np.asarray(out).astype(np.int16) + np.random.default_rng(rng.randint(0, 9999)).integers(-10, 10, out.size[::-1] + (3,))
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    if quality == "lowres":
        small = img.resize((W * 55 // 100, H * 55 // 100), Image.BILINEAR).filter(ImageFilter.GaussianBlur(0.5))
        return small.convert("L").convert("RGB")
    # photo: tilt, uneven light, a little blur, JPEG
    out = img.rotate(rng.uniform(-2.5, 2.5), expand=True, fillcolor=(235, 232, 225), resample=Image.BICUBIC)
    w, h = out.size
    grad = np.linspace(rng.uniform(0.78, 0.9), 1.0, w)[None, :, None] * np.linspace(1.0, rng.uniform(0.82, 0.95), h)[:, None, None]
    arr = (np.asarray(out).astype(np.float32) * grad).clip(0, 255).astype(np.uint8)
    out = Image.fromarray(arr).filter(ImageFilter.GaussianBlur(0.8)).resize((int(w * 0.75), int(h * 0.75)))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=rng.choice([55, 65, 75]))
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=list(POOLS))
    ap.add_argument("--per", type=int, default=4, help="documents per (layout, quality)")
    args = ap.parse_args()
    pool = POOLS[args.split]
    rng = random.Random(pool["seed"])
    out = OUT / args.split
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*"):
        old.unlink()
    truth = []
    plans = [("ITR", "itr_v", itr), ("ITR", "itr_u", itr), ("SALARY", "stacked", salary),
             ("SALARY", "side_by_side", salary), ("SALARY", "form_xix", salary)]
    n = 0
    for kind, layout, make in plans:
        for quality in QUALITIES:
            for _ in range(args.per):
                img, fields = make(rng, pool, layout)
                img = degrade(img, quality, rng)
                name = f"{kind.lower()}_{layout}_{quality}_{n:03d}" + (".jpg" if quality == "photo" else ".pdf")
                if quality == "photo":
                    img.save(out / name, "JPEG", quality=85)
                else:
                    img.save(out / name, "PDF", resolution=200 if quality == "scan" else 110)
                truth.append({"file": name, "kind": kind, "layout": layout, "quality": quality, "fields": fields})
                n += 1
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")
    print(f"{args.split}: {n} documents -> {out}")


if __name__ == "__main__":
    main()
