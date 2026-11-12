"""
Build the standalone ELIGIBILITY folder (backend + frontend together) from this repo's current code.

    python scripts/build_eligibility_bundle.py [out_dir]     # default: ~/Downloads/eligibility-agent (+ .zip)

    eligibility-agent/
      README.md  start.ps1
      backend/   main.py, the Eligibility Agent (app/agents/eligibility + app/api/routes/eligibility_api.py) and ONLY
                 the shared LOS code it imports (auth, permissions, audit, agent DB), its config, requirements
      frontend/  the eligibility screen (Node 18+, no npm install): server.js + public/

The backend serves exactly what the frontend proxies: /api/v1/eligibility/* (same routes as the LOS server on 8010).
The build copies every app module the copied code imports (module level, and inside functions of the eligibility
code) and refuses to finish if one is missing. Lazy imports on paths the screen never takes are listed in OPTIONAL.
"""

from __future__ import annotations

import ast
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "eligibility-agent"
AGENT = "app/agents/eligibility"
SEEDS = [
    "app/__init__.py", "app/api/__init__.py", "app/api/routes/__init__.py", "app/api/routes/eligibility_api.py",
    "app/api/errors.py", "app/static/eligibility_ui.html", "app/config/eligibility_config.yaml",
    "app/config/agent_databases.yaml",
    # the shared LOS code the screen's routes load (traced 2026-11-03: ui, form, cases, cases/{id}, preview, check)
    "app/agent_db/__init__.py", "app/agent_db/core.py", "app/agent_db/redact.py", "app/agent_db/responses.py",
    "app/agent_db/rule_sets.py", "app/agent_db/rule_store.py", "app/agents/__init__.py",
    "app/agents/applicant/__init__.py", "app/agents/applicant/audit.py", "app/agents/applicant/config.py",
    "app/agents/applicant/copilot/__init__.py", "app/agents/applicant/copilot/semantics/__init__.py",
    "app/agents/applicant/copilot/semantics/intents.py", "app/agents/applicant/permissions.py",
    "app/core/__init__.py", "app/core/constants.py", "app/core/summary_points.py",
    "app/observability/__init__.py", "app/observability/tracing.py", "app/security/__init__.py",
    "app/security/auth.py",
    # where audit.record writes (lazy imports inside it)
    "app/observability/audit_log.py", "app/agents/applicant/chat_store.py",
]
# Files whose FUNCTION-LEVEL imports are followed too (their module-level imports always are, for every file).
FOLLOW_LAZY = ("app/agents/eligibility/", "app/api/routes/eligibility_api.py", "app/agent_db/")
# Lazy imports on paths the eligibility screen never takes -- left out on purpose.
OPTIONAL_PREFIXES = {
    "app.mcp": "GET /eligibility/{case_id} (the verdict the LOS pipeline recorded) -- LOS server only",
    "app.agents.document_agent": "OCR / extraction",
    "app.agents.credit": "the credit agents (bureau via the 'los' data provider; this bundle uses 'postgres')",
    "app.llm.provider": "the chat copilot's model client",
    "app.knowledge": "the chat copilot's knowledge base",
}
FRONTEND = ["server.js", "README.md", "public/index.html", "public/styles.css", "public/app.js"]

