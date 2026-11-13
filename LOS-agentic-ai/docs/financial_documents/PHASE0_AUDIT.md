# Bank Statement & ITR extraction: Phase 0 audit (2026-11-02)

Read-only audit. No code was changed for this report.

## 1. Environment, measured on the development machine

| Item | Found | Note |
|---|---|---|
| Python | **3.12.10** | the brief says 3.11.9 |
| OCR engine in use | **RapidOCR 3.10 / rapidocr-onnxruntime 1.4.4, PP-OCRv4 (ONNX)** on onnxruntime 1.29 | `app/agents/document_agent/ocr.py` |
| PaddleOCR / paddlepaddle | **not installed** | the brief's PaddleOCR 3.4 / Paddle 3.2.1 are historical |
| Tesseract | installed (pytesseract 0.3.13 + `tesseract.exe`) | `TesseractEngine` adapter exists, used for targeted reads |
| PyMuPDF | 1.28.2 | native text, first choice (`bank_statement/fastpdf.py`) |
| pdfplumber | 0.11.10 | table cells, fallback text |
| pypdf | present | second fallback |
| pdf2image (+ Poppler) | used by the ITR OCR path | |
| OpenVINO | **not installed** | |
| Camelot | **not installed** | |
| OpenCV | 5.0.0.93 | |
| Git | branch `dev/rishabh`; **549 uncommitted changes**, including `bank_statement/extract.py`, `fastpdf.py` and `document_agent/ocr.py` | must be protected; nothing reset |

## 2. Existing architecture

### Bank statement: `app/agents/bank_statement/` (4,216 lines)

```
extract_bank_statement(path)                      extract.py
 ├─ _page_texts: PyMuPDF -> pypdf -> pdfplumber    native text per page
 ├─ classify_source(pages, pages_with_text)        DIGITAL / SCANNED / MIXED by text count (>= 60 chars)
 ├─ SCANNED, > inline_ocr_pages  -> REQUIRES_OCR   queued for the background OCR worker (ocr_queue)
 ├─ SCANNED, small: extract_via_grid               ruled-grid reconstruction from OCR tokens (grid.py)
 │                  extract_via_columns            word-position columns when there is no grid
 ├─ DIGITAL: extract_via_native_columns            text-layer coordinates (layout.py)
 │           extract_via_tables                    pdfplumber tables, page pool, time budget, projection
 ├─ text-line parsing (parse.py)                   last fallback, never used for a timed-out table
 ├─ side assignment, opening-row lifting, date outliers
 └─ reconciliation: RECONCILED / MISMATCH / INCONCLUSIVE, rows disagreeing with the running balance,
    serial gaps (rows_not_read), pages_unread, amount_source / side_source / placement per row
```

Already in place and good:
- Native extraction comes first, so digital statements are not OCR'd.
- Time budgets with a background queue; a timed-out table is queued, never truncated into a wrong answer (documented with measurements).
- Per-row provenance: `amount_source`, `side_source`, `placement`, `agrees_with_balance`, `balance_delta`.
- Statement-level reconciliation reports its basis.
- OCR models are loaded once per worker, with a dedicated executor and warmup.

### ITR: `app/agents/itr/` (410 lines)

- Reads **only the ITR-V acknowledgement** (the same page for every form):
  - PAN, name, form number, AY, acknowledgement number, filing date
  - total income, net tax payable, taxes paid, refund/payable, verification
- Native text first. OCR only with no text layer, via `pdf2image` at 200 DPI and RapidOCR.
- One `confidence` number for the whole document.

## 3. Gaps against the brief (accuracy risk first)

| # | Gap | Risk | Where |
|---|---|---|---|
| G1 | **MIXED statements: the scanned pages are never OCR'd.** Only counted (`pages_unread`) and warned. | **Transactions omitted** (a release blocker under §18) | extract.py 1485, 1590 |
| G2 | A page is "digital" by **character count alone** (>= 60 chars). No image-coverage or text-quality check. A scan with a digital header/stamp passes as digital. | Wrong route: a scanned table is read as empty or partial | `_page_texts`, `classify_source` |
| G3 | Page classification is **per document**, not per page. | Same as G1 and G2 | `classify_source` |
| G4 | **ITR: no schedules.** No salary, house property, business, capital gains or other-sources breakdown, no 80C/80D, no TDS / advance tax / self-assessment split. | Fields the brief requires are missing | itr/extract.py |
| G5 | **ITR: not form-aware or AY-aware.** One regex map, no versioned schema, no cross-checks (schedules against GTI, payments against final result). | Wrong reading on newer or older layouts goes unnoticed | itr/extract.py |
| G6 | **ITR: no field-level evidence** (page, bbox, raw vs normalised, method). One document confidence. | Cannot route a single doubtful amount to review | itr/schemas.py |
| G7 | ITR OCR renders **every page** at 200 DPI before cutting to `max_ocr_pages`. | Time and memory on long scans | itr/extract.py ~189 |
| G8 | No labelled ground truth for bank statements or ITRs. `samples/ground_truth.json` holds 18 other documents. | **No accuracy metric can be computed honestly** (§14) | samples/ |
| G9 | Bank fields: account type, IFSC and value date are read only where a layout gives them. No per-field confidence object (raw, normalised, page, bbox). | Partial provenance for header fields | schemas.py |

## 4. Latency: what is known, what must be measured

- Recorded in code comments (not re-measured yet):
  - PyMuPDF text: 0.12 s against pypdf's 8.0 s on 39 pages.
  - pdfplumber tables on 104 pages: up to 200 s, hence the page pool and the budget.
  - A 26-page scan OCR: about 30 s.
- **Baseline to measure next** (Phase 1, on an idle machine):
  - per document and per stage: render, OCR, table, validation
  - P50 / P95, peak RAM, cold vs warm start
  - over the sample PDFs (`samples/documents`: about 5 bank statements, about 22 ITR files)

## 5. Recommended plan (incremental, existing API kept)

1. **Phase 1: page routing and measurement**
   - A per-page inspector (PyMuPDF): characters, text-quality ratio, image coverage, rotation.
   - Route each page native or OCR, and OCR the scanned pages of MIXED documents with the existing grid/column readers (fixes G1–G3).
   - A benchmark runner that prints stage timings and RAM and never prints document contents.
2. **Phase 2: OCR configuration**
   - Benchmark DPI 200 / 250 / 300 and the RapidOCR det/rec variants available locally (PP-OCRv4; PP-OCRv5/6 ONNX only if the files are already present: model downloads from ModelScope are blocked here).
   - Tesseract only on low-confidence numeric crops.
3. **Phase 3: ITR**
   - Versioned schema per form and AY (config, not code).
   - Schedule extraction from the full return where present.
   - Deterministic reconciliation rules per AY; REVIEW, never a silent correction (fixes G4–G7).
4. **Phase 4:** field-level evidence and confidence objects for the ITR and the statement header (G6, G9), without breaking the current response fields.
5. **Phase 5:**
   - OpenVINO evaluated only if it can be installed and the models convert. It is not installed today.
   - Thread and DPI tuning, measured.
6. **Phase 6:** hardening, regression, benchmark report.

## 6. External blocker to state plainly

The accuracy metrics in §14 need **manually verified ground truth**: 50 statements and 50 ITRs, labelled by a person who has seen the documents. That labelling cannot come from our own extraction (§14 forbids it), and it is not in the repository. Until it exists:
- speed, routing and arithmetic consistency can be measured and improved;
- **accuracy percentages cannot be claimed.**
