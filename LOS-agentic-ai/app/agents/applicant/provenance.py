"""
PROVENANCE -- why the system produced this answer, as a checked chain.

    question -> ROUTE -> FACT (stage) -> FINDING -> IMPACT -> ACTION
                             │             ▲
                             │             └── EVENT (history), KNOWLEDGE (RAG)
                             └── TOOL (governed read), COMPOSER (words only)

ONE ADAPTER OVER WHAT THE REQUEST ALREADY HAS. Evidence items (Slice 6),
history events (7), impacts (8) and next actions (9) each carry their own
source; this links them into nodes, `relation.derived_from` pointing up the
chain. Nothing is re-read: the case ledger the evidence came from is the one
validation checks against, and tool results are not fetched again.

CHECKED, NOT DECORATIVE (`validate`). Every node that names a record is
checked against that CASE's records: the record exists on this case, its
party is the node's party, its document belongs to that party, the rule
exists, the action is the one its rule names, the stage is the one the case
record holds. A node that fails is marked unverified and is never used in
anything a person reads -- and nothing derived from it survives either.

A MODEL IS NEVER A SOURCE. When a model worded the answer, a COMPOSER node
lists the fact nodes it was given. The words are not provenance.

TWO PROJECTIONS. `build` is INTERNAL (record ids, rule codes, tools,
transport) -- for audit, debugging and later slices. `public` is business
labels and codes only; `explain` is one or two plain sentences. Neither ever
carries a record id, a rule reference, a tool name, a file or a path.
"""

from __future__ import annotations

from typing import Any

from app.agents.applicant.ledger import stable_id

ROUTE, FACT, FINDING, IMPACT, ACTION = "ROUTE", "FACT", "FINDING", "IMPACT", "ACTION"
EVENT, KNOWLEDGE, TOOL, COMPOSER = "EVENT", "KNOWLEDGE", "TOOL", "COMPOSER"


def _node(kind: str, *, derived_from: list[str] | None = None,
          **fields: Any) -> dict[str, Any]:
    body = {k: v for k, v in fields.items()}
    body["node_id"] = stable_id(
        kind, body.get("source_type"), body.get("record_id"),
        body.get("finding_code"), body.get("party_id"), body.get("field"),
        body.get("document_id"), body.get("rule_code"), body.get("value"),
        body.get("action_code"), body.get("event_type"))
    body["kind"] = kind
    body["relation"] = {"derived_from": list(derived_from or [])}
    body["verified"] = None
    return body


# ==========================================================================
# BUILD -- the internal chain
# ==========================================================================