MAIN = '''"""
ELIGIBILITY AGENT -- backend on its own: the same /api/v1/eligibility/* routes as the LOS server.

    uvicorn main:app --host 127.0.0.1 --port 5180
    GET  /                                 the eligibility screen (../frontend/public; no Node needed)
    GET  /api/v1/eligibility/ui            the older built-in screen
    GET  /api/v1/eligibility/form          the screen's fields
    GET  /api/v1/eligibility/cases         cases to pick from
    POST /api/v1/eligibility/check         run the rules on a case
    GET  /health
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")    # real environment variables win
os.environ.setdefault("OTEL_ENABLED", "false")
(HERE / "runtime").mkdir(exist_ok=True)
os.environ.setdefault("LOS_DEV_PGDATA", str(HERE / "runtime" / "pgdata"))

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.agents.eligibility import config as eligibility_config  # noqa: E402
from app.api.routes.eligibility_api import router  # noqa: E402
from app.security.auth import require_jwt, validate_auth_mode  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
validate_auth_mode()          # refuses AUTH_ENABLED=false outside development
print(f"eligibility rules  : {eligibility_config.validate_for_startup()} -- data: {eligibility_config.provider_name()}")

app = FastAPI(title="Eligibility Agent API", version="1.0.0")
app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_jwt)])


@app.on_event("startup")
def _startup() -> None:
    # development only: no reachable ELIGIBILITY_DSN / LOS_AGENTIC_DSN -> the embedded dev PostgreSQL + demo cases
    try:
        from app.agents.eligibility.data import dev_db

        if dev_db.ensure():
            print("eligibility data   : DSN unreachable -> embedded dev PostgreSQL, database 'eligibility'")
    except Exception as exc:  # noqa: BLE001 - the screen reports it; the app still starts
        print(f"eligibility data   : not prepared ({type(exc).__name__})")
    try:   # the summary model warm before the first officer asks (background; never blocks start-up)
        from app.agents.eligibility.output import summary

        if summary.enabled() and "pytest" not in sys.modules:
            threading.Thread(target=summary.warm, name="eligibility-explain-warm", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy", "service": "eligibility-agent"}


# THE SCREEN, served by this backend (what frontend/server.js did, so Node is not needed): same origin, so the page's
# /api/v1/eligibility/* calls reach the routes above. Mounted LAST: every API route above is matched first.
SECURITY_HEADERS = {
    "content-security-policy": "; ".join([
        "default-src 'self'", "script-src 'self'", "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com", "img-src 'self' data:", "connect-src 'self'",
        "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'"]),
    "x-content-type-options": "nosniff", "x-frame-options": "DENY", "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()", "cross-origin-opener-policy": "same-origin",
}
SCREEN = HERE.parent / "frontend" / "public"


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):   # the API docs page needs its CDN
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
    return response


if SCREEN.is_dir():
    app.mount("/", StaticFiles(directory=SCREEN, html=True), name="screen")
'''

ENV = '''# Copy to .env (beside main.py). Real environment variables override this file. Never commit the real .env.

# ---- local: no token (development only) --------------------------------------------------------------------------
ENVIRONMENT=development
AUTH_ENABLED=false
AUTH_LOCAL_SCOPES=los.read agents:execute

# ---- the database: ONE Postgres for every agent, eligibility in its own schema `eligibility` -----------------------
# tables (eligibility_cases, eligibility_inputs, eligibility_runs) are created on first use (data/migrations.py)
LOS_AGENTIC_DSN=postgresql://postgres:<password>@localhost:5432/agentic_LOS
# or a database of its own (tables in public):
# ELIGIBILITY_DSN=postgresql://postgres:<password>@localhost:5432/los_eligibility

# ---- data source: postgres = the eligibility_cases table (app/config/eligibility_config.yaml data_provider) ----------
# ELIGIBILITY_DATA_PROVIDER=postgres
# ELIGIBILITY_ALLOW_DEMO_POLICY=true          # production refuses the DEMO policy unless this is true

# ---- the AI summary (optional; the rules answer never waits for it) ------------------------------------------------
# OLLAMA_HOST=http://127.0.0.1:11434
# OLLAMA_MODEL=qwen3:4b-instruct-2507-q4_K_M

# ---- production: tokens on (comment the three development lines above) ---------------------------------------------
# ENVIRONMENT=production
# JWT_ALGORITHM=RS256
# JWT_ISSUER=<your issuer>
# JWT_AUDIENCE=<your audience>
# JWT_JWKS_URL=https://<your-idp>/.well-known/jwks.json
#   a token needs the scope agents:execute
'''

