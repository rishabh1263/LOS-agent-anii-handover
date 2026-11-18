"""
Build the standalone CREDIT DOCUMENT DESK folder (backend + frontend together) from this repo's current code.

    python scripts/build_docs_credit_bundle.py [out_dir]     # default: ~/Downloads/credit-document-desk (+ .zip)

    credit-document-desk/
      README.md  start.ps1  check.py
      backend/   main.py, the Credit Document Policy Agent (app/agents/docs_verify_credit_agent) and ONLY the shared
                 LOS code it runs on (case store, auth, product checklist, LOS queries), the demo seed, requirements
      frontend/  the dashboard (Node 18+, no npm install): server.js + public/

The shared file list is what the agent loads when it evaluates the 14 demo cases (measured, not guessed); the build
refuses to finish if any copied file imports an app module that is not copied.
"""

from __future__ import annotations

import ast
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "credit-document-desk"
AGENT = "app/agents/docs_verify_credit_agent"

# the shared LOS code the agent runs on (traced: seed + both routes over the 14 demo cases + the credit reader)
SHARED = [
    "app/__init__.py", "app/agents/__init__.py",
    "app/agents/applicant/__init__.py", "app/agents/applicant/actions.py", "app/agents/applicant/audit.py",
    "app/agents/applicant/case_memory_facts.py", "app/agents/applicant/config.py",
    "app/agents/applicant/copilot/__init__.py", "app/agents/applicant/copilot/agent.py",
    "app/agents/applicant/copilot/answering/__init__.py", "app/agents/applicant/copilot/answering/answer.py",
    "app/agents/applicant/copilot/caching.py", "app/agents/applicant/copilot/conversation/__init__.py",
    "app/agents/applicant/copilot/conversation/followup.py", "app/agents/applicant/copilot/facts/__init__.py",
    "app/agents/applicant/copilot/facts/document_facts.py", "app/agents/applicant/copilot/routing/__init__.py",
    "app/agents/applicant/copilot/routing/subjects.py", "app/agents/applicant/copilot/semantics/__init__.py",
    "app/agents/applicant/copilot/semantics/intents.py", "app/agents/applicant/counters.py",
    "app/agents/applicant/delay.py", "app/agents/applicant/facts.py", "app/agents/applicant/grounding.py",
    "app/agents/applicant/history.py", "app/agents/applicant/impact.py", "app/agents/applicant/income_facts.py",
    "app/agents/applicant/knowledge_answer.py", "app/agents/applicant/ledger.py",
    "app/agents/applicant/permissions.py", "app/agents/applicant/query_types.py", "app/agents/applicant/routing.py",
    "app/agents/applicant/status_facts.py", "app/agents/applicant/workflow.py",
    "app/agents/credit/__init__.py", "app/agents/credit/repayment/__init__.py",
    "app/agents/credit/repayment/ai_summary.py", "app/agents/credit/repayment/config.py",
    "app/agents/credit/schemas.py",
    "app/agents/los/__init__.py", "app/agents/los/co_applicants.py", "app/agents/los/config.py",
    "app/agents/los/parties.py", "app/agents/los/queries.py", "app/agents/los/stages.py",
    "app/agents/policy/__init__.py", "app/agents/policy/engine.py", "app/agents/policy/loader.py",
    "app/agents/signature/__init__.py", "app/agents/signature/analysis.py", "app/agents/signature/config.py",
    "app/agents/signature/presence.py", "app/agents/signature/schemas.py", "app/agents/signature/service.py",
    "app/agents/verification/__init__.py", "app/agents/verification/agent.py", "app/agents/verification/basic.py",
    "app/agents/verification/slots.py", "app/agents/verification/taxonomy.py",
    "app/config/applicant_agent.yaml", "app/config/documents.yaml", "app/config/stage_requirements.yaml",
    "app/core/__init__.py", "app/core/text_match.py",
    "app/knowledge/__init__.py", "app/knowledge/embeddings.py", "app/knowledge/markdown_repo.py",
    "app/knowledge/models.py", "app/knowledge/repository.py", "app/knowledge/retriever.py",
    "app/llm/__init__.py", "app/llm/config.py",
    "app/observability/__init__.py", "app/observability/audit_log.py", "app/observability/tracing.py",
    "app/security/__init__.py", "app/security/access.py", "app/security/auth.py",
    "app/services/__init__.py", "app/services/verification_config.py",
    "app/store/__init__.py", "app/store/crypto.py", "app/store/documents.py", "app/store/ingest.py",
    "app/store/models.py", "app/store/postgres_repo.py", "app/store/repository.py", "app/store/request_cache.py",
    "app/store/sql_repo.py",
]
SHARED_DIRS = ["app/config/policies"]              # the product checklists (every *.yaml)
# Lazy imports (inside a function) of the shared LOS code on paths this agent never takes -- left out on purpose.
# Every OTHER app import of a copied file is copied too (the build closes over them), so the bundle never fails with
# an ImportError on a path the demo did not happen to take (e.g. the AI note's checks on a cold cache).
OPTIONAL_PREFIXES = {
    "app.agents.applicant.copilot.": "the chat copilot; the agent only reads the applicant checklist config",
    "app.mcp": "the chat copilot's tools",
    "app.agents.document_agent": "OCR / extraction; the agent reads recorded results, never re-reads a file",
    "app.agents.financial": "financial-document verification internals; never called here",
    "app.agents.kyc": "the KYC agent; never called here",
    "app.agents.verification.reasons": "verification wording for the chat; never called here",
    "app.agents.verification.rules": "verification rules (PAN structure); the agent never verifies",
    "app.knowledge.grounding": "the chat copilot's grounding",
    "app.llm.provider": "the chat copilot's model client",
    "app.agents.los.summary": "the chat copilot's model keep-alive",
    "app.agents.bank_statement": "the OCR worker re-reading a statement (LOS_OCR_WORKER_ENABLED, off by default)",
    "app.agents.verification.bank_statement_checks": "the OCR worker's statement checks (worker off by default)",
    "app.agents.verification.scoring": "the OCR worker's statement scoring (worker off by default)",
    "app.store.demo_seed": "the LOS demo index seed, only with the demo index env on (off by default)",
}
FRONTEND = ["server.js", "public/index.html", "public/styles.css", "public/app.js", "public/theme.js"]