def build(*, question: str, envelope: dict[str, Any], context: Any,
          evidence_items: list[dict[str, Any]] | None = None,
          retrieved: Any = None) -> dict[str, Any]:
    """The internal provenance of one answer, from what the request holds."""
    nodes: list[dict[str, Any]] = []
    subject = envelope.get("subject") or {}

    nodes.append(_node(
        ROUTE, source_type="ROUTING", intent=envelope.get("intent"),
        base_intent=envelope.get("base_intent"),
        route=envelope.get("category"),
        subject=subject.get("kind") or "CASE",
        stage=getattr(getattr(context, "stage", None), "value", None),
        case_context=bool(envelope.get("case_id"))))

    stage = getattr(context, "stage", None)
    # A KNOWLEDGE ANSWER CARRIES NO CASE FACT: its chain is the route and
    # the knowledge source, never the case's stage.
    case_answer = str(envelope.get("category") or "").upper() in (
        "CASE_ONLY", "MIXED")
    if stage is not None and case_answer:
        resolution = getattr(getattr(context, "resolution", None), "value", None)
        nodes.append(_node(
            FACT, source_type=resolution, record_id=None, field="stage",
            value=stage.value, observed_at=getattr(context, "since", None),
            scope="CASE"))

    for trace in envelope.get("tool_trace") or []:
        nodes.append(_node(
            TOOL, source_type="GOVERNED_TOOL", tool=trace.get("tool"),
            transport=trace.get("transport"), provider=trace.get("provider"),
            ok=trace.get("ok"), correlation_id=envelope.get("request_id")))

    # FINDING -> IMPACT -> (per-problem) ACTION
    impact_nodes: dict[tuple, str] = {}
    for item in evidence_items or []:
        source = item.get("source") or {}
        kind = str(item.get("finding") or source.get("type") or "")
        finding = _node(
            FINDING, source_type="CASE_DECISION" if kind == "DECISION" else kind,
            record_type=source.get("type"),
            record_id=source.get("record_id"),
            document_id=source.get("document_id"),
            version=source.get("version"),
            party_id=item.get("party_id"), party_role=item.get("party_role"),
            scope=item.get("scope") or "CASE",
            finding_code=item.get("type"), document=item.get("document"),
            field=",".join(sorted({str(e.get("field")) for e in
                                   item.get("evidence") or [] if e.get("field")})) or None,
            value_reference=("comparisons" if item.get("evidence") else None),
            observed_at=item.get("observed_at"))
        nodes.append(finding)
        effect = item.get("impact")
        if isinstance(effect, dict):
            impact_node = _node(
                IMPACT, source_type="IMPACT_RULE",
                rule_code=effect.get("source_rule"),
                finding_code=effect.get("finding_code"),
                impact_code=effect.get("impact_code"),
                blocking=effect.get("blocking"),
                party_id=effect.get("party_id"),
                party_role=effect.get("party_role"),
                scope=effect.get("scope"), policy_status=effect.get("policy_status"),
                derived_from=[finding["node_id"]])
            nodes.append(impact_node)
            impact_nodes[(effect.get("finding_code"), effect.get("party_id"))] = \
                impact_node["node_id"]
            if effect.get("action_code"):
                nodes.append(_node(
                    ACTION, source_type="IMPACT_RULE",
                    rule_code=effect.get("source_rule"),
                    action_code=effect.get("action_code"),
                    party_id=effect.get("party_id"),
                    party_role=effect.get("party_role"),
                    scope=effect.get("scope"), role="PER_PROBLEM",
                    derived_from=[impact_node["node_id"]]))

    # THE CASE'S NEXT ACTIONS (Slice 9), when computed on this request
    nba = envelope.get("_nba_internal") or {}
    for position, action in enumerate(
            [nba.get("primary")] + list(nba.get("additional") or [])):
        if not action:
            continue
        subject_of = action.get("subject") or {}
        parents = [impact_nodes[k] for k in impact_nodes
                   if k[0] in (action.get("reason_codes") or [])
                   and k[1] == subject_of.get("party_id")]
        nodes.append(_node(
            ACTION, source_type=action.get("source_rule"),
            record_id=action.get("record_id"),
            action_code=action.get("action_code"),
            party_id=subject_of.get("party_id"),
            party_role=subject_of.get("party_role"),
            scope=subject_of.get("scope"),
            role="PRIMARY" if position == 0 else "ADDITIONAL",
            derived_from=parents))

    # WHAT CHANGED (Slice 7), when asked
    for event in envelope.get("_history_events") or []:
        source = event.get("source") or {}
        nodes.append(_node(
            EVENT, source_type=source.get("type"),
            record_id=source.get("record_id"),
            document_id=source.get("document_id"),
            event_type=event.get("event_type"),
            party_id=(event.get("subject") or {}).get("party_id"),
            party_role=(event.get("subject") or {}).get("party_role"),
            scope=(event.get("subject") or {}).get("scope"),
            previous=event.get("previous"), current=event.get("current"),
            observed_at=event.get("changed_at")))

    # KNOWLEDGE -- never case evidence
    knowledge = envelope.get("knowledge") or {}
    if knowledge.get("grounded"):
        nodes.append(_node(
            KNOWLEDGE, source_type="HANDBOOK", knowledge_type="HANDBOOK",
            citations=list(knowledge.get("sources") or []),
            stage=knowledge.get("stage"), version=None))
    for item in getattr(getattr(retrieved, "process", None), "evidence", ()) or ():
        provenance = getattr(item, "provenance", {}) or {}
        nodes.append(_node(
            KNOWLEDGE, source_type="STAGE_GUIDE", knowledge_type="STAGE_GUIDE",
            stage=provenance.get("stage"), source_id=provenance.get("source_id"),
            version=provenance.get("version")))
    for item in getattr(getattr(retrieved, "case", None), "evidence", ()) or ():
        provenance = getattr(item, "provenance", {}) or {}
        # RETRIEVED CASE TEXT SUPPORTS; it is never the case's record.
        nodes.append(_node(
            KNOWLEDGE, source_type="CASE_RECORD_RETRIEVAL",
            knowledge_type="DERIVED_CASE_TEXT", authoritative=False,
            record_id=provenance.get("source_id"),
            document_type=provenance.get("document_type"),
            stage=provenance.get("stage")))

    if envelope.get("_jev_notes"):
        nodes.append(_node(KNOWLEDGE, source_type="JEV",
                           knowledge_type="ANNOTATION", authoritative=False))

    if str(envelope.get("response_source") or "") == "LLM":
        facts = [n["node_id"] for n in nodes
                 if n["kind"] in (FACT, FINDING, IMPACT, ACTION, EVENT)]
        nodes.append(_node(COMPOSER, source_type="MODEL_WORDING",
                           authoritative=False, derived_from=facts))

    return {"question_intent": envelope.get("intent"), "nodes": nodes}


# ==========================================================================
# VALIDATE -- against the case's own records
# ==========================================================================

