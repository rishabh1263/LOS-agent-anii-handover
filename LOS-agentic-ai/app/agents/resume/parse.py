"""
RESUME FIELDS FROM ITS LINES -- deterministic, no LLM (2026-10-16). Every word list is app/config/resume.yaml.

    contact          name, email(s), Indian mobile(s), LinkedIn
    personal         date of birth (+ age), current location (city / state / PIN / address)
    education        each qualification with its level and year; the highest one
    experience       each job: employer, designation, from-to, months, internship; the current one; total months;
                     employment gaps
    offer            current CTC, expected CTC, notice period
    languages        the languages listed (only from a "Languages" label or section)

A value is taken ONLY from its own label or its own shape, never guessed: a field the resume does not print is
absent. NEVER RETURNED (data minimisation): father's / mother's / spouse's name, gender, marital status, religion,
caste and every identity number (PAN, Aadhaar, passport, bank account) -- masked in any returned text.
"""

from __future__ import annotations

import functools
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "resume.yaml"
_LOADED: dict[str, Any] = {"mtime": None, "data": {}}


def cfg() -> dict[str, Any]:
    """app/config/resume.yaml, parsed once per change."""
    import yaml

    try:
        mtime = _CONFIG.stat().st_mtime
    except OSError:
        return {}
    if _LOADED["mtime"] != mtime:
        _LOADED.update(mtime=mtime, data=yaml.safe_load(_CONFIG.read_text(encoding="utf-8")) or {})
        _compiled.cache_clear()
    return _LOADED["data"]


def _words(key: str) -> list[str]:
    return [str(w).strip().lower() for w in cfg().get(key) or [] if str(w).strip()]


def _phrase(words: list[str]) -> str:
    """Phrases -> one alternation, longest first, spaces / dots / hyphens inside a phrase optional."""
    def one(w: str) -> str:
        tokens = [t for t in re.split(r"[\s.\-_/]+", w) if t]
        return r"[\s.\-_/]*".join(re.escape(t) for t in tokens) if tokens else re.escape(w)
    return "|".join(one(w) for w in sorted(set(words), key=len, reverse=True))


@functools.lru_cache(maxsize=1)
def _compiled() -> dict[str, Any]:
    c = cfg()
    labels = {k: [str(x).lower() for x in v] for k, v in (c.get("labels") or {}).items()}
    labels["_never"] = _words("never_return_labels")
    labels["_weak_location"] = _words("weak_location_labels")
    labels["_not_location"] = _words("not_current_location_labels")
    every = [w for ws in labels.values() for w in ws]
    out = {
        "labels": {k: re.compile(rf"(?<![A-Za-z])(?:{_phrase(v)})(?![A-Za-z])", re.I) for k, v in labels.items() if v},
        # a label FOLLOWED BY its separator (":", "-", "=", "|", or two+ spaces / a tab), anywhere in a line
        # ("D.O.B.:" / "Contact No.:" -- a trailing dot before the separator is part of the label)
        "any_label": re.compile(rf"(?<![A-Za-z])(?:{_phrase(every)})\.?\s*(?:[:：=–—|]|-(?!\d)|\s{{2,}})", re.I),
        "headings": {re.sub(r"[^A-Z]", "", str(h).upper()): s for s, hs in (c.get("headings") or {}).items()
                     for h in hs},
        "heading_keywords": {str(k).upper(): v for k, v in (c.get("heading_keywords") or {}).items()},
        "levels": [(x["level"], re.compile(x["pattern"])) for x in c.get("qualification_levels") or []],
        "not_qualification": re.compile(c.get("not_a_qualification") or r"(?!x)x"),
        "graduation_label": re.compile(c.get("graduation_label") or r"(?!x)x"),
        "institution": re.compile(rf"\b(?:{_phrase(_words('institution_words'))})\b", re.I),
        "pursuing": re.compile(rf"\b(?:{_phrase(_words('pursuing_words'))})\b", re.I),
        "not_full_time": re.compile(rf"\b(?:{_phrase(_words('not_full_time_words'))})\b", re.I),
        "company": re.compile(rf"(?<![A-Za-z])(?:{_phrase(_words('company_markers'))})(?![A-Za-z])", re.I),
        "known_employer": re.compile(rf"(?<![A-Za-z])(?:{_phrase(_words('known_employers'))})(?![A-Za-z])", re.I),
        "designation": re.compile(rf"(?<![A-Za-z])(?:{_phrase(_words('designation_words'))})(?![A-Za-z])", re.I),
        "joins": [str(j).lower() for j in c.get("role_employer_joins") or []],
        "languages": re.compile(rf"(?<![A-Za-z])({_phrase(_words('languages'))})(?![A-Za-z])", re.I),
        "states": re.compile(rf"(?<![A-Za-z])({_phrase(_words('states'))})(?![A-Za-z])", re.I),
        "state_abbr": {str(k).lower(): str(v) for k, v in (c.get("state_abbreviations") or {}).items()},
        "cities": re.compile(rf"(?<![A-Za-z])({_phrase(_words('cities'))})(?![A-Za-z])", re.I),
        "mask": [re.compile(p) for p in (c.get("mask_patterns") or {}).values()],
    }
    ctc = c.get("ctc") or {}
    for key in ("lakh_words", "crore_words", "thousand_words", "monthly_words", "annual_words", "not_a_number"):
        words = [str(w).lower() for w in ctc.get(key) or []]
        out["ctc_" + key] = re.compile(rf"(?<![A-Za-z])(?:{_phrase(words)})(?![A-Za-z])", re.I) if words else None
    notice = c.get("notice") or {}
    out["immediate"] = re.compile(rf"(?<![A-Za-z])(?:{_phrase([str(w) for w in notice.get('immediate_words') or []])})"
                                  r"(?![A-Za-z])", re.I)
    out["serving"] = re.compile(rf"(?<![A-Za-z])(?:{_phrase([str(w) for w in notice.get('serving_words') or []])})"
                                r"(?![A-Za-z])", re.I)
    return out


