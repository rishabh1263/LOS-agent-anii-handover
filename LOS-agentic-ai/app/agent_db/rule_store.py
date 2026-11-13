"""
THE RULES OF EVERY AGENT, READ FROM ITS SCHEMA (owner 2026-11-02: "rules db me config rahenge sabke"; config
app/config/agent_databases.yaml `rules`).

    load(name) -> content            the version in force (rule_set_active), cached `cache_seconds`: a version activated
                                     elsewhere is in force here within that time, no restart
    load_family(name) -> {member: content}
                                     a rule set kept one per file (docs_policy:<file name>)
    import_version(name, content, by=, note=, activate=) -> hash
                                     a new version (validated first by the set's validator); stored once
    activate(name, hash, by=, note=) the version in force switched -- rule_set_changes says who, when, from, to, why
    history(name), versions(name)    what was in force when; every version stored

WHERE A VERSION COMES FROM. The file named in config is only a source: scripts/rules.py import puts its content in
the database. Outside production, a rule set with no version yet is imported once from its file and activated (said
in the log); with no database configured at all, the file is read directly (a laptop). PRODUCTION never does either:
no database or no active version is an error that names the command to run (fail closed). A database that cannot be
read after a version was loaded keeps serving that version (said once) -- it was the one in force.
"""

from __future__ import annotations

import copy
import fnmatch
import importlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from app.agent_db import core, rule_sets

logger = logging.getLogger(__name__)

ROOT = core.ROOT
FAMILY_SEPARATOR = ":"

RULE_STORE_SQL = rule_sets.RULE_SETS_SQL + """
ALTER TABLE rule_sets ADD COLUMN IF NOT EXISTS created_by TEXT;
ALTER TABLE rule_sets ADD COLUMN IF NOT EXISTS note TEXT;

-- which version of each rule set is in force now
CREATE TABLE IF NOT EXISTS rule_set_active (
    name           TEXT        PRIMARY KEY,
    rule_set_hash  TEXT        NOT NULL REFERENCES rule_sets (rule_set_hash),
    activated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_by   TEXT        NOT NULL
);

-- every activation, append-only (who, when, from which version to which, why)
CREATE TABLE IF NOT EXISTS rule_set_changes (
    change_id   BIGSERIAL   PRIMARY KEY,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    name        TEXT        NOT NULL,
    from_hash   TEXT        REFERENCES rule_sets (rule_set_hash),
    to_hash     TEXT        NOT NULL REFERENCES rule_sets (rule_set_hash),
    changed_by  TEXT        NOT NULL,
    note        TEXT        NOT NULL,
    is_demo     BOOLEAN     NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS rule_set_changes_name ON rule_set_changes (name, changed_at);
""" + rule_sets.audit_guard_sql(["rule_set_changes"])

#: the rule store's tables, in every schema that holds rules (each agent's migration list carries it too)
MIGRATION: core.Migration = ("rules-0001", "rule_set_active, rule_set_changes: the rules in force, who changed them",
                             RULE_STORE_SQL)


class RulesUnavailable(RuntimeError):
    """The rules cannot be read where they must be (production without the database or without an active version)."""


class RulesInvalid(ValueError):
    """A version refused by its rule set's validator (never stored)."""


# ------------------------------------------------------------------------------------------------- config
def settings() -> dict[str, Any]:
    value = core.config().get("rules")
    if not isinstance(value, dict) or not isinstance(value.get("sets"), dict):
        raise RulesUnavailable("agent_databases.yaml has no rules.sets")
    return value


def _base(name: str) -> str:
    return name.split(FAMILY_SEPARATOR, 1)[0]


def spec(name: str) -> dict[str, Any]:
    try:
        return dict(settings()["sets"][_base(name)])
    except KeyError:
        raise RulesUnavailable(f"no rule set '{_base(name)}' in agent_databases.yaml rules.sets") from None


def names() -> list[str]:
    return list(settings()["sets"])


def production() -> bool:
    s = settings()
    environment = (os.getenv(str(s.get("environment_env") or "ENVIRONMENT")) or
                   str(s.get("environment_default") or "production")).strip().lower()
    return environment in {str(e).lower() for e in s.get("production_environments") or ["production"]}


