"""
IMPORT THE DEVIATION MASTER (owner's sheet, 2026-10-12) -> app/config/deviation_master.yaml.

    python scripts/import_deviation_master.py [path/to/DEVIATION_MASTER.ods]

The sheet is the source of truth; nothing in code lists a deviation or an authority. Read with the standard library
(an .ods is a zip of XML). Sheet2 -- "Deviation Description | Category | ACM | RCM | ZCM | HEADCREDIT" with a tick per
authority that may approve -- is the catalogue; Sheet0 (authority / deviation / category / severity rows) is
cross-checked and every disagreement is written into the generated file under `conflicts` for the business to settle.
Each deviation gets a STABLE code from its description (DVM- + 6 hex of sha1; a raised deviation record is DEV-), so re-importing an edited sheet keeps
the codes of unchanged rows.
"""
from __future__ import annotations

import hashlib
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "docs" / "frontend" / "DEVIATION_MASTER.ods"
OUT = ROOT / "app" / "config" / "deviation_master.yaml"
T = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
P = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p"
AUTHORITY_COLUMNS = {"ACM": "ACM", "RCM": "RCM", "ZCM": "ZCM", "HEADCREDIT": "HEAD_CREDIT", "HEAD_CREDIT": "HEAD_CREDIT"}


def _sheets(path: Path) -> dict[str, list[list[str]]]:
    root = ET.fromstring(zipfile.ZipFile(path).read("content.xml"))
    out = {}
    for sheet in root.iter(T + "table"):
        rows = []
        for row in sheet.iter(T + "table-row"):
            cells = []
            for cell in row.findall(T + "table-cell"):
                repeat = int(cell.get(T + "number-columns-repeated", "1"))
                text = " ".join("".join(p.itertext()) for p in cell.findall(P)).strip()
                cells.extend([text] * min(repeat, 20))
            if any(cells):
                rows.append(cells)
        out[sheet.get(T + "name")] = rows
    return out


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


def _category(text: str) -> str:
    return "_".join(_norm(text).upper().split())


def _code(description: str) -> str:
    return "DVM-" + hashlib.sha1(_norm(description).lower().encode("utf-8")).hexdigest()[:6].upper()


def build(path: Path) -> dict:
    sheets = _sheets(path)
    matrix = next(rows for name, rows in sheets.items() if rows and "Deviation Description" in rows[0][0])
    header = [_norm(h).upper().replace(" ", "") for h in matrix[0]]
    authority_at = {i: AUTHORITY_COLUMNS[h] for i, h in enumerate(header) if h in AUTHORITY_COLUMNS}
    deviations, seen = [], set()
    for row in matrix[1:]:
        description = _norm(row[0] if row else "")
        if not description or description.lower() in seen:
            continue
        seen.add(description.lower())
        approvers = [a for i, a in authority_at.items() if i < len(row) and row[i].strip()]
        deviations.append({"code": _code(description), "description": description,
                           "category": _category(row[1] if len(row) > 1 else ""),
                           "approvers": approvers})
    # cross-check against the authority rows (Sheet0): category / authority disagreements are REPORTED, not guessed
    by_desc = {d["description"].lower(): d for d in deviations}
    conflicts, severities = [], {}
    rows0 = next((rows for name, rows in sheets.items() if rows and rows[0] and rows[0][0].upper() == "AUTHORITY"), [])
    for row in rows0[1:]:
        authority = AUTHORITY_COLUMNS.get(_norm(row[0]).upper().replace(" ", ""), _norm(row[0]).upper())
        description = _norm(row[1] if len(row) > 1 else "")
        category = _category(row[2] if len(row) > 2 else "")
        severity = _norm(row[5] if len(row) > 5 else "")
        entry = by_desc.get(description.lower())
        if entry is None:
            conflicts.append({"deviation": description, "issue": "in the authority rows (Sheet0) but not in the matrix"})
            continue
        if severity:
            severities.setdefault(entry["code"], set()).add(severity)
        if category and category != entry["category"]:
            conflicts.append({"deviation": description, "issue": f"category {category} for {authority} in Sheet0, "
                                                                 f"{entry['category']} in the matrix"})
        if authority not in entry["approvers"]:
            conflicts.append({"deviation": description,
                              "issue": f"{authority} listed in Sheet0 but not ticked in the matrix"})
    for d in deviations:
        found = sorted(severities.get(d["code"], set()))
        d["severity"] = found[0] if len(found) == 1 else (found or ["UNSPECIFIED"])[0]
    unique = []
    for c in conflicts:
        if c not in unique:
            unique.append(c)
    return {"deviations": deviations, "conflicts": unique}


def main() -> None:
    import yaml

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    data = build(path)
    header = ("# GENERATED by scripts/import_deviation_master.py from "
              f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name} -- do not edit by hand; edit the "
              "sheet and re-run.\n# Read by app/agents/los/queries.py (deviation catalogue + approving authorities). "
              "`conflicts`: the sheet's own\n# disagreements (Sheet0 vs the matrix) -- the MATRIX is used; the business "
              "should settle each.\n")
    body = yaml.safe_dump({"source": str(path.name), "authorities": ["ACM", "RCM", "ZCM", "HEAD_CREDIT"], **data},
                          allow_unicode=True, sort_keys=False, width=140)
    OUT.write_text(header + body, encoding="utf-8")
    print(f"{len(data['deviations'])} deviations, {len(data['conflicts'])} conflicts -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
