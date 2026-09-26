"""
THE CASE LEDGER -- one case's authoritative records, read once, seen two ways.

    stage_transitions ─┐                        ┌─> evidence()  (Slice 6)
    case_findings      │                        │   problem -> source -> record
    case_decisions     ├─> Ledger (lazy, 1 read ┤   -> subject -> field -> value
    document_versions  │   per family) ─────────┤   -> finding -> impact? -> action?
    documents          │                        │
    application        ┘                        └─> events()    (Slice 7)
                                                    what changed, in order

ONE SOURCE OF TRUTH, TWO VIEWS. The evidence behind a problem and the history
behind "what changed?" are read from the SAME rows, normalised by the SAME
subject and source rules, so a problem and the change that introduced it can
never disagree about whose it is or where it came from.

WHAT IS A RECORD, AND WHAT IS NOT. Everything here is a stored row the
pipeline or the stage service wrote. Nothing is taken from the conversation,
from retrieval, or from a model, and nothing is inferred: a change is an
event only when two recorded rows show it. What the store does not keep --
the checklist over time, application fields over time, a re-upload as a new
version -- is reported as NOT RECORDED (`completeness`), never reconstructed.

MIRRORS ARE NOT COUNTED TWICE. A stage transition is also written to the
timeline; OCR completion and an LOS run are also recorded as findings and a
decision. The ledger reads each fact from its primary record only.

INTERNAL. Record ids live here for audit and for later slices (Impact, Next
Action, Provenance). They are never published to chat; the published views
strip them (copilot_api), and the guardrails refuse them in any answer.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: Event families the store records, and those it does not.
RECORDED = ("STAGE", "DOCUMENT_UPLOAD", "VERIFICATION", "FINDING", "DECISION",
            "APPLICATION_CREATED")
NOT_RECORDED = ("CHECKLIST", "APPLICATION_FIELDS", "DOCUMENT_REUPLOAD")

_EXPLAINING = ("KYC", "FINANCIAL", "RISK", "RCU")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _kind(finding: Any) -> str:
    kind = getattr(finding, "finding_kind", "")
    return str(getattr(kind, "value", kind) or "").upper()


def stable_id(*parts: Any) -> str:
    """A stable identity for equivalent evidence -- never a fabricated
    record id, only a hash of what makes two items the same one."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(joined.encode()).hexdigest()[:16]