def _path(value: str, env: str | None) -> Path:
    raw = (os.getenv(env) or "").strip() if env else ""
    path = Path(raw or value)
    return path if path.is_absolute() else ROOT / path


def source_files(name: str) -> dict[str, Path]:
    """{rule set name: its source file} -- one for a plain set, one per matching file for a family."""
    s = spec(name)
    if s.get("folder"):
        folder = _path(str(s["folder"]), s.get("folder_env"))
        found = sorted(folder.glob(str(s.get("pattern") or "*"))) if folder.is_dir() else []
        return {f"{_base(name)}{FAMILY_SEPARATOR}{f.name}": f for f in found}
    return {_base(name): _path(str(s["file"]), s.get("file_env"))}


def read_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)


def describe(name: str, content: Any) -> dict[str, Any]:
    """status / version label / demo flag of a version, by the set's configured keys."""
    s, data = spec(name), content if isinstance(content, dict) else {}
    status = data.get(str(s.get("status_key") or "status"))
    confirmed = list(s.get("confirmed") or [])
    is_confirmed = status in confirmed or str(status).upper() in {str(c).upper() for c in confirmed}
    version = data.get(str(s.get("version_key") or "version"))
    return {"status": None if status is None else str(status), "version_label": None if version is None else str(version),
            "is_demo": not is_confirmed}


# ------------------------------------------------------------------------------------------------- validation
def _shape_validator(name: str) -> Callable[[Any], None]:
    """A set without a validator of its own: a new version keeps every top-level section of the shipped file, each
    of the same kind (mapping / list / value) -- a section dropped or retyped by mistake is refused."""
    def check(content: Any) -> None:
        if not isinstance(content, dict):
            raise RulesInvalid("the rules must be a mapping")
        files = source_files(name)
        reference = files.get(name) or next(iter(files.values()), None)
        if reference is None or not reference.is_file():
            return
        shipped = read_file(reference) or {}
        kind = lambda v: dict if isinstance(v, dict) else list if isinstance(v, list) else object
        missing = [k for k in shipped if k not in content]
        retyped = [k for k in shipped if k in content and kind(shipped[k]) is not kind(content[k])]
        if missing or retyped:
            raise RulesInvalid(f"missing sections {missing}, sections of another kind {retyped}")
    return check


def validator(name: str) -> Callable[[Any], None]:
    target = spec(name).get("validator")
    if not target:
        return _shape_validator(name)
    module, _, function = str(target).partition(":")
    return getattr(importlib.import_module(module), function)


def validate(name: str, content: Any) -> None:
    try:
        validator(name)(copy.deepcopy(content))
    except RulesInvalid:
        raise
    except Exception as exc:  # noqa: BLE001 - the agent's own validator: its message, as a refusal
        raise RulesInvalid(f"{name}: {exc}") from None


# ------------------------------------------------------------------------------------------------- database
def _connect(name: str, *, write: bool = False):
    """A connection to the set's schema; None when no database is configured. Only a WRITE (import, activate, a first
    load) creates the rule store's tables -- reading never changes a database (2026-11-02: a read-only `list` once
    created them in the owner's databases)."""
    import psycopg

    value = core.dsn(str(spec(name)["agent"]))
    if not value:
        return None
    timeout = int((core.config().get("pool") or {}).get("connect_timeout_seconds", 5))
    conn = psycopg.connect(value, connect_timeout=timeout)
    if write:
        _migrate(conn, str(spec(name)["agent"]), value)
    return conn


def _has_tables(conn: Any) -> bool:
    return conn.execute("SELECT to_regclass('rule_set_active') IS NOT NULL").fetchone()[0]


_MIGRATED: set[str] = set()


def _migrate(conn: Any, agent: str, value: str) -> None:
    if value not in _MIGRATED:
        core.apply_migrations(conn, agent, [MIGRATION])
        _MIGRATED.add(value)


def _active(conn: Any, name: str) -> tuple[str, Any] | None:
    if not _has_tables(conn):
        return None
    row = conn.execute("SELECT s.rule_set_hash, s.content FROM rule_set_active a JOIN rule_sets s USING "
                       "(rule_set_hash) WHERE a.name = %s", (name,)).fetchone()
    return None if row is None else (row[0], row[1])


