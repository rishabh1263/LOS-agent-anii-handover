"""
The synthetic LOS: all seven stages, and recognisably not real.

WHAT THIS DEMO HAS TO BE. The Copilot is about to be demonstrated
across the whole lifecycle, and a demo that only covers FOS would prove
nothing about the six stages that have no data. So the dataset spans
every stage, gives one applicant several cases, and records the trail a
case took to reach where it is -- because "what happened before this
reached Credit" is a question the demo must be able to answer from
stored rows rather than from a script.

AND IT HAS TO BE OBVIOUSLY SYNTHETIC. Every id begins `DEMO-`, every
address contains SYNTHETIC. A reader who finds one of these rows in a
store must be able to tell at a glance that it was planted, including
from a single field in a log line.
"""

from __future__ import annotations

import pytest

from app.agents.los.stages import LosStage, Resolution, resolve
from app.store import demo_seed, set_repository
from app.store.models import FindingKind
from app.store.sqlite_repo import SQLiteRepository

ALL_STAGES = {s.value for s in LosStage}


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "demo.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def memory_on():
    """
    The stage lives on the case timeline, which `stages.resolve()`
    reads only when case memory is enabled. See `test_without_case_
    memory_only_fos_is_visible` for what the flag actually buys.
    """
    from unittest.mock import patch

    with patch("app.agents.los.config.case_memory_enabled",
               return_value=True):
        yield


@pytest.fixture
def seeded(repo):
    demo_seed.seed(repo)
    return repo


def cases(repo, applicant_id):
    return repo.list_applications(applicant_id)


# ==========================================================================
# IT COVERS THE WHOLE LIFECYCLE
# ==========================================================================


def test_every_stage_has_at_least_one_case(seeded):
    """
    A demo missing a stage cannot demonstrate it. All seven, or the
    Copilot has nothing to answer about six of them.
    """
    stages = set()
    for case in demo_seed._CASES:
        stages.add(str(case["stage"]))

    assert stages == ALL_STAGES


def test_each_case_reports_its_stage_through_the_real_resolver(
        seeded, memory_on):
    """
    NOT A SEPARATE PATH. The seeded stage is read by
    `stages.resolve()`, exactly as a real case's would be -- from the
    case timeline, not from anything the demo invented.
    """
    for case in demo_seed._CASES:
        context = resolve(str(case["case_id"]))

        assert context.stage is LosStage(case["stage"]), case["case_id"]
        assert context.resolution is Resolution.CASE_TIMELINE


def test_without_case_memory_only_fos_is_visible(seeded):
    """
    THE DEMO'S ONE PREREQUISITE, stated as a test rather than a README
    line. With `LOS_CASE_MEMORY_ENABLED` off the timeline is not read,
    and the only other stage source is `ApplicationStatus` -- whose
    four values are all inside FOS. Six of the seven stages vanish.
    """
    from unittest.mock import patch

    with patch("app.agents.los.config.case_memory_enabled",
               return_value=False):
        reported = {resolve(str(c["case_id"])).stage
                    for c in demo_seed._CASES}

    assert reported == {LosStage.FOS}


def test_a_case_carries_the_trail_that_brought_it_there(seeded):
    """
    A case in RCU did not appear there. "What happened before this
    reached Credit" needs the earlier stages to exist as rows.
    """
    timeline = seeded.get_case_timeline("DEMO-CASE-005")
    stages = [event.stage for event in timeline]

    assert stages == ["FOS", "CPA", "CREDIT", "RCU"]
    assert [e.sequence for e in timeline] == [1, 2, 3, 4]


def test_every_timeline_event_explains_itself(seeded):
    """
    The summaries are what a reviewer reads -- and what a later phase
    will index. They must be sentences, not codes.
    """
    for event in seeded.get_case_timeline("DEMO-CASE-004"):
        assert event.summary
        assert event.summary.endswith(".")


# ==========================================================================
# ONE APPLICANT, SEVERAL CASES
# ==========================================================================


def test_one_applicant_holds_several_cases(seeded):
    """Without this, no applicant-level or cross-case question works."""
    assert len(cases(seeded, "DEMO-APP-001")) == 3


def test_every_case_belongs_to_exactly_one_applicant(seeded):
    owners = {}
    for case in demo_seed._CASES:
        owners.setdefault(str(case["applicant_id"]), set()).add(
            str(case["case_id"]))

    for applicant_id, expected in owners.items():
        found = {a.case_id for a in cases(seeded, applicant_id)}
        assert found == expected, applicant_id


def test_an_applicants_cases_do_not_include_anyone_elses(seeded):
    mine = {a.case_id for a in cases(seeded, "DEMO-APP-001")}

    assert "DEMO-CASE-004" not in mine
    assert "DEMO-CASE-008" not in mine


def test_the_ownership_check_agrees_with_the_seed(seeded):
    assert seeded.applicant_owns_case("DEMO-APP-001", "DEMO-CASE-001")
    assert not seeded.applicant_owns_case("DEMO-APP-001", "DEMO-CASE-004")


# ==========================================================================
# THERE IS SOMETHING TO ANSWER WITH
# ==========================================================================