START = r'''# One click: sets up Python the first time, then starts the backend, which serves the eligibility screen too, on :5180 (no Node).
#   double-click START.bat   (or: powershell -ExecutionPolicy Bypass -File .\start.ps1)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $here "backend"
$py = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  Write-Host "First run: creating backend\.venv and installing the requirements (a few minutes, once)..."
  $venv = Join-Path $backend ".venv"
  if (Get-Command py -ErrorAction SilentlyContinue) { & py -3.12 -m venv $venv }
  elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv $venv }
  if (-not (Test-Path $py)) { Write-Host "Python 3.12 is not installed: install it from python.org, then run this again."; Read-Host "Enter to close"; exit 1 }
  & $py -m pip install --disable-pip-version-check -q -r (Join-Path $backend "requirements.txt")
}
if (-not (Test-Path (Join-Path $backend ".env"))) { Copy-Item (Join-Path $backend ".env.example") (Join-Path $backend ".env"); Write-Host "backend\.env created from .env.example: set LOS_AGENTIC_DSN in it." }
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$backend'; & '$py' -m uvicorn main:app --host 127.0.0.1 --port 5180"
Start-Sleep -Seconds 8
Start-Process "http://localhost:5180"
Write-Host "Screen   http://localhost:5180     (API docs: http://localhost:5180/docs)"
'''

BAT = '''@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
'''

README = '''# Eligibility Agent -- backend + screen

The eligibility screen and the backend it runs on, on their own (no rest of the LOS needed). Same routes and rules as
the LOS server. The backend serves the screen itself, so only Python is needed (no Node.js).

```
eligibility-agent/
  START.bat   double-click: sets up Python the first time, starts everything, opens http://localhost:5180
  backend/    main.py, app/ (eligibility agent + the shared code it imports), requirements.txt, .env.example
  frontend/   the screen's files (public/), served by the backend; server.js = optional Node server, not needed
```

## Run

Double-click **START.bat**. The first time it creates backend/.venv and installs the requirements by itself; then it
starts the backend on :5180 and opens http://localhost:5180. Needs Python 3.12 installed.

* Database: `backend/.env` (`LOS_AGENTIC_DSN`). A new machine: copy `.env.example` to `.env` and set it.
* API docs: http://localhost:5180/docs
* Runs beside the full LOS server (that one is on 8010).

## Data

* Postgres schema `eligibility` in the database `LOS_AGENTIC_DSN` names (or `ELIGIBILITY_DSN`, tables in `public`).
  Tables `eligibility_cases`, `eligibility_inputs`, `eligibility_runs` are created on first use
  (`app/agents/eligibility/data/schema.sql`).
* Rules, products, LTV grid, form fields: `app/config/eligibility_config.yaml` (status DEMO_NON_PRODUCTION).
* Not in this bundle: `GET /api/v1/eligibility/{case_id}` (the verdict the LOS pipeline recorded on a LOS case) --
  that one needs the full LOS server.
'''


def _requirements() -> str:
    pins = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            pins[name.split("[")[0].strip().lower()] = (line.split("==")[0].strip(), ver.strip())
    wanted = ["fastapi", "uvicorn", "starlette", "pydantic", "python-dotenv", "pyyaml", "pyjwt", "cryptography",
              "httpx", "psycopg", "psycopg-binary", "psycopg-pool", "psycopg2-binary", "pgserver", "python-multipart",
              "opentelemetry-api", "opentelemetry-sdk", "orjson", "psutil"]
    lines = ["# Eligibility Agent backend -- Python 3.12 (64-bit). Versions: the LOS repo's requirements.txt.",
             "# pgserver = the embedded development PostgreSQL (not used when a DSN is set and reachable)."]
    import importlib.metadata as md
    for w in wanted:
        if w in pins:
            lines.append(f"{pins[w][0]}=={pins[w][1]}")
        else:
            try:
                lines.append(f"{w}=={md.version(w)}")
            except md.PackageNotFoundError:
                lines.append(w)
    return "\n".join(lines) + "\n"