# ------------------------------------------------------------------------------------------------ shapes
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\d/])(?:\+?\s?91[\s.-]*|0)?\(?[6-9][\d\s().-]{8,13}\d(?![\d/])")
LINKEDIN = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_-]+/?", re.I)
YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
PIN = re.compile(r"(?<!\d)([1-8]\d{2})\s?(\d{3})(?!\d)")
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10,
           "NOV": 11, "DEC": 12}
_MONTH_WORD = (r"(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?|AUG(?:UST)?|"
               r"SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)")
_POINT = (rf"(?:{_MONTH_WORD}\.?,?\s*'?(?:(?:19|20)\d{{2}}|\d{{2}}(?!\d))|(?:0?[1-9]|1[0-2])\s*[/.-]\s*(?:19|20)\d{{2}}"
          r"|(?:19|20)\d{2})")
_NOW = r"(?:PRESENT|TILL\s*DATE|TILL\s*NOW|CURRENT(?:LY)?|ONGOING|TODAY|NOW|TO\s*DATE|CONTINUING|CONTD\.?)"
_RANGE_JOIN = r"\s*(?:-|–|—|~|TO\b:?|TILL\b|UNTIL\b)\s*"
RANGE = re.compile(rf"({_POINT}){_RANGE_JOIN}({_POINT}|{_NOW})", re.I)
# experience tables put FROM and TO in two cells ("Jan 2019 | Mar 2021", "Jan 2019   Present" in a text table):
# only inside the experience block
RANGE_CELLS = re.compile(rf"({_POINT})(?:\s*\|\s*|\s{{3,}})({_POINT}|{_NOW})(?=\s*(?:\||$|\s{{3,}}))", re.I)
# a PHOTOGRAPHED table row loses its cell gaps ("Mahindra Finance Sales Executive Feb 2019 Feb 2021", 2026-10-19):
# two MONTH-YEAR points side by side at the END of the row are its From / To -- month words only (bare years side by
# side are an education row's "2015 65%" / "2012 2014" and stay unread)
_MONTH_POINT = rf"(?:{_MONTH_WORD}\.?,?\s*'?(?:(?:19|20)\d{{2}}|\d{{2}}(?!\d)))"
RANGE_TAIL = re.compile(rf"({_MONTH_POINT})\s+({_MONTH_POINT}|{_NOW})\s*$", re.I)
SINGLE_MONTH = re.compile(rf"(?<![A-Z])({_MONTH_POINT})(?!\d)", re.I)
_BULLET = re.compile(r"^\s*[•●▪■◦○◆◇►▶➢➤✓✔\-*–·o]\s+")
_TITLE_WORDS = {"RESUME", "CURRICULUMVITAE", "CV", "BIODATA", "BIODATAFORM", "RESUMEOF", "PROFILE"}
LEVEL_ORDER = {"10th": 1, "12th": 2, "diploma": 2, "graduation": 3, "post_graduation": 4, "phd": 5}


# ------------------------------------------------------------------------------------------------ data
@dataclass
class Education:
    level: str
    year: int | None
    text: str
    pursuing: bool = False


@dataclass
class Job:
    start: tuple[int, int]            # (year, month); month 1 when only the year is printed
    end: tuple[int, int] | None       # None = present
    text: str
    internship: bool = False
    employer: str | None = None
    designation: str | None = None

    @property
    def months(self) -> int:
        today = date.today()
        end = self.end or (today.year, today.month)
        return max(0, (end[0] - self.start[0]) * 12 + end[1] - self.start[1])


@dataclass
class Resume:
    name: str | None = None
    email: str | None = None
    emails: list[str] = field(default_factory=list)
    phone: str | None = None
    phones: list[str] = field(default_factory=list)
    linkedin: str | None = None
    date_of_birth: str | None = None          # YYYY-MM-DD
    age: int | None = None
    location: dict = field(default_factory=dict)   # city, state, pin_code, address, source
    sections: list[str] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    experience: list[Job] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    current_ctc: dict | None = None
    expected_ctc: dict | None = None
    notice_period: dict | None = None
    text_chars: int = 0

    def public(self) -> dict:
        out = asdict(self)
        out["highest_qualification"] = highest(self.education)
        out["total_experience_months"] = total_months(self.experience)
        out["experience"] = [{"employer": j.employer, "designation": j.designation, "start": _fmt(j.start),
                              "end": _fmt(j.end) if j.end else "PRESENT", "months": j.months,
                              "internship": j.internship, "text": j.text} for j in self.experience]
        current = current_job(self.experience)
        out["current_employer"] = current.employer if current else None
        out["current_designation"] = current.designation if current else None
        out["employment_gaps"] = employment_gaps(self.experience)
        out.pop("text_chars", None)
        return out


def _fmt(point: tuple[int, int] | None) -> str | None:
    return f"{point[0]:04d}-{point[1]:02d}" if point else None


def mask(text: str) -> str:
    """Identity numbers (PAN, Aadhaar, passport) masked in any text this service returns."""
    for pattern in _compiled()["mask"]:
        text = pattern.sub(lambda m: "X" * max(0, len(m.group(0)) - 4) + m.group(0)[-4:], text)
    return text


