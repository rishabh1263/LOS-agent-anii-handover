# API Handover

What this service is, what was verified before handing it over, what was
measured, and what is still open.

Everything below was checked against the **running HTTP API** on the date of
handover. Where something could not be verified on the available hardware,
this document says so rather than claiming it.

---

## 1. What you are receiving

A FastAPI service that takes an applicant's documents in one multipart call
and returns one structured result.

```
POST /api/v1/los/process
```

Classification, verification, extraction, KYC, cross-document comparison,
`decision` and `next_action` are **all deterministic**: OCR plus regex and
spatial reasoning. A language model writes the `summary` sentence and nothing
else, after every decision is already final.

### Not included, deliberately

| | Why |
|---|---|
| `samples/` | Real customer documents. Not distributable. |
| `.env` | Real configuration. Use `.env.example`. |
| `auth_keys/`, `*.pem` | Generated RSA key material. |
| `auth_provider.sqlite3` | Dev refresh-token store. |
| `runtime/audit/risk_decisions.jsonl` | Audit log containing real applicant data. |
| Ollama model files | A model is not a Python dependency. `ollama pull qwen2.5:3b`. |
| `.venv`, `__pycache__`, `.pytest_cache` | Machine-specific. |
| Benchmark / diagnostic scratch scripts | All read `samples/`, which is not shipped. |

**Consequence of dropping `samples/`:** tests that read real sample documents
**skip** rather than fail, so your skip count will be higher than the
development checkout's. Nothing breaks. To restore full coverage, supply your
own documents under `samples/` using the paths in `tests/`.

---

## 2. Getting it running

`docs/QUICKSTART.md` is the step-by-step. The short version:

```bash
python -m venv .venv && .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env          # then set the three JWT variables
python -m uvicorn main:app --host 0.0.0.0 --port 8010
```

### Verified environment

| | |
|---|---|
| Python | **3.12.10** — a clean `venv`, `pip install -r requirements.txt`, nothing else |
| Packages installed | 87 |
| Result | Every runtime module imports; `main.app` loads; OpenAPI 3.1 generates; `/api/v1/los/process` present |

**Python 3.11 was not available on the validation machine and was therefore
not tested.** The code needs 3.10 or newer (it uses `X | None`). 3.11 is
expected to work and is not claimed to have been verified.

### Three optional system components

None is installed by `pip`. All three **degrade rather than crash** — the
service starts and returns correct decisions without any of them.

| | Without it |
|---|---|
| **Tesseract OCR** | Names come back unspaced (`LAXMISANTOSHGUPTA`). |
| **Poppler** | Scanned PDFs yield no OCR text. Digital PDFs and images unaffected. |
| **Ollama** | Every `summary_source` is `deterministic`. Nothing else changes. |

---

## 3. Authentication

RS256 JWT, verified against your identity provider's JWKS endpoint. Full
detail in `docs/AUTHENTICATION.md`.

```
Authorization: Bearer <access_token>
```

| Variable | Required | Default |
|---|---|---|
| `JWT_JWKS_URL` | **yes** | — |
| `JWT_ISSUER` | **yes** | — |
| `JWT_AUDIENCE` | **yes** | — |
| `JWT_ALGORITHM` | | `RS256` |
| `JWT_LEEWAY_SECONDS` | | `30` |
| `JWT_JWKS_CACHE_SECONDS` | | `300` |

The service **refuses to start** without the first three. There is no
configuration that disables authentication. Asymmetric algorithms only —
`HS256` and `none` are rejected.

`auth_provider_dev.py` is shipped as a **local development** identity
provider. Never run it in a shared or production environment.

---

## 4. The LLM summary — read this before you rely on it

**The model decides nothing.** It runs last, is shown statuses and counts
only (never an extracted PAN, name or date of birth), and its sentence is
validated before use — rejected outright if it is empty, over-long, returns
structured data, states a number not present in the evidence, or asserts a
verdict other than the computed one. A rejected sentence is discarded whole
and the deterministic one is used.

