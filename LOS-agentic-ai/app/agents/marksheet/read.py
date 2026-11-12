"""
READING A MARKSHEET'S TEXT (config app/config/marksheet.yaml):

    other_document  a phrase that belongs to another document (PAN, passbook, degree ...) -> its type
    education       weighted marksheet words, summed -> is this a school board's marksheet / certificate?
    school_class    12TH / 10TH from the most specific phrases; a board's own name ("Central Board of Secondary
                    Education") is removed first so it never reads as "secondary" = 10th; the top of the page decides
                    when the two disagree (a 12th marksheet may mention the 10th exam it follows)
    board           reported when found, never decides
    candidate_name  the value right of (or under) a name label; a father's / mother's / school's name never counts
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

@dataclass
class Row:
    tokens: list
    text: str
    top: float
    bottom: float
    confidence: float


def _nfkc(token):
    """Full-width punctuation the recogniser emits ("：", "，") as ASCII, on a copy of the token."""
    text = unicodedata.normalize("NFKC", token.text or "")
    return token if text == token.text else token.model_copy(update={"text": text})


def rows_of(tokens: list) -> list[Row]:
    """Tokens grouped into visual rows (vertical overlap), each left to right -- the passbook reader's rule, kept here
    so the marksheet service stands on its own (no bank / IFSC code)."""
    rows: list[list] = []
    for t in sorted((_nfkc(t) for t in tokens), key=lambda t: (t.cy, t.x0)):
        h = max(1.0, t.y1 - t.y0)
        if rows:
            last = rows[-1]
            cy = sum(x.cy for x in last) / len(last)
            lh = max(1.0, sum(x.y1 - x.y0 for x in last) / len(last))
            if abs(t.cy - cy) <= 0.5 * min(h, lh):
                last.append(t)
                continue
        rows.append([t])
    out = []
    for r in rows:
        r.sort(key=lambda t: t.x0)
        out.append(Row(r, " ".join(t.text for t in r), min(t.y0 for t in r), max(t.y1 for t in r),
                       min(float(t.confidence or 0) for t in r)))
    return out


def _upper(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "").upper()).strip()


def _has(text: str, phrase: str) -> bool:
    """`phrase` in `text` as whole words ("CLASS X" is not inside "CLASS XII")."""
    return re.search(rf"(?<![A-Z0-9]){re.escape(phrase.upper())}(?![A-Z0-9])", text) is not None


def other_document(text: str, table: dict[str, str]) -> str | None:
    up = _upper(text)
    return next((kind for phrase, kind in table.items() if _has(up, phrase)), None)


def education_score(text: str, markers: dict[str, float]) -> float:
    up = _upper(text)
    return round(min(1.0, sum(float(w) for phrase, w in markers.items() if _has(up, phrase))), 2)


def _class_of(text: str, cfg: dict) -> tuple[int, int]:
    up = _upper(text)
    for noise in sorted(cfg["board_name_noise"], key=len, reverse=True):
        up = up.replace(noise.upper(), " ")
    twelve = sum(_has(up, p) for p in cfg["class_12"])
    ten = sum(_has(up, p) for p in cfg["class_10"])
    return twelve, ten


def school_class(rows: list[Row], cfg: dict) -> str | None:
    """12TH / 10TH, or None when the page does not say (or says both equally)."""
    if not rows:
        return None
    bottom = max(r.bottom for r in rows) or 1.0
    head = " ".join(r.text for r in rows if r.top <= 0.4 * bottom)
    # the exam's own title line ("SECONDARY SCHOOL CERTIFICATE EXAMINATION 2019") settles a tie
    title = " ".join(r.text for r in rows if re.search(r"EXAM", _upper(r.text)))
    for text in (title, head, " ".join(r.text for r in rows)):
        twelve, ten = _class_of(text, cfg)
        if twelve > ten:
            return "12TH"
        if ten > twelve:
            return "10TH"
    # nothing read exactly: the title / header with an OCR slip ("SECONDARY SCH0OL CERTIFLCATE") -- long phrases only
    fuzzy = cfg.get("class_fuzzy") or {}
    min_letters, ratio = int(fuzzy.get("min_letters", 10**6)), float(fuzzy.get("ratio", 1.0))
    for text in (title, head):
        up = _upper(text)
        for noise in sorted(cfg["board_name_noise"], key=len, reverse=True):
            up = up.replace(noise.upper(), " ")
        hits = {key: sum(_fuzzy_label(up, p, ratio) is not None for p in cfg[key]
                         if len(re.sub(r"[^A-Z]", "", p.upper())) >= min_letters) for key in ("class_12", "class_10")}
        if hits["class_12"] > hits["class_10"]:
            return "12TH"
        if hits["class_10"] > hits["class_12"]:
            return "10TH"
    return None


def board(text: str, table: dict[str, str]) -> str | None:
    up = _upper(text)
    return next((code for phrase, code in sorted(table.items(), key=lambda kv: -len(kv[0])) if _has(up, phrase)),
                None)


# ---- the candidate's name -------------------------------------------------------------------------------------------
@dataclass
class Name:
    value: str | None
    confidence: float
    status: str                        # FOUND | LOW_CONFIDENCE | NOT_FOUND


def _clean(value: str, spec: dict) -> str | None:
    """The name part of a value: letters, spaces, dots, apostrophes; cut at the first stop word; a few words."""
    words = []
    stop = {str(w).upper() for w in spec["stop_words"]} | {str(w).upper() for w in spec["not_the_candidate"]}
    value = re.sub(r"\([^)]*\)", " ", value)            # "(SURNAME FIRST)" is a hint, not the name
    value = re.sub(r"[()\[\]]", " ", value).strip(" :-.|,;")   # a bracket OCR broke ("(SURNAME FIRST PATIL")
    hints = sorted((str(h).upper() for h in spec.get("hints") or []), key=len, reverse=True)
    while True:                                          # the label's hint words before the name
        head = next((h for h in hints if re.match(rf"{re.escape(h)}(?![A-Z])", value.upper())), None)
        if head is None:
            break
        value = value[len(head):].strip(" :-.|,;")
    for raw in re.split(r"\s+", value):
        word = raw.strip(" :-|,;")
        if not word:
            continue
        bare = re.sub(r"'S$", "", word.upper().rstrip("."))      # MOTHER'S -> MOTHER
        if bare in stop or re.search(r"\d", word):
            break
        if not re.fullmatch(r"[A-Za-z][A-Za-z.']*", word):
            break
        words.append(word)
        if len(words) >= int(spec["max_words"]):
            break
    name = " ".join(words).strip(" .")
    return name if sum(c.isalpha() for c in name) >= int(spec["min_letters"]) else None


def _fuzzy_label(up: str, label: str, ratio: float) -> tuple[int, int] | None:
    """A long label read with an OCR slip ("THIS IS TO CERTITY THAT", spaces lost): compare letters only, in a
    window the label's length; (start, end) in `up`, or None."""
    from difflib import SequenceMatcher

    want = re.sub(r"[^A-Z]", "", label.upper())
    at = [i for i, c in enumerate(up) if "A" <= c <= "Z"]
    letters = "".join(up[i] for i in at)
    best = None
    for size in (len(want) - 1, len(want), len(want) + 1):
        for s in range(0, max(0, len(letters) - size) + 1):
            score = SequenceMatcher(None, letters[s:s + size], want).ratio()
            if score >= ratio and (best is None or score > best[0]):
                best = (score, at[s], at[s + size - 1] + 1)
    return (best[1], best[2]) if best else None