MAIN = '''"""
CREDIT DOCUMENT DESK -- backend: the Credit Document Policy Agent on its own.

    uvicorn main:app --host 127.0.0.1 --port 8020
    POST /api/v1/credit/document-readiness/{case_id}          the readiness answer (JSON)
    POST /api/v1/credit/document-readiness/{case_id}/rules    the same + the dashboard's rules board / checklist
    GET  /health
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")    # real environment variables win
os.environ.setdefault("OTEL_ENABLED", "false")
# the case store and its files live beside this file, whatever folder the server is started from
(HERE / "runtime").mkdir(exist_ok=True)
os.environ.setdefault("LOS_DEV_PGDATA", str(HERE / "runtime" / "pgdata"))
os.environ.setdefault("LOS_DOCUMENT_STORE_PATH", str(HERE / "runtime" / "documents"))

from fastapi import Depends, FastAPI  # noqa: E402

from app.agents.docs_verify_credit_agent.api.routes import router  # noqa: E402
from app.agents.docs_verify_credit_agent.config import reasons  # noqa: E402
from app.security.auth import require_jwt, validate_auth_mode  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
validate_auth_mode()          # refuses AUTH_ENABLED=false outside development
reasons.get()                 # a broken config/reasons.yaml stops start-up by name

app = FastAPI(title="Credit Document Desk API", version="1.0.0")
app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_jwt)])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy", "service": "credit-document-desk"}
'''