| | |
|---|---|
| Model | **`qwen2.5:3b`** — `ollama pull qwen2.5:3b` |
| Budget | **1.5 s** (`LOS_LLM_SUMMARY_TIMEOUT_SECONDS`) |
| Token cap | **64** (`LOS_LLM_SUMMARY_MAX_TOKENS`) |
| Calls per request | Exactly one, application-level. Never one per document. |
| Retries | None. |

### Measured on the handover machine

Intel Core 7 240H, 10 physical / 16 logical cores @ 2.5 GHz, 15.5 GB RAM,
**integrated graphics only — Ollama runs on CPU.**

**Through the real HTTP API**, real multipart uploads, 8 requests per row,
shipped defaults:

| Documents | `summary_source: "llm"` |
|---|---|
| 1 | **7 of 8** |
| 2 | **2 of 8** |
| 4 | **7 of 8** |

**Before the tuning in §5, the same benchmark returned `deterministic` on
every single request.**

**Generation cost alone** (model resident, 12 runs per shape): median
**1076–1496 ms**, p95 1176–1562 ms.

**Cold start always falls back**: with the model evicted, one call costs
~8.0 s — 3.7 s loading the model, 2.9 s reading the prompt, 0.9 s generating.
That is why the model is warmed at startup and held resident.

### What this means for you

- **`summary_source` is observed, not guaranteed.** Single- and
  four-document requests usually get a model-written sentence on this
  hardware; two-document bundles with several KYC mismatch codes sit right on
  the 1.5 s boundary and often do not.
- **Do not write client logic that depends on `summary_source == "llm"`.**
  Read the field, show the sentence, treat either value as normal.
- `summary_source: "deterministic"` **is not an error**. The request
  succeeded; only the sentence was written without a model.

### Configuration

| Variable | Default | |
|---|---|---|
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Use a small **non-reasoning** model. `qwen3` / `deepseek-r1` emit a thinking block and cannot meet the budget. |
| `LOS_LLM_SUMMARY_TIMEOUT_SECONDS` | `1.5` | Fast-fail bound, not a generation allowance. |
| `LOS_LLM_SUMMARY_MAX_TOKENS` | `64` | Bounds the work rather than the patience. |
| `LOS_LLM_KEEP_ALIVE` | `30m` | How long Ollama holds the model resident. Ollama's own default is 5 min. |
| `LLM_CONNECT_TIMEOUT_SECONDS` | `0.5` | TCP probe before generating. |
| `LLM_UNAVAILABLE_COOLDOWN_SECONDS` | `60` | Provider **not listening**. |
| `LLM_SLOW_COOLDOWN_SECONDS` | `2` | Provider **answered late**. See §5. |
| `LLM_AVAILABLE_CACHE_SECONDS` | `5` | Trust a successful probe this long. |

To see `llm` more often: give Ollama a GPU or a host with spare CPU, raise the
timeout, lower the token cap, or set `LLM_SLOW_COOLDOWN_SECONDS=0`. **Raising
the timeout raises p99 latency on a synchronous API** — that is the trade, and
it is yours to make.

### Enabled in production, disabled in tests

| | |
|---|---|
| **Production** | LLM **ON**. `app/config/documents.yaml` → `los.llm_summary_enabled: true`, overridable with `LOS_LLM_SUMMARY_ENABLED`. |
| **Tests** | LLM **OFF**, by an autouse fixture in `tests/conftest.py`. |

The suite must not require Ollama to be installed, running, or holding any
particular model — and must not get slower because a model became responsive.
Tests that exercise the model enable it explicitly and stub the generation
call, which works because the fixture only changes a *default*.

---

## 5. What was changed to get there

Every item below was measured before and after. Nothing was kept on
principle.

### The timeout was NOT raised

It is still **1.5 s**. The work went into making generation fit inside it.

### 1. A shorter, more specific system prompt

Generation time on CPU is dominated by output length, and this model runs at
~17 tokens/second here. The old wording allowed "one or two sentences" and
produced a median of 21 output tokens.

The first attempt — simply demanding brevity — was fast and **wrong**: on an
application whose document passed but whose KYC wanted more sources, the
model wrote "Document verification for loan **failed**". That was caught by
the validator 11 times in 12 and discarded, so the speed bought nothing.