def _label_at(text: str, spec: dict) -> tuple[int, int] | None:
    """(start, end) of the candidate-name label in a row's text, or None (a parent's / school's name label)."""
    up = _upper(text)
    fuzzy = spec.get("fuzzy_labels") or {}
    for label in sorted(spec["labels"], key=len, reverse=True):
        m = re.search(rf"(?<![A-Z]){re.escape(label.upper())}(?![A-Z])", up)
        span = (m.start(), m.end()) if m else None
        if span is None and len(re.sub(r"[^A-Z]", "", label.upper())) >= int(fuzzy.get("min_letters", 10**6)):
            span = _fuzzy_label(up, label, float(fuzzy.get("ratio", 1.0)))
        if span is None:
            continue
        before = up[:span[0]]
        label_part = up[max(0, span[0] - 20):span[1]]
        if any(_has(label_part, w) or _has(before[-25:], w) for w in spec["not_the_candidate"]):
            return None
        return span
    return None


def candidate_name(rows: list[Row], spec: dict) -> Name:
    for i, row in enumerate(rows):
        at = _label_at(row.text, spec)
        if at is None:
            continue
        # the value right of the label, else the next line under it
        rest = _upper(row.text)[at[1]:]
        name, conf = _clean(rest, spec), row.confidence
        if not name and i + 1 < len(rows):
            below = rows[i + 1]
            # the line under the label, only when it is a bare value: never another label or a parent's line
            close = (below.top - row.bottom) <= 2.5 * max(1.0, row.bottom - row.top)
            other_label = _has(_upper(below.text), "NAME") or any(_has(_upper(below.text), w)
                                                                  for w in spec["not_the_candidate"])
            if close and not other_label:
                name, conf = _clean(_upper(below.text), spec), below.confidence
        if name:
            status = "FOUND" if conf >= float(spec["min_confidence"]) else "LOW_CONFIDENCE"
            return Name(name, round(float(conf), 3), status)
    return Name(None, 0.0, "NOT_FOUND")


__all__ = ["Name", "board", "candidate_name", "education_score", "other_document", "rows_of", "school_class"]
