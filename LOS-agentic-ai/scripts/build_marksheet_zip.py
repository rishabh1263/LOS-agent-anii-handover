"""
Build the standalone 10th / 12th marksheet API zip (ONE route + /health) from this repo's current code.

    python scripts/build_marksheet_zip.py [out.zip]     # default: ~/Downloads/marksheet-verify-api.zip

Copies only what the route imports (the marksheet service, the shared PP-OCRv6 OCR engine and its executor, the
upload gate and the auth checks) and writes main.py, README.md, .env.example and requirements.txt beside them.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "marksheet-verify-api"

FILES = [
    "app/__init__.py", "app/agents/__init__.py", "app/api/__init__.py", "app/api/routes/__init__.py",
    "app/core/__init__.py", "app/core/text_match.py", "app/observability/__init__.py", "app/observability/tracing.py",
    "app/security/__init__.py", "app/security/access.py", "app/security/auth.py", "app/security/malware_scan.py",
    "app/security/upload_gate.py",
    "app/agents/marksheet/__init__.py", "app/agents/marksheet/config.py", "app/agents/marksheet/quality.py",
    "app/agents/marksheet/read.py", "app/agents/marksheet/service.py", "app/config/marksheet.yaml",
    "app/api/routes/marksheet_api.py",
    "app/agents/passbook/__init__.py", "app/agents/passbook/config.py", "app/agents/passbook/engine.py",
    "app/config/passbook.yaml",
    # the OCR executor and token type the engine runs on (document_agent: the same set the resume zip ships)
    "app/agents/document_agent/__init__.py", "app/agents/document_agent/ocr.py", "app/agents/document_agent/schemas.py",
    "app/agents/document_agent/classify.py", "app/agents/document_agent/name_spacing.py",
    "app/agents/document_agent/normalize.py", "app/agents/document_agent/pipeline.py",
    "app/agents/document_agent/preprocess.py", "app/agents/document_agent/preprocess_plan.py",
    "app/agents/document_agent/quality.py", "app/agents/document_agent/validate.py",
    "app/agents/document_agent/fields/__init__.py", "app/agents/document_agent/fields/candidates.py",
    "app/agents/document_agent/fields/common.py", "app/agents/document_agent/fields/dl.py",
    "app/agents/document_agent/fields/mrz.py", "app/agents/document_agent/fields/pan.py",
    "app/agents/document_agent/fields/passport.py", "app/agents/document_agent/fields/voter.py",
]

MAIN = '''"""
10th / 12th MARKSHEET VERIFY API -- standalone service.

    uvicorn main:app --host 0.0.0.0 --port 8010
    POST /api/v1/marksheet/verify-10th          multipart: file=<the 10th marksheet>  -> must be 10th + name
    POST /api/v1/marksheet/verify-12th          multipart: file=<the 12th marksheet>  -> must be 12th + name
    POST /api/v1/marksheet/verify-marksheet     multipart: file=<either>              -> which class + name
    GET  /health