The shipped prompt names the fields to lean on and forbids outcome words the
data does not use. Across five envelope shapes, 8 runs each:

| prompt | accepted | median | p95 | in budget |
|---|---|---|---|---|
| previous wording | 40/40 | 1379 ms | 2081 ms | 25/40 |
| brevity only | **31/40** | 994 ms | 1233 ms | 40/40 |
| **shipped** | **40/40** | **1112 ms** | **1518 ms** | **37/40** |

### 2. Compact JSON in the prompt

`indent=2` cost ~40 prompt tokens on a multi-document payload. Prompt tokens
are read at the same rate as everything else.

### 3. The counts are computed, not left to the model

Given only a list of statuses, the model wrote "3 succeeded, 1 rejected" —
accurate, derivable, and **rejected**, because 3 and 1 appeared nowhere in
what it was shown. On a four-document bundle that false rejection fired 5
times in 8. The payload now carries `document_status_counts`. No new
information reaches the model: it is the same statuses, counted.

### 4. Per-document statuses count as computed verdicts

The validator accepted only `status`, `verification_status` and `kyc_status`,
so a sentence saying "one document was REJECTED" — quoting the payload — was
discarded as an uncomputed verdict. Document statuses are equally computed
and equally shown to the model, so they are now in the set.

**This did not loosen the check.** A verdict the pipeline never produced is
still refused, and there are tests for both directions.

### 5. `keep_alive` — the model stays resident

Ollama unloads an idle model after 5 minutes. Reloading costs 3.7 s, which
overruns the budget on its own and then suppresses the model for the whole
cooldown. One quiet stretch cost far more than one summary.

### 6. "Slow" and "absent" are no longer the same thing

**The single highest-impact fix.** A generation that overran used to call
`mark_unavailable`, the same path as a refused connection — a **60-second**
hold-off.

Observed in the benchmark: one request overran the 1.5 s budget by **3 ms**,
and the next **fifteen consecutive requests** skipped the model entirely,
every one of which had time to spare.

A generation timeout now calls `mark_slow`, with its own much shorter window.
The default was chosen by measurement:

| slow cooldown | 1 doc | 2 docs | 4 docs | overall |
|---|---|---|---|---|
| 5 s | 2/5 | 1/5 | 4/5 | 47% |
| **2 s** | 6/8 | 4/8 | 6/8 | **67%** |
| 0 s | 3/5 | 4/5 | 4/5 | 73% |

Zero scores best and bounds nothing; five throws away most of the benefit.
Connection failures still get the full 60 s — that protection is unchanged.

### 7. Stage timings now reach the log

`ocr_ms`, `classification_ms`, `summary_ms`, `llm_ms` and the rest were
computed and then discarded. One line per request now carries them, keyed by
`request_id`, holding numbers only. **They are still absent from the
response** — `processing_ms` remains the only public timing field.

### Considered and rejected

| | Why not |
|---|---|
| Raising the timeout | Explicitly out of scope, and it hides the problem rather than fixing it. |
| `num_predict=32` | No faster than 64 (757 ms vs 741 ms) and it truncated sentences into wrong ones. |
| `num_thread` 10 / 16 | Within noise of the default (740 / 896 vs 741 ms). |
| A persistent HTTP client | Already present — `ollama.AsyncClient` pools connections and the client is `lru_cache`d. |
| Lowering OCR quality | Out of scope and not the bottleneck worth trading. |

### 8. ONNX threads bounded — the one change that moved end-to-end latency

Every optimisation above concerns the summary sentence. This one concerns the
whole request.

ONNX Runtime sizes its intra-op thread pool from the core count **per
session**, and the service holds one session per OCR worker. Two workers
therefore asked for two machines' worth of threads, and the contention was
paid on every request. The default is now `(logical CPUs / 2) / workers` — 4
on the reference host — which caps the process at 8 OCR threads instead of 32.

Measured end to end, real multipart HTTP, 5 requests per row, each against a
server started and stopped by the harness so no previous configuration could
leak in:

| Documents | LLM | Before | After | |
|---|---|---|---|---|
| 1 | OFF | 1577 ms | **925 ms** | 1.70× |
| 2 | OFF | 2438 ms | **1754 ms** | 1.39× |
| 4 | OFF | 5002 ms | **2814 ms** | **1.78×** |
| 1 | ON | 2643 ms | **2107 ms** | 1.25× |
| 2 | ON | 3406 ms | **3172 ms** | 1.07× |
| 4 | ON | 6135 ms | **3916 ms** | **1.57×** |

`DOCUMENT_OCR_WORKERS=1` was tested and is **not** an improvement: recognition
serialises and four documents took 29,316 ms.

No deterministic output changed. PAN, Driving Licence, Voter ID, bank
statement, two-document KYC, correct and mismatched `expected_types`, and the
VERIFY operation were each run under both settings and compared field by
field — every response byte-identical apart from the summary sentence, the
identifiers and the timings.

### End-to-end cost, with and without the model

Real multipart HTTP requests, `processing_ms` from the response, 8 runs per
row, warmed first:

| Scenario | Docs | LLM | Min | Median | P95 | Max | `summary_source` |
|---|---|---|---|---|---|---|---|
| Single | 1 | **ON** | 2697 | **3087** | 3230 | 3302 | llm 7/8 |
| Single | 1 | OFF | 1627 | **1757** | 1823 | 1867 | deterministic |
| Multi | 2 | **ON** | 3906 | **4402** | 4620 | 4688 | llm 2/8 |
| Multi | 2 | OFF | 2716 | **2814** | 2905 | 2936 | deterministic |
| Multi | 4 | **ON** | 6406 | **6537** | 6972 | 8501 | llm 7/8 |
| Multi | 4 | OFF | 5509 | **5700** | 5904 | 6637 | deterministic |

**LLM overhead** = median(ON) − median(OFF):

| Docs | Overhead |
|---|---|
| 1 | **+1330 ms** |
| 2 | **+1588 ms** |
| 4 | **+838 ms** |

### Where the time actually goes

Internal medians from the service log (**not** the response):

| Docs | ocr | classify | verify | extract | kyc | summary | total |
|---|---|---|---|---|---|---|---|
| 1 | 1222 | 5 | 1 | 2 | 1 | 1271 | 3087 |
| 2 | 3340 | 7 | 1 | 7 | 1 | 1514 | 4402 |
| 4 | 5787 | 22 | 2 | 11 | 1 | 776 | 6537 |

**OCR dominates every request** — 40–89% of the total. The deterministic
decision stages (classification, verification, extraction, KYC) together cost
**under 40 ms** even on four documents. If you need this service faster, OCR
is the only lever that matters; the summary is the second, and it is optional.

---

## 6. The request contract

`multipart/form-data`. Full detail in `docs/API_INTEGRATION.md`.

| Field | | |
|---|---|---|
| `files` | **required** | One or more. Max **10**, **25 MB** each. |
| `operation` | | `PROCESS` (default), `EXTRACT`, `VERIFY` |
| `applicant_id` | | Echoed back. |
| `case_id` | | Omit to start a case; supply to add to one. |
| `expected_types` | | Comma-separated, **positional**. `AUTO` skips one. |

### `expected_types` is positional

The *n*-th entry applies to the *n*-th `files` part.

```
files[0] = PAN.pdf
files[1] = DL.pdf
expected_types = PAN,DRIVING_LICENCE
```

**A wrong assertion is refused, not guessed at.** Verified live:

| | |
|---|---|
| `verification` | `FAIL` |
| `reason_codes` | contains `DOCUMENT_TYPE_MISMATCH` |
| `extraction` | `null` |
| Extractor / specialist | **does not run** |
| `decision` | `REVIEW` |
| `next_action` | `REQUEST_CORRECT_DOCUMENT` |

This behaviour is deliberate and was not changed during handover
preparation.

### Upload limits

The limit on `/api/v1/los/process` is **not** configurable: it is the constant
`MAX_UPLOAD_BYTES` in `app/agents/document_agent/workflow.py`, fixed at 25 MB
per file. The `MAX_UPLOAD_BYTES` environment variable applies to
`/api/v1/extract-document` only.