def _store(conn: Any, name: str, content: Any, by: str, note: str, source: Path | None) -> str:
    info = describe(name, content)
    with conn.cursor() as cur:
        digest = rule_sets.record(cur, name, content, version_label=info["version_label"], status=info["status"],
                                  source_files=[source] if source else [], is_demo=info["is_demo"])
        cur.execute("UPDATE rule_sets SET created_by = COALESCE(created_by, %s), note = COALESCE(note, %s) "
                    "WHERE rule_set_hash = %s", (by, note, digest))
    return digest


def _activate(conn: Any, name: str, digest: str, by: str, note: str) -> bool:
    row = conn.execute("SELECT rule_set_hash FROM rule_set_active WHERE name = %s FOR UPDATE", (name,)).fetchone()
    before = row[0] if row else None
    if before == digest:
        return False
    demo = conn.execute("SELECT is_demo FROM rule_sets WHERE rule_set_hash = %s", (digest,)).fetchone()
    if demo is None:
        raise RulesInvalid(f"no stored version {digest[:12]}… of {name}")
    conn.execute("INSERT INTO rule_set_active (name, rule_set_hash, activated_by) VALUES (%s, %s, %s) "
                 "ON CONFLICT (name) DO UPDATE SET rule_set_hash = EXCLUDED.rule_set_hash, activated_at = now(), "
                 "activated_by = EXCLUDED.activated_by", (name, digest, by))
    conn.execute("INSERT INTO rule_set_changes (name, from_hash, to_hash, changed_by, note, is_demo) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (name, before, digest, by, note, bool(demo[0])))
    return True


def _who(by: str, note: str) -> tuple[str, str]:
    by, note = str(by or "").strip(), str(note or "").strip()
    if not by or not note:
        raise RulesInvalid("a rules change names the person (by) and the reason (note)")
    return by, note


def import_version(name: str, content: Any, *, by: str, note: str, activate: bool = False,
                   source: Path | None = None) -> str:
    """Validate and store a version (once); with activate=True also put it in force. Its hash."""
    by, note = _who(by, note)
    validate(name, content)
    conn = _connect(name, write=True)
    if conn is None:
        raise RulesUnavailable(f"the {spec(name).get('title', name)} database is not configured")
    with conn:
        with conn.transaction():
            digest = _store(conn, name, content, by, note, source)
            if activate:
                _activate(conn, name, digest, by, note)
    forget()
    return digest


def activate(name: str, digest: str, *, by: str, note: str) -> bool:
    """Put a stored version in force. False when it already was."""
    by, note = _who(by, note)
    conn = _connect(name, write=True)
    if conn is None:
        raise RulesUnavailable(f"the {spec(name).get('title', name)} database is not configured")
    with conn:
        with conn.transaction():
            row = conn.execute("SELECT rule_set_hash, content FROM rule_sets WHERE rule_set_hash LIKE %s",
                               (digest + "%",)).fetchall()
            if len(row) != 1:
                raise RulesInvalid(f"{digest!r} names {len(row)} stored versions of {name}")
            validate(name, row[0][1])
            changed = _activate(conn, name, row[0][0], by, note)
    forget()
    return changed


def history(name: str) -> list[dict[str, Any]]:
    conn = _connect(name)
    if conn is None:
        return []
    with conn:
        if not _has_tables(conn):
            return []
        rows = conn.execute("SELECT changed_at, from_hash, to_hash, changed_by, note FROM rule_set_changes "
                            "WHERE name = %s ORDER BY change_id", (name,)).fetchall()
    return [dict(zip(("changed_at", "from_hash", "to_hash", "changed_by", "note"), r)) for r in rows]


def versions(name: str) -> list[dict[str, Any]]:
    conn = _connect(name)
    if conn is None:
        return []
    with conn:
        if not _has_tables(conn):
            return []
        rows = conn.execute(
            "SELECT s.rule_set_hash, s.version_label, s.status, s.is_demo, s.first_loaded_at, s.created_by, s.note, "
            "a.name IS NOT NULL FROM rule_sets s LEFT JOIN rule_set_active a ON a.rule_set_hash = s.rule_set_hash AND "
            "a.name = s.name WHERE s.name = %s OR s.name LIKE %s ORDER BY s.first_loaded_at",
            (name, name + FAMILY_SEPARATOR + "%")).fetchall()
    keys = ("hash", "version_label", "status", "is_demo", "first_loaded_at", "created_by", "note", "active")
    return [dict(zip(keys, r)) for r in rows]


def in_force(name: str) -> tuple[str, Any] | None:
    """READ ONLY (preflight, reports): (hash, content) of the version in force in the database, None when there is
    none -- never a first load, never the file, never the cache."""
    conn = _connect(name)
    if conn is None:
        raise RulesUnavailable(f"{name}: no database configured")
    with conn:
        return _active(conn, name)


def active_names(name: str) -> list[str]:
    conn = _connect(name)
    if conn is None:
        return []
    with conn:
        if not _has_tables(conn):
            return []
        return [r[0] for r in conn.execute("SELECT name FROM rule_set_active WHERE name = %s OR name LIKE %s "
                                           "ORDER BY name", (name, name + FAMILY_SEPARATOR + "%"))]


# ------------------------------------------------------------------------------------------------- loading
_CACHE: dict[str, tuple[float, str, Any]] = {}


def _key(value: str, name: str) -> str:
    """The cache key: the database (by its connection, hashed -- never kept as text) and the rule set."""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] + "|" + name