"""

from __future__ import annotations

import logging
import os
import threading

from dotenv import load_dotenv

load_dotenv()                 # .env beside main.py (see .env.example); real environment variables win
os.environ.setdefault("OTEL_ENABLED", "false")
# with tokens off (development only), the local caller holds the upload scope this service needs -- so a fresh copy
# answers without a .env; a real AUTH_LOCAL_SCOPES (or .env) still wins, and with tokens on this changes nothing
os.environ.setdefault("AUTH_LOCAL_SCOPES", "documents:write upload_document")

from fastapi import FastAPI  # noqa: E402

from app.api.routes import marksheet_api  # noqa: E402
from app.security.auth import validate_auth_mode  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
validate_auth_mode()          # refuses to start with AUTH_ENABLED=false outside development
from app.agents.marksheet import config as _marksheet_config  # noqa: E402

_marksheet_config.get()       # a missing / broken app/config/marksheet.yaml stops startup by name

app = FastAPI(title="10th / 12th Marksheet Verify API", version="1.0.0")
# ONLY the marksheet routes are served
app.add_api_route("/api/v1/marksheet/verify-10th", marksheet_api.verify_10th, methods=["POST"],
                  tags=["Marksheet"], summary="Verify a 10th marksheet and read the candidate's name")
app.add_api_route("/api/v1/marksheet/verify-12th", marksheet_api.verify_12th, methods=["POST"],
                  tags=["Marksheet"], summary="Verify a 12th marksheet and read the candidate's name")
app.add_api_route("/api/v1/marksheet/verify-marksheet", marksheet_api.verify_marksheet, methods=["POST"],
                  tags=["Marksheet"], summary="Verify a 10th / 12th marksheet and read the candidate's name (one call)")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy", "service": "marksheet-verify-api"}


@app.on_event("startup")
def _warm_ocr() -> None:
    # the OCR model (PP-OCRv6) loaded on EVERY OCR worker before the first upload (each worker owns its engine), in
    # the background so /health answers at once. MARKSHEET_OCR_WARMUP=false skips it.
    if (os.getenv("MARKSHEET_OCR_WARMUP") or "true").lower() != "true":
        return

    def warm() -> None:
        import time

        try:
            from app.agents.document_agent.ocr import get_ocr_executor
            from app.agents.passbook import engine

            started = time.perf_counter()
            pool = get_ocr_executor()
            for future in [pool.submit(engine.warm) for _ in range(pool._max_workers)]:
                future.result(timeout=180)
            logging.getLogger("marksheet-verify-api").info("OCR warm-up done in %.0f ms",
                                                           (time.perf_counter() - started) * 1000)
        except Exception:  # noqa: BLE001 - warm-up is best effort
            logging.getLogger("marksheet-verify-api").warning("OCR warm-up failed; the first upload loads it",
                                                              exc_info=True)

    threading.Thread(target=warm, name="ocr-warmup", daemon=True).start()
'''

ENV = '''# Copy to .env (same folder as main.py) and edit. Real environment variables override this file.

# ---- MODE ------------------------------------------------------------------------------------------
# Local testing WITHOUT a token (only allowed when ENVIRONMENT is development / dev / local / test):
ENVIRONMENT=development
AUTH_ENABLED=false
AUTH_LOCAL_SCOPES=documents:write

# Production WITH tokens (comment the three lines above, fill these in). Tokens: RS256 JWT carrying one of the
# scopes documents:write / upload_document, signed by your identity provider (public keys via JWKS).
# ENVIRONMENT=production
# JWT_ALGORITHM=RS256
# JWT_ISSUER=<your issuer>
# JWT_AUDIENCE=<your audience>
# JWT_JWKS_URL=https://<your-idp>/.well-known/jwks.json

# ---- OPTIONAL (defaults are the tested ones; leave commented) ----------------------------------------
# MARKSHEET_OCR_WARMUP=true       # load the OCR model on every worker at startup
# DOCUMENT_OCR_WORKERS=2          # parallel OCR workers; default chosen from the CPU count
# PASSBOOK_OCR_THREADS=4          # CPU threads per OCR engine
# Words, labels, limits: app/config/marksheet.yaml
'''

README = r'''# 10th / 12th Marksheet Verify API

**One call**: upload a marksheet (photo, scan or PDF) and get back whether it is a **10th or 12th marksheet**,
**which one**, the **board** when printed, and the **candidate's name** -- never a father's, mother's, guardian's or
school's name.

| Status | Meaning |
|---|---|
| `VERIFIED` | a 10th / 12th marksheet; class and name read |
| `MANUAL_REVIEW` | a school marksheet, but the class or the name could not be read with confidence |
| `NOT_MARKSHEET` | another document; `document.detected_as` names it (PAN, Aadhaar, passbook, salary slip, degree, resume, invoice ...) |
| `BLANK` | nothing on the page (blank page, a plain photo) |
| `UNREADABLE` | too blurry / too little text |

**Wrong uploads are refused fast, cheapest first**: a wrong file type, empty, corrupt or tiny file at once; a blank or
smudged page before any OCR (~20 ms); another document after one read (~0.6 s). A digital PDF (e.g. DigiLocker) is
read from its own text without OCR (~70 ms).

Measured on made-up marksheets (8 board layouts: CBSE, ICSE / ISC, Maharashtra SSC / HSC, UP High School /
Intermediate, Karnataka SSLC / PUC, Tamil Nadu, Bihar; photos, scans, PDFs), names never seen while building it:

| held-out set | result |
|---|---|
| status (61 files incl. wrong documents, blank, blurry) | **61 / 61** |
| class 10th / 12th (43 marksheets) | **43 / 43** |
| candidate's name | **43 / 43** |
| time, marksheet (laptop CPU, warm) | **P50 ~1.1 s, P95 ~1.2-1.3 s** |
| time, wrong document / blank | ~0.6 s / ~20 ms |

Limits: names printed only in Hindi / a regional script are not read (-> `MANUAL_REVIEW`); measured on synthetic
layouts -- check a sample of your real marksheets before go-live. No internet is needed at runtime.

## 1. Install (once)

**Unzip to a SHORT folder**, e.g. `C:\marksheet-verify-api` (a long Windows path can fail to load a library).
You need **Python 3.12 (64-bit)**.

**Windows (PowerShell)**
```powershell
cd C:\marksheet-verify-api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

**Linux / macOS**
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
cp .env.example .env
```
(Linux: `sudo apt install libgl1 libglib2.0-0`)

## 2. Run

```
uvicorn main:app --host 0.0.0.0 --port 8010
```
Open http://127.0.0.1:8010/docs to try it. The OCR model loads in the background at startup (a few seconds).

## 3. Call it

Three routes, the same reading behind each:

| Route | Use it when | Answer |
|---|---|---|
| `POST /api/v1/marksheet/verify-10th` | the upload slot is the 10th marksheet | `VERIFIED` only for a 10th marksheet; a 12th one is `WRONG_CLASS` |
| `POST /api/v1/marksheet/verify-12th` | the upload slot is the 12th marksheet | `VERIFIED` only for a 12th marksheet; a 10th one is `WRONG_CLASS` |
| `POST /api/v1/marksheet/verify-marksheet` | either one | which class it is |

The name is returned only with `VERIFIED` (any other status: `"name": {"status": "NOT_EXTRACTED"}`). On `WRONG_CLASS` the class it is is returned, so the screen
can say "This is a 12TH marksheet, not a 10TH marksheet. Please upload the 10TH marksheet."

```bash
curl -X POST -F "file=@10th.jpg" http://127.0.0.1:8010/api/v1/marksheet/verify-10th
curl -X POST -F "file=@12th.pdf" http://127.0.0.1:8010/api/v1/marksheet/verify-12th
```
(PowerShell: `curl.exe`.) With tokens on, add `-H "Authorization: Bearer <token>"` (scope `documents:write` or
`upload_document`).

```json
{
  "request_id": "...",
  "status": "VERIFIED",
  "is_valid": true,
  "message": "This is a 12TH marksheet and the candidate's name was read.",
  "document": {"type": "MARKSHEET", "class": "12TH", "board": "CBSE"},
  "name": {"value": "RAHUL KUMAR SHARMA", "status": "FOUND", "confidence": 0.97},
  "reason_codes": [],
  "source": "ocr",
  "reused_result": false,
  "time_ms": 1050.2,
  "timings_ms": {"validate_ms": 0.7, "decode_ms": 9.1, "quality_ms": 7.0, "ocr_ms": 1020.3, "read_ms": 1.0, "total_ms": 1050.2}
}
```

Errors are `{"detail": {"code", "message", "request_id"}}`: 400 `EMPTY_FILE`, 413 `FILE_TOO_LARGE`, 415
`UNSUPPORTED_FILE_TYPE`, 422 `CORRUPT_IMAGE` / `CORRUPT_PDF` / `IMAGE_TOO_SMALL` / `FILE_SIGNATURE_MISMATCH` /
`TOO_MANY_PAGES` / `PDF_ENCRYPTED`, 504 `PROCESSING_TIMEOUT`, 401 / 403 auth. Accepted: JPG, PNG, WEBP, BMP, TIFF,
PDF up to 10 MB.

## 4. Tune

Every word, label and limit is in `app/config/marksheet.yaml`: the other documents' phrases, the 10th / 12th phrases,
the board names, the name labels, the blank / blur limits. Quote YES / NO / ON / OFF there (YAML reads them as
true / false).
'''


def build(out: Path) -> Path:
    stage = Path(tempfile.mkdtemp()) / NAME
    for rel in FILES:
        src = ROOT / rel
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (stage / "main.py").write_text(MAIN, encoding="utf-8")
    (stage / ".env.example").write_text(ENV, encoding="utf-8")
    (stage / "README.md").write_text(README, encoding="utf-8")
    (stage / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(stage.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(stage.parent).as_posix())
    shutil.rmtree(stage.parent, ignore_errors=True)
    return out


REQUIREMENTS = '''# 10th / 12th Marksheet Verify API -- Python 3.12 (64-bit). Versions are the ones this service was tested with.
fastapi==0.138.0
uvicorn==0.52.4
python-multipart==0.0.32
pydantic==2.13.5
PyJWT==2.13.0
cryptography==46.0.7
httpx==0.28.1
python-dotenv==1.2.3
PyYAML==6.0.3

# OCR + images + PDF
numpy==2.5.2
opencv-python==5.0.0.93
Pillow==12.3.0
onnxruntime==1.29.0
rapidocr-onnxruntime==1.4.4
pyclipper==1.4.0
shapely==2.1.2
PyMuPDF==1.28.2
pypdf==6.17.0
pdf2image==1.17.0
pytesseract==0.3.13

# tracing hooks (off by default: OTEL_ENABLED=false)
opentelemetry-api==1.44.0
opentelemetry-sdk==1.44.0

# OCR for photos and scanned PDFs: PP-OCRv6 (the models ship inside this package -- no download at runtime)
rapidocr==3.10.0
opencv-python-headless==5.0.0.93
omegaconf==2.4.0
colorlog==6.12.0
tqdm==4.70.0
requests==2.34.2
six==1.17.0
'''


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / f"{NAME}.zip"
    print("wrote", build(target))