---

## 7. The response contract

Exactly these thirteen keys, every time:

```
request_id  applicant_id  case_id  status  documents  kyc  cross_document
decision  next_action  summary  summary_source  processing_ms  errors
```

Verified against the live OpenAPI document: **every live response matched the
published schema exactly** — no undocumented key, no missing key, no type
mismatch.

### What never leaves the building

Checked programmatically across every live response:

OCR tokens · bounding boxes · candidate lists · prompts · MCP payloads ·
stack traces · filesystem paths · internal stage timings · internal agent
state · provider internals

Stage timings (`ocr_ms`, `classification_ms`, `specialist_ms`, `mcp_ms`,
`orchestration_ms`, `summary_ms`, `llm_ms`) are computed and logged but stay
internal — they describe how the service is built, not what it concluded.
`processing_ms` is the one number a client has a use for.

---

## 8. Validation performed before handover

All against the running service over real HTTP, not `TestClient`.

| | Result |
|---|---|
| `GET /health` | `200` |
| `GET /ready` | `200 ready` |
| No `Authorization` header | `401` |
| Invalid bearer token | `401` |
| Valid token, single PAN | `200`, `verification=PASS`, fields released |
| Valid token, two documents | `200`, both classified and extracted |
| Correct `expected_types` | `200`, both echoed, both `PASS` |
| Wrong `expected_type` | `200`, `FAIL` + `DOCUMENT_TYPE_MISMATCH`, `extraction=null`, `REQUEST_CORRECT_DOCUMENT` |
| Cross-document mismatch | `200`, `kyc=REVIEW`, per-check sources reported |
| Bank statement | `200`, routed to the Financial Agent |
| Unknown `operation` | `422` |
| No files | `422` |
| Empty file | `400 EMPTY_FILE` |
| `/docs`, `/redoc`, `/openapi.json` | `200` |
| Swagger request schema | `files` is an array of `format: binary` — Swagger UI renders a **multi-file** picker |
| Swagger security | `HTTPBearer` scheme present on the operation |
| Live JSON vs OpenAPI | Match, including nested schemas |
| Fresh Python 3.12 venv from `requirements.txt` | 87 packages; imports, starts, `/health` `/ready` `/docs` `/redoc` `/openapi.json` all `200` |
| Secret / PII scan | No credential or key material in the archive |
| LLM benchmark, direct | Cold and warm, 12 runs per shape, through the production code path |
| LLM benchmark, full API | 8 real multipart requests per row, LLM on and off |
| Model cannot change a decision | Same documents run until both an `llm` and a `deterministic` summary were observed; **every** deterministic field byte-identical across the two |
| Stage timings absent from the response | Every property name reachable from `LosProcessResponse` checked against a forbidden list — `processing_ms` is the only timing field |

---

## 9. Known limitations

These are real and are not being presented as solved.

1. **`summary_source: "llm"` is still not guaranteed on CPU-only hardware.**
   It went from 0 of 24 requests to 16 of 24 (§4, §5), which is a real
   improvement and not a fix. The weak case is a **two-document bundle with
   several KYC mismatch codes**: there is more to say, the sentence is longer,
   and generation lands on the 1.5 s boundary — 2 of 8 on the validation
   machine. Treat `deterministic` as a normal outcome on any CPU-only host.

   Not a defect: an optional final stage missing an intentionally tight
   budget, after every decision is already final.

2. **The risk policy is not signed off.** Thresholds marked `[PLACEHOLDER]`
   in `app/config/risk_policy.yaml` are not authoritative. The service warns
   at startup and refuses to start with `ENVIRONMENT=production` until credit
   approves them. Do **not** set `ALLOW_UNSIGNED_RISK_POLICY=true` to get past
   this.

3. **Python 3.11 was not tested** — unavailable on the validation machine.
   3.12.10 was verified end to end.

4. **`opencv-python` and `opencv-python-headless` are both installed.**
   `rapidocr-onnxruntime` depends on the non-headless build, and the service
   asks for the headless one. Both provide the same `cv2` module and the
   combination works, but a container image can drop the non-headless variant
   if image size matters.