# ------------------------------------------------------------------------------------------------ headings
def heading_of(line: str) -> str | None:
    """A section heading: an exact known one, or a SHORT line that names its section ("Relevant Experience",
    "Additional Experience", even "Relavent Experience" -- 2026-10-16, real sample resumes)."""
    c = _compiled()
    if re.search(r"\S\s{3,}\S|\s\|\s|:\s*\S", line.strip()):
        return None          # a label with its value ("Languages Known   Hindi, English"), a table row: not a heading
    compact = re.sub(r"[^A-Z]", "", line.upper())
    if not compact or len(line.split()) > 5:
        return None
    if compact in c["headings"]:
        return c["headings"][compact]
    words = line.strip().rstrip(":").split()
    heading_like = line.upper() == line or line.rstrip().endswith(":") or all(w[:1].isupper() or not w[:1].isalpha()
                                                                              for w in words)
    if len(words) <= 4 and heading_like and not re.search(r"\d", line) and ":" not in line.rstrip(":"):
        for word, section in c["heading_keywords"].items():
            if word in compact:
                return section
    return None


# ------------------------------------------------------------------------------------------------ labels
def _segments(line: str) -> list[tuple[str, str]]:
    """(label key, value) pairs a line carries: "Date of Birth : 12/03/1995 | Gender: M" -> [(date_of_birth, ...),
    (_never, ...)]. A value runs to the next label, a " | " cell boundary, or the end of the line."""
    c = _compiled()
    out = []
    cells = [x.strip(" :") for x in re.split(r"\s*\|\s*|\s{3,}", line) if x.strip(" :")]
    if len(cells) >= 2 and all(any(p.fullmatch(cell) for p in c["labels"].values()) or cell.lower() in
                               ("from", "to", "duration", "period", "year", "percentage", "marks", "%")
                               for cell in cells):
        return []                      # a TABLE HEADER row ("Organisation | Designation | From | To"): no values
    marks = list(c["any_label"].finditer(line))
    for i, m in enumerate(marks):
        label_text = m.group(0)
        bare = re.sub(r"\.?\s*(?:[:：=–—|]|-|\s{2,})\s*$", "", label_text).strip()
        key = next((k for k, p in c["labels"].items() if p.fullmatch(bare)), None)
        if key is None:
            continue
        # the label must START its segment: line start, or after a separator / cell boundary
        before = line[:m.start()]
        if before.strip() and not re.search(r"(?:[|,;•]|\s{2,})\s*$", before):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(line)
        value = line[m.end():end]
        value = re.split(r"\s\|\s", value)[0]
        out.append((key, value.strip(" \t|,;:-–")))
    return out


def labelled(lines: list[str], key: str, *, multi_line: int = 0) -> list[tuple[int, str]]:
    """(line index, value) for every `key` label; an empty value takes the next line (a label above its value).
    multi_line > 0: continuation lines (no label, no heading) are appended, up to that many."""
    out = []
    for i, line in enumerate(lines):
        for k, value in _segments(line):
            if k != key:
                continue
            j = i
            if not value and i + 1 < len(lines) and not _segments(lines[i + 1]) and not heading_of(lines[i + 1]):
                value, j = lines[i + 1].strip(), i + 1
            extra = []
            for n in range(j + 1, min(len(lines), j + 1 + multi_line)):
                nxt = lines[n]
                if _segments(nxt) or heading_of(nxt) or EMAIL.search(nxt) or len(nxt) > 90:
                    break
                extra.append(nxt.strip())
            if value or extra:
                out.append((i, ", ".join([v for v in [value, *extra] if v])))
    return out


# ------------------------------------------------------------------------------------------------ dates
def _point(text: str) -> tuple[int, int] | None:
    t = text.upper().strip().rstrip(".,")
    if re.fullmatch(_NOW, t, re.I):
        return None
    m = re.match(rf"({_MONTH_WORD})\.?,?\s*'?((?:19|20)\d{{2}}|\d{{2}})", t)
    if m:
        y = int(m.group(2))
        y = y if y > 100 else (2000 + y if y <= date.today().year % 100 + 1 else 1900 + y)
        return y, _MONTHS[m.group(1)[:3]]
    m = re.match(r"(0?[1-9]|1[0-2])\s*[/.-]\s*((?:19|20)\d{2})", t)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.match(r"((?:19|20)\d{2})", t)
    return (int(m.group(1)), 1) if m else None


def parse_date(text: str) -> date | None:
    """A full calendar date as Indian resumes write it: 12/03/1995, 12-03-95, 12.03.1995, 12th March 1995,
    March 12, 1995, 1995-03-12. Day first unless only month-first is possible (13+ in the second place)."""
    t = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", str(text or ""), flags=re.I).upper()
    candidates = []
    m = re.search(r"(?<!\d)((?:19|20)\d{2})[/.-](\d{1,2})[/.-](\d{1,2})(?!\d)", t)
    if m:
        candidates.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    m = re.search(r"(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*((?:19|20)?\d{2})(?!\d)", t)
    if m and not candidates:
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if len(y) == 4 else (1900 + int(y) if int(y) > date.today().year % 100 else 2000 + int(y))
        candidates.append((y, a, b) if b > 12 >= a else (y, b, a))
    m = re.search(rf"(?<!\d)(\d{{1,2}})\s*[-/.,]?\s*({_MONTH_WORD})\.?\s*[-/.,]?\s*((?:19|20)?\d{{2}})(?!\d)", t)
    if m and not candidates:
        y = m.group(3)
        y = int(y) if len(y) == 4 else (1900 + int(y) if int(y) > date.today().year % 100 else 2000 + int(y))
        candidates.append((y, _MONTHS[m.group(2)[:3]], int(m.group(1))))
    m = re.search(rf"({_MONTH_WORD})\.?\s*(\d{{1,2}})\s*,?\s*((?:19|20)\d{{2}})(?!\d)", t)
    if m and not candidates:
        candidates.append((int(m.group(3)), _MONTHS[m.group(1)[:3]], int(m.group(2))))
    for y, mo, d in candidates:
        try:
            return date(y, mo, d)
        except ValueError:
            continue
    return None


