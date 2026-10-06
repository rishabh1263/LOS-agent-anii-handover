"""
STAGE GATES -- "can my case move to CPA?", "credit ka pending kar do",
"move it to CPA" -- evaluated from AUTHORITATIVE records, orchestrating the
existing agents, never deciding for them.

    the case's CURRENT stage (app.agents.los.stages.resolve -- always re-read)
      -> its gate (app/config/stage_gates.yaml; every criterion UNCONFIRMED)
      -> each check reads ONE recorded source:
           readiness     the configured FOS readiness rules (workflow.readiness)
           kyc           the current KYC finding of every party
           eligibility   the recorded eligibility verdict (eligibility.get)
           underwriting  the recorded credit assessment (credit persistence)
           human         a recorded human decision (none exists in this build)
      -> PASS / REVIEW / BLOCKED / NOT_READY per check, and for the gate
         (REVIEW and SKIPPED never become PASS; no criteria = CONFIGURATION_GAP)

ACT, THEN READ BACK. "credit ka pending kar do" runs every missing check the
action registry lets the assistant run -- today RUN_CREDIT_UNDERWRITING,
through the EXISTING credit agent, unchanged, which enforces its own scope
and stage -- then re-reads every source and re-evaluates the gate.

THE COPILOT NEVER MOVES A STAGE. By design no Copilot module reaches the
stage transition service (an architecture test enforces it). When a gate
passes and a move is asked for, the answer carries a STAGE_TRANSITION action
for the existing operator endpoint -- POST /api/v1/los/cases/{id}/stage --
with the stage it was evaluated at as `expected_stage` (a stale read is
refused there) and an idempotency key (a repeated submit is REPLAYED, never a
second move). The frontend submits it under the user's OWN authorization,
which the endpoint checks (transition scope + write access to the case).

NOTHING HERE APPROVES, DECLINES OR DISBURSES. "Ready for the next stage" is
the gate's words; a loan decision is a human's, recorded elsewhere.
"""

from __future__ import annotations

import functools
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PASS, REVIEW, BLOCKED, NOT_READY, GAP = "PASS", "REVIEW", "BLOCKED", "NOT_READY", "CONFIGURATION_GAP"
EVALUATE, MOVE, RUN_PENDING = "EVALUATE", "MOVE", "RUN_PENDING"
SOURCES = ("readiness", "kyc", "eligibility", "underwriting", "human")
_RANK = {PASS: 0, NOT_READY: 1, REVIEW: 2, BLOCKED: 3}


# ---- configuration -------------------------------------------------------------------
def _path() -> Path:
    return Path(__file__).resolve().parents[4] / "config" / "stage_gates.yaml"