@dataclass
class Ledger:
    """One case's records. Each family is read at most once, on first use."""

    case_id: str
    _cache: dict[str, Any] = field(default_factory=dict)

    # -- the records ---------------------------------------------------------

    def _load(self, name: str, reader) -> Any:
        if name not in self._cache:
            try:
                self._cache[name] = reader()
            except Exception as exc:
                logger.warning("Ledger %s unavailable for %s: %r", name,
                               self.case_id, exc)
                self._cache[name] = [] if name != "application" else None
        return self._cache[name]

    def _repo(self):
        from app.store import get_repository

        return get_repository()

    @property
    def application(self):
        return self._load("application",
                          lambda: self._repo().get_application(self.case_id))

    @property
    def documents(self) -> list:
        return self._load("documents",
                          lambda: self._repo().list_documents(self.case_id))

    @property
    def findings(self) -> list:
        """EVERY run's findings: the history, not only the current ones."""
        return self._load("findings",
                          lambda: self._repo().get_case_findings(self.case_id))

    @property
    def decisions(self) -> list:
        return self._load("decisions",
                          lambda: self._repo().get_case_decisions(self.case_id))

    @property
    def transitions(self) -> list:
        return self._load("transitions",
                          lambda: self._repo().get_stage_transitions(self.case_id))

    @property
    def timeline(self) -> list:
        return self._load("timeline",
                          lambda: self._repo().get_case_timeline(self.case_id))

    @property
    def versions(self) -> list:
        def read():
            repo = self._repo()
            rows = []
            for document in self.documents:
                rows.extend(repo.get_document_versions(document.document_id))
            return rows
        return self._load("versions", read)

    @property
    def parties(self) -> list:
        from app.agents.applicant import subjects

        return self._load("parties", lambda: subjects.parties_of(
            self.case_id, application=self.application,
            documents=self.documents))

    def current_findings(self) -> list:
        from app.store.repository import current_findings

        return current_findings(self.findings)

    # -- subject: whose record this is -------------------------------------

    def _roles(self) -> dict[str, str]:
        return {p.party_id: p.role.value for p in self.parties}

    def _document(self, document_id: str | None):
        for document in self.documents:
            if document_id and document.document_id == document_id:
                return document
        return None

    def subject_of(self, party_id: str | None,
                   document_id: str | None = None) -> dict[str, Any]:
        """
        PARTY when the record names its party (or its document's stamped
        owner); CASE otherwise. Never guessed from anything else.
        """
        owner = str(party_id or "").strip()
        if not owner and document_id:
            document = self._document(document_id)
            owner = str(getattr(document, "owner_id", "") or "").strip() \
                if document is not None else ""
        if not owner:
            return {"scope": "CASE", "party_id": None, "party_role": None}
        return {"scope": "PARTY", "party_id": owner,
                "party_role": self._roles().get(owner)}

    # -- the evidence view (Slice 6) ----------------------------------------

    def memory(self, party_id: str | None = None) -> dict[str, Any]:
        """
        The CURRENT findings and the decisions, in case memory's shape plus
        the internal fields evidence needs (record id, when observed, source
        type, version). `party_id` narrows exactly as case memory does: that
        party and the case-level findings.
        """
        from app.agents.applicant import case_memory_facts as cm

        findings = []
        for finding in self.current_findings():
            if party_id and finding.party_id not in (None, party_id):
                continue
            row = cm._public_finding(finding)
            row.update({
                "finding_id": getattr(finding, "finding_id", None),
                "observed_at": _iso(getattr(finding, "updated_at", None)
                                    or getattr(finding, "created_at", None)),
                "source_type": getattr(finding, "source_type", None),
                "version": getattr(finding, "version", None),
            })
            findings.append(row)
        decisions = []
        for decision in self.decisions:
            row = cm._public_decision(decision)
            row["decision_id"] = getattr(decision, "decision_id", None)
            decisions.append(row)
        return {"findings": findings, "decisions": decisions,
                "timeline": [cm._public_event(e) for e in self.timeline]}

    def evidence(self, *, since: str | None = None,
                 party_id: str | None = None,
                 stage: str | None = None) -> list[dict[str, Any]]:
        """
        Every current problem, each with its full evidence chain -- and its
        IMPACT (Slice 8), by rule, for its own subject. `next_action` on a
        problem is the action that rule names for it; the case's primary
        next action is the Next Best Action engine's (actions.py).
        """
        from app.agents.applicant import evidence, impact

        documents = [{"document_id": d.document_id, "party_id": d.owner_id}
                     for d in self.documents]
        found = evidence.problems(self.memory(party_id), since=since,
                                  parties=self.parties, documents=documents,
                                  limit=None)
        for problem in found:
            problem["impact"] = impact.for_problem(problem, stage)
            problem["next_action"] = problem["impact"].get("action_code")
        return found

    # -- the history view (Slice 7) -----------------------------------------

    def events(self) -> list[dict[str, Any]]:
        """
        Every recorded change, oldest first, by `changed_at` and then a
        stable key -- never storage order. An event without a time sorts
        last and keeps `changed_at: None`; it is never given one.
        """
        events: list[dict[str, Any]] = []
        events.extend(self._stage_events())
        events.extend(self._upload_events())
        events.extend(self._finding_events())
        events.extend(self._decision_events())
        application = self.application
        if application is not None and getattr(application, "created_at", None):
            events.append(self._event(
                "APPLICATION_CREATED", _iso(application.created_at),
                self.subject_of(None), "APPLICATION", self.case_id,
                previous=None, current="CREATED"))
        # TIES BREAK ON THE RECORD'S OWN SEQUENCE, never on a hash: two
        # stage moves written in one instant are ordered by the stage
        # record's version, uploads and findings by their write order.
        return sorted(events, key=lambda e: (e["changed_at"] is None,
                                             e["changed_at"] or "",
                                             e["_order"], e.get("_seq", 0),
                                             e["event_id"]))

    _ORDER = {"APPLICATION_CREATED": 0, "STAGE_CHANGED": 1,
              "STAGE_STATUS_CHANGED": 2, "DOCUMENT_UPLOADED": 3,
              "VERIFICATION_RECORDED": 4, "VERIFICATION_CHANGED": 4,
              "FINDING_RECORDED": 5, "FINDING_CHANGED": 5,
              "DECISION_RECORDED": 6, "DECISION_CHANGED": 6}

    def _event(self, event_type: str, changed_at: str | None,
               subject: dict[str, Any], source_type: str,
               record_id: str | None, *, previous: Any, current: Any,
               document_id: str | None = None,
               document_type: str | None = None,
               codes: list[str] | None = None,
               detail: str | None = None) -> dict[str, Any]:
        return {
            "event_id": stable_id(event_type, record_id, changed_at, current),
            "event_type": event_type,
            "changed_at": changed_at,
            "subject": subject,
            "source": {"type": source_type, "record_id": record_id,
                       "document_id": document_id,
                       "document_type": document_type},
            "previous": previous,
            "current": current,
            **({"codes": list(codes)} if codes else {}),
            **({"detail": detail} if detail else {}),
            "_order": self._ORDER.get(event_type, 9),
        }

    def _stage_events(self) -> list[dict[str, Any]]:
        out = []
        transitions = self.transitions
        if transitions:
            for position, t in enumerate(transitions):
                entered = str(t.kind or "").upper() == "STAGE_ENTERED"
                out.append({**self._event(
                    "STAGE_CHANGED" if entered else "STAGE_STATUS_CHANGED",
                    _iso(getattr(t, "created_at", None)),
                    self.subject_of(None), "STAGE_TRANSITION",
                    t.transition_id,
                    previous=t.from_stage if entered else t.from_status,
                    current=t.to_stage if entered else t.to_status,
                    detail=t.reason),
                    "_seq": getattr(t, "version", None) or position})
            return out
        # A CASE NEVER TRANSITIONED: its timeline may still record the
        # stages it entered (demo fixtures, legacy rows). The previous
        # stage is the prior recorded one -- known from record order.
        from app.agents.los import stages

        previous = None
        for event in self.timeline:
            stage = stages.parse(getattr(event, "stage", None))
            if str(getattr(event, "event_type", "")).upper() != "STAGE_ENTERED" \
                    or stage is None:
                continue
            if previous != stage.value:
                out.append(self._event(
                    "STAGE_CHANGED", _iso(getattr(event, "created_at", None)),
                    self.subject_of(None), "CASE_EVENT",
                    getattr(event, "event_id", None),
                    previous=previous, current=stage.value))
            previous = stage.value
        return out

    def _upload_events(self) -> list[dict[str, Any]]:
        out = []
        seen: set[str] = set()
        versions = sorted(self.versions,
                          key=lambda v: (_iso(v.created_at) or "", v.version))
        for version in versions:
            if version.document_id in seen:
                continue            # a re-run's same version, not an upload
            seen.add(version.document_id)
            document = self._document(version.document_id)
            out.append(self._event(
                "DOCUMENT_UPLOADED", _iso(version.created_at),
                self.subject_of(version.party_id, version.document_id),
                "DOCUMENT_VERSION", version.document_version_id,
                previous=None, current="UPLOADED",
                document_id=version.document_id,
                document_type=getattr(document, "document_type", None)))
        # A document with no version row still has its own upload time.
        for document in self.documents:
            if document.document_id in seen or not document.uploaded_at:
                continue
            out.append(self._event(
                "DOCUMENT_UPLOADED", _iso(document.uploaded_at),
                self.subject_of(document.party_id, document.document_id),
                "DOCUMENT", None, previous=None, current="UPLOADED",
                document_id=document.document_id,
                document_type=document.document_type))
        return out

    def _finding_events(self) -> list[dict[str, Any]]:
        """
        A finding slot's rows, in write order. The first is RECORDED; each
        later row whose status or reasons differ is CHANGED, previous ->
        current. An identical re-run updates its row in place and is not a
        row here -- so it is not a change.
        """
        from app.store.repository import _slot

        slots: dict[tuple, list] = {}
        for finding in self.findings:
            kind = _kind(finding)
            if kind != "VERIFICATION" and kind not in _EXPLAINING:
                continue
            slots.setdefault(_slot(finding), []).append(finding)
        out = []
        for rows in slots.values():
            rows.sort(key=lambda f: _iso(f.created_at) or "")
            before = None
            for row in rows:
                kind = _kind(row)
                family = "VERIFICATION" if kind == "VERIFICATION" else "FINDING"
                state = (str(row.status or "").upper(),
                         tuple(row.reason_codes or ()))
                if before is not None and state == before[0]:
                    continue
                document = self._document(row.document_id)
                out.append(self._event(
                    f"{family}_{'RECORDED' if before is None else 'CHANGED'}",
                    _iso(row.created_at),
                    self.subject_of(row.party_id, row.document_id),
                    kind, row.finding_id,
                    previous=None if before is None else before[0][0],
                    current=state[0], codes=list(row.reason_codes or ()),
                    document_id=row.document_id,
                    document_type=(getattr(document, "document_type", None)
                                   or (row.payload or {}).get("type"))))
                before = (state, row)
        return out

    def _decision_events(self) -> list[dict[str, Any]]:
        out = []
        before = None
        for decision in sorted(self.decisions,
                               key=lambda d: _iso(d.created_at) or ""):
            state = (str(decision.decision or "").upper(),
                     str(decision.status or "").upper())
            if before is not None and state == before:
                continue
            out.append(self._event(
                "DECISION_RECORDED" if before is None else "DECISION_CHANGED",
                _iso(decision.created_at), self.subject_of(None),
                "CASE_DECISION", decision.decision_id,
                previous=None if before is None else before[0],
                current=state[0], codes=list(decision.reason_codes or ())))
            before = state
        return out

    # -- what the store can and cannot answer ------------------------------

    def completeness(self) -> dict[str, Any]:
        dated = [e["changed_at"] for e in self.events() if e["changed_at"]]
        return {"recorded": list(RECORDED), "not_recorded": list(NOT_RECORDED),
                "earliest": min(dated) if dated else None}