_LOCK = threading.RLock()
_SAID: set[str] = set()


def forget() -> None:
    """Drop the cached versions (after a change here; tests)."""
    with _LOCK:
        _CACHE.clear()
        _FILES.clear()
        _MIGRATED.clear()


def _say_once(key: str, message: str, *args: Any) -> None:
    if key not in _SAID:
        _SAID.add(key)
        logger.warning(message, *args)


def _from_database(name: str, source: Path | None) -> tuple[str, Any] | None:
    """(hash, content) in force; imported first from its file outside production; None = no database configured."""
    conn = _connect(name)
    if conn is None:
        return None
    with conn:
        found = _active(conn, name)
        if found is None:
            s = settings()
            if production() or not s.get("import_missing_outside_production", False):
                raise RulesUnavailable(
                    f"no {name} rules are in force in the database: run  python scripts/rules.py import {name} "
                    "--by <your name> --note <why> --activate")
            if source is None or not source.is_file():
                raise RulesUnavailable(f"no {name} rules in the database and no file to load them from")
            content = read_file(source)
            validate(name, content)
            _migrate(conn, str(spec(name)["agent"]), core.dsn(str(spec(name)["agent"])) or "")
            with conn.transaction():
                # one process loads it: another one starting at the same moment waits here, then finds it in force
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("rules:" + name,))
                found = _active(conn, name)
                if found is None:
                    digest = _store(conn, name, content, "system:first-load", f"first load from {source.name}", source)
                    _activate(conn, name, digest, "system:first-load", f"first load from {source.name}")
                    logger.warning("%s rules: none in the database yet -- loaded from %s and put in force (%s)",
                                   name, source.name, digest[:12])
                    found = (digest, content)
        elif source is not None and source.is_file() and rule_sets.fingerprint(read_file(source)) != found[0]:
            _say_once(f"differs:{name}", "%s rules: the database version %s is in force; %s differs from it -- "
                      "python scripts/rules.py import %s puts the file's version in", name, found[0][:12], source.name,
                      _base(name))
        return found


_FILES: dict[str, tuple[float, str, Any]] = {}


def _file(source: Path) -> tuple[str, Any]:
    """(hash, content) of a file, re-read when it changes."""
    key, stamp = str(source.resolve()), source.stat().st_mtime
    with _LOCK:
        cached = _FILES.get(key)
        if cached and cached[0] == stamp:
            return cached[1], cached[2]
    content = read_file(source)
    digest = rule_sets.fingerprint(content)
    with _LOCK:
        _FILES[key] = (stamp, digest, content)
    return digest, content


def _from_file(name: str, source: Path | None) -> tuple[str, Any]:
    """No database configured, outside production: the file itself (re-read when it changes)."""
    if production() or not settings().get("file_without_database_outside_production", False):
        raise RulesUnavailable(f"{name}: the rules live in the database and none is configured "
                               f"(set {core.config().get('shared_dsn_env', 'LOS_AGENTIC_DSN')})")
    if source is None or not source.is_file():
        raise RulesUnavailable(f"{name}: no database configured and no file {source}")
    return _file(source)