def _age_on(born: date, today: date) -> int:
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


# ------------------------------------------------------------------------------------------------ contact
def _mobile(raw: str) -> str | None:
    """A 10-digit Indian mobile from any common way of writing it (+91 98765 43210, 987-654-3210, (987) 654 3210,
    098765...), or None -- a date, a pin code or a reference number is never taken for one."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 and digits[0] in "6789" else None


def _clean_name(raw: str) -> str | None:
    name = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z .'-]", " ", raw)).strip(" .-")
    name = re.sub(r"^(?:MR|MRS|MS|MISS|DR|SHRI|SMT|KUM)\.?\s+", "", name, flags=re.I)
    return name.upper() if len(re.sub(r"[^A-Za-z]", "", name)) >= 3 else None


def _name(lines: list[str], blocks: dict[str, list[str]] | None = None, email: str | None = None) -> str | None:
    """A "Name:" label first; else the best NAME-LIKE line near the top. Scored, not first-found: a two-column
    resume read sidebar-first puts "CONTACT / SKILLS / Tally ERP" above the name (2026-10-16), so a line under the
    Skills / Languages sections, or holding a company / title / city / skill word, is not a name; a line whose words
    are in the candidate's own email address, or that comes before any heading, is."""
    for line in lines[:25]:
        m = re.match(r"^\s*(?:FULL\s+)?NAME\s*(?:OF\s+(?:THE\s+)?CANDIDATE\s*)?[:\-|]\s*(.+)$", line, re.I)
        if m and not re.match(r"^\s*(?:FATHER|MOTHER|HUSBAND|SPOUSE|COMPANY|INSTITUT|SCHOOL|COLLEGE)", line, re.I):
            value = re.split(r"\s\|\s|\s{2,}", m.group(1))[0]
            return _clean_name(value)
    c = _compiled()
    blocks = blocks or {}
    not_names = set()
    for section in ("skills", "languages", "hobbies", "certifications", "achievements", "projects", "summary"):
        not_names.update(blocks.get(section) or [])
    header = set(blocks.get("header") or lines[:6])
    user = re.sub(r"[^a-z]", " ", (email or "").split("@")[0].lower()).split()
    best = None
    for pos, line in enumerate(lines[:40]):
        if heading_of(line) or EMAIL.search(line) or PHONE.search(line) or re.search(r"\d", line):
            continue
        if re.sub(r"[^A-Z]", "", line.upper()) in _TITLE_WORDS or line in not_names:
            continue
        if c["cities"].search(line) or c["states"].search(line) or c["languages"].search(line):
            continue
        words = line.replace(".", ". ").split()
        if not (1 < len(words) <= 5 and all(re.fullmatch(r"[A-Za-z.'-]+", w) for w in words)):
            continue
        if c["company"].search(line) or c["designation"].search(line) or c["known_employer"].search(line):
            continue
        tokens = [w.lower().strip(".") for w in words if len(w.strip(".")) > 1]
        in_email = sum(1 for t in tokens if any(t in u or u in t for u in user if len(u) > 2))
        score = 3 * in_email + (2 if line in header else 0) + (1 if line.isupper() or line.istitle() else 0) - pos / 50
        if best is None or score > best[0]:
            best = (score, line)
    return _clean_name(best[1]) if best else None


# ------------------------------------------------------------------------------------------------ location
def _city_state(text: str) -> tuple[str | None, str | None]:
    c = _compiled()
    cities = [m.group(1) for m in c["cities"].finditer(text)]
    states = [m.group(1) for m in c["states"].finditer(text)]
    if not states:
        m = re.search(r"(?:,|\s)\s*([A-Za-z]{2})\s*(?:[-,]|\s)?\s*\d{6}\b|,\s*([A-Za-z]{2})\s*$", text)
        abbr = (m.group(1) or m.group(2)).lower() if m else None
        if abbr in c["state_abbr"]:
            states = [c["state_abbr"][abbr]]
    city = cities[-1].title() if cities else None
    state = states[-1].title() if states else None
    if city and state and city.lower() == state.lower():          # "Delhi" is both: a city
        state = None if len(states) < 2 else state
    return city, state


def _location(lines: list[str], header: list[str]) -> dict:
    c = _compiled()
    usable = [ln for ln in lines if not c["labels"].get("_not_location", re.compile("(?!x)x")).search(ln)]
    found: dict = {}
    for _, value in labelled(usable, "current_location"):
        city, state = _city_state(value)
        if city or state:
            found = {"city": city, "state": state, "source": "label"}
            break
    addresses = labelled(usable, "address", multi_line=3)
    if addresses:
        _, value = addresses[0]
        value = mask(value)
        city, state = _city_state(value)
        pin = PIN.search(value)
        found.setdefault("city", city)
        if not found.get("city"):
            found["city"] = city
        found.setdefault("state", state)
        if not found.get("state"):
            found["state"] = state
        found["address"] = value[:200]
        found["pin_code"] = "".join(pin.groups()) if pin else None
        found.setdefault("source", "address")
    if not found.get("pin_code"):
        for _, value in labelled(usable, "pin_code"):
            pin = PIN.search(value)
            if pin:
                found["pin_code"] = "".join(pin.groups())
                break
    if not found.get("city"):
        for line in header:
            if EMAIL.search(line) and not re.search(r"[|,•]", line):
                continue
            for part in re.split(r"\s*[|•·]\s*|\s{3,}", line):
                if EMAIL.search(part) or _mobile(part):
                    continue
                city, state = _city_state(part)
                if city:
                    found.update(city=city, state=found.get("state") or state, source=found.get("source") or "header")
                    pin = PIN.search(part)
                    if pin and not found.get("pin_code"):
                        found["pin_code"] = "".join(pin.groups())
                    break
            if found.get("city"):
                break
    if not found.get("city"):
        for _, value in labelled(usable, "_weak_location"):
            city, state = _city_state(value)
            if city:
                found.update(city=city, state=found.get("state") or state, source=found.get("source") or "place")
                break
    return {k: v for k, v in found.items() if v is not None} if any(found.get(k) for k in ("city", "state",
                                                                                            "pin_code", "address")) \
        else {}