def _imports(f: Path, backend: Path) -> list[tuple[str, bool]]:
    tree = ast.parse(f.read_text(encoding="utf-8-sig"))
    rel = f.relative_to(backend).as_posix()
    pkg = ".".join(f.relative_to(backend).with_suffix("").parts[:-1])
    lazy_nodes: set[int] = set()
    if not rel.startswith(FOLLOW_LAZY):
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
    return [(n, fn) for n, fn in out if n.split(".")[0] == "app"]


def _module_file(name: str) -> Path | None:
    base = ROOT / Path(*name.split("."))
    for f in (base.with_suffix(".py"), base / "__init__.py"):
        if f.is_file():
            return f
    return None


def _optional(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in OPTIONAL_PREFIXES)


def _missing_imports(backend: Path) -> dict[str, list[str]]:
    have = {p.relative_to(backend).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
            for p in backend.rglob("*.py")}
    missing: dict[str, list[str]] = {}
    for f in sorted(backend.rglob("*.py")):
        if f.name == "main.py" and f.parent == backend:
            continue
        for name, from_name in _imports(f, backend):
            if name in have:
                continue
            if from_name and name.rsplit(".", 1)[0] in have and _module_file(name) is None:
                continue
            missing.setdefault(name, []).append(f.relative_to(backend).as_posix())
    return missing


def _copy(src: Path, backend: Path) -> bool:
    dst = backend / src.relative_to(ROOT)
    if dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _close_over(backend: Path) -> list[str]:
    added = []
    while True:
        todo = [n for n in _missing_imports(backend) if not _optional(n) and _module_file(n)]
        if not todo:
            return added
        for name in todo:
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                f = _module_file(".".join(parts[:i]))
                if f is not None and _copy(f, backend):
                    added.append(f.relative_to(ROOT).as_posix())


def _config_files(backend: Path) -> list[str]:
    named = set()
    for f in backend.rglob("*.py"):
        named |= set(re.findall(r"([A-Za-z0-9_-]+\.ya?ml)\b", f.read_text(encoding="utf-8-sig")))
    added = []
    for p in sorted((ROOT / "app" / "config").rglob("*.y*ml")):
        if p.name in named and _copy(p, backend):
            added.append(p.relative_to(ROOT).as_posix())
    return added


def build(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    backend, frontend = out / "backend", out / "frontend"
    backend.mkdir(parents=True)
    for p in sorted((ROOT / AGENT).rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".sql", ".yaml", ".yml", ".json"):
            _copy(p, backend)
    for rel in SEEDS:
        _copy(ROOT / rel, backend)
    (backend / "main.py").write_text(MAIN, encoding="utf-8")
    (backend / ".env.example").write_text(ENV, encoding="utf-8")
    (backend / "requirements.txt").write_text(_requirements(), encoding="utf-8")
    (backend / "runtime").mkdir(exist_ok=True)
    (backend / "runtime" / ".keep").write_text("", encoding="utf-8")
    for rel in FRONTEND:
        dst = frontend / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "eligibility-frontend" / rel, dst)
    (out / "README.md").write_text(README, encoding="utf-8")
    (out / "start.ps1").write_text(START, encoding="utf-8")
    (out / "START.bat").write_text(BAT, encoding="utf-8")

    for _ in range(3):                     # config files can name modules' configs and vice versa: settle both
        added, configs = _close_over(backend), _config_files(backend)
        if not added and not configs:
            break
    missing = {k: v for k, v in _missing_imports(backend).items() if not _optional(k)}
    if missing:
        raise SystemExit("These app modules are imported but not in the bundle:\n  "
                         + "\n  ".join(f"{k}  <- {', '.join(sorted(set(v)))}" for k, v in sorted(missing.items())))
    archive = out.with_suffix(".zip")
    archive.unlink(missing_ok=True)
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