def test_findings_cover_the_kinds_that_had_no_producer(seeded):
    """
    FINANCIAL, RISK and RCU existed as enum members with nothing
    writing them. The demo gives each one real rows, so a question
    about an RCU flag has an answer.
    """
    kinds = set()
    for case in demo_seed._CASES:
        for finding in seeded.get_case_findings(str(case["case_id"])):
            kinds.add(finding.finding_kind)

    for kind in (FindingKind.VERIFICATION, FindingKind.KYC,
                 FindingKind.FINANCIAL, FindingKind.RISK, FindingKind.RCU):
        assert kind in kinds, kind


def test_a_flagged_case_records_why(seeded):
    findings = seeded.get_case_findings("DEMO-CASE-005")
    rcu = [f for f in findings if f.finding_kind is FindingKind.RCU]

    assert rcu, "the RCU case has no RCU finding"
    assert rcu[0].status == "FAIL"
    assert "ADDRESS_MISMATCH" in rcu[0].reason_codes


def test_every_case_has_a_decision_and_a_next_action(seeded):
    for case in demo_seed._CASES:
        decisions = seeded.get_case_decisions(str(case["case_id"]))

        assert decisions, case["case_id"]
        assert decisions[-1].decision
        assert decisions[-1].next_action


def test_documents_are_attached_to_their_party(seeded):
    """The joint case's third document belongs to the co-applicant."""
    documents = {d.source_id: d for d in seeded.list_documents("DEMO-CASE-004")}

    assert documents["pan.jpg"].party_role == "PRIMARY_APPLICANT"
    assert documents["copan.jpg"].party_role == "CO_APPLICANT"
    assert documents["copan.jpg"].party_id == "DEMO-COAPP-004"


def test_a_rejected_document_records_its_reason(seeded):
    documents = seeded.list_documents("DEMO-CASE-008")

    assert documents[0].verification_status == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in documents[0].reason_codes


# ==========================================================================
# DETERMINISTIC, IDEMPOTENT, AND OFF BY DEFAULT
# ==========================================================================


def test_seeding_twice_changes_nothing(repo):
    first = demo_seed.seed(repo)
    before = _snapshot(repo)

    second = demo_seed.seed(repo)

    assert first.wrote_anything
    assert second.already_present
    assert _snapshot(repo) == before


def test_forcing_a_reseed_does_not_duplicate_anything(repo):
    demo_seed.seed(repo)
    before = _snapshot(repo)

    demo_seed.seed(repo, force=True)

    assert _snapshot(repo) == before


def test_the_seed_is_the_same_every_time(tmp_path):
    """Two fresh stores, same data -- a demo answer cannot drift."""
    snapshots = []
    for name in ("one", "two"):
        repository = SQLiteRepository(tmp_path / f"{name}.sqlite3")
        repository.initialise()
        demo_seed.seed(repository)
        snapshots.append(_snapshot(repository))

    assert snapshots[0] == snapshots[1]


def test_seeding_is_off_unless_asked_for(monkeypatch):
    """
    Seeding a store a real case lives in must not happen because a
    service restarted.
    """
    monkeypatch.delenv(demo_seed.ENV_FLAG, raising=False)
    assert demo_seed.enabled() is False

    monkeypatch.setenv(demo_seed.ENV_FLAG, "true")
    assert demo_seed.enabled() is True


def test_an_empty_store_is_not_mistaken_for_a_seeded_one(repo):
    assert demo_seed.is_seeded(repo) is False

    demo_seed.seed(repo)

    assert demo_seed.is_seeded(repo) is True


# ==========================================================================
# RECOGNISABLY SYNTHETIC
# ==========================================================================


def test_every_identifier_announces_itself_as_demo_data(seeded):
    for case in demo_seed._CASES:
        assert str(case["case_id"]).startswith(demo_seed.PREFIX)
        assert str(case["applicant_id"]).startswith(demo_seed.PREFIX)

    for record in demo_seed._APPLICANTS:
        assert str(record["applicant_id"]).startswith(demo_seed.PREFIX)


def test_no_row_could_be_mistaken_for_a_real_person(seeded):
    """
    Even one field on its own -- in a log line, say -- must give it
    away.
    """
    for record in demo_seed._APPLICANTS:
        assert "SYNTHETIC" in record["address"]
        assert record["mobile"].startswith("90000000")


def test_the_seed_writes_through_the_repository_not_around_it():
    """
    A seed that reached past the abstraction would be a second way to
    create a case, and the two would disagree the first time the real
    one changed.
    """
    import inspect

    source = inspect.getsource(demo_seed)

    for forbidden in ("sqlite3", "INSERT", "CREATE TABLE", "cursor", "execute("):
        assert forbidden not in source, forbidden


def _snapshot(repository) -> dict:
    """Everything the seed wrote, in a comparable form."""
    out: dict = {}
    for case in demo_seed._CASES:
        case_id = str(case["case_id"])
        out[case_id] = {
            "documents": sorted(
                (d.source_id, d.verification_status, d.party_role)
                for d in repository.list_documents(case_id)),
            "findings": sorted(
                (f.finding_kind.value, f.status, tuple(f.reason_codes))
                for f in repository.get_case_findings(case_id)),
            "decisions": sorted(
                (d.decision, d.next_action)
                for d in repository.get_case_decisions(case_id)),
            "timeline": [(e.sequence, e.stage)
                         for e in repository.get_case_timeline(case_id)],
        }
    return out
