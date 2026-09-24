"""
The LOS stage foundation: one vocabulary, one registry, one Copilot.

WHAT THIS REPLACES. There was no stage vocabulary at all. `stage` was a
free string and the only values anything produced were
`DOCUMENT_COLLECTION` and `UNDER_REVIEW` -- application statuses INSIDE
the FOS stage, not stages. A Copilot meant to serve seven desks had no
way to say which desk it was answering as.

THE TWO PROPERTIES THESE TESTS EXIST FOR:

  THE STAGE IS READ, NEVER INFERRED. Not from the wording of the
  question, and not from a stage the caller supplied when the case
  record says otherwise -- a frontend that can set the stage can choose
  which stage's answers it gets.

  AN UNBUILT STAGE SAYS SO. Six of the seven have no corpus and no
  capability. A process question for one of them must return
  CAPABILITY_UNAVAILABLE, not an answer out of the only corpus that
  exists, which is FOS's.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agents.los import stage_registry, stages
from app.agents.los.stages import LosStage, Resolution

UNBUILT = [LosStage.CPA, LosStage.CREDIT, LosStage.RCU,
           LosStage.BOPS, LosStage.HOPS, LosStage.DISBURSEMENT]


class Event:
    """A case timeline row, as the repository returns one."""

    def __init__(self, stage=None, sequence=0):
        self.stage = stage
        self.sequence = sequence
        self.event_type = "TEST"


class Application:
    def __init__(self, status):
        self.status = status


def with_case(timeline=None, application=None, memory_on=True):
    """Patch the two authoritative sources `resolve` reads."""
    repository = type("Repo", (), {
        "get_case_timeline": lambda self, case_id: list(timeline or []),
        "get_application": lambda self, case_id: application,
    })()
    return (
        patch("app.store.get_repository", return_value=repository),
        patch("app.agents.los.config.case_memory_enabled",
              return_value=memory_on),
    )


def resolve(case_id="CASE-1", claimed=None, **kw):
    store, memory = with_case(**kw)
    with store, memory:
        return stages.resolve(case_id, claimed)


# ==========================================================================
# A. THE VOCABULARY
# ==========================================================================


def test_the_seven_stages_are_exactly_the_lifecycle():
    assert [s.value for s in LosStage] == [
        "FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT"]


def test_the_declared_order_is_the_lifecycle_order():
    assert list(stages.ORDER) == list(LosStage)


def test_unresolved_is_not_a_stage():
    """
    A case is never "in the UNRESOLVED stage". Not knowing which stage a
    case is in is a different claim from knowing, and making it an enum
    member would let it be passed anywhere a real stage is accepted.
    """
    assert "UNRESOLVED" not in {s.value for s in LosStage}
    assert stages.UNRESOLVED.stage is None
    assert stages.UNRESOLVED.resolution is Resolution.UNRESOLVED


@pytest.mark.parametrize("text,expected", [
    ("FOS", LosStage.FOS), ("fos", LosStage.FOS), (" Credit ", LosStage.CREDIT),
    ("disbursement", LosStage.DISBURSEMENT),
])
def test_a_stage_name_is_parsed_however_it_is_written(text, expected):
    assert stages.parse(text) is expected


@pytest.mark.parametrize("rubbish", ["", None, "UNDERWRITING", "FOS2", 17, {}])
def test_anything_that_is_not_a_stage_parses_to_nothing(rubbish):
    assert stages.parse(rubbish) is None


# ==========================================================================
# B. FOS RESOLVES
# ==========================================================================


def test_the_case_timeline_decides_the_stage():
    context = resolve(timeline=[Event("FOS")])

    assert context.stage is LosStage.FOS
    assert context.resolution is Resolution.CASE_TIMELINE


def test_the_most_recent_recorded_stage_wins():
    context = resolve(timeline=[Event("FOS"), Event("CPA")])

    assert context.stage is LosStage.CPA


@pytest.mark.parametrize("status", [
    "APPLICATION_CREATED", "DOCUMENT_COLLECTION",
    "BASIC_DOCUMENT_VERIFICATION", "READY_FOR_CPA", "UNDER_REVIEW",
])
def test_every_application_status_is_inside_the_fos_stage(status):
    """
    `ApplicationStatus` documents itself as "where an application sits in
    the FOS stage" -- all four are steps WITHIN FOS.
    """
    context = resolve(application=Application(status))

    assert context.stage is LosStage.FOS
    assert context.resolution is Resolution.APPLICATION_STATUS


def test_ready_for_cpa_is_still_fos():
    """
    READY TO HAND OVER IS NOT HANDED OVER. Reporting CPA here would
    claim a transition nobody recorded, and route the question to a desk
    that has not received the case.
    """
    assert resolve(application=Application("READY_FOR_CPA")).stage is LosStage.FOS


def test_the_timeline_outranks_the_application_status():
    context = resolve(timeline=[Event("CREDIT")],
                      application=Application("DOCUMENT_COLLECTION"))

    assert context.stage is LosStage.CREDIT


# ==========================================================================
# C. IT DOES NOT GUESS
# ==========================================================================


def test_nothing_recorded_means_unresolved_not_fos():
    """
    DEFAULTING TO FOS WOULD ANSWER EVERY UNRESOLVABLE CASE OUT OF THE
    FOS CORPUS. Absence is reported as absence.
    """
    context = resolve()

    assert context.stage is None
    assert context.resolution is Resolution.UNRESOLVED
    assert context.resolved is False


def test_the_case_record_overrides_what_the_caller_claimed():
    """
    A frontend that could set the stage could choose which stage's
    answers it receives, which makes stage scoping a preference rather
    than a boundary.
    """
    context = resolve(claimed="DISBURSEMENT", timeline=[Event("FOS")])

    assert context.stage is LosStage.FOS
    assert context.resolution is Resolution.CASE_TIMELINE


def test_a_claimed_stage_is_used_only_when_the_record_is_silent():
    context = resolve(claimed="CPA")

    assert context.stage is LosStage.CPA
    assert context.resolution is Resolution.CALLER_SUPPLIED


def test_the_response_says_the_stage_was_only_claimed():
    """A reader must be able to tell a claim from the case record."""
    assert resolve(claimed="CPA").public() == {
        "stage": "CPA", "stage_resolution": "CALLER_SUPPLIED"}


def test_an_unresolved_stage_publishes_no_stage_at_all():
    assert resolve().public() == {"stage_resolution": "UNRESOLVED"}


def test_a_nonsense_claimed_stage_is_not_a_stage():
    assert resolve(claimed="UNDERWRITING").stage is None


def test_an_unreachable_store_resolves_to_unresolved_rather_than_raising():
    with patch("app.store.get_repository", side_effect=RuntimeError("down")):
        context = stages.resolve("CASE-1")

    assert context.stage is None


def test_the_question_never_decides_the_stage():
    """
    `resolve` takes a case and an advisory claim. It has no access to
    the message, so a CPA-sounding question cannot put a case in CPA.
    """
    import inspect

    assert set(inspect.signature(stages.resolve).parameters) == {
        "case_id", "claimed"}


# ==========================================================================
# D-E. THE REGISTRY REFLECTS REALITY
# ==========================================================================


def test_every_stage_is_registered():
    assert set(stage_registry.REGISTRY) == set(LosStage)


def test_fos_is_the_one_stage_that_can_read_a_case():
    fos = stage_registry.capabilities_for(LosStage.FOS)

    assert fos.supported
    assert fos.knowledge_corpus == "FOS"
    assert fos.answers_knowledge()
    assert fos.answers_downstream()
    assert "case_history" in fos.capabilities


def test_fos_reaches_the_live_mcp_registry_rather_than_a_copy():
    """A second list of tool names would drift from the contracts."""
    from app.mcp.contracts import CONTRACTS

    assert stage_registry.mcp_tools(LosStage.FOS) == tuple(sorted(CONTRACTS))


@pytest.mark.parametrize("stage", UNBUILT)
def test_a_guide_only_stage_claims_a_corpus_and_nothing_else(stage):
    """
    CHANGED WHEN THE GUIDES WERE INDEXED. These six used to claim
    nothing at all. B4 indexed a demonstration stage guide for every
    stage, so each can now answer "how does this stage work" -- and
    still cannot read a case at that stage.

    Registering the corpus without the capabilities is the honest
    description. Leaving the corpus unregistered would have made the
    Copilot refuse a question it could answer.
    """
    registered = stage_registry.capabilities_for(stage)

    assert registered.knowledge_corpus == stage.value
    assert registered.answers_knowledge()

    # No case capability, no MCP: nothing reads a case at this stage.
    assert registered.capabilities == frozenset()
    assert not registered.answers_downstream()
    assert stage_registry.mcp_tools(stage) == ()


def test_rcu_has_no_case_capability_despite_having_a_finding_kind():
    """
    `FindingKind.RCU` exists in case memory and nothing produces those
    findings. An enum member is not a capability -- and a stage guide
    is not one either: RCU can explain itself and cannot read a case.
    """
    from app.store.models import FindingKind

    registered = stage_registry.capabilities_for(LosStage.RCU)

    assert FindingKind.RCU.value == "RCU"
    assert registered.capabilities == frozenset()
    assert not registered.answers_downstream()
    assert "no RCU case capability" in registered.note


@pytest.mark.parametrize("stage", UNBUILT)
def test_an_unbuilt_stage_has_a_recorded_reason(stage):
    assert stage_registry.capabilities_for(stage).note


def test_an_unresolved_stage_supports_nothing():
    assert not stage_registry.supports(None)
    assert stage_registry.mcp_tools(None) == ()


def test_the_unavailable_answer_names_the_stage():
    published = stage_registry.unavailable(LosStage.BOPS)

    assert published["status"] == "CAPABILITY_UNAVAILABLE"
    assert published["stage"] == "BOPS"
    assert published["message"] == (
        "No BOPS Copilot capability is currently registered for this request.")


def test_the_unavailable_answer_for_an_unresolved_stage_says_so():
    published = stage_registry.unavailable(None)

    assert published["status"] == "CAPABILITY_UNAVAILABLE"
    assert "stage" not in published
    assert "could not be determined" in published["message"]


def test_the_registry_does_not_branch_on_stage_names():
    """
    A dict, not a chain of `if stage is ...`. The seventh stage must
    cost what the second did.
    """
    import inspect

    source = inspect.getsource(stage_registry.capabilities_for)

    for stage in LosStage:
        assert stage.value not in source


# ==========================================================================
# THE FOS BOUNDARY IS UNTOUCHED
# ==========================================================================


def test_fos_retrieval_is_still_pinned_to_its_own_corpus():
    """
    `knowledge_answer.STAGE` is not read from the registry and was not
    widened. FOS retrieval behaves exactly as it did.
    """
    from app.agents.applicant import knowledge_answer

    assert knowledge_answer.STAGE == "FOS"


def test_the_registry_does_not_route_retrieval():
    """
    It records what exists; it does not choose a corpus at runtime.
    Nothing in knowledge retrieval imports it.
    """
    import inspect

    from app.agents.applicant import knowledge_answer

    source = inspect.getsource(knowledge_answer)

    assert "stage_registry" not in source
    assert "LosStage" not in source
