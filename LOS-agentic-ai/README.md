# LOS Agentic AI

Document intake for a Loan Origination System. One call takes an applicant's
documents end to end: each is classified, verified and extracted, financial
uploads are routed to the Financial Agent, the normalised results are
cross-checked by KYC, and one response comes back.

**Every decision on that path is deterministic.** OCR plus regex and spatial
reasoning decide what a document is and what it says. A language model, when
switched on, writes the summary sentence and nothing else — after every
decision is already final.

---

## The endpoint

```
POST /api/v1/los/process        multipart/form-data
```

| Field | | |
|---|---|---|
| `files` | **required** | One or more documents. Max 10, 25 MB each. |
| `operation` | | `PROCESS` (default), `EXTRACT`, `VERIFY` |
| `applicant_id` | | Your identifier, echoed back. |
| `case_id` | | Omit to start a case; supply to add to one. |
| `expected_types` | | Comma-separated, **positional**. `AUTO` skips one. |

Returns `request_id`, `applicant_id`, `case_id`, `status`, `documents`, `kyc`,
`cross_document`, `decision`, `next_action`, `summary`, `summary_source`,
`processing_ms`, `errors`.

Bearer JWT required. Full contract: **`docs/API_INTEGRATION.md`**.

### Document types

PAN · Driving Licence · Voter ID · Passport · Bank Statement · ITR ·
Salary Slip · Sale Deed · Business Evidence

---

## Start here

| | |
|---|---|
| **`docs/QUICKSTART.md`** | Install to first call, ~15 minutes. |
| **`docs/API_INTEGRATION.md`** | The request/response contract. |
| **`docs/AUTHENTICATION.md`** | JWT validation, scopes, key rotation. |
| **`docs/API_HANDOVER.md`** | What was verified and measured, and what is still open. |

---

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

copy .env.example .env            # Linux/macOS: cp .env.example .env
# Set JWT_JWKS_URL, JWT_ISSUER and JWT_AUDIENCE -- the service will not
# start without them. There is no way to switch authentication off.

python -m uvicorn main:app --host 0.0.0.0 --port 8010
```

| URL | |
|---|---|
| `http://127.0.0.1:8010/docs` | Swagger UI |
| `http://127.0.0.1:8010/redoc` | ReDoc |
| `http://127.0.0.1:8010/openapi.json` | OpenAPI 3.1 |
| `/health` `/ready` `/metrics` | Unauthenticated probes |

### Optional, all degrade rather than crash

```bash
ollama pull qwen2.5:3b            # summary sentence only, decides nothing
winget install UB-Mannheim.TesseractOCR    # word spacing inside names
winget install oschwartz10612.Poppler      # scanned-PDF rasterisation
```

Without Ollama every summary is deterministic and nothing else changes.
Without Tesseract names come back unspaced. Without Poppler scanned PDFs yield
no OCR text. `docs/QUICKSTART.md` §3 has the Linux and macOS equivalents.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
pytest -m "not ocr"      # skip the slow real-OCR tests
```

Tests that read real sample documents skip when `samples/` is absent — that
directory holds real customer documents and is not distributed.

---

## What this service does not do

- **It does not establish authenticity.** Verification checks class,
  legibility and identifier format. It does not prove a document is genuine.
- **It does not establish ownership.** No capability here does.
- **It does not make a credit decision.** `decision` restates the document
  roll-up under a name a client can read. Underwriting is elsewhere.
- **It does not let a model decide anything.** Not classification, not
  verification, not extraction, not KYC, not `decision`, not `next_action`.

---

## Layout

```
app/
  agents/          document, financial, KYC, fraud & risk, specialists
  api/routes/      FastAPI routers
  config/          agents.yaml, documents.yaml, kyc_policies.yaml, risk_policy.yaml
  llm/             the one place an Ollama client is built
  mcp/             MCP servers
  orchestration/   LangGraph graph, registry, resilience
  security/        JWT authentication
docs/              integration, authentication, quickstart, handover
tests/
main.py            entrypoint  (risk_main.py is a compatibility alias)
```