def file_sections(name: str) -> list[str]:
    return [str(x) for x in spec(name).get("file_sections") or []]


def file_section(name: str, section: str) -> Any:
    """A DEPLOYMENT section of a set (config file_sections): always its file, in every environment -- never the
    database (where the database is cannot be read from the database)."""
    source = source_files(name).get(name)
    if source is None or not source.is_file():
        raise RulesUnavailable(f"{name}: no file {source} for its deployment settings")
    return (_file(source)[1] or {}).get(section)


def _overlay(name: str, content: Any, source: Path | None) -> Any:
    """The version from the database with its deployment sections taken from the file (config file_sections)."""
    sections = file_sections(name)
    if not sections or not isinstance(content, dict) or source is None or not source.is_file():
        return content
    local = _file(source)[1] or {}
    return {**content, **{k: local[k] for k in sections if k in local}}


def _resolve(name: str, source: Path | None) -> tuple[str, Any]:
    """(hash, content) of the version in force."""
    value = core.dsn(str(spec(name)["agent"]))
    if not value:
        return _from_file(name, source)
    key = _key(value, name)                                   # one cache per database: never another one's rules
    now = time.monotonic()
    ttl = float(settings().get("cache_seconds", 60))
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1], cached[2]
    try:
        found = _from_database(name, source)
    except RulesUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - the database could not be read
        if cached:
            _say_once(f"down:{name}", "%s rules: the database could not be read (%s) -- the version in force (%s) "
                      "stays", name, type(exc).__name__, cached[1][:12])
            return cached[1], cached[2]
        raise RulesUnavailable(f"the {name} rules could not be read from the database ({type(exc).__name__})") \
            from None
    if found is None:                                         # the DSN went away between the two looks
        return _from_file(name, source)
    found = (found[0], _overlay(name, found[1], source))
    with _LOCK:
        _CACHE[key] = (now, found[0], found[1])
    return found


def _load(name: str, source: Path | None) -> Any:
    return _resolve(name, source)[1]


def load(name: str) -> Any:
    """The content of the version in force -- the cached object itself (read it, never change it; a caller that
    changes what it reads takes copy.deepcopy first)."""
    return _load(name, source_files(name).get(name))


def load_with_hash(name: str) -> tuple[str, Any]:
    """(hash of the version in force, its content) -- the hash a decision stores as rule_set_hash."""
    return _resolve(name, source_files(name).get(name))


def load_family(name: str) -> dict[str, Any]:
    """{member name: content} of a set kept one per file: every member in force in the database; outside production
    a file with no version yet is imported. The member names are `<set>:<file name>`."""
    files = source_files(name)
    value = core.dsn(str(spec(name)["agent"]))
    key, now = _key(value or "", f"family{FAMILY_SEPARATOR}{name}"), time.monotonic()
    with _LOCK:
        cached = _CACHE.get(key)
    if not value:
        known = set()                                        # no database: the files (outside production)
    elif cached and now - cached[0] < float(settings().get("cache_seconds", 60)):
        known = cached[2]
    else:
        try:
            known = set(active_names(name))
        except Exception:  # noqa: BLE001 - the database could not be read: the members known before stay
            if not cached:
                raise RulesUnavailable(f"the {name} rules could not be read from the database") from None
            known = cached[2]
        with _LOCK:
            _CACHE[key] = (now, "", known)
    if production():
        missing = sorted(set(files) - known)
        if missing:
            _say_once(f"family:{missing}", "%s: files with no version in the database are not used: %s", name,
                      ", ".join(missing))
        members = known
    else:
        members = known | set(files)
    return {m: _load(m, files.get(m)) for m in sorted(members)}


__all__ = ["MIGRATION", "RulesInvalid", "RulesUnavailable", "activate", "active_names", "describe", "forget",
           "file_section", "file_sections", "history", "import_version",
           "in_force", "load", "load_family", "load_with_hash", "names", "production",
           "read_file", "settings", "source_files", "spec", "validate", "versions"]
