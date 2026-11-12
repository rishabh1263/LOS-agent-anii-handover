"""
A RESUME'S TEXT, the cheapest honest way (2026-10-16). The FORMAT is read from the bytes, never from the file name
(a "resume.doc" saved by a job portal is often HTML or RTF inside; a phone sends a PDF as .jpg):

    .docx           the document's own XML: body in order, tables (a data row = one line "a | b | c", a layout table
                    = its columns one after the other), text boxes, header and footer
    .doc            Word 97-2003: the compound file's text through its piece table (stdlib only, see _cfb_streams)
    .odt            OpenDocument text (content.xml)
    .rtf            RTF control words stripped, tables kept as rows
    .html / .htm    the text of the page (a portal's "Word" export)
    .txt            decoded (UTF-8 / UTF-16 / Windows-1252)
    .pdf, native    the PDF's own words with their positions -- two-column layouts read column by column -- no OCR
    .pdf, scanned   pages rendered and OCR'd (only the pages with no text layer)
    .jpg/.png/...   OCR (PP-OCRv6, the passbook engine), the same column-aware layout

Returns lines in reading order plus how they were obtained; never logs content.
"""

from __future__ import annotations

import html.parser
import io
import re
import struct
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

MAX_PAGES = 5
# Long side a photographed / scanned page is OCR'd at. MEASURED 2026-10-16 on the held-out photos: 1280 / 1152 /
# 1024 px all 19/19 verdicts and 48/48 fields; P50 1950 / 1831 / 1656 ms. 1152 keeps headroom under 3 s without
# shrinking a real A4 photo's small print as far as 1024.
OCR_MAX_SIDE = 1152
NATIVE_MIN_CHARS = 80
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_ODF_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