ENV = '''# Copy to .env (beside main.py). Real environment variables override this file.

# ---- local demo: no token, embedded PostgreSQL under backend/runtime (development only) --------------------------
ENVIRONMENT=development
AUTH_ENABLED=false
AUTH_LOCAL_SCOPES=los.credit.underwrite

# ---- a real case store (leave empty for the embedded development one) ---------------------------------------------
# LOS_STORE_DSN=postgresql://user:password@host:5432/los

# ---- the LOS behaviour the answers are built on (as in the LOS server; the demo's expected answers assume these) ---
# the documents' read values (dates, periods) are kept in the case store: RULE-06 needs them
LOS_CASE_MEMORY_ENABLED=true
# only a recorded STAGE_ENTERED event says a case is at a stage
LOS_STAGE_GATE_IN_SERVICE=true
# a co-applicant gives their own PAN, address proof, employment proof and signature
LOS_COAPP_MANDATORY_DOCS=true
# true on the LOS store once its co-applicant identity migration (0004) is applied; a fresh demo store has none
LOS_COAPP_IDENTITY=false
# no OCR here: this service reads what the document agents recorded
LOS_OCR_WORKER_ENABLED=false
# the JEV decision layer is not part of this service
JEV_ENABLED=false

# ---- production: tokens on (comment the three demo lines above) ----------------------------------------------------
# ENVIRONMENT=production
# JWT_ALGORITHM=RS256
# JWT_ISSUER=<your issuer>
# JWT_AUDIENCE=<your audience>
# JWT_JWKS_URL=https://<your-idp>/.well-known/jwks.json
#   a token needs the scope los.credit.underwrite (DOCS_CREDIT_SCOPES to change it)
# DOCS_CREDIT_ALLOW_SAMPLE_POLICY=false     # production refuses the SAMPLE policies unless this is true

# ---- the AI note (optional; the rules answer never waits for it) ---------------------------------------------------
# OLLAMA_HOST=http://127.0.0.1:11434
# OLLAMA_MODEL=qwen2.5:3b-instruct
# DOCS_CREDIT_AI_SUMMARY=false              # rules-only note

# ---- optional ---------------------------------------------------------------------------------------------------
# DOCS_CREDIT_RULES_OFF=RULE-07,RULE-11     # switch rules off on top of the policy (RULE-10, audit, cannot be)
# DOCS_CREDIT_TIMEOUT_SECONDS=10
'''

START = r'''# Starts the backend (:8020) and the dashboard (:5182) in two windows, after seeding the 14 demo cases once.
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "Install first (README step 1): backend\.venv is missing."; exit 1 }
if (-not (Test-Path (Join-Path $here "backend\runtime\pgdata"))) {
  Write-Host "Seeding the 14 demo cases (first run only)..."
  Push-Location (Join-Path $here "backend")          # the case store lives under backend\runtime
  & $py "scripts\seed_docs_credit_demo.py" --quiet
  Pop-Location
}
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$here\backend'; & '$py' -m uvicorn main:app --host 127.0.0.1 --port 8020"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$here\frontend'; `$env:PORT='5182'; `$env:BACKEND_URL='http://127.0.0.1:8020'; node server.js"
Write-Host "Backend   http://127.0.0.1:8020/docs"
Write-Host "Dashboard http://localhost:5182  (try CASE-D0C500000005)"
'''


def _requirements() -> str:
    pins = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            pins[name.split("[")[0].strip().lower()] = (line.split("==")[0].strip(), ver.strip())
    wanted = ["fastapi", "uvicorn", "starlette", "pydantic", "python-dotenv", "pyyaml", "pyjwt", "cryptography",
              "httpx", "psycopg", "psycopg-binary", "psycopg-pool", "pgserver", "python-multipart",
              "opentelemetry-api", "opentelemetry-sdk", "orjson", "psutil"]
    lines = ["# Credit Document Desk backend -- Python 3.12 (64-bit). Versions: the LOS repo's requirements.txt.",
             "# pgserver = the embedded development PostgreSQL (not used when LOS_STORE_DSN is set)."]
    for w in wanted:
        if w in pins:
            lines.append(f"{pins[w][0]}=={pins[w][1]}")
        else:
            import importlib.metadata as md
            try:
                lines.append(f"{w}=={md.version(w)}")
            except md.PackageNotFoundError:
                lines.append(w)
    return "\n".join(lines) + "\n"


# The applicant package is loaded only for its checklist config (policy_engine -> applicant.config); its package
# __init__ pulls the chat modules in, whose FUNCTION-LEVEL imports are the chat's own paths. Those are not followed
# (module-level imports of these files always are).
CHAT_FILES = ("app/agents/applicant/",)