# ------------------------------------------------------------------------------------------------ CTC / notice
_NUMBER = re.compile(r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)")


def parse_ctc(value: str) -> dict | None:
    """'4.2 LPA' / 'Rs. 3,60,000 p.a.' / '35,000 per month' / '6-7 Lakhs' / 'Negotiable' -> amounts in rupees."""
    c = _compiled()
    text = str(value or "").strip()
    if not text:
        return None
    out: dict = {"text": text[:80]}
    if c["ctc_not_a_number"] is not None and c["ctc_not_a_number"].search(text) and not _NUMBER.search(text):
        out["negotiable"] = True
        return out
    nums = [m for m in _NUMBER.finditer(text)][:2]
    if not nums:
        return out if len(text) <= 40 else None
    tail = text[nums[-1].end():nums[-1].end() + 24]
    mult = 1.0
    if c["ctc_crore_words"] is not None and c["ctc_crore_words"].match(tail.strip(" .")):
        mult = 1e7
    elif c["ctc_lakh_words"] is not None and c["ctc_lakh_words"].match(tail.strip(" .")):
        mult = 1e5
    elif c["ctc_thousand_words"] is not None and c["ctc_thousand_words"].match(tail.strip(" .")):
        mult = 1e3
    amounts = [float(m.group(1).replace(",", "")) * mult for m in nums]
    monthly = c["ctc_monthly_words"] is not None and bool(c["ctc_monthly_words"].search(text))
    annual = (c["ctc_annual_words"] is not None and bool(c["ctc_annual_words"].search(text))) or \
        bool(re.search(r"\bLPA\b|\bP\.?A\b", text, re.I)) or mult >= 1e5 and not monthly
    period = "monthly" if monthly else "annual" if annual else None
    if len(amounts) == 2 and re.search(r"\d\s*(?:-|–|to)\s*\d", text[nums[0].start():nums[1].end()], re.I):
        out["amount_min_inr"], out["amount_max_inr"] = round(amounts[0]), round(amounts[1])
        amount = amounts[0]
    else:
        amount = amounts[0]
        out["amount_inr"] = round(amount)
    out["period"] = period
    if period == "annual":
        out["annual_inr"] = round(amount)
    elif period == "monthly":
        out["annual_inr"] = round(amount * 12)
    return out


