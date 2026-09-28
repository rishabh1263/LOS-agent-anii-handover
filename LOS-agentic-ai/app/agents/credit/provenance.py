"""
Underwriting provenance -- the checked chain behind every finding.

    TOOL (governed read) -> FACT (source, record, field, value)
        -> OBSERVATION (the signal a rule read) -> FINDING -> ASSESSMENT

THE SAME ARCHITECTURE AS THE COPILOT'S (app/agents/applicant/provenance.py):
nodes with a `kind`, a stable `node_id` (ledger.stable_id -- a hash of what
makes two items the same, never a fabricated record id), a
`relation.derived_from` pointing up the chain, and `verified`, set by
checking each FACT against THIS CASE'S records. Nothing is re-read that the
graph did not already read, except the record-existence checks themselves.

RECORD IDS ARE THE REAL ONES: a case-store finding id, a document id, the
application's case id, or the bureau provider's report id. An eligibility
reference arrives keyed by case (eligibility.get does not return the row id)
and is resolved here to the recorded ELIGIBILITY finding it came from.

A fact that fails the check is `verified: False`; every finding resting on it
is marked UNVERIFIED and the assessment says so (assessment.py). INTERNAL:
record ids live here, and this block is never published as-is.
"""

from __future__ import annotations

from typing import Any

from app.agents.applicant.ledger import stable_id
from app.agents.applicant.provenance import FACT, FINDING, TOOL
from app.agents.credit.schemas import EvidenceRef, Finding, Observation

OBSERVATION, ASSESSMENT = "OBSERVATION", "ASSESSMENT"


def _node(kind: str, key: tuple, derived_from: list[str] | None = None,
          **fields: Any) -> dict[str, Any]:
    body = {k: v for k, v in fields.items() if v is not None}
    body["node_id"] = stable_id(kind, *key)
    body["kind"] = kind
    body["relation"] = {"derived_from": list(derived_from or [])}
    body["verified"] = None
    return body


def _resolve_eligibility_ids(refs: list[EvidenceRef], case_id: str, repository: Any) -> None:
    """Point case-keyed eligibility references at the recorded ELIGIBILITY finding."""
    targets = [r for r in refs if r.record_type == "FINDING:FINANCIAL/ELIGIBILITY"
               and r.record_id == case_id]
    if not targets:
        return
    rows = [f for f in repository.get_current_findings(case_id, kind="FINANCIAL")
            if getattr(f, "source_type", None) == "ELIGIBILITY"]
    if rows:
        for ref in targets:
            ref.record_id = rows[-1].finding_id


def _verify_fact(node: dict[str, Any], case_id: str, repository: Any,
                 findings_by_id: dict[str, Any], bureau_reports: dict[str | None, set[str]]
                 ) -> tuple[bool, str]:
    record_type, record_id = node.get("record_type") or "", node.get("record_id")
    party_id = node.get("party_id")
    if record_type.startswith("FINDING:"):
        row = findings_by_id.get(record_id)
        if row is None:
            return False, "RECORD_NOT_ON_CASE"
        if row.party_id and party_id and row.party_id != party_id:
            return False, "PARTY_MISMATCH"
        return True, "CASE_FINDING"
    if record_type == "APPLICATION":
        ok = record_id == case_id and repository.get_application(case_id) is not None
        return ok, "APPLICATION" if ok else "RECORD_NOT_ON_CASE"
    if record_type == "DOCUMENT":
        document = repository.get_document(record_id) if record_id else None
        if document is None or document.case_id != case_id:
            return False, "RECORD_NOT_ON_CASE"
        owner = getattr(document, "party_id", None)
        if owner and party_id and owner != party_id:
            return False, "PARTY_MISMATCH"
        return True, "DOCUMENT"
    if record_type.startswith("BUREAU:"):
        ok = record_id in bureau_reports.get(party_id, set())
        return ok, "PROVIDER_REPORT" if ok else "REPORT_NOT_RETURNED"
    return False, "UNKNOWN_RECORD_TYPE"