@dataclass
class ResumeText:
    lines: list[str]
    source: str                      # docx | doc | odt | rtf | html | txt | native_pdf_text | ocr | ...
    pages: int = 1
    ocr_pages: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class Unreadable(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _clean(lines) -> list[str]:
    """Whitespace normalised -- but a WIDE gap (a tab, a table column, 3+ spaces) is kept as exactly three spaces:
    it is a column boundary the parser reads ("Jan 2019   Present" in a text table)."""
    out = []
    for line in lines:
        line = str(line or "").replace("\t", "   ")
        line = re.sub(r"[\u00a0\u2002\u2003\u2009]", " ", re.sub(r"[\u200b\ufeff]", "", line))
        line = re.sub(r" {3,}", "\x00", line)
        line = re.sub(r" {2}", " ", line).replace("\x00", "   ").strip()
        line = re.sub(r"(?:\s*\|\s*)+$", "", re.sub(r"^(?:\s*\|\s*)+", "", line))       # empty table cells
        line = re.sub(r"(?:\s*\|\s*){2,}", " | ", line)
        if line:
            out.append(line)
    return out


# ============================================================================== format from the bytes
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sniff(content: bytes, filename: str = "") -> str:
    """docx | odt | doc | rtf | pdf | html | txt | image | zip | ole -- what the bytes ARE."""
    head = content[:2048]
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                names = set(z.namelist())
                if "word/document.xml" in names:
                    return "docx"
                if "content.xml" in names and b"opendocument.text" in (z.read("mimetype") if "mimetype" in names
                                                                       else b""):
                    return "odt"
        except zipfile.BadZipFile:
            return "zip"
        return "zip"
    if head.startswith(_OLE):
        return "doc" if _ole_has_word(content) else "ole"
    if head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"{\\rtf"):
        return "rtf"
    if head.find(b"%PDF-") != -1:
        return "pdf"
    low = head.lower().lstrip(b"\xef\xbb\xbf \r\n\t")
    if low.startswith((b"<!doctype html", b"<html", b"<?xml")) and b"<html" in content[:8192].lower() \
            or (b"<html" in low[:600] and b"<body" in content[:20000].lower()):
        return "html"
    if head.startswith((b"\xff\xd8", b"\x89PNG", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")) or head[8:12] == b"WEBP":
        return "image"
    if _looks_like_text(content):
        return "txt"
    return "image"


def _looks_like_text(content: bytes) -> bool:
    sample = content[:4096]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in sample:
        return False
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        text = sample.decode("cp1252", errors="replace")
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    return len(text) > 0 and printable / len(text) > 0.95


# ============================================================================== layout (PDF words, OCR tokens)
@dataclass
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(1.0, self.y1 - self.y0)


def _rows(boxes: list[_Box]) -> list[list[_Box]]:
    """Boxes grouped into visual rows (vertical overlap), each left to right."""
    rows: list[list[_Box]] = []
    for b in sorted(boxes, key=lambda b: (b.cy, b.x0)):
        if rows:
            last = rows[-1]
            cy = sum(x.cy for x in last) / len(last)
            lh = sum(x.h for x in last) / len(last)
            if abs(b.cy - cy) <= 0.5 * min(b.h, lh):
                last.append(b)
                continue
        rows.append([b])
    return [sorted(r, key=lambda b: b.x0) for r in rows]


def _row_text(row: list[_Box]) -> str:
    """One row's text; a gap wider than a word space (a tab stop, a table cell boundary) is kept as three spaces --
    the column boundary the parser reads. A word space is ~0.25 x the text height; a Word table's cells, fitted to
    their content, sit ~1-1.5 x apart (measured 2026-10-16: at 2.5 x, "Tata Capital Ltd Collection Officer" ran
    together)."""
    parts, prev = [], None
    for b in row:
        if prev is not None:
            gap = b.x0 - prev.x1
            parts.append("   " if gap > max(0.8 * max(prev.h, b.h), 6.0) else " ")
        parts.append(b.text)
        prev = b
    return "".join(parts)


def _column_split(boxes: list[_Box], width: float, height: float) -> float | None:
    """THE x WHERE A TWO-COLUMN PAGE SPLITS, or None. A vertical band that (almost) no box crosses below the header,
    with real text on both sides over most of the page. A single-column resume with right-aligned dates is NOT split:
    its description lines run across the band (measured on the synthetic and the real sample resumes)."""
    if len(boxes) < 30:
        return None
    # measured on the CONTENT, not the page: a short resume fills only the top third of an A4 page
    top, bottom = min(b.y0 for b in boxes), max(b.y1 for b in boxes)
    x_min, x_max = min(b.x0 for b in boxes), max(b.x1 for b in boxes)
    height, width = max(1.0, bottom - top), max(1.0, x_max - x_min)
    body = [b for b in boxes if b.y0 > top + 0.12 * height]
    if len(body) < 25:
        return None
    best = None
    for frac in [i / 100 for i in range(22, 79)]:
        x = x_min + frac * width
        crossing = sum(1 for b in body if b.x0 < x - 2 and b.x1 > x + 2)
        left = [b for b in body if b.x1 <= x + 2]
        right = [b for b in body if b.x0 >= x - 2]
        if not left or not right:
            continue
        if crossing / len(body) > 0.02 or min(len(left), len(right)) < 0.18 * len(body):
            continue
        span_l = max(b.y1 for b in left) - min(b.y0 for b in left)
        span_r = max(b.y1 for b in right) - min(b.y0 for b in right)
        width_l = max(b.x1 for b in left) - min(b.x0 for b in left)
        width_r = max(b.x1 for b in right) - min(b.x0 for b in right)
        if min(span_l, span_r) < 0.45 * height or min(width_l, width_r) < 0.18 * width:
            continue
        score = crossing / len(body)
        if best is None or score < best[0]:
            best = (score, x)
    return best[1] if best else None


def _glued_headings(lines: list[str]) -> int:
    """Lines read straight across in which a KNOWN section heading (config resume.yaml) sits beside other text
    across a wide gap -- "SKILLS   EXPERIENCE", "CONTACT   RAHUL SHARMA": the signature of two columns read as one.
    A table never does this: its headings are rows of their own (2026-10-16, "CBSE" / "Languages Known" table
    cells made a heading count split label | value tables)."""
    from app.agents.resume.parse import heading_of

    glued = 0
    for line in lines:
        parts = [p for p in re.split(r"\s{3,}", line.strip()) if p]
        if len(parts) >= 2 and any(heading_of(p) for p in parts):
            glued += 1
    return glued


def layout_lines(boxes: list[_Box], width: float, height: float) -> list[str]:
    """Boxes -> lines in reading order. A page that splits into two columns is read column by column (the header
    first, then the column holding the largest text -- the name -- then the other) ONLY when reading it straight
    across glues section headings to the other column (two or more such lines); a label | value table or
    right-aligned dates are read straight across, as before."""
    boxes = [b for b in boxes if b.text.strip()]
    if not boxes:
        return []
    across = [_row_text(r) for r in _rows(boxes)]
    if _glued_headings(across) < 1:
        return across
    split = _column_split(boxes, width, height)
    if split is None:
        return across
    left, right = _by_columns(boxes, split, height, sides=True)
    # BOTH columns carry sections of their own (sidebar: CONTACT / SKILLS; main: EXPERIENCE / EDUCATION). A label |
    # value table splits too, but its value side never holds a heading (2026-10-16).
    from app.agents.resume.parse import heading_of

    if not (any(heading_of(x) for x in left) and any(heading_of(x) for x in right)):
        return across
    return _by_columns(boxes, split, height)


def _by_columns(boxes: list[_Box], split: float, height: float, sides: bool = False):
    """The header, then each column top to bottom (the column with the largest text -- the name -- first).
    sides=True: (left column lines, right column lines) only, header excluded."""
    top = min(b.y0 for b in boxes)
    height = max(1.0, max(b.y1 for b in boxes) - top)
    columns_from = min((b.y0 for b in boxes if b.y0 > top + 0.12 * height
                        and not (b.x0 < split - 2 and b.x1 > split + 2)), default=top)
    header = [b for b in boxes if b.x0 < split - 2 and b.x1 > split + 2 and b.y1 <= columns_from + 1]
    rest = [b for b in boxes if b not in header]
    left = [b for b in rest if (b.x0 + b.x1) / 2 < split]
    right = [b for b in rest if (b.x0 + b.x1) / 2 >= split]
    if sides:
        return [_row_text(r) for r in _rows(left)], [_row_text(r) for r in _rows(right)]
    cols = [left, right]
    if right and left and max(b.h for b in right) > max(b.h for b in left) * 1.15:
        cols = [right, left]
    out = [_row_text(r) for r in _rows(header)]
    for col in cols:
        out += [_row_text(r) for r in _rows(col)]
    return out


# ============================================================================== .docx
def _docx_paragraph(p) -> str:
    parts = []
    for node in p.iter():
        tag = node.tag
        if tag == f"{_MC}Fallback":
            continue
        if tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif tag in (f"{_W}tab", f"{_W}ptab"):
            parts.append("   ")
        elif tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
        elif tag == f"{_W}noBreakHyphen":
            parts.append("-")
    return "".join(parts)


def _strip_fallbacks(root) -> None:
    """mc:Fallback holds a SECOND COPY of every text box (old VML): read once, not twice."""
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag == f"{_MC}Fallback":
                parent.remove(child)


def _docx_block(node, out: list[str]) -> None:
    """Body content in document order: paragraphs (with any text box inside them read after the paragraph), tables,
    content controls."""
    for child in node:
        tag = child.tag
        if tag == f"{_W}p":
            boxes = [p for box in child.iter(f"{_W}txbxContent") for p in box.iter(f"{_W}p")]
            inner = {id(x) for x in boxes}
            own = [n for n in child.iter() if id(n) not in inner]
            text = "".join((n.text or "") if n.tag == f"{_W}t" else "   " if n.tag in (f"{_W}tab", f"{_W}ptab")
                           else "\n" if n.tag in (f"{_W}br", f"{_W}cr") else "-" if n.tag == f"{_W}noBreakHyphen"
                           else "" for n in own)
            out.extend(text.split("\n"))
            for p in boxes:
                out.extend(_docx_paragraph(p).split("\n"))
        elif tag == f"{_W}tbl":
            _docx_table(child, out)
        elif tag in (f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml", f"{_W}smartTag"):
            _docx_block(child, out)


def _docx_table(tbl, out: list[str]) -> None:
    """A DATA table (short cells: Qualification | Board | Year) -> one line per row, cells joined by " | ".
    A LAYOUT table (a two-column resume built from a table: long cells) -> each column's text, column by column."""
    rows = [r for r in tbl if r.tag == f"{_W}tr"]
    grid: list[list[list[str]]] = []
    for r in rows:
        cells = []
        for c in r:
            if c.tag != f"{_W}tc":
                continue
            lines: list[str] = []
            _docx_block(c, lines)
            cells.append([x for x in (ln.strip() for ln in lines) if x])
        grid.append(cells)
    long_cells = sum(1 for row in grid for cell in row if len(cell) > 3)
    if long_cells:
        width = max((len(row) for row in grid), default=0)
        for col in range(width):
            for row in grid:
                if col < len(row):
                    out.extend(row[col])
        return
    for row in grid:
        out.append(" | ".join(" ".join(cell) for cell in row if cell))


def read_docx(content: bytes) -> ResumeText:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = z.namelist()
            if "word/document.xml" not in names:
                raise Unreadable("CORRUPT_FILE", "The Word file has no document body.")
            if z.getinfo("word/document.xml").file_size > 20 * 2**20:
                raise Unreadable("FILE_TOO_LARGE", "The Word document body is too large.")
            root = ElementTree.fromstring(z.read("word/document.xml"))
            parts = {n: ElementTree.fromstring(z.read(n)) for n in sorted(names)
                     if re.fullmatch(r"word/(header|footer)\d*\.xml", n) and z.getinfo(n).file_size < 2 * 2**20}
    except Unreadable:
        raise
    except Exception as exc:  # noqa: BLE001 - zip / XML errors: not a readable Word file
        raise Unreadable("CORRUPT_FILE", "The Word file could not be opened.") from exc
    _strip_fallbacks(root)
    lines: list[str] = []
    for name, part in parts.items():
        if "header" in name:
            _strip_fallbacks(part)
            _docx_block(part, lines)
    body = root.find(f"{_W}body")
    _docx_block(body if body is not None else root, lines)
    for name, part in parts.items():
        if "footer" in name:
            _strip_fallbacks(part)
            _docx_block(part, lines)
    return ResumeText(_dedupe_repeats(_clean(lines)), "docx")


def _dedupe_repeats(lines: list[str]) -> list[str]:
    """A line repeated straight after itself (a header copied into the body) is kept once."""
    out: list[str] = []
    for line in lines:
        if not (out and out[-1] == line):
            out.append(line)
    return out


# ============================================================================== .odt
def read_odt(content: bytes) -> ResumeText:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if z.getinfo("content.xml").file_size > 20 * 2**20:
                raise Unreadable("FILE_TOO_LARGE", "The document body is too large.")
            root = ElementTree.fromstring(z.read("content.xml"))
    except Unreadable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Unreadable("CORRUPT_FILE", "The OpenDocument file could not be opened.") from exc

    def para(node) -> str:
        parts = [node.text or ""]
        for child in node:
            if child.tag == f"{_ODF_TEXT}s":
                parts.append(" " * int(child.get(f"{_ODF_TEXT}c", "1")))
            elif child.tag == f"{_ODF_TEXT}tab":
                parts.append("   ")
            elif child.tag == f"{_ODF_TEXT}line-break":
                parts.append("\n")
            else:
                parts.append(para(child))
            parts.append(child.tail or "")
        return "".join(parts)

    lines: list[str] = []

    def walk(node) -> None:
        for child in node:
            if child.tag in (f"{_ODF_TEXT}p", f"{_ODF_TEXT}h"):
                lines.extend(para(child).split("\n"))
            elif child.tag == f"{_ODF_TABLE}table-row":
                cells = []
                for c in child:
                    if c.tag == f"{_ODF_TABLE}table-cell":
                        sub: list[str] = []
                        for p in c.iter():
                            if p.tag in (f"{_ODF_TEXT}p", f"{_ODF_TEXT}h"):
                                sub.append(para(p))
                        cells.append(sub)
                if any(len(c) > 3 for c in cells):
                    for c in cells:
                        lines.extend(c)
                else:
                    lines.append(" | ".join(" ".join(c) for c in cells if c))
            else:
                walk(child)

    walk(root)
    return ResumeText(_clean(lines), "odt")


# ============================================================================== .doc (Word 97-2003)
def _cfb_streams(content: bytes, wanted: set[str]) -> dict[str, bytes]:
    """The named streams of an OLE compound file (MS-CFB), standard library only. Bounded: a malformed or hostile
    file raises Unreadable, never loops."""
    if len(content) < 512 or not content.startswith(_OLE):
        raise Unreadable("CORRUPT_FILE", "Not a Word 97-2003 file.")
    sector_shift, mini_shift = struct.unpack_from("<HH", content, 30)
    if sector_shift not in (9, 12) or mini_shift != 6:
        raise Unreadable("CORRUPT_FILE", "The Word file's structure is not valid.")
    ss, mss = 1 << sector_shift, 1 << mini_shift
    n_fat, dir_start, _, mini_cutoff, minifat_start, n_minifat, difat_start, n_difat = struct.unpack_from(
        "<IIIIIIII", content, 44)
    n_sectors = (len(content) - ss) // ss + 1

    def sector(i: int) -> bytes:
        off = (i + 1) * ss
        if i < 0 or off + ss > len(content) + ss:
            raise Unreadable("CORRUPT_FILE", "The Word file is truncated.")
        return content[off:off + ss]

    difat = list(struct.unpack_from("<109I", content, 76))
    nxt, guard = difat_start, 0
    while nxt < 0xFFFFFFFA and guard < n_difat + 1 and guard < 10000:
        block = sector(nxt)
        vals = struct.unpack(f"<{ss // 4}I", block)
        difat += list(vals[:-1])
        nxt, guard = vals[-1], guard + 1
    fat: list[int] = []
    for s in difat[:n_fat]:
        if s >= 0xFFFFFFFA:
            break
        fat += struct.unpack(f"<{ss // 4}I", sector(s))

    def chain(start: int, table: list[int], limit: int) -> list[int]:
        out, cur = [], start
        while cur < 0xFFFFFFFA:
            if cur >= len(table) or len(out) > limit:
                raise Unreadable("CORRUPT_FILE", "The Word file's sector chain is broken.")
            out.append(cur)
            cur = table[cur]
        return out

    def read_chain(start: int) -> bytes:
        return b"".join(sector(s) for s in chain(start, fat, n_sectors))

    directory = read_chain(dir_start)
    entries = []
    for off in range(0, len(directory) - 127, 128):
        raw = directory[off:off + 128]
        name_len = struct.unpack_from("<H", raw, 64)[0]
        name = raw[:max(0, name_len - 2)].decode("utf-16-le", errors="ignore")
        etype = raw[66]
        start, size = struct.unpack_from("<II", raw, 116)
        entries.append((name, etype, start, size))
    root = next((e for e in entries if e[1] == 5), None)
    mini_stream = read_chain(root[2]) if root and root[2] < 0xFFFFFFFA else b""
    minifat: list[int] = []
    if n_minifat and minifat_start < 0xFFFFFFFA:
        for s in chain(minifat_start, fat, n_sectors):
            minifat += struct.unpack(f"<{ss // 4}I", sector(s))
    out = {}
    for name, etype, start, size in entries:
        if etype != 2 or name not in wanted:
            continue
        if size > 64 * 2**20:
            raise Unreadable("FILE_TOO_LARGE", "The Word file is too large.")
        if size < mini_cutoff:
            data = b"".join(mini_stream[s * mss:(s + 1) * mss] for s in chain(start, minifat, len(minifat)))
        else:
            data = read_chain(start)
        out[name] = data[:size]
    return out


def _ole_names(content: bytes) -> set[str]:
    try:
        return set(_cfb_streams(content, {"WordDocument", "Macros", "_VBA_PROJECT", "VBA"}))
    except Exception:  # noqa: BLE001
        return set()


def _ole_has_word(content: bytes) -> bool:
    return "WordDocument" in _ole_names(content)


def ole_has_macros(content: bytes) -> bool:
    """A Word 97 file carrying a VBA project (a "Macros" storage / "_VBA_PROJECT" stream)."""
    return b"_\x00V\x00B\x00A\x00_\x00P\x00R\x00O\x00J\x00E\x00C\x00T" in content or \
        b"M\x00a\x00c\x00r\x00o\x00s\x00\x00\x00" in content


def read_doc(content: bytes) -> ResumeText:
    """Word 97-2003 text through the piece table (MS-DOC: FIB -> Clx in the table stream -> PlcPcd)."""
    streams = _cfb_streams(content, {"WordDocument", "0Table", "1Table"})
    word = streams.get("WordDocument")
    if not word or len(word) < 0x1AA or struct.unpack_from("<H", word, 0)[0] != 0xA5EC:
        raise Unreadable("CORRUPT_FILE", "The Word 97-2003 file has no readable document.")
    flags = struct.unpack_from("<H", word, 0x0A)[0]
    if flags & 0x0100:
        raise Unreadable("PDF_ENCRYPTED", "The Word file is password-protected.")
    table = streams.get("1Table" if flags & 0x0200 else "0Table")
    fc_clx, lcb_clx = struct.unpack_from("<II", word, 0x01A2)
    if not table or lcb_clx == 0 or fc_clx + lcb_clx > len(table):
        raise Unreadable("CORRUPT_FILE", "The Word 97-2003 file's text table is missing.")
    clx = table[fc_clx:fc_clx + lcb_clx]
    pos = 0
    while pos < len(clx) and clx[pos] == 0x01:                     # Prc (formatting) entries: skipped
        pos += 3 + struct.unpack_from("<H", clx, pos + 1)[0]
    if pos >= len(clx) or clx[pos] != 0x02:
        raise Unreadable("CORRUPT_FILE", "The Word 97-2003 file's piece table is missing.")
    lcb = struct.unpack_from("<I", clx, pos + 1)[0]
    plc = clx[pos + 5:pos + 5 + lcb]
    n = (len(plc) - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    text = []
    for i in range(n):
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)[0]
        count = cps[i + 1] - cps[i]
        if count <= 0 or count > 5 * 2**20:
            continue
        if fc & 0x40000000:
            off = (fc & 0x3FFFFFFF) // 2
            text.append(word[off:off + count].decode("cp1252", errors="replace"))
        else:
            text.append(word[fc:fc + 2 * count].decode("utf-16-le", errors="replace"))
    raw = "".join(text)
    # main document only (FIB ccpText), field codes removed (keep the field RESULT), control marks as layout
    ccp_text = struct.unpack_from("<i", word, 0x004C)[0]
    if 0 < ccp_text <= len(raw):
        raw = raw[:ccp_text]
    raw = re.sub(r"\x13[^\x13\x14\x15]*\x14", "", raw)
    raw = re.sub(r"\x13[^\x13\x14\x15]*\x15", "", raw).replace("\x15", "")
    raw = raw.replace("\x07\r", "\n").replace("\x07", " | ").replace("\x0b", "\n").replace("\x0c", "\n")
    raw = raw.replace("\r", "\n").replace("\x1e", "-").replace("\x1f", "")
    raw = re.sub(r"[\x00-\x08\x0e-\x1d]", "", raw)
    return ResumeText(_clean(raw.split("\n")), "doc")


# ============================================================================== .rtf
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "objdata", "themedata", "colorschememapping",
             "latentstyles", "datastore", "xmlnstbl", "listtable", "listoverridetable", "rsidtbl", "generator",
             "fldinst", "filetbl", "revtbl", "pgdsctbl", "mmathPr", "wgrffmtfilter", "bkmkstart", "bkmkend",
             "datafield", "shpinst", "nonshppict", "blipuid", "falt", "panose", "fname"}


def read_rtf(content: bytes) -> ResumeText:
    data = content.decode("latin-1")
    m = re.search(r"\\ansicpg(\d+)", data[:2000])
    codepage = f"cp{m.group(1)}" if m else "cp1252"
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip, uc, pending_skip = False, 1, 0
    i, n = 0, len(data)
    token = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+")
    hexbuf = bytearray()

    def flush_hex():
        if hexbuf:
            if not skip:
                out.append(hexbuf.decode(codepage, errors="replace"))
            hexbuf.clear()

    while i < n:
        t = token.match(data, i)
        if not t:
            i += 1
            continue
        i = t.end()
        word, arg, hexch, sym, brace, txt = t.groups()
        if hexch is not None:
            if pending_skip:
                pending_skip -= 1
                continue
            hexbuf.append(int(hexch, 16))
            continue
        flush_hex()
        if brace == "{":
            stack.append((skip, uc))
        elif brace == "}":
            skip, uc = stack.pop() if stack else (False, 1)
        elif word is not None:
            if pending_skip:
                pending_skip -= 1
                continue
            if word in _RTF_SKIP:
                skip = True
            elif word == "uc":
                uc = int(arg or 1)
            elif word == "u" and not skip:
                code = int(arg or 0)
                out.append(chr(code + 65536 if code < 0 else code))
                pending_skip = uc
            elif skip:
                continue
            elif word in ("par", "line", "row", "sect", "page"):
                out.append("\n")
            elif word == "cell":
                out.append(" | ")
            elif word == "tab":
                out.append("   ")
            elif word in ("emdash", "endash"):
                out.append("-")
            elif word in ("bullet",):
                out.append("• ")
            elif word in ("lquote", "rquote"):
                out.append("'")
            elif word in ("ldblquote", "rdblquote"):
                out.append('"')
        elif sym is not None:
            if sym == "*":
                skip = True
            elif sym in "\\{}" and not skip:
                out.append(sym)
            elif sym == "~" and not skip:
                out.append(" ")
            elif sym == "-":
                pass
        elif txt is not None and not skip:
            if pending_skip:
                drop = min(pending_skip, len(txt))
                txt, pending_skip = txt[drop:], pending_skip - drop
            out.append(txt)
    flush_hex()
    return ResumeText(_clean("".join(out).split("\n")), "rtf")


# ============================================================================== .html
class _HTMLText(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article",
             "header", "footer", "ul", "ol", "dl", "dt", "dd", "hr", "pre", "blockquote", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "noscript", "template"):
            self.skip += 1
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "noscript", "template"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def _decode(content: bytes) -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", errors="replace")
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16", errors="replace")
    m = re.search(rb"charset=[\"']?([A-Za-z0-9_-]+)", content[:4096])
    for enc in ([m.group(1).decode("ascii", "ignore")] if m else []) + ["utf-8", "cp1252"]:
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("latin-1")


def read_html(content: bytes) -> ResumeText:
    p = _HTMLText()
    try:
        p.feed(_decode(content))
        p.close()
    except Exception as exc:  # noqa: BLE001
        raise Unreadable("CORRUPT_FILE", "The page could not be read.") from exc
    return ResumeText(_clean("".join(p.parts).split("\n")), "html")


def read_txt(content: bytes) -> ResumeText:
    return ResumeText(_clean(_decode(content).replace("\r\n", "\n").replace("\r", "\n").split("\n")), "txt")


# ============================================================================== OCR, PDF
def _ocr_image(image) -> list[str]:
    from app.agents.passbook import engine

    scale = OCR_MAX_SIDE / max(image.size)
    if scale < 1:
        from PIL import Image

        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.BILINEAR)
    tokens, _info = engine.read(image.convert("RGB"))
    import unicodedata

    boxes = [_Box(t.x0, t.y0, t.x1, t.y1, unicodedata.normalize("NFKC", t.text or "")) for t in tokens]
    return layout_lines(boxes, float(image.width), float(image.height))


def _pdf_page_lines(page) -> list[str]:
    words = page.get_text("words") or []
    boxes = [_Box(w[0], w[1], w[2], w[3], w[4]) for w in words]
    return layout_lines(boxes, float(page.rect.width), float(page.rect.height))


def read_pdf(content: bytes) -> ResumeText:
    import pymupdf
    from PIL import Image

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise Unreadable("CORRUPT_FILE", "The PDF could not be opened.") from exc
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise Unreadable("PDF_ENCRYPTED", "The PDF is password-protected.")
        if doc.page_count == 0:
            raise Unreadable("CORRUPT_FILE", "The PDF has no pages.")
        pages = min(doc.page_count, MAX_PAGES)
        lines: list[str] = []
        ocr_pages = 0
        for number in range(pages):
            page = doc[number]
            text = page.get_text("text") or ""
            if len(re.sub(r"\s", "", text)) >= NATIVE_MIN_CHARS:
                lines.extend(_pdf_page_lines(page))
                continue
            pix = page.get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72), alpha=False)
            lines.extend(_ocr_image(Image.frombytes("RGB", (pix.width, pix.height), pix.samples)))
            ocr_pages += 1
        notes = [f"only the first {MAX_PAGES} of {doc.page_count} pages were read"] if doc.page_count > MAX_PAGES else []
        lines.extend(_pdf_link_lines(doc, pages, "\n".join(lines)))
    source = "native_pdf_text" if ocr_pages == 0 else ("ocr" if ocr_pages == pages else "native_pdf_text+ocr")
    return ResumeText(_clean(lines), source, pages, ocr_pages, notes)


