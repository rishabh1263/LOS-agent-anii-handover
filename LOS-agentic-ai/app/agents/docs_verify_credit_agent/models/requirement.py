"""One requirement-level outcome of the ten rules (internal: every outcome, satisfied or not, is audited)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.agents.docs_verify_credit_agent.config import reasons as _reasons


class _RuleNames:
    """The eleven rules and their names, from config/reasons.yaml `rule_names` (read-only mapping)."""

    def __getitem__(self, rule_id: str) -> str:
        return _reasons.rule_name(rule_id)

    def items(self):
        return [(r, _reasons.rule_name(r)) for r in _reasons.rule_ids()]

    def keys(self):
        return list(_reasons.rule_ids())

    def __iter__(self):
        return iter(_reasons.rule_ids())

    def __contains__(self, rule_id: object) -> bool:
        return rule_id in _reasons.rule_ids()

    def __len__(self) -> int:
        return len(_reasons.rule_ids())


RULES = _RuleNames()

# outcome statuses: the first two are satisfied (never listed in the API's requirements array)
SATISFIED, WAIVED = "SATISFIED", "WAIVED"
ACTION_REQUIRED, PENDING_VERIFICATION, UNDER_REVIEW = "ACTION_REQUIRED", "PENDING_VERIFICATION", "UNDER_REVIEW"
MISMATCH, UNRESOLVED, EXCEPTION_PENDING = "MISMATCH", "UNRESOLVED", "EXCEPTION_PENDING"
DONE = {SATISFIED, WAIVED}

BLOCKING, NON_BLOCKING = "BLOCKING", "NON_BLOCKING"

# recommended actions, most urgent first (the response's overall action is the most urgent open one)
ACTIONS = ("RESOLVE_POLICY_CONFIGURATION", "CAPTURE_APPLICATION_DATA", "REQUEST_DOCUMENTS", "REQUEST_FRESH_DOCUMENT",
           "RECHECK_DOCUMENT", "AWAIT_VERIFICATION", "RAISE_EXCEPTION", "AWAIT_EXCEPTION_APPROVAL",
           "AWAIT_CREDIT_STAGE", "PROCEED")


@dataclass
class Outcome:
    rule_id: str
    check_id: str                       # the policy rule / slot this outcome is about (stable)
    status: str
    severity: str
    reason: str
    recommended_action: str
    party_id: str | None = None
    party_role: str | None = None
    slot: str | None = None
    missing_documents: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)       # document:<id> / deviation:<id> / policy:<rule id>
    deviation_codes: list[str] = field(default_factory=list)

    @property
    def rule_name(self) -> str:
        return RULES[self.rule_id]

    @property
    def open(self) -> bool:
        return self.status not in DONE

    def as_dict(self) -> dict:
        return {"rule_name": self.rule_name, **asdict(self)}


__all__ = ["ACTIONS", "ACTION_REQUIRED", "BLOCKING", "DONE", "EXCEPTION_PENDING", "MISMATCH", "NON_BLOCKING",
           "Outcome", "PENDING_VERIFICATION", "RULES", "SATISFIED", "UNDER_REVIEW", "UNRESOLVED", "WAIVED"]