@functools.lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    try:
        return yaml.safe_load(_path().read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - no configuration: every gate is a gap
        return {}


def validate(cfg: dict[str, Any] | None = None) -> list[str]:
    """
    Configuration problems, empty when sound: unknown stages, unknown sources,
    unknown actions, duplicate check ids, a value mapped to two outcomes, and
    an AUTO transition while the criteria are UNCONFIRMED.
    """
    from app.agents.applicant.copilot.capabilities import work

    cfg = config() if cfg is None else cfg
    problems: list[str] = []
    known = set(lifecycle()["next"])
    actions = set(work.registry())
    transition = cfg.get("transition") or {}
    if str(transition.get("mode") or "").upper() not in ("CONFIRM", "NEVER"):
        problems.append(f"transition.mode {transition.get('mode')!r} is not CONFIRM or NEVER")
    if transition.get("action") and transition["action"] not in actions:
        problems.append(f"transition.action {transition['action']} is not a registered action")
    for stage, gate in (cfg.get("gates") or {}).items():
        if stage not in known:
            problems.append(f"gate for unknown stage {stage}")
        seen: set[str] = set()
        for check in (gate or {}).get("checks") or []:
            cid = str(check.get("id") or "")
            if not cid or cid in seen:
                problems.append(f"{stage}: missing or duplicate check id {cid!r}")
            seen.add(cid)
            if check.get("source") not in SOURCES:
                problems.append(f"{stage}.{cid}: unknown source {check.get('source')!r}")
            if check.get("missing_action") and check["missing_action"] not in actions:
                problems.append(f"{stage}.{cid}: unknown action {check['missing_action']}")
            mapped: dict[str, str] = {}
            for outcome in ("pass", "review", "blocked", "not_ready"):
                for value in check.get(outcome) or []:
                    if value in mapped:
                        problems.append(f"{stage}.{cid}: {value} is both {mapped[value]} and {outcome}")
                    mapped[value] = outcome
    return problems


# ---- the request ---------------------------------------------------------------------
_STAGE_WORD = r"(fos|cpa|credit|rcu|bops|hops|disbursement|next\s+stage|agle\s+stage|aage)"
_EVALUATE = re.compile(
    r"\b(can|could|is|kya)\b[^?]{0,40}\b(move|go|proceed|forward|ja\s+sakt\w*|bhej\s+sakt\w*|"
    r"badh\s+sakt\w*|ready)\b[^?]{0,25}\b" + _STAGE_WORD + r"\b"
    r"|\bready\s+for\s+" + _STAGE_WORD + r"\b"
    r"|\b" + _STAGE_WORD + r"\s+(ke\s+liye|mein|me)\s+(ready|taiyar|tayyar|ja\s+sakta|bhej\s+sakte)\b"
    r"|\b(stage\s+gate|gate\s+(status|check)|what\s+is\s+blocking\s+(the\s+)?(stage|move|case))\b"
    r"|\bwhat\s+is\s+(blocking|holding\s+up|stopping)\s+(the\s+)?" + _STAGE_WORD + r"\b"
    r"|\b" + _STAGE_WORD + r"\s+(ka|ki|ke)\s+(kya\s+)?(scene|status|haal|update|kya\s+hua)\b"
    r"|\b" + _STAGE_WORD + r"\s+(me|mein)\s+kya\s+(atka|atki|ruka|ruki|pending)\b"
    r"|\b(fos|cpa|credit)\s+(complete|poora|pura)\s+(hua|ho\s+gaya|hai)\b"
    r"|\baage\s+(badh|ja|bhej)\s*(sakt\w*|payega|paega)\b"
    r"|\b(underwriting|credit)\b[^?]{0,20}\b(mein|me|in)\b[^?]{0,15}\b(kya\s+)?(issue|problem|dikkat|atka|ruka)\b"
    r"|\b(underwriting|credit)\s+(issue|problem|blocker)s?\b"
    r"|\b(pending|baaki|baki|blocking|stuck)\s+(in|for|at|mein|me)\s+(the\s+)?(credit|underwriting)\b",
    re.IGNORECASE)
_MOVE = re.compile(
    r"^\s*(please\s+|pls\s+)?(move|send|forward|push|proceed)\b[^?]{0,30}\b(to|into)\s+"
    + _STAGE_WORD + r"\b"
    r"|\b" + _STAGE_WORD + r"\s+(mein|me|in|par|pe)\s+(bhej|move\s+kar|daal)\s*(do|de|dijiye|dena)\b"
    r"|\b(aage\s+badha\s*(do|de|dijiye|o)|next\s+stage\s+(par|pe|mein)\s+bhej\s*(do|de))\b"
    r"|^\s*(please\s+)?(move|push|take)\s+(it|this|the\s+case|my\s+case)\s+(ahead|forward|on|to\s+the\s+next\s+stage)\b",
    re.IGNORECASE)
_RUN_PENDING = re.compile(
    r"\b(credit|underwriting|eligibility)\b[^?]{0,30}\b(pending|baaki|baki)\b[^?]{0,20}\b(kar|complete|run|chala)\w*"
    r"|\b(run|start|chala\w*|complete)\b[^?]{0,15}\b(credit\s+underwriting|underwriting|credit\s+check)\b"
    r"|\b(credit|underwriting)\b[^?]{0,20}\b(kar\s*d[oe]|chala\s*d[oe]|run\s+kar\w*)\b",
    re.IGNORECASE)


@dataclass(frozen=True)
class Request:
    kind: str
    target: str | None = None


def _request_as_typed(message: str) -> Request | None:
    text = " ".join(str(message or "").split())
    if not text:
        return None
    named = re.search(r"\b(fos|cpa|credit|rcu|bops|hops|disbursement)\b", text, re.I)
    target = named.group(1).upper() if named else None
    if target is None and re.search(r"\bunderwriting\b", text, re.I):
        target = "CREDIT"                   # underwriting is the Credit stage's work
    if _RUN_PENDING.search(text):
        return Request(RUN_PENDING, "CREDIT")
    if _MOVE.search(text):
        return Request(MOVE, target)
    if _EVALUATE.search(text):
        return Request(EVALUATE, target)
    return None


# ---- reading the sources (after authorization, by the caller of this module) ---------
def _dig(data: Any, path: str) -> Any:
    for part in str(path or "").split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def read_sources(case_id: str, *, results: dict[str, Any], repository: Any) -> dict[str, Any]:
    """Every source a gate may read, from the records -- never computed here."""
    readiness = ((results.get("workflow.readiness") or {}).get("readiness")
                 or results.get("workflow.readiness"))
    eligibility = (results.get("eligibility.get") or {})
    kyc = []
    try:
        latest: dict[str, Any] = {}
        for f in repository.get_current_findings(case_id, kind="KYC") or []:
            latest[str(getattr(f, "party_id", "") or "")] = f
        kyc = [{"party_id": p, "status": str(getattr(f, "status", "") or "").upper(),
                "reason_codes": list(getattr(f, "reason_codes", []) or [])} for p, f in latest.items()]
    except Exception:  # noqa: BLE001 - unreadable: NOT_READY, never a guess
        kyc = []
    underwriting = None
    try:
        from app.agents.credit import persistence

        underwriting = persistence.latest(case_id)
    except Exception:  # noqa: BLE001
        underwriting = None
    # OUTSTANDING QUERIES / PENDING DEVIATIONS, as the gate checks configuration
    # (app/config/queries.yaml) says they are -- read, never decided here
    open_checks: list[dict[str, Any]] = []
    try:
        from app.agents.los import queries

        open_checks = queries.gate_checks(case_id, repository=repository)
    except Exception:  # noqa: BLE001 - unreadable: no extra check, logged by the service
        open_checks = []
    return {"readiness": readiness if isinstance(readiness, dict) else None,
            "eligibility": (eligibility.get("eligibility") if eligibility.get("recorded") else None),
            "kyc": kyc or None, "underwriting": underwriting, "human": None,
            "open_items": open_checks}


# ---- evaluation ----------------------------------------------------------------------
def _outcome(check: dict[str, Any], value: Any) -> str:
    for outcome, key in ((PASS, "pass"), (REVIEW, "review"), (BLOCKED, "blocked"), (NOT_READY, "not_ready")):
        if value is not None and str(value).upper() in [str(v).upper() for v in check.get(key) or []]:
            return outcome
    return NOT_READY


def _check(check: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import work

    source = check.get("source")
    record = sources.get(source)
    out = {"id": check.get("id"), "label": check.get("label") or check.get("id"), "source": source,
           "recorded": record is not None, "value": None, "status": NOT_READY,
           "reason_code": None, "evidence": None, "next_action": None}
    if source == "human":
        out.update(status=REVIEW, reason_code="HUMAN_DECISION_REQUIRED",
                   evidence="No human decision is recorded for this case.")
    elif record is None:
        out.update(status=NOT_READY, reason_code=f"{out['id']}_NOT_RECORDED")
    elif source == "kyc":
        statuses = [_outcome(check, p.get("status")) for p in record]
        out.update(value=[p.get("status") for p in record],
                   status=max(statuses, key=lambda s: _RANK[s]) if statuses else NOT_READY,
                   evidence=[{"status": p.get("status"), "reason_codes": p.get("reason_codes")} for p in record])
    else:
        value = _dig(record, check.get("path") or "status")
        out.update(value=value, status=_outcome(check, value))
        if source == "readiness":
            out["evidence"] = [{"code": i.get("code"), "detail": i.get("detail"), "slot": i.get("slot")}
                               for i in (record.get("blocking_items") or []) if isinstance(i, dict)]
    if out["status"] != PASS and out["reason_code"] is None:
        out["reason_code"] = f"{out['id']}_{out['status']}"
    action = check.get("missing_action")
    if out["status"] in (NOT_READY, REVIEW) and action:
        spec = work.registry().get(action) or {}
        out["next_action"] = {"action": action, "owner": spec.get("owner"),
                              "auto_execute": bool(spec.get("auto_execute"))}
    return out


def lifecycle() -> dict[str, Any]:
    """
    What follows what, from the LOS stage ORDER (app.agents.los.stages -- the
    reader's view; nothing here moves a case), and the scope a move needs,
    from the Copilot's own action registry (STAGE_TRANSITION).
    """
    from app.agents.applicant.copilot.capabilities import work
    from app.agents.los import stages

    order = [s.value for s in stages.ORDER]
    scopes = (work.registry().get("STAGE_TRANSITION") or {}).get("required_scopes") or []
    return {"next": {stage: order[i + 1:i + 2] for i, stage in enumerate(order)},
            "transition_scopes": [str(s) for s in scopes]}


def live_stage(case_id: str) -> str | None:
    """The case's stage as the record resolves it now (never from memory)."""
    from app.agents.los import stages

    stage = stages.resolve(case_id).stage
    return stage.value if stage is not None else None


def evaluate(stage: str | None, sources: dict[str, Any]) -> dict[str, Any]:
    """The gate out of `stage`, from the recorded sources."""
    cfg = config()
    gate = (cfg.get("gates") or {}).get(str(stage or "").upper()) or {}
    nexts = lifecycle()["next"].get(str(stage or "").upper()) or []
    checks = [_check(c, sources) for c in gate.get("checks") or []]
    if not checks:
        status = GAP
        # an open query / pending deviation is still named on an unconfigured gate
        checks = [dict(c, next_action=None, recorded=True, value=c["status"])
                  for c in sources.get("open_items") or []]
    else:
        checks += [dict(c, next_action=None, recorded=True, value=c["status"])
                   for c in sources.get("open_items") or []]
        status = max((c["status"] for c in checks), key=lambda s: _RANK[s])
    return {"stage": stage, "next_stage": nexts[0] if nexts else None, "status": status,
            "criteria_status": gate.get("status") or cfg.get("status") or "UNCONFIRMED",
            "policy_version": cfg.get("policy_version"), "checks": checks,
            "transition_mode": str((cfg.get("transition") or {}).get("mode") or "CONFIRM").upper(),
            "blockers": [c for c in checks if c["status"] != PASS]}


# ---- the words ------------------------------------------------------------------------
def _readable(stage: str | None) -> str:
    from app.agents.applicant import config as agent_config

    return agent_config.stage_label(stage) if stage else "the next stage"


def compose(gate: dict[str, Any], *, can_move: bool, ran: list[str] | None = None) -> tuple[str, dict | None]:
    """(answer, a yes/no offer or None), from the evaluated gate only."""
    here, there = _readable(gate["stage"]), _readable(gate["next_stage"])
    lines = []
    for action in ran or []:
        lines.append(action)
    if gate["status"] == GAP:
        lines.append(f"No criteria are configured yet for moving a case out of {here}, so I can't say "
                     f"whether it's ready for {there}. That needs the business rules to be configured.")
        return " ".join(lines), None
    if gate["status"] == PASS:
        # an operations assistant states the fact; it does not congratulate (2026-10-06)
        lines.append(f"Every configured {here} check is complete, so the case is ready to move to {there}.")
        if gate["criteria_status"] != "APPROVED":
            lines.append("(These gate rules are not yet business-approved.)")
        if gate["transition_mode"] == "CONFIRM" and can_move:
            lines.append(f"You can move it to {there} now -- confirm the move to submit it.")
        elif gate["transition_mode"] == "CONFIRM":
            lines.append(f"Someone with stage-transition permission can move it to {there}.")
        return " ".join(lines), None
    lines.append(f"Your case can't move from {here} to {there} yet.")
    for c in gate["blockers"]:
        said = {REVIEW: "needs review", BLOCKED: "not met", NOT_READY: "not complete yet"}[c["status"]]
        detail = ""
        if c["id"] == "FOS_READINESS" and c.get("evidence"):
            detail = ": " + "; ".join(str(e.get("detail") or e.get("code")).rstrip(".")
                                      for e in c["evidence"][:4])
        elif str(c.get("reason_code") or "").endswith("_NOT_RECORDED"):
            detail = " -- no result is recorded yet"
        elif c.get("recorded") and c.get("value") and not isinstance(c.get("value"), list):
            detail = f" -- recorded as {str(c['value']).replace('_', ' ').lower()}"
        elif c.get("reason_code") == "HUMAN_DECISION_REQUIRED":
            detail = " -- a human decision is required; I can't make or record it"
        lines.append(f"{c['label']} ({said}){detail}.")
    # offered only where NOTHING is recorded yet: re-running on the same data
    # would only replay the assessment already recorded
    runnable = [c for c in gate["blockers"]
                if (c.get("next_action") or {}).get("auto_execute") and not c.get("recorded")]
    if runnable:
        lines.append(f"I can run {runnable[0]['label'].lower()} now -- just ask.")
    return " ".join(lines), None


def transition_action(case_id: str, gate: dict[str, Any]) -> dict[str, Any] | None:
    """
    The move, as an ACTION for the existing operator endpoint -- only for a
    gate that passed. The frontend submits it under the user's own token;
    the endpoint enforces scope, write access, `expected_stage` and the
    idempotency key.
    """
    if gate.get("status") != PASS or not gate.get("next_stage") or gate.get("transition_mode") == "NEVER":
        return None
    return {"action": "STAGE_TRANSITION", "owner": "USER", "confirmation_required": True,
            "method": "POST", "endpoint": f"/api/v1/los/cases/{case_id}/stage",
            "required_scopes": lifecycle()["transition_scopes"],
            "body": {"target_stage": gate["next_stage"], "expected_stage": gate["stage"],
                     "idempotency_key": transition_key(case_id, gate["stage"], gate["next_stage"], gate),
                     "reason": f"{gate['stage']} gate passed (Copilot evaluation, criteria "
                               f"{gate.get('criteria_status')})", "source": "OPERATOR",
                     # re-evaluated by the server at the moment of the move: an
                     # offer made when the case was ready never moves it once it is not
                     "mode": "GATED"}}


def transition_key(case_id: str, stage: str, target: str, gate: dict[str, Any]) -> str:
    """One key per (case, move, evaluated state): a repeated "yes" replays, never moves twice."""
    basis = "|".join([case_id, stage, target] + [f"{c['id']}={c['status']}" for c in gate["checks"]])
    return "copilot-" + hashlib.sha256(basis.encode()).hexdigest()[:32]


__all__ = ["BLOCKED", "EVALUATE", "GAP", "MOVE", "NOT_READY", "PASS", "REVIEW", "RUN_PENDING",
           "Request", "compose", "config", "evaluate", "lifecycle", "live_stage", "read_sources", "request",
           "transition_action", "transition_key", "validate"]


def request(message: str) -> Request | None:
    """As typed, else as its canonical form (a Hindi or Marathi request)."""
    found = _request_as_typed(message)
    if found is None and message:
        from app.agents.applicant import language

        canonical = language.canonicalise(message)
        if canonical.changed:
            found = _request_as_typed(canonical.text)
    return found