def validate(provenance: dict[str, Any], ledger: Any, context: Any,
             ) -> dict[str, Any]:
    """
    Mark every node verified or not, against THIS case's records; drop the
    trust of anything derived from an unverified node. Returns the problems.
    """
    from app.agents.applicant import impact as impacts

    findings = {f.finding_id: f for f in getattr(ledger, "findings", []) or []}
    decisions = {d.decision_id for d in getattr(ledger, "decisions", []) or []}
    documents = {d.document_id: d for d in getattr(ledger, "documents", []) or []}
    transitions = {t.transition_id for t in getattr(ledger, "transitions", []) or []}
    versions = {v.document_version_id for v in getattr(ledger, "versions", []) or []}
    rules = (impacts.rules().get("rules") or {})
    problems: list[str] = []
    by_id = {n["node_id"]: n for n in provenance["nodes"]}

    def owner_of(document_id: str | None) -> str | None:
        document = documents.get(document_id) if document_id else None
        return getattr(document, "owner_id", None) if document is not None else None

    def check(node: dict[str, Any]) -> str | None:
        kind = node["kind"]
        if kind == FACT and node.get("field") == "stage":
            stage = getattr(getattr(context, "stage", None), "value", None)
            return None if node.get("value") == stage else "stage differs"
        if kind == FINDING:
            record = node.get("record_id")
            if node.get("source_type") == "CASE_DECISION":
                return None if record in decisions else "decision not on case"
            finding = findings.get(record)
            if finding is None:
                return "finding not on case"
            if node.get("finding_code") not in (finding.reason_codes or []):
                return "finding code not recorded"
            party = finding.party_id or owner_of(finding.document_id)
            if (node.get("party_id") or None) != (party or None):
                return "party differs from the record"
            if node.get("document_id") and node["document_id"] not in documents:
                return "document not on case"
            return None
        if kind == IMPACT:
            code = str(node.get("finding_code") or "").upper()
            expected = f"impact_rules:{code}" if code in rules else None
            return None if node.get("rule_code") == expected else "rule mismatch"
        if kind == ACTION and node.get("role") == "PER_PROBLEM":
            code = str((node.get("rule_code") or "").split(":")[-1])
            wanted = (rules.get(code) or {}).get("action")
            return None if node.get("action_code") == wanted else "action not the rule's"
        if kind == ACTION and node.get("source_type") == "recorded_decision":
            return None if node.get("record_id") in decisions else "decision not on case"
        if kind == EVENT:
            record, source = node.get("record_id"), node.get("source_type")
            known = {"STAGE_TRANSITION": transitions, "DOCUMENT_VERSION": versions,
                     "CASE_DECISION": decisions}.get(source)
            if known is None and source in ("VERIFICATION", "KYC", "RISK",
                                            "FINANCIAL", "RCU"):
                known = set(findings)
            if known is not None:
                return None if record in known else "event record not on case"
            if source == "APPLICATION":
                # The application is keyed by the case: that key, or nothing.
                return None if record in (None, getattr(ledger, "case_id", None))                     else "application record not this case"
            if record is None and source in ("DOCUMENT", "CASE_EVENT"):
                return None       # a source with no row id, honestly None
            return "event source unknown"
        return None

    for node in provenance["nodes"]:
        reason = check(node)
        node["verified"] = reason is None
        if reason:
            problems.append(f"{node['kind']}:{reason}")
    # NOTHING DERIVED FROM AN UNVERIFIED NODE IS TRUSTED.
    changed = True
    while changed:
        changed = False
        for node in provenance["nodes"]:
            if node["verified"] and any(
                    by_id.get(p, {}).get("verified") is False
                    for p in node["relation"]["derived_from"]
                    if node["kind"] != COMPOSER):
                node["verified"] = False
                problems.append(f"{node['kind']}:derived from unverified")
                changed = True
    provenance["validated"] = not problems
    provenance["problems"] = problems
    return provenance


# ==========================================================================
# PUBLIC -- business words and codes, nothing internal
# ==========================================================================

_LABELS = {
    "STAGE_RECORD": "the application's recorded stage",
    "CASE_TIMELINE": "the case's recorded timeline",
    "APPLICATION_STATUS": "the application's recorded status",
    "KYC": "the recorded KYC check",
    "VERIFICATION": "the recorded document verification",
    "CASE_DECISION": "the recorded case decision",
    "RISK": "a recorded case check", "FINANCIAL": "a recorded income check",
    "RCU": "a recorded RCU check",
    "IMPACT_RULE": "the configured review rules",
    "workflow.next_action": "the application's document workflow",
    "recorded_decision": "the recorded case decision",
    "STAGE_TRANSITION": "the recorded stage history",
    "DOCUMENT_VERSION": "the recorded document uploads",
    "DOCUMENT": "the recorded document uploads",
    "APPLICATION": "the application record",
    "HANDBOOK": "the approved handbook",
    "STAGE_GUIDE": "the stage guide",
}


