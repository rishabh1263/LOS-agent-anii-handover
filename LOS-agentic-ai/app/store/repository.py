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

from app.store.models import Applicant, Application, Document


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


__all__ = ["Repository", "RepositoryError"]
