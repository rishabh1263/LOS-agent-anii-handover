"""What every rule reads (Evidence) and how it says what it found -- names, sentences and severities from the wording
config (bank_verification_wording.yaml `rules.<id>`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.credit.bank_verification.config import say, words
from app.agents.credit.bank_verification.schemas import (Comparison, DocumentFacts, EnteredDetails, Mode,
                                                         OwnershipStatus, RiskFlag, RuleResult, RuleStatus, Severity,
                                                         StatementData)


@dataclass(frozen=True)
class PriorDocument:
    """A statement already on file in ANOTHER case (rule 7)."""
    case_id: str
    sha256: str | None
    fingerprint: str | None
    account_number: str | None
    transaction_keys: frozenset[str] = frozenset()


@dataclass
class Evidence:
    """Everything one verification run knows, each source kept apart."""
    case_id: str
    mode: Mode
    applicant_name: str | None
    entered: EnteredDetails                       # the Leviosa form (AA prefill / extracted / typed)
    aa: StatementData | None = None               # the (simulated) Account Aggregator response
    statement: StatementData | None = None        # what the agent read off the statement file
    facts: DocumentFacts | None = None            # what the agent found about that file
    ownership: OwnershipStatus | None = None      # the agency's branch confirmation, when there is one
    prior_documents: list[PriorDocument] = field(default_factory=list)


class Words:
    """One rule's wording: `w("pass")`, `w("fail", fields=...)`, `w.name`, `w.list("evidence")`."""

    def __init__(self, rule_id: str):
        self.rule_id = rule_id

    def __call__(self, key: str, **values: Any) -> str:
        return say(f"rules.{self.rule_id}.{key}", **values)

    def raw(self, key: str) -> Any:
        return words(f"rules.{self.rule_id}.{key}")

    def list(self, key: str) -> list[str]:
        return [str(x) for x in self.raw(key)]

    @property
    def name(self) -> str:
        return self("name")


def reference(ev: "Evidence") -> tuple[Any, str] | tuple[None, None]:
    """The source the form is checked against in this mode (config reference_source, first one present), and its
    label: AA received -> the AA data (the statement when no AA data); AA not received -> the statement."""
    from app.agents.credit.bank_verification.config import section

    order = (section("reference_source") or {}).get(ev.mode.value) or ["STATEMENT"]
    for name in order:
        data = ev.aa if str(name) == "AA" else ev.statement
        if data is not None:
            return data, common("aa_data") if str(name) == "AA" else common("statement")
    return None, None


def common(key: str, **values: Any) -> str:
    return say(f"common.{key}", **values)


def result(rule_id: str, status: RuleStatus, reason: str, **extra: Any) -> RuleResult:
    return RuleResult(rule_id=rule_id, rule_name=Words(rule_id).name, status=status, reason=reason, **extra)


def flag(rule_id: str, code: str, message: str | None = None, **values: Any) -> RiskFlag:
    """A risk flag: its severity (and, unless given, its sentence) from rules.<id>.flags.<code>."""
    spec = words(f"rules.{rule_id}.flags.{code}")
    text = message if message is not None else str(spec["text"]).format(**values)
    return RiskFlag(code=code, severity=Severity(str(spec["severity"])), message=text, rule_id=rule_id)


def unchecked(ev: "Evidence", key: str) -> bool:
    """A form value the statement filled (config statement_prefill) that the CPA has not yet checked against it."""
    from app.agents.credit.bank_verification.config import section
    from app.agents.credit.bank_verification.schemas import Provenance

    if not section("statement_prefill").get("require_confirmation", True):
        return False
    return ev.entered.provenance.get(key) == Provenance.EXTRACTED and ev.mode.value in \
        [str(m) for m in section("statement_prefill").get("modes") or []]


def checked(ev: "Evidence", key: str) -> bool:
    from app.agents.credit.bank_verification.schemas import Provenance

    return ev.entered.provenance.get(key) == Provenance.CONFIRMED


def verdict(comparisons: list[Comparison]) -> RuleStatus:
    """Any mismatch -> FAIL; anything that could not be compared -> REVIEW_REQUIRED; else PASS."""
    if any(c.match is False for c in comparisons):
        return RuleStatus.FAIL
    if not comparisons or any(c.match is None for c in comparisons):
        return RuleStatus.REVIEW_REQUIRED
    return RuleStatus.PASS