5. **Tesseract and Poppler failures are silent by design.** A missing binary
   degrades output — unspaced names, empty scanned-PDF OCR — and is logged at
   debug level, not surfaced in the response. Confirm both are on `PATH` in
   your image.

6. **The case store is SQLite, behind a repository interface.** Cases,
   applicants, documents and their verdicts persist across restarts. SQLite
   is the current implementation, not the contract: swap it by registering
   another backend in `app/store/__init__.py`. It is sized for a single
   service instance — a multi-instance deployment needs a backend that is.

7. **Verification does not establish authenticity**, and `decision` is not a
   credit decision. This is now stated in the response, not only here: every
   identity document carries `authenticity: "NOT_ESTABLISHED"` at every
   verdict, including PASS.

   There is no issuer API, no government lookup and no issuer signature to
   check against, so what a PASS means is: **the document is structurally
   valid and internally consistent** — the identifier has the right form and
   check structure, the required fields were read, the dates are plausible,
   and it is the type the caller asked for. It does **not** mean the card was
   issued by the authority printed on it, and a forgery that copies the
   structure correctly will pass.

   Set `VERIFICATION_AUTHENTICITY_POLICY=REQUIRE_EXTERNAL` to cap identity
   documents at `REVIEW` with `AUTHENTICITY_NOT_ESTABLISHED` instead. That is
   the correct setting for a lender that will not treat an unconfirmed card
   as verified; the cost is that no case reaches `READY_FOR_CPA` on documents
   alone until an issuer check exists. The default is `STRUCTURAL_PASS`.

   What IS caught, and is tested on real files: a document that is not the
   type asserted, a document that cannot be read, a blank or dummy image, an
   identifier whose structure is wrong, and a required field that was never
   read. None of those reach PASS, and none of them release extracted fields.

8. **OCR is the latency floor, and it was not touched.** It is 40–89% of every
   request (§5). Lowering OCR quality to go faster was out of scope and is not
   recommended: extraction accuracy depends on it directly. A four-document
   application costs ~5.7 s before the summary is even considered.

9. **`keep_alive=30m` holds roughly 2 GB resident** for that window. On a host
   packing several services into limited memory, lower it — and accept that an
   idle deployment will then pay a ~3.7 s model reload, which always overruns
   the budget and falls back.

---

## 10. Where things are

| | |
|---|---|
| `docs/QUICKSTART.md` | Install to first call |
| `docs/API_INTEGRATION.md` | Full request/response contract |
| `docs/AUTHENTICATION.md` | JWT validation, scopes, rotation |
| `docs/API_HANDOVER.md` | This document |
| `app/agents/los/flow.py` | The end-to-end flow |
| `app/agents/los/response.py` | The single place the public response is shaped |
| `app/agents/los/schemas.py` | The published OpenAPI models |
| `app/agents/los/summary.py` | Summary generation, validation and fallback |
| `app/security/auth.py` | JWT authentication |
| `app/config/*.yaml` | Agents, documents, KYC policy, risk policy |
| `requirements.txt` | Runtime dependencies, audited against every import |
| `requirements-dev.txt` | Test-only additions |
| `.env.example` | Every variable the code reads, placeholders only |

### Endpoints

| | |
|---|---|
| Local | `http://127.0.0.1:8010` |
| Swagger | `http://127.0.0.1:8010/docs` |
| ReDoc | `http://127.0.0.1:8010/redoc` |
| OpenAPI | `http://127.0.0.1:8010/openapi.json` |
| Production | *Not assigned. Supply your own base URL.* |

---

## 11. First things to do

1. Stand up an identity provider and set the three `JWT_*` variables.
2. Install Tesseract and Poppler in your image — the `Dockerfile` already
   does both.
3. `ollama pull qwen2.5:3b` if you want generated summaries, and read §4
   about what to expect on your hardware.
4. Get the risk policy signed off before production.
5. Decide where `case_id` is persisted.
6. Supply your own `samples/` if you want the sample-backed tests to run.