def parse_notice(value: str) -> dict | None:
    c = _compiled()
    text = str(value or "").strip()
    if not text:
        return None
    out: dict = {"text": text[:80], "serving": bool(c["serving"].search(text)), "days": None}
    m = re.search(r"(\d+)\s*(day|days|week|weeks|wk|month|months|mon|mnth|mnths|m)\b", text, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        out["days"] = n if unit.startswith("d") else n * 7 if unit.startswith("w") else n * 30
    elif c["immediate"].search(text):
        out["days"] = 0
    elif re.search(r"\b(one|two|three)\s+months?\b", text, re.I):
        out["days"] = {"one": 30, "two": 60, "three": 90}[re.search(r"\b(one|two|three)\b", text, re.I)
                                                            .group(1).lower()]
    return out


# ------------------------------------------------------------------------------------------------ jobs
def _split_parts(text: str) -> list[str]:
    c = _compiled()
    joins = "|".join(re.escape(j) for j in c["joins"] if j != "@")
    parts = re.split(rf"\s+[-–—|]\s+|\s*\|\s*|\s{{3,}}|,\s+|\s+@\s*|\s+(?:{joins})\s+(?=[A-Z])" if joins else
                     r"\s+[-–—|]\s+|\s*\|\s*|\s{3,}|,\s+|\s+@\s*", text)
    return [p.strip(" ()[]:-–—|,.") for p in parts if p and p.strip(" ()[]:-–—|,.")]


def _kind(part: str) -> tuple[int, int]:
    """(employer score, designation score) of one part."""
    c = _compiled()
    if EMAIL.search(part) or _mobile(part) or RANGE.search(part.upper()) or YEAR.fullmatch(part.strip()):
        return 0, 0
    if c["cities"].fullmatch(part.strip()) or c["states"].fullmatch(part.strip()):
        return 0, 0
    emp = 2 if c["company"].search(part) else 0
    emp = max(emp, 2 if c["known_employer"].search(part) else 0)
    des = 2 if c["designation"].search(part) else 0
    if emp and des:
        # "Bank Relationship Manager" vs "Bajaj Finance Ltd": the marker at the END decides
        last = part.split()[-1] if part.split() else ""
        if c["company"].fullmatch(last.strip(".,")) or c["known_employer"].search(part):
            des = 1
        else:
            emp = 1
    return emp, des


def _job_fields(job_line: str, label: str, context: list[str], labels: dict) -> tuple[str | None, str | None]:
    """Employer and designation of one job: its own labels first, then the parts of its own line, then the short
    heading-like lines around it (never a description bullet)."""
    employer = labels.get("employer")
    designation = labels.get("designation")
    candidates = [(q, 0) for p in _split_parts(label) for q in _split_glued(p)]
    for dist, line in context:
        if _BULLET.match(line) or len(line.split()) > 9 or line.rstrip().endswith(".") and len(line.split()) > 4:
            continue
        candidates += [(p, dist) for p in _split_parts(line)]
    best_e = best_d = None
    for part, dist in candidates:
        if len(part) < 2 or len(part.split()) > 8:
            continue
        e, d = _kind(part)
        if e and (best_e is None or (e, -dist) > (best_e[1], -best_e[2])):
            best_e = (part, e, dist)
        if d and (best_d is None or (d, -dist) > (best_d[1], -best_d[2])):
            best_d = (part, d, dist)
    if best_e and best_d and best_e[0] == best_d[0]:
        best_d = None if best_e[1] >= best_d[1] else best_d
        best_e = best_e if best_d is None else None
    employer = employer or (best_e[0] if best_e else None)
    designation = designation or (best_d[0] if best_d else None)
    return _tidy(employer), _tidy(designation)


def _split_glued(part: str) -> list[str]:
    """
    An employer and a designation GLUED in one cell (a photographed table row loses its gaps, 2026-10-19:
    "Mahindra Finance Sales Executive"): cut after the LAST company marker / known employer when what follows holds a
    designation word and no company marker -> ["Mahindra Finance", "Sales Executive"]. Otherwise the part is kept
    whole ("Tech Mahindra", "Branch Manager").
    """
    c = _compiled()
    marks = [m.end() for m in c["company"].finditer(part)] + [m.end() for m in c["known_employer"].finditer(part)]
    if not marks:
        return [part]
    cut = max(marks)
    head, tail = part[:cut].strip(" ,-–"), part[cut:].strip(" ,-–")
    # the head must NAME a company: a known employer, or a word that is not itself a marker / role word -- "Software
    # Engineer" (a real resume, 2026-10-19) and "Bank Manager" are roles, never "Software" + "Engineer"
    named = bool(c["known_employer"].search(head)) or any(
        not c["company"].fullmatch(w) and not c["designation"].fullmatch(w)
        for w in re.findall(r"[A-Za-z][A-Za-z.&'-]*", head))
    # what follows names a ROLE: it holds a designation word ("Customer Service Executive", "Customer Relationship
    # Officer") and no company marker
    if named and head and tail and c["designation"].search(tail) and not c["company"].search(tail) \
            and not c["known_employer"].search(tail):
        return [head, tail]
    return [part]


def _tidy(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", mask(value)).strip(" ,.-–|:")
    if value.count("(") > value.count(")"):
        value += ")"                     # "Treehouse Group (Blackstone Affiliate)": the part split took its ")"
    return value[:80] or None


def _employer_header(line: str) -> str | None:
    """An employer HEADER line above its roles: "Morgan Stanley   Los Angeles, CA", "HDFC Bank Ltd   Mumbai" -- an
    organisation, a gap, a place; no date, not a bullet, not a role (2026-10-19, a real resume)."""
    c = _compiled()
    m = re.match(r"^\s*(?P<org>\S.*?\S)\s{3,}(?P<loc>[A-Za-z][A-Za-z .'-]*(?:,\s*[A-Za-z][A-Za-z .'-]*)?)\s*$", line)
    if not m or _BULLET.match(line) or RANGE.search(line.upper()) or YEAR.search(line):
        return None
    loc = m.group("loc").strip()
    if "," not in loc and not c["cities"].fullmatch(loc) and not c["states"].fullmatch(loc):
        return None
    org = m.group("org").strip()
    emp, des = _kind(org)
    if des > emp or len(org.split()) > 8:
        return None
    return org


def _jobs(exp_lines: list[str]) -> list[Job]:
    c = _compiled()
    hits = []
    for i, line in enumerate(exp_lines):
        upper = line.upper()
        found = list(RANGE.finditer(upper)) or list(RANGE_CELLS.finditer(upper)) or list(RANGE_TAIL.finditer(upper))
        for m in found:
            start, end = _point(m.group(1)), _point(m.group(2))
            if start:
                hits.append((i, m, start, end))
        if not found:
            # ONE MONTH, NO RANGE ("Software Developer Intern | Qualyval Pvt. Ltd." + "... | January2026", a real
            # resume, 2026-10-19): a job only when this line or the one above names BOTH a role and a company; it
            # starts and ends that month (never "still there")
            points = list(SINGLE_MONTH.finditer(upper))
            near = " ".join(exp_lines[max(0, i - 1):i + 1])
            # a month FOLLOWED BY A RANGE JOIN is a range whose end OCR garbled ("Aug'12 - Jul*14"): its end is
            # unknown, so no job is made from it -- missing, never a wrong one-month job (held-out, 2026-10-19)
            broken = bool(points) and re.match(r"\s*(?:-|–|—|~|TO\b|TILL\b|UNTIL\b)", upper[points[0].end():])
            if len(points) == 1 and not broken and not _BULLET.match(line) and c["designation"].search(near) \
                    and (c["company"].search(near) or c["known_employer"].search(near)):
                start = _point(points[0].group(1))
                if start:
                    hits.append((i, points[0], start, start))
    jobs = []
    for n, (i, m, start, end) in enumerate(hits):
        line = exp_lines[i]
        label = (line[:m.start()] + " | " + line[m.end():]).strip(" |,-–—:")
        label = re.sub(r"(?i)\b(?:duration|period|tenure|from|dates?|worked from|since)\s*[:\-]?\s*$", "", label)
        prev_i = hits[n - 1][0] if n else -1
        next_i = hits[n + 1][0] if n + 1 < len(hits) else len(exp_lines)
        before = [(i - k, exp_lines[k]) for k in range(max(prev_i + 1, i - 3), i)]
        after = []
        for k in range(i + 1, min(next_i, i + 3)):
            if _employer_header(exp_lines[k]):
                break                   # the NEXT employer's header ("Fabrikam Capital   Pune") is never this job's
            after.append((k - i, exp_lines[k]))
        # labelled employer / designation in this job's own window
        window = exp_lines[max(prev_i + 1, i - 4):min(next_i, i + 4)]
        labels = {}
        for key in ("employer", "designation"):
            for _, value in labelled(window, key):
                labels.setdefault(key, value)
        # the window BEFORE belongs to this job only when the previous job's own lines are not in it
        context = before + after if not labels else after
        employer, designation = _job_fields(line, label, context, labels)
        if not employer:
            # the nearest EMPLOYER HEADER above ("Company   City, State"), shared by the roles listed under it
            employer = next((_tidy(h) for k in range(i - 1, -1, -1) if (h := _employer_header(exp_lines[k]))), None)
        text = mask(label or (exp_lines[i - 1] if i else ""))[:160]
        nft = bool(c["not_full_time"].search(" ".join([label, designation or "", line])))
        jobs.append(Job(start, end, text, nft, employer, designation))
    return jobs


def current_job(jobs: list[Job]) -> Job | None:
    full = [j for j in jobs if not j.internship] or jobs
    if not full:
        return None
    present = [j for j in full if j.end is None]
    if present:
        return max(present, key=lambda j: j.start)
    return max(full, key=lambda j: (j.end or (9999, 12), j.start))


def employment_gaps(jobs: list[Job]) -> list[dict]:
    """Gaps of at least gaps.report_months between full-time jobs (overlaps merged first)."""
    minimum = int(((cfg().get("gaps") or {}).get("report_months")) or 6)
    today = date.today()
    spans = sorted((j.start[0] * 12 + j.start[1] - 1, ((j.end or (today.year, today.month))[0] * 12
                    + (j.end or (today.year, today.month))[1] - 1)) for j in jobs if not j.internship)
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    gaps = []
    for (_, e1), (s2, _) in zip(merged, merged[1:]):
        months = s2 - e1 - 1
        if months >= minimum:
            gaps.append({"from": f"{(e1 + 1) // 12:04d}-{(e1 + 1) % 12 + 1:02d}",
                         "to": f"{(s2 - 1) // 12:04d}-{(s2 - 1) % 12 + 1:02d}", "months": months})
    return gaps


# ------------------------------------------------------------------------------------------------ languages
def _languages(lines: list[str], blocks: dict[str, list[str]]) -> list[str]:
    c = _compiled()
    sources = [v for _, v in labelled(lines, "languages")] + list(blocks.get("languages") or [])
    # a photographed key-value row loses its gap: "Languages Known English, Hindi" -- a line that STARTS with a
    # languages label is read whatever follows it (only language names are taken from it)
    lead = c["labels"].get("languages")
    if lead is not None:
        sources += [s[m.end():] for s in (line.strip() for line in lines) if (m := lead.match(s)) is not None]
    found: list[str] = []
    for text in sources:
        # a STATE is never a language: "Tamil Nadu" in an address is not Tamil (2026-10-19)
        text = c["states"].sub(" ", text)
        for m in c["languages"].finditer(text):
            name = m.group(1).title()
            name = {"Bangla": "Bengali", "Oriya": "Odia"}.get(name, name)
            if name not in found:
                found.append(name)
    return found


# ------------------------------------------------------------------------------------------------ parse
def scrub(lines: list[str]) -> list[str]:
    """Every "label: value" segment whose label we NEVER return (father's name, gender, marital status, religion,
    PAN / Aadhaar / passport / bank numbers) cut out of its line before any field is read; an emptied line dropped.
    So no returned text -- a job's text, an education line, a skill, the address -- can carry them."""
    out = []
    for line in lines:
        segs = list(_compiled()["any_label"].finditer(line))
        if not segs:
            out.append(line)
            continue
        keep, cut = [], 0
        pairs = _segments(line)
        if not any(k == "_never" for k, _ in pairs):
            out.append(line)
            continue
        never = _compiled()["labels"]["_never"]
        for i, m in enumerate(segs):
            end = segs[i + 1].start() if i + 1 < len(segs) else len(line)
            label = re.sub(r"\.?\s*(?:[:：=–—|]|-|\s{2,})\s*$", "", m.group(0)).strip()
            if never.fullmatch(label):
                keep.append(line[cut:m.start()])
                cut = end
        keep.append(line[cut:])
        cleaned = re.sub(r"\s*\|\s*(?:\|\s*)+", " | ", "".join(keep)).strip(" |,;")
        if cleaned:
            out.append(cleaned)
    return out


def parse(lines: list[str], *, today: date | None = None) -> Resume:
    c = _compiled()
    today = today or date.today()
    lines = scrub(lines)
    r = Resume(text_chars=sum(len(x) for x in lines))
    # lines that carry ONLY data we never return are not parsed into any field (father's name, gender, ...)
    never = c["labels"].get("_never")
    text = "\n".join(lines)

    current, blocks = "header", {"header": []}
    for line in lines:
        section = heading_of(line)
        if section:
            current = section
            if section not in r.sections:
                r.sections.append(section)
            blocks.setdefault(section, [])
            continue
        blocks.setdefault(current, []).append(line)

    # the candidate's own contact: never a REFEREE's (their section is left out) nor a line we never return
    referees = set(blocks.get("references") or [])
    own = "\n".join(ln for ln in lines if ln not in referees and not (never and never.match(ln.strip())))
    r.emails = list(dict.fromkeys(m.group(0).lower() for m in EMAIL.finditer(own)))
    r.email = r.emails[0] if r.emails else None
    r.phones = list(dict.fromkeys(filter(None, (_mobile(m.group(0)) for m in PHONE.finditer(own)))))
    r.phone = r.phones[0] if r.phones else None
    top = [m.group(0).lower() for m in EMAIL.finditer("\n".join(lines[:15]))]
    r.email = top[0] if top else r.email               # the candidate's own email is the one at the top
    m = LINKEDIN.search(text)
    r.linkedin = m.group(0) if m else None
    r.name = _name(lines, blocks, r.email)

    # date of birth / age (labelled only; a plausible age)
    for _, value in labelled(lines, "date_of_birth"):
        born = parse_date(value)
        if born and 14 <= _age_on(born, today) <= 75:
            r.date_of_birth, r.age = born.isoformat(), _age_on(born, today)
            break
    if r.age is None:
        for _, value in labelled(lines, "age"):
            m = re.match(r"\s*(\d{2})\b", value)
            if m and 14 <= int(m.group(1)) <= 75:
                r.age = int(m.group(1))
                break
    r.location = _location(lines, blocks.get("header") or lines[:8])

    edu_lines = blocks.get("education") or [x for x in lines if any(p.search(x.upper()) for _, p in c["levels"])
                                            and c["institution"].search(x)]
    seen = set()
    # a "Graduation: 2026" LABEL is the year of the degree nearby, never level evidence of its own (resume.yaml)
    levels_at = [next((n for n, pat in c["levels"] if pat.search(c["graduation_label"].sub(" ", x.upper()))), None)
                 for x in edu_lines]
    # AN INSTITUTION HEADER ABOVE ITS DEGREE (2026-10-19, a real resume): "● B.K. Birla College ... (Aug 2023 - Apr
    # 2026)" over "Bachelor of Science in IT (CGPA 9.8)" -- the header's dates are the degree's, and the header is not a
    # qualification itself ("Sankalp Hindi High School (2020-2021)" over "Secondary School Certificate")
    def _has_year(n: int) -> bool:
        return bool(YEAR.search(edu_lines[n]))

    header_at: list[bool] = []
    for i, x in enumerate(edu_lines):
        # the line above is a degree still WAITING for its year (none of its own, no header above it): this dated
        # line is that degree's tail, never a header for the next one
        waiting = i > 0 and bool(levels_at[i - 1]) and not _has_year(i - 1) and not (i > 1 and header_at[i - 2])
        header_at.append(bool(c["institution"].search(x)) and _has_year(i) and i + 1 < len(edu_lines)
                         and bool(levels_at[i + 1]) and not _has_year(i + 1) and not waiting)
    for i, line in enumerate(edu_lines):
        upper = line.upper()
        level = levels_at[i]
        if not level or c["not_qualification"].search(upper) or header_at[i]:
            continue
        years = [int(y) for y in YEAR.findall(line)]
        if not years and i > 0 and header_at[i - 1]:
            years = [int(y) for y in YEAR.findall(edu_lines[i - 1])]
        for j in range(i + 1, min(i + 3, len(edu_lines))):
            if years or levels_at[j] or header_at[j]:
                break
            years = [int(y) for y in YEAR.findall(edu_lines[j])]
        window = " ".join(edu_lines[i:i + 3])
        year = max(years) if years else None
        if (level, year) in seen:
            continue
        seen.add((level, year))
        r.education.append(Education(level, year, mask(line)[:160], bool(c["pursuing"].search(window))))

    r.experience = _jobs(blocks.get("experience") or [])

    skills_block = " , ".join(blocks.get("skills") or [])
    # a grouped skills row ("Languages: C#, Python", "AI/ML: NumPy") keeps the skill, never its group label
    items = [re.sub(r"^[A-Za-z][A-Za-z/&+ ]{1,24}:\s*(?=\S)", "", _BULLET.sub("", s.strip()).strip())
             for s in re.split(r"[,;|•\n]| {2,}", skills_block)]
    # a leading dot is part of a skill (".NET 8"); only a trailing one is punctuation
    r.skills = [s.strip(" •*-").rstrip(".") for s in items if 1 < len(s.strip(" •*-").rstrip(".")) <= 40][:40]
    r.languages = _languages(lines, blocks)

    cur = labelled(lines, "current_ctc")
    if cur:
        r.current_ctc = parse_ctc(cur[0][1])
    exp = labelled(lines, "expected_ctc")
    if exp:
        r.expected_ctc = parse_ctc(exp[0][1])
    notice = labelled(lines, "notice_period")
    if notice:
        r.notice_period = parse_notice(notice[0][1])
    elif re.search(r"(?i)\b(?:immediate\s+joiner|can\s+join\s+immediately|available\s+to\s+join\s+immediately)\b",
                   text):
        r.notice_period = {"text": "Immediate", "serving": False, "days": 0}
    return r


def highest(education: list[Education]) -> str | None:
    done = [e for e in education if not e.pursuing] or education
    return max(done, key=lambda e: LEVEL_ORDER.get(e.level, 0)).level if done else None


def total_months(jobs: list[Job]) -> int:
    """Months of work, overlapping periods counted once; internships excluded."""
    today = date.today()
    spans = sorted((j.start[0] * 12 + j.start[1], (j.end or (today.year, today.month))[0] * 12
                    + (j.end or (today.year, today.month))[1]) for j in jobs if not j.internship)
    total, cur_s, cur_e = 0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return max(0, total)


def __getattr__(name: str):
    # back-compat for callers that import the heading table / levels (verify.py, tests)
    if name == "HEADINGS":
        return {s: tuple(h) for s, h in (cfg().get("headings") or {}).items()}
    if name == "LEVELS":
        return _compiled()["levels"]
    raise AttributeError(name)


__all__ = ["Education", "Job", "LEVEL_ORDER", "Resume", "cfg", "current_job", "employment_gaps", "heading_of",
           "highest", "labelled", "mask", "parse", "parse_ctc", "parse_date", "parse_notice", "total_months"]