def _imports(f: Path, backend: Path) -> list[tuple[str, bool]]:
    """(module, is_from_name) for every app.* import in a file -- module level, or inside a function unless the
    file is chat code (CHAT_FILES)."""
    tree = ast.parse(f.read_text(encoding="utf-8-sig"))
    rel = f.relative_to(backend).as_posix()
    pkg = ".".join(f.relative_to(backend).with_suffix("").parts[:-1])
    skip_lazy = rel.startswith(CHAT_FILES)
    lazy_nodes = set()
    if skip_lazy:
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lazy_nodes |= {id(n) for n in ast.walk(fn)}
    out = []
    for node in ast.walk(tree):
        if id(node) in lazy_nodes:
            continue
        if isinstance(node, ast.Import):
            out += [(a.name, False) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")[: len(pkg.split(".")) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            out += [(base, False)] + [(f"{base}.{a.name}", True) for a in node.names]
    return [(n, from_name) for n, from_name in out if n.split(".")[0] == "app"]


def _module_file(name: str) -> Path | None:
    base = ROOT / Path(*name.split("."))
    for f in (base.with_suffix(".py"), base / "__init__.py"):
        if f.is_file():
            return f
    return None


def _optional(name: str) -> bool:
    return any(name == p.rstrip(".") or name.startswith(p if p.endswith(".") else p + ".") or name.startswith(p)
               for p in OPTIONAL_PREFIXES)


def _missing_imports(backend: Path) -> dict[str, list[str]]:
    """app.* modules imported somewhere in the copied code (even lazily, inside a function) but not copied.
    `from pkg import x` needs x's file when x is a module in the repo; otherwise x is a name inside pkg."""
    have = {p.relative_to(backend).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
            for p in backend.rglob("*.py")}
    missing: dict[str, list[str]] = {}
    for f in sorted(backend.rglob("*.py")):
        for name, from_name in _imports(f, backend):
            if name in have:
                continue
            if from_name and name.rsplit(".", 1)[0] in have and _module_file(name) is None:
                continue                              # a name defined inside a copied module
            missing.setdefault(name, []).append(f.relative_to(backend).as_posix())
    return missing


def _close_over(backend: Path) -> list[str]:
    """Copy every non-optional app module the copied code imports (and the packages on its way) until none is
    missing. Returns what was added."""
    added = []
    while True:
        todo = [n for n in _missing_imports(backend) if not _optional(n) and _module_file(n)]
        if not todo:
            return added
        for name in todo:
            parts = name.split(".")
            for i in range(1, len(parts) + 1):                     # the module and every package __init__ above it
                f = _module_file(".".join(parts[:i]))
                if f is not None and not (backend / f.relative_to(ROOT)).exists():
                    dst = backend / f.relative_to(ROOT)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                    added.append(f.relative_to(ROOT).as_posix())


def _config_files(backend: Path) -> list[str]:
    """Every app/config yaml a bundled module names (its own config, read on some path): copied so no path finds
    its config missing. Returns what was added."""
    import re

    named = set()
    for f in backend.rglob("*.py"):
        named |= set(re.findall(r"([A-Za-z0-9_-]+\.ya?ml)\b", f.read_text(encoding="utf-8-sig")))
    added = []
    for p in sorted((ROOT / "app" / "config").rglob("*.y*ml")):
        dst = backend / p.relative_to(ROOT)
        if p.name in named and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            added.append(p.relative_to(ROOT).as_posix())
    return added


def build(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    backend, frontend = out / "backend", out / "frontend"
    for p in sorted((ROOT / AGENT).rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            dst = backend / p.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    for rel in SHARED:
        dst = backend / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    for d in SHARED_DIRS:
        for p in sorted((ROOT / d).glob("*.yaml")):
            dst = backend / p.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    (backend / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts" / "seed_docs_credit_demo.py", backend / "scripts" / "seed_docs_credit_demo.py")
    (backend / "main.py").write_text(MAIN, encoding="utf-8")
    (backend / ".env.example").write_text(ENV, encoding="utf-8")
    (backend / "requirements.txt").write_text(_requirements(), encoding="utf-8")
    (backend / "runtime").mkdir(exist_ok=True)
    (backend / "runtime" / ".keep").write_text("", encoding="utf-8")
    for rel in FRONTEND:
        dst = frontend / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "docs-credit-frontend" / rel, dst)
    bundle = ROOT / "scripts" / "docs_credit_bundle"
    for name in ("README.md", "check.py", "expected_demo.json"):
        shutil.copy2(bundle / name, out / name)
    shutil.copy2(bundle / "frontend_README.md", frontend / "README.md")
    (out / "start.ps1").write_text(START, encoding="utf-8")

    added = _close_over(backend)
    if added:
        print(f"closed over {len(added)} more module(s): {', '.join(added)}")
    configs = _config_files(backend)
    if configs:
        print(f"config files named by the code: {', '.join(configs)}")
    missing = {k: v for k, v in _missing_imports(backend).items() if not _optional(k)}
    if missing:
        raise SystemExit("These app modules are imported but not in the bundle:\n  "
                         + "\n  ".join(f"{k}  <- {', '.join(sorted(set(v)))}" for k, v in sorted(missing.items())))
    archive = out.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, p.relative_to(out.parent).as_posix())
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / NAME
    done = build(target)
    files = sum(1 for p in done.rglob("*") if p.is_file())
    print(f"wrote {done} ({files} files) and {done.with_suffix('.zip')}")
