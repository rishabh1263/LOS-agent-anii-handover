"""
THE RULE BOARD for the demo dashboard: all eleven rules for one case, each with one status, built from the SAME
evaluation the API answers with (readiness_service.evaluate_with_outcomes). Nothing is re-decided here.

    PASS   every check of the rule is satisfied (or validly waived)
    WARN   something is open, but nothing that blocks (a non-critical failure)
    FAIL   at least one BLOCKING check is open (critical)
    N/A    the rule had nothing to check on this case (no exception raised, no ITR given, ...)
    OFF    the rule is switched off (policy `rules` / DOCS_CREDIT_RULES_OFF)

RULE-10 (audit) is PASS when the evaluation was recorded, FAIL when it was not.
"""

from __future__ import annotations

from functools import lru_cache

from app.agents.docs_verify_credit_agent.config import reasons as T
from pathlib import Path
from typing import Any

from app.agents.docs_verify_credit_agent.models import requirement as R

_CONFIG = Path(__file__).resolve().parents[1] / "config" / "dashboard.yaml"


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8")) or {}


def _status(checks: list[dict]) -> str:
    open_ = [c for c in checks if c["status"] not in R.DONE]
    if any(c["severity"] == R.BLOCKING for c in open_):
        return "FAIL"
    return "WARN" if open_ else "PASS"


def sections(board: list[dict]) -> list[dict]:
    """The configured sections, each with its rules' statuses counted (every rule exactly once)."""
    by_id = {r["rule_id"]: r for r in board}
    out, seen = [], set()
    for section in config().get("sections") or []:
        ids = [r for r in section.get("rules") or [] if r in by_id and r not in seen]
        seen.update(ids)
        statuses = [by_id[r]["status"] for r in ids]
        out.append({"id": section.get("id"), "name": section.get("name"), "help": section.get("help", ""),
                     "rules": ids, "counts": {s: statuses.count(s) for s in ("PASS", "WARN", "FAIL", "N/A", "OFF")}})
    rest = [r for r in by_id if r not in seen]
    if rest:                                                   # a rule no section names is still shown
        out.append({"id": "other", "name": "Other", "help": "", "rules": rest,
                    "counts": {s: [by_id[r]["status"] for r in rest].count(s) for s in ("PASS", "WARN", "FAIL",
                                                                                         "N/A", "OFF")}})
    return out


def build(answer: Any, outcomes: list[dict]) -> list[dict]:
    help_, explain = config().get("rule_help") or {}, config().get("rule_explain") or {}
    disabled = set(answer.audit.rules_disabled or [])
    board = []
    for rule_id, name in R.RULES.items():
        checks = [{"check_id": o["check_id"], "status": o["status"], "severity": o["severity"],
                   "party_role": o.get("party_role"), "reason": o["reason"]}
                  for o in outcomes if o.get("rule_id") == rule_id]
        if rule_id == "RULE-10":
            status = "PASS" if answer.audit.recorded else "FAIL"
            checks = [{"check_id": "AUDIT", "status": "SATISFIED" if answer.audit.recorded else "UNRESOLVED",
                       "severity": R.BLOCKING, "party_role": None,
                       "reason": T.say("audit_recorded", evaluation_id=answer.audit.evaluation_id)
                       if answer.audit.recorded else T.say("audit_not_recorded")}]
        elif rule_id in disabled:
            status = "OFF"
        elif not checks:
            status = "N/A"
        else:
            status = _status(checks)
        board.append({"rule_id": rule_id, "name": name, "help": help_.get(rule_id, ""),
                      "explain": explain.get(rule_id, ""), "status": status,
                      "passed": sum(1 for c in checks if c["status"] in R.DONE), "total": len(checks),
                      "checks": checks})
    return board


# ---- the document checklist: one row per required document, one column per person ----------------------------

#: rules whose outcome is THE outcome of one document requirement of one person (check_id "ROLE:SLOT")
_SLOT_RULES = {"RULE-02", "RULE-03", "RULE-04", "RULE-05", "RULE-08", "RULE-09"}
#: rules that find a problem with a document that IS on file (dates, file); joined to the requirement by document id
_QUALITY_RULES = {"RULE-06", "RULE-11"}
ROLES = ("PRIMARY_APPLICANT", "CO_APPLICANT")


def _docs(outcome: dict) -> list[str]:
    return [e.split(":", 1)[1] for e in outcome.get("evidence") or [] if str(e).startswith("document:")]


def _label(slot: str) -> str:
    labels = config().get("labels") or {}
    return labels.get(slot) or slot.replace("_", " ").capitalize()


def _cell(outcome: dict, issues: list[dict]) -> dict:
    status = outcome["status"]
    if status == R.SATISFIED:
        state = ("ISSUE" if any(i["severity"] == R.BLOCKING for i in issues) else "MINOR") if issues else "OK"
    elif status == R.WAIVED:
        state = "WAIVED"
    elif status == R.EXCEPTION_PENDING:
        state = "AWAITING_APPROVAL"
    elif status in (R.PENDING_VERIFICATION, R.UNDER_REVIEW):
        state = "IN_REVIEW"
    elif status == R.ACTION_REQUIRED:
        state = "REPLACE" if _docs(outcome) else "MISSING"
    else:
        state = "CHECK"
    open_blocking = status not in R.DONE and outcome["severity"] == R.BLOCKING
    return {"state": state, "blocking": open_blocking or any(i["severity"] == R.BLOCKING for i in issues),
            "reasons": [outcome["reason"]] + [i["reason"] for i in issues],
            "accepts": outcome.get("missing_documents") or [], "rule_id": outcome["rule_id"]}


def _file_line(reason: str) -> str | None:
    """'The PAN file is a JPEG of 320 KB.' -> 'JPEG · 320 KB' (the file check's own words, shortened)."""
    import re

    m = re.search(r"is an? (\w+) of ([\d.]+ (?:KB|MB))", reason or "")
    return f"{m.group(1)} · {m.group(2)}" if m else None


def documents(outcomes: list[dict]) -> dict:
    """{"people": [roles on the case], "rows": [{slot, label, cells: {role: cell}}]} -- a role with no requirement
    for a document has no cell (not required). Built only from the outcomes; nothing is re-decided."""
    issues: dict[str, list[dict]] = {}
    files: dict[str, str] = {}
    for o in outcomes:
        if o.get("rule_id") in _QUALITY_RULES and o["status"] not in R.DONE:
            for d in _docs(o):
                issues.setdefault(d, []).append(o)
        if o.get("rule_id") == "RULE-11" and o["status"] == R.SATISFIED and _file_line(o["reason"]):
            for d in _docs(o):
                files[d] = _file_line(o["reason"])
    rows: dict[str, dict] = {}
    people: list[str] = []
    for o in outcomes:
        check = str(o.get("check_id") or "")
        role, _, slot = check.partition(":")
        if o.get("rule_id") not in _SLOT_RULES or role not in ROLES or not slot or ":" in slot or slot == "COVERAGE":
            continue
        found = [i for d in _docs(o) for i in issues.get(d, [])]
        cell = _cell(o, found)
        cell["file"] = next((files[d] for d in _docs(o) if d in files), None)
        rows.setdefault(slot, {"slot": slot, "label": _label(slot), "cells": {}})["cells"][role] = cell
        if role not in people:
            people.append(role)
    return {"people": [r for r in ROLES if r in people], "rows": list(rows.values())}


__all__ = ["build", "config", "documents", "sections"]
