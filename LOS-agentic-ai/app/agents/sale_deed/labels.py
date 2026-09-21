"""
Label anchors for Sale Deed extraction, English and Devanagari.

Labels are ANCHORS, not translations. Nothing here converts OCR text from one
language to another: a Hindi label locates a value, and the value is kept in
the script it was printed in. Transliteration happens only where a canonical
comparison needs it, and the original is never discarded.

OCR DAMAGE IS THE NORM, not the exception, so every anchor is matched against
a COMPACTED form of the page -- punctuation, spacing and the Devanagari danda
stripped -- because that is what survives a scan. Real pages in this corpus
produced "मो०नं०" for a phone label and "सम्पत्ति का प्रकार :-" with the
colon and dash fused to the caption.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Devanagari normalisation
# ---------------------------------------------------------------------------

# Characters OCR routinely substitutes for one another in Devanagari. The
# nukta-bearing forms and their plain counterparts are the common pair; the
# abbreviation sign ० is read as the digit zero and vice versa.
_DEVANAGARI_FOLD = {
    "ऩ": "न",  # ऩ -> न
    "ऱ": "र",  # ऱ -> र
    "ऴ": "ळ",  # ऴ -> ळ
    "ग़": "ग",  # ग़ -> ग
    "ज़": "ज",  # ज़ -> ज
    "ड़": "ड",  # ड़ -> ड
    "ढ़": "ढ",  # ढ़ -> ढ
    "फ़": "फ",  # फ़ -> फ
    "ॠ": "ऋ",
}

_STRIP_CHARS = re.compile(r"[\s\.\,\:\;\-\_\|\(\)\[\]\/\\'\"०॰।॥]+")


def fold(text: str) -> str:
    """Fold OCR-confusable Devanagari forms onto one spelling."""
    return "".join(_DEVANAGARI_FOLD.get(character, character) for character in text)


def compact(text: str) -> str:
    """
    Reduce a line to the form anchors are matched against.

    Punctuation and spacing are what OCR loses first, so they are removed
    from both sides of the comparison rather than being relied upon.
    """
    return _STRIP_CHARS.sub("", fold(text or "")).upper()


def has_devanagari(text: str, minimum: int = 12) -> bool:
    """
    Whether a page carries real Devanagari rather than stray marks.

    Tesseract's English model emits a few Devanagari codepoints from seal and
    ornament noise, so a handful of characters is not evidence of a Hindi
    page.
    """
    return sum(1 for c in text or "" if "ऀ" <= c <= "ॿ") >= minimum


# ---------------------------------------------------------------------------
# Anchors
#
# Each concept lists English and Devanagari variants. Hindi variants are drawn
# from the UP registration form in the corpus and from the standard vocabulary
# of Indian deeds; where a variant has NOT been seen on a real sample it is
# still listed, because an anchor that never matches costs nothing and a
# missing one loses a field.
# ---------------------------------------------------------------------------

SELLER = (
    "First Party", "1st Party", "Party 1", "Seller", "Vendor", "Executant",
    "विक्रेता", "प्रथम पक्ष", "विक्रेतागण", "बेचने वाला",
)

BUYER = (
    "Second Party", "2nd Party", "Party 2", "Buyer", "Purchaser", "Claimant",
    "क्रेता", "खरीदार", "द्वितीय पक्ष", "क्रेतागण", "खरीददार",
)

DEED_TITLE = (
    "Sale Deed", "Deed of Sale", "Conveyance",
    "विक्रय विलेख", "विक्रय पत्र", "बैनामा", "विक्रयनामा",
)

REGISTRATION_NUMBER = (
    "Registration Number", "Registration No", "Deed No", "Document No",
    "पंजीकरण संख्या", "पंजीयन संख्या", "लेखपत्र संख्या", "क्रमांक",
)

REGISTRATION_DATE = (
    "Registration Date", "Date of Registration",
    "पंजीकरण दिनांक", "पंजीयन दिनांक", "पंजीकरण तिथि",
)

EXECUTION_DATE = (
    "Execution Date", "Date of Execution",
    "निष्पादन दिनांक", "निष्पादन तिथि",
)

PROPERTY = (
    "Property Description", "Property", "Schedule of Property",
    "संपत्ति", "सम्पत्ति", "सम्पत्ति का विवरण", "सम्पत्ति का प्रकार",
)

ADDRESS = (
    "Address", "Property Address",
    "पता", "स्थाई पता", "अस्थाई पता", "संपत्ति का पता",
)

VILLAGE = (
    "Village", "Mauza",
    "ग्राम", "गांव", "मौजा", "मौहल्ला", "मोहल्ला",
)

DISTRICT = ("District", "Distt", "जिला", "जनपद")

STATE = ("State", "राज्य", "प्रदेश")

AREA = (
    "Area", "Total Area", "Built up Area",
    "क्षेत्रफल", "कुल क्षेत्रफल", "निर्मित क्षेत्रफल", "क्षेत्रफल कुल",
)

SURVEY_NUMBER = ("Survey No", "Survey Number", "सर्वे संख्या", "सर्वे नंबर")

PLOT_NUMBER = ("Plot No", "Plot Number", "प्लाट संख्या", "प्लॉट नंबर", "भूखंड संख्या")

KHASRA_NUMBER = ("Khasra No", "Khasra Number", "खसरा संख्या", "खसरा नंबर")

KHATA_NUMBER = ("Khata No", "Khata Number", "खाता संख्या", "खाता नंबर")

CONSIDERATION = (
    "Consideration Price", "Consideration Amount", "Sale Consideration",
    "Consideration",
    "विक्रय प्रतिफल", "बिक्री मूल्य", "प्रतिफल", "विक्रय मूल्य",
)

STAMP_DUTY = (
    "Stamp Duty Amount", "Duty Amount", "Stamp Duty",
    "स्टाम्प शुल्क", "स्टाम्प ड्यूटी", "मुद्रांक शुल्क",
)

REGISTRATION_FEE = (
    "Registration Fee", "Other Fees",
    "पंजीकरण शुल्क", "निबंधन शुल्क",
)

REGISTRATION_OFFICE = (
    "Registration Office", "Registering Officer", "Office of the Sub-Registrar",
    "पंजीकरण कार्यालय", "निबंधन कार्यालय",
)

SUB_REGISTRAR = (
    "Sub-Registrar", "Sub Registrar", "उप-पंजीयक", "उप पंजीयक", "अवर निबंधक",
)

WITNESS = ("Witness", "Witnesses", "गवाह", "साक्षी")

PROPERTY_TYPE = (
    "Property Type", "सम्पत्ति का प्रकार", "भूमि का प्रकार",
)


#: Concept name -> anchors, for callers that want to iterate.
ANCHORS: dict[str, tuple[str, ...]] = {
    "seller": SELLER,
    "buyer": BUYER,
    "deed_title": DEED_TITLE,
    "registration_number": REGISTRATION_NUMBER,
    "registration_date": REGISTRATION_DATE,
    "execution_date": EXECUTION_DATE,
    "property": PROPERTY,
    "address": ADDRESS,
    "village": VILLAGE,
    "district": DISTRICT,
    "state": STATE,
    "area": AREA,
    "survey_number": SURVEY_NUMBER,
    "plot_number": PLOT_NUMBER,
    "khasra_number": KHASRA_NUMBER,
    "khata_number": KHATA_NUMBER,
    "consideration": CONSIDERATION,
    "stamp_duty": STAMP_DUTY,
    "registration_fee": REGISTRATION_FEE,
    "registration_office": REGISTRATION_OFFICE,
    "sub_registrar": SUB_REGISTRAR,
    "witness": WITNESS,
    "property_type": PROPERTY_TYPE,
}


def anchor_hits(text: str, anchors: tuple[str, ...]) -> bool:
    """Whether any anchor appears in the compacted form of the text."""
    haystack = compact(text)
    return any(compact(anchor) in haystack for anchor in anchors if anchor.strip())


def value_after_anchor(line: str, anchors: tuple[str, ...]) -> str | None:
    """
    The remainder of a line after whichever anchor it carries.

    Compared on compacted forms so a caption OCR'd as "सम्पत्ति का प्रकार :-"
    still matches "सम्पत्ति का प्रकार", then mapped back to the original line
    so the VALUE keeps its real spacing and script.
    """
    folded = fold(line or "")
    haystack = compact(folded)

    for anchor in anchors:
        needle = compact(anchor)
        if not needle or needle not in haystack:
            continue

        # Walk the original line, compacting as we go, until the compacted
        # prefix covers the anchor. Whatever follows is the value.
        built = []
        for index, character in enumerate(folded):
            built.append(character)
            if compact("".join(built)).endswith(needle):
                tail = folded[index + 1:].strip(" :.-|;")
                return tail or None

    return None


__all__ = [
    "ANCHORS",
    "anchor_hits",
    "compact",
    "fold",
    "has_devanagari",
    "value_after_anchor",
] + [name for name in ANCHORS]