def build(*, case_id: str, findings: list[Finding], refs: dict[str, EvidenceRef],
          observations: list[Observation], tool_trace: list[dict[str, Any]],
          signal_values: dict[str, dict[str, Any]], assessment_status: str | None = None,
          repository: Any = None) -> dict[str, Any]:
    """
    The chain for these findings, validated against the case.

    `signal_values` maps finding_id -> {signal, value, quality, party_id}.
    Returns {"nodes": [...], "unverified_findings": [finding ids]}.
    """
    if repository is None:
        from app.store import get_repository

        repository = get_repository()

    _resolve_eligibility_ids(list(refs.values()), case_id, repository)

    # Which tool call produced each evidence reference.
    ref_origin: dict[str, tuple[str, str | None]] = {}
    for observation in observations:
        for ref in observation.evidence:
            ref_origin.setdefault(ref.ref_id, (observation.tool, observation.party_id))
    bureau_reports: dict[str | None, set[str]] = {}
    for observation in observations:
        if observation.tool == "bureau.get" and observation.data.get("report_id"):
            bureau_reports.setdefault(observation.party_id, set()).add(
                observation.data["report_id"])
    findings_by_id = {f.finding_id: f for f in repository.get_current_findings(case_id)}

    nodes: list[dict[str, Any]] = []
    tool_nodes: dict[tuple[str, str | None], str] = {}
    for call in tool_trace:
        node = _node(TOOL, (call["tool"], call.get("party_id")), tool=call["tool"],
                     party_id=call.get("party_id"), status=call.get("status"),
                     attempts=call.get("attempts"), is_demo=call.get("is_demo") or None)
        node["verified"] = True        # it ran; the trace is the record
        tool_nodes[(call["tool"], call.get("party_id"))] = node["node_id"]
        nodes.append(node)

    fact_nodes: dict[str, dict[str, Any]] = {}
    for ref_id, ref in refs.items():
        origin = ref_origin.get(ref_id)
        # A case-scoped tool can return a party-attributed observation (risk.get
        # reports the party its recorded result belongs to); its call is still
        # the case-level one.
        parent = (tool_nodes.get(origin) or tool_nodes.get((origin[0], None))) if origin \
            else None
        node = _node(FACT, (ref.source.value, ref.record_type, ref.record_id, ref.field,
                            ref.party_id),
                     derived_from=[parent] if parent else [],
                     source_type=ref.source.value, record_type=ref.record_type,
                     record_id=ref.record_id, field=ref.field, value=ref.value_summary,
                     party_id=ref.party_id, is_demo=ref.is_demo or None, ref_id=ref_id)
        ok, basis = _verify_fact(node, case_id, repository, findings_by_id, bureau_reports)
        node["verified"], node["verification_basis"] = ok, basis
        fact_nodes[ref_id] = node
        nodes.append(node)

    unverified: list[str] = []
    finding_nodes: list[str] = []
    for finding in findings:
        facts = [fact_nodes[r] for r in finding.evidence_refs if r in fact_nodes]
        derived: list[str] = []
        if finding.evidence_refs:
            sv = signal_values.get(finding.finding_id) or {}
            observation = _node(OBSERVATION, (sv.get("signal"), sv.get("party_id"),
                                              sv.get("value")),
                                derived_from=[f["node_id"] for f in facts],
                                signal=sv.get("signal"), value=sv.get("value"),
                                quality=sv.get("quality"), party_id=sv.get("party_id"))
            observation["verified"] = bool(facts) and all(f["verified"] for f in facts) \
                and len(facts) == len(finding.evidence_refs)
            nodes.append(observation)
            derived = [observation["node_id"]]
            if not observation["verified"]:
                unverified.append(finding.finding_id)
        node = _node(FINDING, (finding.finding_id,), derived_from=derived,
                     finding_id=finding.finding_id, category=finding.category.value,
                     status=finding.status.value, severity=finding.severity.value,
                     rule_code=finding.policy_rule_id,
                     party_id=finding.subject.party_id if finding.subject else None)
        node["verified"] = finding.finding_id not in unverified
        if not finding.evidence_refs:
            # A DATA_GAP states an ABSENCE: it has nothing to derive from,
            # and says so (the generic completeness check reads this flag).
            node["evidence_free"] = True
        finding_nodes.append(node["node_id"])
        nodes.append(node)

    if assessment_status is not None:
        top = _node(ASSESSMENT, (case_id, assessment_status), derived_from=finding_nodes,
                    status=assessment_status)
        top["verified"] = not unverified
        nodes.append(top)
    return {"nodes": nodes, "unverified_findings": unverified}


def chain(provenance: dict[str, Any], finding_id: str) -> list[dict[str, Any]]:
    """One finding's chain, top-down: FINDING -> OBSERVATION -> FACT(s) -> TOOL(s)."""
    by_id = {n["node_id"]: n for n in provenance.get("nodes") or []}
    start = next((n for n in by_id.values()
                  if n["kind"] == FINDING and n.get("finding_id") == finding_id), None)
    if start is None:
        return []
    out, frontier, seen = [], [start], set()
    while frontier:
        node = frontier.pop(0)
        if node["node_id"] in seen:
            continue
        seen.add(node["node_id"])
        out.append(node)
        frontier.extend(by_id[i] for i in node["relation"]["derived_from"] if i in by_id)
    return out


__all__ = ["ASSESSMENT", "OBSERVATION", "build", "chain"]
