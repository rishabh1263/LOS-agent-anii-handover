"""
SYNTHETIC IDENTITY DOCUMENTS WITH GROUND TRUTH (2026-10-15) -- fake people, fake numbers, real layouts' wording.

    python -m evals.documents.synthetic --split dev --per 2          # samples/synthetic/dev/
    python -m evals.documents.synthetic --split heldout --per 2      # samples/synthetic/heldout/

WHY SYNTHETIC: accuracy needs many labelled documents; real ID cards of real people must not be pulled from the
internet (personal data, often leaked). Every value here is generated, so the label IS the truth -- never copied from
an extractor. Aadhaar is out of scope (owner, 2026-10-14) and never generated.

WHAT VARIES: document type (PAN, VOTER_ID, DRIVING_LICENCE, PASSPORT), language of the labels and regional fields
(en / hi / mr -- Devanagari rendered with real shaping via raqm + Nirmala), and image quality (clean, phone photo,
skew, low light, heavy JPEG, small). DEV and HELDOUT use different seeds AND different name pools: no person, number
or name appears in both. Layouts are shared (the template is what a card looks like, not who is on it).

Writes <split>/<file>.png and <split>/ground_truth.json in the samples/ground_truth.json format (fields asserted are
the ones printed and legible; values: dates ISO, names as printed).
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "samples" / "synthetic"
FONT_LATIN = "C:/Windows/Fonts/arial.ttf"
FONT_LATIN_BOLD = "C:/Windows/Fonts/arialbd.ttf"
FONT_DEVA = "C:/Windows/Fonts/Nirmala.ttc"
LAYOUT = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
W, H = 1012, 638                                   # a CR80 card at 300 dpi

# (latin, devanagari) -- two DISJOINT pools so dev and heldout never share a person
FIRST = {
    "dev": [("RAHUL", "राहुल"), ("PRIYA", "प्रिया"), ("AMIT", "अमित"), ("SNEHA", "स्नेहा"), ("VIKAS", "विकास"),
            ("POOJA", "पूजा"), ("SANJAY", "संजय"), ("KAVITA", "कविता"), ("ANIL", "अनिल"), ("MEENA", "मीना"),
            ("RAJESH", "राजेश"), ("ANJALI", "अंजली"), ("SURESH", "सुरेश"), ("NEHA", "नेहा"), ("MAHESH", "महेश")],
    "heldout": [("KIRAN", "किरण"), ("SUNITA", "सुनीता"), ("ARJUN", "अर्जुन"), ("DEEPA", "दीपा"), ("NITIN", "नितीन"),
                ("ASHA", "आशा"), ("GANESH", "गणेश"), ("SWATI", "स्वाती"), ("PRAKASH", "प्रकाश"), ("REKHA", "रेखा"),
                ("YOGESH", "योगेश"), ("SHWETA", "श्वेता"), ("DINESH", "दिनेश"), ("JYOTI", "ज्योती"), ("SACHIN", "सचिन")],
}
FEMALE = {"PRIYA", "SNEHA", "POOJA", "KAVITA", "MEENA", "ANJALI", "NEHA", "SUNITA", "DEEPA", "ASHA", "SWATI", "REKHA",
          "SHWETA", "JYOTI"}
SURNAME = {
    "dev": [("SHARMA", "शर्मा"), ("PATIL", "पाटील"), ("KULKARNI", "कुलकर्णी"), ("VERMA", "वर्मा"), ("DESHMUKH", "देशमुख"),
            ("GUPTA", "गुप्ता"), ("JADHAV", "जाधव"), ("YADAV", "यादव")],
    "heldout": [("JOSHI", "जोशी"), ("PAWAR", "पवार"), ("MISHRA", "मिश्रा"), ("SHINDE", "शिंदे"), ("CHAVAN", "चव्हाण"),
                ("SINGH", "सिंह"), ("GAIKWAD", "गायकवाड"), ("TIWARI", "तिवारी")],
}
MALE_FATHERS = {"dev": [("RAMESH", "रमेश"), ("VIJAY", "विजय"), ("DILIP", "दिलीप"), ("ASHOK", "अशोक")],
                "heldout": [("MOHAN", "मोहन"), ("SUDHIR", "सुधीर"), ("ARUN", "अरुण"), ("BHARAT", "भरत")]}
CITIES = [("PUNE", "पुणे", "411001", "MH", "12"), ("MUMBAI", "मुंबई", "400001", "MH", "01"),
          ("NAGPUR", "नागपूर", "440001", "MH", "31"), ("NASHIK", "नाशिक", "422001", "MH", "15")]
LANGS = {"PAN": ["en", "hi"], "VOTER_ID": ["en", "hi", "mr"], "DRIVING_LICENCE": ["en", "hi", "mr"],
         "PASSPORT": ["en", "hi"]}
QUALITIES = ["clean", "phone", "skew", "low_light", "jpeg", "small"]

LABELS = {
    "hi": {"name": "नाम", "father": "पिता का नाम", "husband": "पति का नाम", "dob": "जन्म तिथि", "gender": "लिंग",
           "male": "पुरुष", "female": "महिला", "eci": "भारत निर्वाचन आयोग", "elector": "निर्वाचक का नाम",
           "address": "पता", "issue": "जारी करने की तिथि", "valid": "वैधता"},
    "mr": {"name": "नाव", "father": "वडिलांचे नाव", "husband": "पतीचे नाव", "dob": "जन्म तारीख", "gender": "लिंग",
           "male": "पुरुष", "female": "स्त्री", "eci": "भारत निवडणूक आयोग", "elector": "मतदाराचे नाव",
           "address": "पत्ता", "issue": "जारी केल्याची तारीख", "valid": "वैधता"},
}


def font(size: int, deva: bool = False, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_DEVA if deva else (FONT_LATIN_BOLD if bold else FONT_LATIN)
    return ImageFont.truetype(path, size, layout_engine=LAYOUT)


def dmy(d: date, sep: str = "/") -> str:
    return d.strftime(f"%d{sep}%m{sep}%Y")


class Card:
    def __init__(self, bg=(242, 238, 228)):
        self.im = Image.new("RGB", (W, H), bg)
        self.d = ImageDraw.Draw(self.im)

    def text(self, xy, s, size=30, deva=False, bold=False, fill=(20, 20, 20)):
        self.d.text(xy, s, font=font(size, deva, bold), fill=fill)

    def band(self, y0, y1, colour):
        self.d.rectangle([0, y0, W, y1], fill=colour)

    def photo(self, x, y, w=200, h=240):
        self.d.rectangle([x, y, x + w, y + h], fill=(200, 205, 212), outline=(120, 120, 120), width=2)
        self.d.ellipse([x + 55, y + 40, x + w - 55, y + 130], fill=(160, 165, 172))
        self.d.rectangle([x + 35, y + 140, x + w - 35, y + h - 10], fill=(160, 165, 172))


# ---------------------------------------------------------------- people and numbers (all fake)

def person(rng: random.Random, split: str) -> dict:
    first, first_d = rng.choice(FIRST[split])
    sur, sur_d = rng.choice(SURNAME[split])
    fa, fa_d = rng.choice(MALE_FATHERS[split])
    female = first in FEMALE
    married = female and rng.random() < 0.4
    rel_first, rel_first_d = (rng.choice(MALE_FATHERS[split]) if married else (fa, fa_d))
    dob = date(1965, 1, 1) + timedelta(days=rng.randrange(0, 365 * 38))
    city = rng.choice(CITIES)
    return {"first": first, "first_d": first_d, "sur": sur, "sur_d": sur_d, "female": female,
            "relation_type": "HUSBAND" if married else "FATHER", "rel_first": rel_first, "rel_first_d": rel_first_d,
            "dob": dob, "city": city}


def pan_number(rng: random.Random, p: dict) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(rng.choice(letters) for _ in range(3)) + "P" + p["sur"][0] + f"{rng.randrange(1000, 9999)}" \
        + rng.choice(letters)


def epic_number(rng: random.Random) -> str:
    return "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(3)) + f"{rng.randrange(10 ** 6, 10 ** 7):07d}"


def dl_number(rng: random.Random, p: dict, issued: date) -> str:
    _, _, _, state, rto = p["city"]
    return f"{state}{rto}{issued.year}{rng.randrange(10 ** 6, 10 ** 7):07d}"


def mrz_check(s: str) -> str:
    """ICAO 9303 7-3-1 check digit."""
    val = {**{str(i): i for i in range(10)}, **{chr(65 + i): 10 + i for i in range(26)}, "<": 0}
    return str(sum(val[c] * (7, 3, 1)[i % 3] for i, c in enumerate(s)) % 10)


# ---------------------------------------------------------------- the four documents

def make_pan(rng, p, lang) -> tuple[Image.Image, dict]:
    c = Card(bg=(214, 228, 238))
    c.band(0, 110, (40, 92, 150))
    if lang == "hi":
        c.text((30, 12), "आयकर विभाग", 34, deva=True, fill="white")
        c.text((640, 12), "भारत सरकार", 34, deva=True, fill="white")
    c.text((30, 60), "INCOME TAX DEPARTMENT", 30, bold=True, fill="white")
    c.text((640, 60), "GOVT. OF INDIA", 30, bold=True, fill="white")
    pan = pan_number(rng, p)
    lab = (lambda h, e: f"{h} / {e}") if lang == "hi" else (lambda h, e: e)
    c.text((40, 140), lab("स्थायी लेखा संख्या कार्ड", "Permanent Account Number Card"), 26, deva=lang == "hi")
    c.text((40, 180), pan, 46, bold=True)
    name = f"{p['first']} {p['sur']}"
    father = f"{p['rel_first'] if p['relation_type'] == 'FATHER' else 'RAMCHANDRA'} {p['sur']}"
    if p["relation_type"] == "HUSBAND":
        father = f"{rng.choice(MALE_FATHERS['dev' if p['first'] in dict(FIRST['dev']) else 'heldout'])[0]} {p['sur']}"
    y = 260
    for h, e, v in (("नाम", "Name", name), ("पिता का नाम", "Father's Name", father),
                    ("जन्म की तारीख", "Date of Birth", dmy(p["dob"]))):
        c.text((40, y), lab(h, e), 22, deva=lang == "hi", fill=(70, 70, 70))
        c.text((40, y + 30), v, 32, bold=True)
        y += 95
    c.photo(760, 150)
    c.text((760, 410), "Signature", 20, fill=(90, 90, 90))
    truth = {"pan_number": pan, "name": name, "father_name": father, "date_of_birth": p["dob"].isoformat()}
    return c.im, truth


def make_voter(rng, p, lang) -> tuple[Image.Image, dict]:
    c = Card(bg=(248, 244, 236))
    c.band(0, 100, (200, 90, 40))
    if lang in ("hi", "mr"):
        c.text((30, 8), LABELS[lang]["eci"], 34, deva=True, fill="white")
    c.text((30, 55), "ELECTION COMMISSION OF INDIA", 30, bold=True, fill="white")
    epic = epic_number(rng)
    c.text((40, 120), "IDENTITY CARD", 26, bold=True)
    c.text((560, 120), epic, 40, bold=True)
    name = f"{p['first']} {p['sur']}"
    rel = f"{p['rel_first']} {p['sur']}"
    rel_label_en = "Husband's Name" if p["relation_type"] == "HUSBAND" else "Father's Name"
    y = 190
    if lang in ("hi", "mr"):
        L = LABELS[lang]
        c.text((40, y), f"{L['elector']} : {p['first_d']} {p['sur_d']}", 28, deva=True); y += 48
    c.text((40, y), f"Elector's Name : {name}", 28); y += 52
    if lang in ("hi", "mr"):
        L = LABELS[lang]
        rl = L["husband"] if p["relation_type"] == "HUSBAND" else L["father"]
        c.text((40, y), f"{rl} : {p['rel_first_d']} {p['sur_d']}", 28, deva=True); y += 48
    c.text((40, y), f"{rel_label_en} : {rel}", 28); y += 52
    gender_en = "Female" if p["female"] else "Male"
    if lang in ("hi", "mr"):
        L = LABELS[lang]
        c.text((40, y), f"{L['gender']} / Gender : {L['female'] if p['female'] else L['male']} / {gender_en}", 28,
               deva=True); y += 52
    else:
        c.text((40, y), f"Gender : {gender_en}", 28); y += 52
    c.text((40, y), f"Date of Birth : {dmy(p['dob'])}", 28)
    c.photo(790, 200, 180, 220)
    truth = {"epic_number": epic, "name": name, "relation_name": rel, "relation_type": p["relation_type"],
             "gender": "FEMALE" if p["female"] else "MALE"}
    return c.im, truth


def make_dl(rng, p, lang) -> tuple[Image.Image, dict]:
    c = Card(bg=(236, 242, 246))
    c.band(0, 90, (30, 70, 120))
    c.text((30, 10), "Union of India", 24, fill="white")
    c.text((30, 42), "MAHARASHTRA STATE MOTOR DRIVING LICENCE", 30, bold=True, fill="white")
    issued = date(2016, 1, 1) + timedelta(days=rng.randrange(0, 365 * 8))
    valid = date(issued.year + 20, issued.month, min(issued.day, 28))
    if valid.year - p["dob"].year > 50:
        valid = date(p["dob"].year + 50, p["dob"].month, min(p["dob"].day, 28))
    dl = dl_number(rng, p, issued)
    shown = f"{dl[:4]} {dl[4:]}"
    name = f"{p['first']} {p['sur']}"
    guardian = f"{p['rel_first']} {p['sur']}"
    city, city_d, pin, _, _ = p["city"]
    classes = rng.choice([["MCWG"], ["LMV"], ["MCWG", "LMV"]])
    L = LABELS.get(lang)
    rows = [("DL No", None, shown), ("Name", L and L["name"], name), ("S/D/W of", None, guardian),
            ("Date of Birth", L and L["dob"], dmy(p["dob"], "-")), ("Issue Date", L and L["issue"], dmy(issued, "-")),
            ("Valid Till (NT)", L and L["valid"], dmy(valid, "-")),
            ("Address", L and L["address"], f"FLAT 12, MAIN ROAD, {city} {pin}"),
            ("COV", None, "  ".join(classes))]
    y = 110
    for en, reg, value in rows:
        label = f"{reg} / {en}" if reg else en
        c.text((40, y), f"{label} : ", 24, deva=bool(reg), fill=(60, 60, 60))
        lw = c.d.textlength(f"{label} : ", font=font(24, deva=bool(reg)))
        c.text((40 + lw, y - 2), value, 28, bold=True)
        y += 58
    if lang in ("hi", "mr"):
        c.text((40, y), f"{p['first_d']} {p['sur_d']}, {city_d}", 26, deva=True)
    c.photo(800, 120, 170, 210)
    truth = {"dl_number": dl, "name": name, "guardian_name": guardian, "date_of_birth": p["dob"].isoformat(),
             "date_of_issue": issued.isoformat(), "valid_till": valid.isoformat(), "pin_code": pin,
             "vehicle_classes": classes}
    return c.im, truth


def make_passport(rng, p, lang) -> tuple[Image.Image, dict]:
    pw, ph = 1250, 880                                   # a passport data page at ~300 dpi
    im = Image.new("RGB", (pw, ph), (238, 236, 246))
    d = ImageDraw.Draw(im)
    if lang == "hi":
        d.text((40, 20), "भारत गणराज्य", font=font(34, deva=True), fill=(30, 30, 90))
    d.text((40, 70), "REPUBLIC OF INDIA", font=font(34, bold=True), fill=(30, 30, 90))
    number = rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") + f"{rng.randrange(10 ** 6, 10 ** 7):07d}"
    issued = date(2015, 1, 1) + timedelta(days=rng.randrange(0, 365 * 9))
    expiry = date(issued.year + 10, issued.month, issued.day) - timedelta(days=1)
    sex = "F" if p["female"] else "M"
    rows = [("Type", "P"), ("Country Code", "IND"), ("Passport No.", number), ("Surname", p["sur"]),
            ("Given Name(s)", p["first"]), ("Nationality", "INDIAN"), ("Sex", sex),
            ("Date of Birth", dmy(p["dob"])), ("Place of Birth", p["city"][0]),
            ("Date of Issue", dmy(issued)), ("Date of Expiry", dmy(expiry))]
    y = 140
    for i, (label, value) in enumerate(rows):
        x = 330 if i % 2 == 0 else 780
        d.text((x, y), label, font=font(20), fill=(90, 90, 110))
        d.text((x, y + 24), value, font=font(30, bold=True), fill=(20, 20, 30))
        if i % 2 == 1:
            y += 78
    y += 78
    d.rectangle([40, 150, 290, 460], fill=(200, 205, 212), outline=(120, 120, 120), width=2)
    name_field = f"{p['sur']}<<{p['first']}".ljust(39, "<")
    line1 = f"P<IND{name_field}"[:44]
    dob6, exp6 = p["dob"].strftime("%y%m%d"), expiry.strftime("%y%m%d")
    num9 = number.ljust(9, "<")
    personal = "<" * 14
    body = f"{num9}{mrz_check(num9)}IND{dob6}{mrz_check(dob6)}{sex}{exp6}{mrz_check(exp6)}{personal}{mrz_check(personal)}"
    composite = mrz_check(num9 + mrz_check(num9) + dob6 + mrz_check(dob6) + exp6 + mrz_check(exp6) + personal
                          + mrz_check(personal))
    line2 = (body + composite)[:44]
    mono = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 38)
    d.text((40, ph - 150), line1, font=mono, fill=(10, 10, 10))
    d.text((40, ph - 90), line2, font=mono, fill=(10, 10, 10))
    truth = {"passport_number": number, "surname": p["sur"], "given_names": p["first"], "nationality": "IND",
             "sex": "FEMALE" if p["female"] else "MALE", "date_of_birth": p["dob"].isoformat(),
             "date_of_expiry": expiry.isoformat()}
    return im, truth


MAKERS = {"PAN": make_pan, "VOTER_ID": make_voter, "DRIVING_LICENCE": make_dl, "PASSPORT": make_passport}


# ---------------------------------------------------------------- image quality (what a field officer's phone does)

def degrade(im: Image.Image, quality: str, rng: random.Random) -> tuple[Image.Image, int]:
    """(image, jpeg quality to save with)."""
    if quality == "clean":
        return im, 95
    if quality == "phone":
        import cv2
        bg = Image.new("RGB", (im.width + 240, im.height + 240), tuple(rng.randrange(90, 160) for _ in range(3)))
        bg.paste(im, (120, 120))
        a = np.array(bg)
        h, w = a.shape[:2]
        src = np.float32([[120, 120], [w - 120, 120], [w - 120, h - 120], [120, h - 120]])
        jitter = lambda: rng.randrange(-35, 35)  # noqa: E731
        dst = np.float32([[120 + jitter(), 120 + jitter()], [w - 120 + jitter(), 120 + jitter()],
                          [w - 120 + jitter(), h - 120 + jitter()], [120 + jitter(), h - 120 + jitter()]])
        a = cv2.warpPerspective(a, cv2.getPerspectiveTransform(src, dst), (w, h), borderMode=cv2.BORDER_REPLICATE)
        light = np.linspace(0.85, 1.1, w)[None, :, None]
        a = np.clip(a * light + np.random.default_rng(rng.randrange(1 << 30)).normal(0, 6, a.shape), 0, 255)
        return Image.fromarray(a.astype("uint8")).filter(ImageFilter.GaussianBlur(0.8)), 82
    if quality == "skew":
        angle = rng.choice([-1, 1]) * rng.uniform(4, 9)
        return im.rotate(angle, expand=True, fillcolor=(255, 255, 255), resample=Image.BICUBIC), 90
    if quality == "low_light":
        a = np.array(im).astype("float32") * 0.45 + 10
        a += np.random.default_rng(rng.randrange(1 << 30)).normal(0, 8, a.shape)
        return Image.fromarray(np.clip(a, 0, 255).astype("uint8")), 85
    if quality == "jpeg":
        return im, 22
    if quality == "small":
        return im.resize((int(im.width * 0.5), int(im.height * 0.5)), Image.BILINEAR), 85
    raise ValueError(quality)


def generate(split: str, per: int) -> Path:
    rng = random.Random({"dev": 20261015, "heldout": 99991015}[split])
    out = OUT / split
    out.mkdir(parents=True, exist_ok=True)
    truth = {"_README": {"purpose": "SYNTHETIC: every person, number and date is generated by "
                                    "evals/documents/synthetic.py; the label is the truth (never extractor output).",
                         "split": split, "aadhaar": "out of scope (owner, 2026-10-14) -- never generated"}}
    for doc_type, maker in MAKERS.items():
        for lang in LANGS[doc_type]:
            for quality in QUALITIES:
                for n in range(per):
                    p = person(rng, split)
                    im, fields = maker(rng, p, lang)
                    im, q = degrade(im, quality, rng)
                    name = f"{doc_type.lower()}_{lang}_{quality}_{n}.jpg"
                    im.convert("RGB").save(out / name, "JPEG", quality=q)
                    truth[name] = {"document_type": doc_type, "language": lang, "quality": quality, "fields": fields}
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "heldout"], required=True)
    ap.add_argument("--per", type=int, default=2)
    a = ap.parse_args()
    out = generate(a.split, a.per)
    print(f"{out}: {len(list(out.glob('*.jpg')))} images")


if __name__ == "__main__":
    main()
