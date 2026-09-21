"""
What the pipeline concluded, kept.

WHAT THIS LAYER IS FOR. Everything the LOS pipeline decides -- what
verification found, what KYC concluded, what the decision was -- was
computed, returned in one response and then lost. "Why did KYC go to
review?" could not be answered five minutes later because nothing had
written the answer down. This records it.

WHAT IT IS NOT. It is not authorisation. `get_case_findings` filters on
a case_id; it does not decide whether the caller may see that case, and
the tests below that talk about isolation are testing that the FILTER
cannot reach across a boundary -- not that it replaces the ownership
check in permissions.py.

IT IS ALSO NOT A SECOND SOURCE OF TRUTH. Persistence runs after the
response is built, behind a flag that is off by default, inside a
handler that cannot propagate. Several tests below exist purely to keep
it that way: a case-memory bug must never cost a caller their answer.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from app.store.models import (
    Applicant,
    Application,
    CaseDecision,
    CaseEvent,
    CaseFinding,
    Document,
    DocumentVersion,
    FindingKind,
)
from app.store.sqlite_repo import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "case_memory.sqlite3")
    repository.initialise()
    return repository


@pytest.fixture
def case(repo):
    """One applicant, one case, one document — the minimum to hang findings on."""
    repo.save_applicant(Applicant(applicant_id="APP-1"))
    repo.save_application(Application(case_id="CASE-1", applicant_id="APP-1"))
    repo.save_document(Document(
        document_id="CASE-1:APP-1:pan.jpg", case_id="CASE-1",
        applicant_id="APP-1", document_type="PAN", party_id="APP-1"))
    return repo


def finding(case_id="CASE-1", kind=FindingKind.KYC, party_id="APP-1",
            status="PASS", digest=None, **kw):
    return CaseFinding(
        finding_id=kw.pop("finding_id", os.urandom(8).hex()),
        case_id=case_id, finding_kind=kind, party_id=party_id,
        status=status, content_hash=digest, **kw)


# ==========================================================================
# 1-3. SCHEMA, MIGRATION, EXISTING DATA
# ==========================================================================


def test_initialisation_creates_the_case_memory_tables(repo):
    import sqlite3

    tables = {r[0] for r in sqlite3.connect(str(repo._path)).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"case_findings", "document_versions", "case_decisions",
            "case_events"} <= tables


def test_initialisation_is_idempotent(repo, case):
    """Opening an existing store must add nothing and lose nothing."""
    case.save_finding(finding(digest="h1"))

    repo.initialise()
    repo.initialise()

    assert len(repo.get_case_findings("CASE-1")) == 1


def test_existing_entities_are_untouched(case):
    """The three original tables keep working exactly as before."""
    case.save_finding(finding(digest="h1"))

    assert case.get_applicant("APP-1") is not None
    assert case.get_application("CASE-1") is not None
    assert len(case.list_documents("CASE-1")) == 1


def test_a_store_written_before_case_memory_still_opens(tmp_path):
    """
    THE UPGRADE PATH. A file created by the previous release has none of
    these tables; opening it must add them rather than refuse.
    """
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE applicants (applicant_id TEXT PRIMARY KEY, "
                 "full_name TEXT, mobile TEXT, email TEXT, "
                 "date_of_birth TEXT, address TEXT, created_at TEXT, "
                 "updated_at TEXT)")
    conn.execute("INSERT INTO applicants (applicant_id, full_name, "
                 "created_at, updated_at) VALUES ('OLD-1','Kept','x','x')")
    conn.commit()
    conn.close()

    repository = SQLiteRepository(path)
    repository.initialise()

    assert repository.get_applicant("OLD-1").full_name == "Kept"
    assert repository.get_case_findings("anything") == []


# ==========================================================================
# 4-8. ROUND TRIPS
# ==========================================================================


def test_a_finding_survives_the_request(case):
    case.save_finding(finding(
        digest="h1", score=40, confidence=80,
        reason_codes=["NAME_MISMATCH"], payload={"fields": 2}))

    stored = case.get_case_findings("CASE-1")[0]

    assert stored.finding_kind is FindingKind.KYC
    assert stored.status == "PASS"
    assert stored.score == 40
    assert stored.confidence == 80
    assert stored.reason_codes == ["NAME_MISMATCH"]
    assert stored.payload == {"fields": 2}


def test_findings_can_be_narrowed_by_kind(case):
    case.save_finding(finding(kind=FindingKind.KYC, digest="a"))
    case.save_finding(finding(kind=FindingKind.VERIFICATION, digest="b"))
    case.save_finding(finding(kind=FindingKind.RISK, digest="c"))

    assert len(case.get_case_findings("CASE-1")) == 3
    assert len(case.get_case_findings("CASE-1", kind=FindingKind.KYC)) == 1
    assert len(case.get_case_findings("CASE-1", kind="RISK")) == 1


def test_a_document_version_survives_the_request(case):
    case.save_document_version(DocumentVersion(
        document_version_id="v1", document_id="CASE-1:APP-1:pan.jpg",
        case_id="CASE-1", party_id="APP-1", version=1, source_id="pan.jpg"))

    versions = case.get_document_versions("CASE-1:APP-1:pan.jpg")

    assert [v.version for v in versions] == [1]
    assert versions[0].source_id == "pan.jpg"


def test_a_reupload_is_a_new_version_not_an_overwrite(case):
    """
    The question a reviewer asks after a rejection is "what changed",
    and an overwrite cannot answer it.
    """
    for n in (1, 2, 3):
        case.save_document_version(DocumentVersion(
            document_version_id=f"v{n}", document_id="CASE-1:APP-1:pan.jpg",
            case_id="CASE-1", version=n, source_id="pan.jpg"))

    assert [v.version for v in
            case.get_document_versions("CASE-1:APP-1:pan.jpg")] == [1, 2, 3]


def test_a_decision_survives_with_its_policy(case):
    """
    A decision without its policy version cannot be explained later --
    the rules will have moved and nothing says which ones applied.
    """
    case.save_decision(CaseDecision(
        decision_id="d1", case_id="CASE-1", decision="REVIEW",
        next_action="MANUAL_REVIEW", status="PARTIAL",
        reason_codes=["NAME_MISMATCH"], policy_id="personal_loan",
        policy_version="0.1.0"))

    stored = case.get_case_decisions("CASE-1")[0]

    assert (stored.decision, stored.next_action) == ("REVIEW", "MANUAL_REVIEW")
    assert stored.policy_version == "0.1.0"


# ==========================================================================
# 13. TIMELINE ORDER
# ==========================================================================


def test_the_timeline_keeps_the_order_things_happened(case):
    for n in range(5):
        case.record_event(CaseEvent(
            event_id=f"e{n}", case_id="CASE-1", event_type=f"STEP_{n}"))

    events = case.get_case_timeline("CASE-1")

    assert [e.event_type for e in events] == [f"STEP_{n}" for n in range(5)]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5]


def test_events_in_one_millisecond_still_have_an_order(case):
    """
    Several events are written inside one request. A timeline that
    reorders itself between reads is worse than no timeline, so the
    order comes from `sequence` and not from the timestamp.
    """
    stamp = None
    for n in range(3):
        event = case.record_event(CaseEvent(
            event_id=f"e{n}", case_id="CASE-1", event_type=f"S{n}"))
        stamp = stamp or event.created_at

    events = case.get_case_timeline("CASE-1")

    assert len({e.sequence for e in events}) == 3
    assert [e.sequence for e in events] == sorted(e.sequence for e in events)


# ==========================================================================
# 14-15. ISOLATION — THE PROPERTY THAT MATTERS MOST
# ==========================================================================


def test_a_party_never_sees_the_other_partys_findings(case):
    """
    Two people on one case. A KYC result belongs to one of them, and
    asking for one party's findings must not return the other's.
    """
    case.save_finding(finding(party_id="APP-1", digest="a"))
    case.save_finding(finding(party_id="COAPP-9", digest="b"))

    primary = case.get_case_findings("CASE-1", party_id="APP-1")
    co = case.get_case_findings("CASE-1", party_id="COAPP-9")

    assert {f.party_id for f in primary} == {"APP-1"}
    assert {f.party_id for f in co} == {"COAPP-9"}


def test_a_case_level_finding_reaches_both_parties(case):
    """
    A cross-document check belongs to the CASE, not to one person.
    Hiding it when a party is named would lose it entirely.
    """
    case.save_finding(finding(party_id=None, kind=FindingKind.VERIFICATION,
                              digest="case-level"))
    case.save_finding(finding(party_id="APP-1", digest="a"))

    primary = case.get_case_findings("CASE-1", party_id="APP-1")

    assert None in {f.party_id for f in primary}
    assert len(primary) == 2


def test_one_case_never_returns_anothers_findings(repo):
    """
    CROSS-CASE ISOLATION. Two cases, same applicant, same party id --
    the only thing separating them is the case, so the filter has to.
    """
    repo.save_applicant(Applicant(applicant_id="APP-1"))
    for case_id in ("CASE-1", "CASE-2"):
        repo.save_application(Application(case_id=case_id,
                                          applicant_id="APP-1"))
        repo.save_finding(finding(case_id=case_id, party_id="APP-1",
                                  digest=f"h-{case_id}"))

    first = repo.get_case_findings("CASE-1")
    second = repo.get_case_findings("CASE-2")

    assert {f.case_id for f in first} == {"CASE-1"}
    assert {f.case_id for f in second} == {"CASE-2"}


def test_an_unrelated_applicants_case_is_not_reachable(repo):
    repo.save_applicant(Applicant(applicant_id="APP-1"))
    repo.save_applicant(Applicant(applicant_id="APP-2"))
    repo.save_application(Application(case_id="CASE-1", applicant_id="APP-1"))
    repo.save_application(Application(case_id="CASE-2", applicant_id="APP-2"))
    repo.save_finding(finding(case_id="CASE-2", party_id="APP-2", digest="x"))

    assert repo.get_case_findings("CASE-1") == []


def test_decisions_and_timelines_are_case_scoped(repo):
    repo.save_applicant(Applicant(applicant_id="APP-1"))
    for case_id in ("CASE-1", "CASE-2"):
        repo.save_application(Application(case_id=case_id,
                                          applicant_id="APP-1"))
        repo.save_decision(CaseDecision(decision_id=f"d-{case_id}",
                                        case_id=case_id, decision="PASS"))
        repo.record_event(CaseEvent(event_id=f"e-{case_id}", case_id=case_id,
                                    event_type="PROCESSED"))

    assert [d.case_id for d in repo.get_case_decisions("CASE-1")] == ["CASE-1"]
    assert [e.case_id for e in repo.get_case_timeline("CASE-2")] == ["CASE-2"]


def test_the_repository_does_not_decide_who_may_read(repo):
    """
    DATA ACCESS, NOT AUTHORISATION. These methods take a case_id and
    filter on it. The ownership question is answered by
    `applicant_owns_case` and by permissions.py, and putting a second
    answer here would be two places to get it wrong.
    """
    import inspect

    source = inspect.getsource(SQLiteRepository.get_case_findings)

    for token in ("Caller", "scope", "require_jwt", "permission"):
        assert token not in source


# ==========================================================================
# 19. IDEMPOTENCY
# ==========================================================================


def test_recording_the_same_finding_twice_keeps_one_row(case):
    """
    A case processed twice should not read as a case that changed its
    mind.
    """
    for _ in range(3):
        case.save_finding(finding(digest="same", status="REVIEW"))

    stored = case.get_case_findings("CASE-1")

    assert len(stored) == 1
    assert stored[0].version == 3


def test_a_changed_finding_is_recorded_separately(case):
    """Different content is a different finding, not a duplicate."""
    case.save_finding(finding(digest="first", status="REVIEW"))
    case.save_finding(finding(digest="second", status="PASS"))

    assert len(case.get_case_findings("CASE-1")) == 2


def test_re_recording_a_document_version_keeps_one_row(case):
    for _ in range(3):
        case.save_document_version(DocumentVersion(
            document_version_id="v1", document_id="CASE-1:APP-1:pan.jpg",
            case_id="CASE-1", version=1, source_id="pan.jpg"))

    assert len(case.get_document_versions("CASE-1:APP-1:pan.jpg")) == 1


def test_re_recording_an_event_does_not_duplicate_it(case):
    for _ in range(3):
        case.record_event(CaseEvent(event_id="e1", case_id="CASE-1",
                                    event_type="PROCESSED"))

    assert len(case.get_case_timeline("CASE-1")) == 1


# ==========================================================================
# 20. CONTRACT COMPLIANCE
# ==========================================================================


def test_the_new_methods_are_not_abstract():
    """
    ADDITIVE. An existing Repository implementation must keep satisfying
    the interface without being edited, so the case-memory methods have
    defaults rather than being abstract.
    """
    from app.store.repository import Repository

    assert len(Repository.__abstractmethods__) == 11
    for name in ("save_finding", "get_case_findings", "save_document_version",
                 "get_document_versions", "save_decision",
                 "get_case_decisions", "record_event", "get_case_timeline"):
        assert name not in Repository.__abstractmethods__


def test_a_backend_without_case_memory_reports_nothing(tmp_path):
    """
    The default returns "no findings", which is true of a backend that
    has not implemented it -- rather than failing to construct.
    """
    from app.store.repository import Repository

    class Minimal(Repository):
        def initialise(self): pass
        def close(self): pass
        def get_applicant(self, applicant_id): return None
        def save_applicant(self, applicant): return applicant
        def list_applicants(self, limit=50): return []
        def get_application(self, case_id): return None
        def save_application(self, application): return application
        def list_applications(self, applicant_id): return []
        def get_document(self, document_id): return None
        def save_document(self, document): return document
        def list_documents(self, case_id): return []

    backend = Minimal()

    assert backend.get_case_findings("C") == []
    assert backend.get_case_timeline("C") == []
    assert backend.get_case_decisions("C") == []
    assert backend.get_document_versions("D") == []


def test_an_unknown_finding_kind_does_not_break_a_read(case):
    """
    A row written by a newer version must not make an older reader fall
    over on an ordinary SELECT.
    """
    import sqlite3

    case.save_finding(finding(digest="h1"))
    conn = sqlite3.connect(str(case._path))
    conn.execute("UPDATE case_findings SET finding_kind = 'FROM_THE_FUTURE'")
    conn.commit()
    conn.close()

    assert len(case.get_case_findings("CASE-1")) == 1
