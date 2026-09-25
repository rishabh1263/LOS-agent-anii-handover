"""
The storage contract.

THIS is what the rest of the service depends on. SQLite is one implementation
of it and the only one in this build; a PostgreSQL repository, or an adapter
onto an existing LOS over HTTP, replaces it by implementing this interface and
changing one configuration value. No caller imports sqlite3, and none should.

The interface is deliberately narrow and record-shaped: get one, list by
parent, upsert one. There is no query language here, because a query language
in the interface is a query language every future backend has to implement.

NOT FOUND IS NOT AN ERROR HERE. A repository returns None for a missing
record and lets the caller decide whether that is a 404, an empty checklist,
or a case that has not been created yet. Raising from the storage layer would
force every caller into a try block to ask an ordinary question.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.store.models import (
    Applicant,
    Application,
    CaseDecision,
    CaseEvent,
    CaseFinding,
    CaseStage,
    StageTransition,
    Document,
    DocumentVersion,
    FindingKind,
)


def _slot(finding: CaseFinding) -> tuple[str, str, str, str]:
    """
    Which logical finding a row is.

    One LOS run writes at most one row per slot: one verification and one
    extraction per document, one KYC per party (or per case), one income
    and one eligibility result -- the last two told apart by source type.
    A later row in the same slot is a later run's answer to the same
    question.
    """
    kind = getattr(finding.finding_kind, "value", str(finding.finding_kind))
    return (kind, finding.source_type or "", finding.party_id or "",
            finding.source_id or "")


def current_findings(findings: list[CaseFinding]) -> list[CaseFinding]:
    """
    The latest-written row of each logical finding, oldest-written first.

    LATEST BY LAST WRITE, NOT FIRST WRITE. A re-run that reaches a
    conclusion an earlier run already recorded updates that row, whose
    `created_at` stays where it was; `updated_at` is what moves. Rows
    written before that column existed fall back to `created_at`. Ties
    keep the order the store returned them in, which is insertion order.
    """
    def written(finding: CaseFinding):
        return finding.updated_at or finding.created_at

    latest: dict[tuple[str, str, str, str], CaseFinding] = {}
    for finding in sorted(findings, key=written):
        latest[_slot(finding)] = finding
    return sorted(latest.values(), key=written)


class RepositoryError(RuntimeError):
    """
    The storage backend could not serve the request.

    Reserved for genuine faults -- an unreachable database, a corrupt file.
    Never raised for "no such record".
    """


class Repository(ABC):
    """Persistent storage for the FOS-stage entities."""

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def initialise(self) -> None:
        """Create or migrate whatever the backend needs. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release connections. Safe to call more than once."""

    def health(self) -> dict[str, object]:
        """
        A cheap liveness answer for the readiness probe.

        Default implementation reports the class name only; a backend with a
        connection to check should override it.
        """
        return {"backend": type(self).__name__, "available": True}

    # -- applicants --------------------------------------------------------

    @abstractmethod
    def get_applicant(self, applicant_id: str) -> Applicant | None:
        """One applicant, or None when there is no such record."""

    @abstractmethod
    def save_applicant(self, applicant: Applicant) -> Applicant:
        """Insert or update by applicant_id. Returns the stored record."""

    @abstractmethod
    def list_applicants(self, limit: int = 50) -> list[Applicant]:
        """Most recently updated first. For operator tooling, not for the LLM."""

    # -- applications ------------------------------------------------------

    @abstractmethod
    def get_application(self, case_id: str) -> Application | None:
        ...

    @abstractmethod
    def save_application(self, application: Application) -> Application:
        """Insert or update by case_id. Returns the stored record."""

    @abstractmethod
    def list_applications(self, applicant_id: str) -> list[Application]:
        """Every application belonging to one applicant, newest first."""

    # -- documents ---------------------------------------------------------

    @abstractmethod
    def get_document(self, document_id: str) -> Document | None:
        ...

    @abstractmethod
    def save_document(self, document: Document) -> Document:
        """Insert or update by document_id. Returns the stored record."""

    @abstractmethod
    def list_documents(self, case_id: str) -> list[Document]:
        """Every document attached to one case, oldest first."""

    # -- authorisation -----------------------------------------------------

    def applicant_owns_case(self, applicant_id: str, case_id: str) -> bool:
        """
        Whether this case belongs to this applicant.

        Lives on the repository because the storage layer is the only thing
        that knows it, and the check has to run before any case data is read.
        The default works for every backend; override only to make it cheaper.
        """
        application = self.get_application(case_id)
        return application is not None and application.applicant_id == applicant_id

    # -- who may access what ------------------------------------------------
    #
    # A GRANT BINDS AN AUTHENTICATED SUBJECT (the JWT `sub`) to an applicant
    # or a case. It is written when that subject creates the resource, and
    # read before any case data is served (app/security/access.py).
    #
    # FAIL CLOSED BY DEFAULT. A backend that does not implement grants
    # records none and grants nothing: every non-service caller is refused
    # rather than silently allowed.

    def grant_access(self, subject: str, resource_type: str,
                     resource_id: str) -> None:
        """Record that `subject` may access this APPLICANT or CASE."""
        raise NotImplementedError(
            f"{type(self).__name__} does not record access grants")

    def has_access(self, subject: str, resource_type: str,
                   resource_id: str) -> bool:
        """Whether `subject` holds a grant on this APPLICANT or CASE."""
        return False


    # -- case memory -------------------------------------------------------
    #
    # ADDITIVE, AND NOT ABSTRACT. Every method below has a default that does
    # nothing and returns nothing, so an existing Repository implementation
    # keeps satisfying this interface without being edited. A backend that
    # has not implemented case memory reports "no findings", which is true
    # of it, rather than failing to construct.
    #
    # DATA ACCESS ONLY. These take a case_id and filter on it; they do not
    # decide whether the caller may see that case. Authorisation stays where
    # it is -- require_jwt, Caller, capability, then ownership -- and moving
    # any of it here would put two answers to one question in the codebase.
    # `applicant_owns_case` above is the seam those checks call.

    def save_finding(self, finding: CaseFinding) -> CaseFinding:
        """Record one conclusion. Re-recording an unchanged one is a no-op."""
        return finding

    def get_case_findings(
        self,
        case_id: str,
        party_id: str | None = None,
        kind: FindingKind | str | None = None,
    ) -> list[CaseFinding]:
        """
        Findings for one case, oldest first.

        `party_id` narrows to one party AND to case-level findings that
        belong to nobody in particular; omitting it returns everything on
        the case. It never widens beyond the case.
        """
        return []

    def get_current_findings(
        self,
        case_id: str,
        party_id: str | None = None,
        kind: FindingKind | str | None = None,
    ) -> list[CaseFinding]:
        """
        The findings that describe the case NOW, oldest-written first.

        `get_case_findings` is the history: every run's conclusions. A case
        processed twice keeps both, and a reader that took the first
        mismatch it met reported a PAN name the pipeline had since
        corrected. This keeps, for each logical finding, only the most
        recently written row -- see `current_findings`.
        """
        return current_findings(
            self.get_case_findings(case_id, party_id=party_id, kind=kind))

    def save_document_version(self, version: DocumentVersion) -> DocumentVersion:
        """Record one upload of one document."""
        return version

    def get_document_versions(self, document_id: str) -> list[DocumentVersion]:
        """Every version of one document, oldest first."""
        return []

    def save_decision(self, decision: CaseDecision) -> CaseDecision:
        """Record a decision the pipeline reached."""
        return decision

    def get_case_decisions(self, case_id: str) -> list[CaseDecision]:
        """Decisions recorded for one case, oldest first."""
        return []

    def record_event(self, event: CaseEvent) -> CaseEvent:
        """Append one event to the case timeline."""
        return event

    def get_case_timeline(self, case_id: str) -> list[CaseEvent]:
        """
        The case timeline, in the order things happened.

        Ordered by `sequence` and not by timestamp: several events are
        written inside one request and can share a millisecond.
        """
        return []

    # -- stage lifecycle -----------------------------------------------------
    #
    # ADDITIVE, AND FAIL CLOSED FOR WRITES. A backend without a stage record
    # reports none (so its cases resolve exactly as before) and refuses to
    # transition anything rather than pretending it did.

    def get_case_stage(self, case_id: str) -> CaseStage | None:
        """The case's authoritative stage record, or None if never set."""
        return None

    def get_stage_transitions(self, case_id: str) -> list[StageTransition]:
        """The case's stage history, oldest first."""
        return []

    def get_stage_transition(self, transition_id: str) -> StageTransition | None:
        """One recorded transition, by id."""
        return None

    def apply_stage_transition(self, expected_version: int, state: CaseStage,
                               transition: StageTransition,
                               event: CaseEvent) -> bool:
        """
        Write a transition ATOMICALLY: the new stage record, its history
        row and its timeline event, or none of them.

        COMPARE-AND-SET. Applied only while the stored record is still at
        `expected_version` (0 = no record yet); returns False, writing
        nothing, when another writer got there first.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not record stage transitions")


__all__ = ["Repository", "RepositoryError"]