def load(case_id: str) -> Ledger:
    """The ledger for one case. Nothing is read until a view asks."""
    return Ledger(case_id)


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    """An event as a response may carry it: no record ids, no internals."""
    source = event.get("source") or {}
    return {k: v for k, v in {
        "event_type": event["event_type"],
        "changed_at": event.get("changed_at"),
        "subject": {"scope": event["subject"]["scope"],
                    "party_role": event["subject"].get("party_role")},
        "source": {"type": source.get("type"),
                   "document_type": source.get("document_type")},
        "previous": event.get("previous"),
        "current": event.get("current"),
        "codes": event.get("codes"),
    }.items() if v is not None}


def public_problem(problem: dict[str, Any]) -> dict[str, Any]:
    """A problem as a response may carry it: the chain, never record ids."""
    source = problem.get("source") or {}
    return {k: v for k, v in {
        "problem_id": problem.get("problem_id"),
        "type": problem.get("type"),
        "message": problem.get("message"),
        "scope": problem.get("scope"),
        "document": problem.get("document"),
        "party_id": problem.get("party_id"),
        "party_role": problem.get("party_role"),
        "source": {"type": source.get("type"),
                   "document_type": problem.get("document")} if source else None,
        "evidence": [{"source": e.get("source"), "field": e.get("field")}
                     for e in problem.get("evidence") or []] or None,
        "observed_at": problem.get("observed_at"),
        "impact": _public_impact(problem.get("impact")),
        "next_action": problem.get("next_action"),
    }.items() if v is not None or k in ("impact", "next_action")}


def _public_impact(value: Any) -> Any:
    from app.agents.applicant import impact

    return impact.public(value) if isinstance(value, dict) else None


__all__ = ["Ledger", "NOT_RECORDED", "RECORDED", "load", "public_event",
           "public_problem", "stable_id"]
