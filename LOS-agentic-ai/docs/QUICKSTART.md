# Quickstart

From an extracted archive to a working `POST /api/v1/los/process` call.

Target: **15 minutes**, most of it waiting on `pip` and `ollama pull`.

---

## 1. Prerequisites

| | Version | Notes |
|---|---|---|
| Python | **3.11 or 3.12** | Verified on 3.12.10. The code uses `X \| None` syntax and so needs at least 3.10. |
| pip | recent | `python -m pip install --upgrade pip` first. |
| Tesseract OCR | 5.x | **Optional.** Name word-spacing. |
| Poppler | 24.x+ | **Optional.** Rasterising scanned PDFs. |
| Ollama | 0.5+ | **Optional.** The summary sentence only. |

The three optional items **degrade rather than crash**. The service starts,
serves and returns correct decisions without any of them. What you lose is
listed in §3.

---

## 2. Install

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

Roughly 85 packages. To run the test suite as well:

```bash
pip install -r requirements-dev.txt
```

### Configure

```bash
cp .env.example .env         # Windows: copy .env.example .env
```

`.env.example` contains **placeholders only**. Edit `.env` and set at minimum
the three required authentication variables (§4); everything else has a
working default.

---

## 3. Optional system components

None of these is a Python package, so `pip` will not install them.

### Tesseract OCR — name word-spacing

Without it, names come back unspaced: `LAXMISANTOSHGUPTA` rather than
`LAXMI SANTOSH GUPTA`. Everything else is unaffected.

```bash
# Windows
winget install UB-Mannheim.TesseractOCR
# Debian / Ubuntu
sudo apt-get install -y tesseract-ocr
# macOS
brew install tesseract
```

Ensure the binary is on `PATH`, or set `DOCUMENT_NAME_SPACING=false` to skip
the pass deliberately rather than silently.

### Poppler — scanned-PDF rasterisation

Without it, a **scanned** bank statement or ITR yields no OCR text and comes
back with reduced or empty extraction. Digital PDFs with a text layer are
unaffected, as are JPG/PNG uploads.

```bash
# Windows
winget install oschwartz10612.Poppler
# Debian / Ubuntu
sudo apt-get install -y poppler-utils
# macOS
brew install poppler
```

`pdftoppm -v` must work from the shell that starts the service.

### Ollama — the summary sentence

**The model decides nothing.** Classification, verification, extraction, KYC,
`decision` and `next_action` are all deterministic and final before the model
is consulted. With no Ollama at all, every response is identical except that
`summary` is the deterministic sentence and `summary_source` is
`"deterministic"`.

```bash
# https://ollama.com/download
ollama pull qwen2.5:3b
ollama list          # confirm it is there
```

Use a **small non-reasoning** model. A reasoning model (`qwen3`,
`deepseek-r1`) emits a thinking block before answering and cannot meet the
1.5 s budget.

**Do not expect `summary_source: "llm"` on modest CPU hardware.** See
`docs/API_HANDOVER.md` §4 for measured numbers on this build.

---

## 4. Authentication — required

The service **refuses to start** without `JWT_JWKS_URL`, `JWT_ISSUER` and
`JWT_AUDIENCE`. There is no way to switch authentication off.

Point them at your identity provider:

```bash
JWT_JWKS_URL=https://idp.example.internal/.well-known/jwks.json
JWT_ISSUER=https://idp.example.internal/
JWT_AUDIENCE=los-agentic-ai
JWT_ALGORITHM=RS256
```

Full detail, including scopes, roles and key rotation: `docs/AUTHENTICATION.md`.

### No identity provider yet?

`auth_provider_dev.py` is a local development IdP. It is **development only**
— never run it in a shared or production environment.

```bash
# terminal 1
JWT_ISSUER=los-local JWT_AUDIENCE=los-agentic-ai python auth_provider_dev.py
```

It generates its own RSA keypair into `auth_keys/` on first run and serves
`http://127.0.0.1:8020/.well-known/jwks.json`. Match your `.env` to it:

```bash
JWT_JWKS_URL=http://127.0.0.1:8020/.well-known/jwks.json
JWT_ISSUER=los-local
JWT_AUDIENCE=los-agentic-ai
```

---

## 5. Run

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8010
```

Startup validates the risk policy, loads an OCR model per worker and warms the
summary model. **Expect several seconds.** Do not route traffic until `/ready`
returns `200`.

```bash
curl http://127.0.0.1:8010/health
curl http://127.0.0.1:8010/ready
```

| URL | |
|---|---|
| `http://127.0.0.1:8010/docs` | Swagger UI — multi-file upload works here |
| `http://127.0.0.1:8010/redoc` | ReDoc |
| `http://127.0.0.1:8010/openapi.json` | OpenAPI 3.1 document |

---

## 6. First call

Get a token from your IdP (or the dev one) into `$TOKEN` — **never paste a
token into a file or a chat**.

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8020/oauth/token \
  -d grant_type=client_credentials \
  -d client_id=los-demo-client \
  -d client_secret="$AUTH_CLIENT_SECRET" \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

### Single document

```bash
curl -s -X POST http://127.0.0.1:8010/api/v1/los/process \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@pan.jpg" \
  -F "operation=PROCESS" \
  -F "applicant_id=APP-0001"
```

### Several documents, with asserted types

`expected_types` is **positional** — the *n*-th entry applies to the *n*-th
`files` part. Use `AUTO` to skip one.

```bash
curl -s -X POST http://127.0.0.1:8010/api/v1/los/process \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@PAN.pdf" \
  -F "files=@DL.pdf" \
  -F "operation=PROCESS" \
  -F "applicant_id=APP-0001" \
  -F "expected_types=PAN,DRIVING_LICENCE"
```

Assert a type that does not match and the document is refused rather than
guessed at: `verification=FAIL`, `reason_codes` contains
`DOCUMENT_TYPE_MISMATCH`, `extraction` is `null`, no extractor runs,
`decision=REVIEW` and `next_action=REQUEST_CORRECT_DOCUMENT`.

---

## 7. Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests that read real sample documents **skip** when `samples/` is absent. The
handover archive does not ship `samples/` — those are real customer documents
— so expect a higher skip count than the development checkout. Nothing fails
because of it.

```bash
pytest -m "not ocr"     # skip the slow real-OCR tests
```

---

## 8. If something is wrong

| Symptom | Cause |
|---|---|
| `RuntimeError: JWT_JWKS_URL is not configured.` at startup | `.env` missing or not loaded. Copy `.env.example`. |
| `401 Unable to resolve JWT signing key.` | JWKS unreachable, or the token's `kid` is not published there. |
| `401 JWT issuer is invalid.` | `JWT_ISSUER` does not match the token's `iss`. |
| Every extraction fails | `rapidocr-onnxruntime` missing. Reinstall from `requirements.txt`. |
| Names come back unspaced | Tesseract not on `PATH`. |
| Scanned PDFs extract nothing | Poppler not on `PATH`. |
| `summary_source` always `deterministic` | Expected on CPU-only hardware. Not a fault — see `docs/API_HANDOVER.md` §4. |
| `503` from `/ready` | Risk policy or agent config failed to load. Read the startup log. |
| First request very slow | `DOCUMENT_OCR_WARMUP=false`. Leave it `true`. |

---

## 9. Next

- `docs/API_INTEGRATION.md` — the full request/response contract
- `docs/AUTHENTICATION.md` — JWT validation in detail
- `docs/API_HANDOVER.md` — what was verified, measured and left open