def _pdf_link_lines(doc, pages: int, text: str) -> list[str]:
    """
    CONTACT BEHIND A LINK (2026-10-19, a real resume: the page shows only "Email | LinkedIn | GitHub", the addresses
    are the links' targets): the e-mail of a mailto: link and a LinkedIn profile URL, as lines -- only when the page
    text does not already carry them. Nothing else a link points to is read.
    """
    found: list[str] = []
    for number in range(pages):
        try:
            links = doc[number].get_links() or []
        except Exception:  # noqa: BLE001 - a broken link table costs the links, never the resume
            continue
        for link in links:
            uri = str(link.get("uri") or "").strip()
            if uri.lower().startswith("mailto:"):
                value = uri[7:].split("?")[0].strip()
            elif re.search(r"(?i)linkedin\.com/in/", uri):
                value = uri
            else:
                continue
            if value and value.lower() not in text.lower() and value not in found:
                found.append(value)
    return found


def read_image(content: bytes) -> ResumeText:
    from PIL import Image, ImageOps

    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise Unreadable("CORRUPT_IMAGE", "The image could not be decoded.") from exc
    if min(image.size) < 300:
        raise Unreadable("IMAGE_TOO_SMALL", "The image is too small to read a resume from.")
    return ResumeText(_clean(_ocr_image(image)), "ocr", 1, 1)


_READERS = {"docx": read_docx, "odt": read_odt, "doc": read_doc, "rtf": read_rtf, "html": read_html, "txt": read_txt,
            "pdf": read_pdf, "image": read_image}


def read(content: bytes, filename: str) -> ResumeText:
    kind = sniff(content, filename)
    reader = _READERS.get(kind)
    if reader is None:
        raise Unreadable("UNSUPPORTED_FILE_TYPE", "The file is not a document this service reads.")
    return reader(content)


__all__ = ["MAX_PAGES", "ResumeText", "Unreadable", "layout_lines", "ole_has_macros", "read", "read_doc", "read_docx",
           "read_html", "read_image", "read_odt", "read_pdf", "read_rtf", "read_txt", "sniff"]