def _label(node: dict[str, Any]) -> str | None:
    return _LABELS.get(str(node.get("source_type") or ""))


def public(provenance: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a response may carry. Verified nodes only; no ids, rules, tools."""
    if not provenance:
        return None
    nodes = [n for n in provenance["nodes"] if n.get("verified") is not False]
    chains = []
    for finding in (n for n in nodes if n["kind"] == FINDING):
        impact = next((n for n in nodes if n["kind"] == IMPACT
                       and finding["node_id"] in n["relation"]["derived_from"]),
                      None)
        action = next((n for n in nodes if n["kind"] == ACTION and impact
                       and impact["node_id"] in n["relation"]["derived_from"]),
                      None)
        chains.append({k: v for k, v in {
            "finding_code": finding.get("finding_code"),
            "subject": finding.get("party_role") or "CASE",
            "source": _label(finding), "document": finding.get("document"),
            "fields": finding.get("field"),
            "observed_at": finding.get("observed_at"),
            "impact_code": impact.get("impact_code") if impact else None,
            "action_code": action.get("action_code") if action else None,
        }.items() if v is not None})
    stage = next((n for n in nodes if n["kind"] == FACT
                  and n.get("field") == "stage"), None)
    route = next((n for n in nodes if n["kind"] == ROUTE), {})
    return {
        "validated": bool(provenance.get("validated")),
        "route": route.get("route"), "subject": route.get("subject"),
        "stage": ({"value": stage["value"], "source": _label(stage)}
                  if stage else None),
        "chains": chains,
        "changes": sum(1 for n in nodes if n["kind"] == EVENT),
        "sources": sorted({label for label in (_label(n) for n in nodes
                                               if n["kind"] in (FACT, FINDING,
                                                                IMPACT, ACTION,
                                                                EVENT, KNOWLEDGE))
                           if label}),
        "case_and_knowledge_kept_apart": True,
        "worded_by_model": any(n["kind"] == COMPOSER for n in nodes),
    }


def _relevant(provenance: dict[str, Any] | None) -> list[str] | None:
    """The verified source labels the answer's kind rests on (or None: all)."""
    if not provenance:
        return None
    nodes = [n for n in provenance["nodes"] if n.get("verified") is not False]
    intent = str(provenance.get("question_intent") or "")

    def labels(kinds: tuple[str, ...], only=None) -> list[str]:
        return sorted({lab for lab in (_label(n) for n in nodes
                                       if n["kind"] in kinds
                                       and (only is None or only(n)))
                       if lab})

    if any(n["kind"] == EVENT for n in nodes):
        return labels((EVENT,))
    if intent == "APPLICATION_STAGE":
        return labels((FACT,))
    if intent == "APPLICATION_STATUS":
        return labels((FACT, FINDING, IMPACT))
    if intent in ("FOS_KNOWLEDGE", "STAGE_PROCESS"):
        return labels((KNOWLEDGE,))
    if intent == "NEXT_ACTION":
        return labels((FINDING, IMPACT, ACTION, KNOWLEDGE))
    return labels((FINDING, IMPACT, ACTION, KNOWLEDGE),
                  only=lambda n: n["kind"] != ACTION or n.get("role") != "PRIMARY")


def explain(provenance: dict[str, Any] | None) -> str:
    """
    "WHY THIS ANSWER?" -- the verified sources, in business words, at most
    two sentences. Never an id, a rule reference, a tool or a file.
    """
    view = public(provenance) or {}
    # THE SOURCES THIS ANSWER RESTS ON, not every source the case has: a
    # stage answer rests on the stage record, a "what changed" answer on the
    # recorded events, an issue answer on the findings. Chosen by what the
    # answer was, from verified nodes only.
    relevant = _relevant(provenance)
    if relevant is not None:
        view = {**view, "sources": relevant,
                "chains": view.get("chains") if any(
                    s in relevant for s in (c.get("source") for c in
                                            view.get("chains") or []))
                else []}
    sources = [s for s in view.get("sources") or []
               if s != "the configured review rules"]
    if not sources:
        return ("I can explain an answer once I have given one about this "
                "application from its records.")
    roles = {c.get("subject") for c in view.get("chains") or []}
    whose = (" for the co-applicant" if roles == {"CO_APPLICANT"} else
             " for the primary applicant" if roles == {"PRIMARY_APPLICANT"}
             else "")
    listed = ", ".join(sources[:-1]) + (" and " if len(sources) > 1 else "") \
        + sources[-1]
    rules = (" and the configured review rules"
             if "the configured review rules" in (view.get("sources") or [])
             else "")
    return (f"That answer is based on {listed}{whose}{rules}. It does not "
            f"rely on our conversation or on a model's guess.")


__all__ = ["build", "explain", "public", "validate"]
