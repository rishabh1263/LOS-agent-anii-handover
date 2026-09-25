"""
ONE Universal Copilot across the whole LOS lifecycle.

The same route, the same agent and the same questions, against cases at
FOS, CPA, CREDIT, RCU, BOPS, HOPS and DISBURSEMENT. What changes with the
stage is what the case's own records and the stage-aware policy say --
never the question's wording, and never a model.

Fixtures are the real demo seed (one case per stage, through the same
timeline the pipeline writes) and cases built through the real ingest path.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.agents.los import stage_registry, stages
from app.agents.policy import engine
from app.agents.verification import taxonomy
from app.store import set_repository
from app.store.models import CaseEvent
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"
ALL_STAGES = ("FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT")
_CODE = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")
_ANY_ID = re.compile(r"case_[0-9a-f]{8,}|APP-[A-Z0-9]{6,}|DEMO-(CASE|APP)-\d+")


# ==========================================================================
# FIXTURES
# ==========================================================================


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "stages.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture(autouse=True)
def memory_on_llm_off(monkeypatch):
    from app.agents.applicant import config as agent_config
    from app.agents.los import config as los_config

    monkeypatch.setenv("LOS_CASE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    los_config.reload()
    agent_config.reload()
    yield
    monkeypatch.undo()
    los_config.reload()
    agent_config.reload()


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


@pytest.fixture
def demo(repo):
    from app.store import demo_seed

    demo_seed.seed(repo, force=True)
    return {c["stage"]: (c["case_id"], c["applicant_id"])
            for c in reversed(demo_seed._CASES)}   # first case per stage wins


def ask(client, message, case_id, applicant_id, **extra) -> dict:
    response = client.post(COPILOT, json={"applicant_id": applicant_id,
                                          "case_id": case_id,
                                          "message": message, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def clean(answer: str) -> None:
    assert not _ANY_ID.search(answer), answer
    assert not _CODE.search(answer), answer


CASE, APP = "case_00112233445566778899aabbccddeeff", "APP-00AA11BB22CC"


def fos_case(*, employment=None, product=None, documents=("PAN", "DRIVING_LICENCE")):
    """A FOS case with the given documents verified, via the real ingest."""
    from app.store.ingest import persist_los_result
    from app.store.models import Application

    persist_los_result({
        "request_id": "r-stage", "applicant_id": APP, "case_id": CASE,
        "status": "SUCCESS", "decision": "PASS", "next_action": "PROCEED",
        "documents": [{"source_id": f"{d.lower()}.jpg", "type": d,
                       "party_id": APP, "verification": "PASS",
                       "reason_codes": []} for d in documents],
    })
    from app.store import get_repository

    repository = get_repository()
    application = repository.get_application(CASE)
    if employment or product:
        repository.save_application(Application(
            **{**application.__dict__,
               "employment_type": employment or application.employment_type,
               "product": product or application.product}))


def enter(repo, stage, sequence):
    repo.record_event(CaseEvent(event_id=f"{CASE}-{stage}", case_id=CASE,
                                event_type="STAGE_ENTERED", stage=stage,
                                sequence=sequence))


# ==========================================================================
# A. THE STAGE RESOLVER -- every stage, from the case record
# ==========================================================================

@pytest.mark.parametrize("stage", ALL_STAGES)
def test_the_resolver_reads_every_stage_from_the_case(demo, stage):
    case_id, _ = demo[stage]

    context = stages.resolve(case_id)

    assert context.stage.value == stage
    assert context.resolution is stages.Resolution.CASE_TIMELINE
    assert context.source == "CASE_STATE"
    assert context.status == "IN_PROGRESS"
    assert context.history[-1]["stage"] == stage
    assert context.history[-1]["ended_at"] is None
    assert context.since == context.history[-1]["started_at"]
    # History is in lifecycle order and closes each earlier visit.
    for earlier, later in zip(context.history, context.history[1:]):
        assert earlier["ended_at"] == later["started_at"]


def test_a_claimed_stage_never_overrides_the_record(demo):
    case_id, _ = demo["CREDIT"]

    assert stages.resolve(case_id, claimed="FOS").stage.value == "CREDIT"


def test_a_fos_case_ready_for_cpa_says_so_in_its_stage_status(repo):
    from app.store.models import Applicant, Application, ApplicationStatus

    repo.save_applicant(Applicant(applicant_id=APP))
    repo.save_application(Application(case_id=CASE, applicant_id=APP,
                                      status=ApplicationStatus.READY_FOR_CPA))

    context = stages.resolve(CASE)

    assert context.stage.value == "FOS"
    assert context.resolution is stages.Resolution.APPLICATION_STATUS
    assert context.status == "READY_FOR_HANDOFF"


# ==========================================================================
# B. THE SAME QUESTIONS, EVERY STAGE -- the stage changes the answer
# ==========================================================================

@pytest.mark.parametrize("stage", ALL_STAGES)
def test_every_stage_answers_the_same_questions_as_itself(client, demo, stage):
    from app.agents.applicant import config

    case_id, applicant_id = demo[stage]
    label = config.stage_label(stage)

    status = ask(client, "What is my application status?", case_id, applicant_id)
    pending = ask(client, "What is pending?", case_id, applicant_id)
    todo = ask(client, "What do I need to do?", case_id, applicant_id)
    required = ask(client, "What documents are required?", case_id, applicant_id)

    for body in (status, pending, todo, required):
        assert body["stage"] == stage
        assert body["stage_source"] == "CASE_STATE"
        assert body["category"] == "CASE_ONLY"
        assert body["tool_invoked"]
        clean(body["answer"])
    if stage == "FOS":
        assert status["answer"].startswith("Your application is currently under ")
    else:
        assert status["answer"].startswith(
            f"Your application is currently at the {label} stage")
        assert "not currently available" in status["answer"]
        # Never described with the FOS vocabulary.
        assert "FOS" not in status["answer"]
    assert todo["intent"] == "NEXT_ACTION"
    assert required["intent"] == "DOCUMENTS_REQUIRED"


def test_later_stages_ask_for_more_than_fos(client, demo):
    fos = ask(client, "What documents are required?", *demo["FOS"])["answer"]
    rcu = ask(client, "What documents are required?", *demo["RCU"])["answer"]

    assert "Signature Verification" not in fos
    assert "Signature Verification" in rcu
    assert "Income Proof" in rcu


def test_a_stage_without_the_capability_says_so(client, demo):
    """CPA-handoff readiness is a FOS question; asked at CREDIT it is not
    answered from FOS, and it is not refused as unauthorised."""
    body = ask(client, "is it ready for CPA?", *demo["CREDIT"])

    assert body["status"] == "CAPABILITY_UNAVAILABLE"
    assert body["answer"].startswith("Your application is currently at the "
                                     "Credit stage.")
    assert body["grounded"] is False

    fos = ask(client, "is it ready for CPA?", *demo["FOS"])
    assert fos["status"] is None and fos["answer"].startswith("Not ready for CPA")


# ==========================================================================
# C. THE STAGE IS NEVER THE MODEL'S
# ==========================================================================

def test_a_composed_answer_naming_another_stage_is_rejected(client, demo,
                                                             monkeypatch):
    from app.api.routes import copilot_api
    from app.knowledge import grounding
    from app.knowledge.grounding import GroundedContext

    class _Confident(GroundedContext):
        @property
        def grounded(self):
            return True

    seen: dict = {}

    async def wrong_stage(question, facts, *_a, **_k):
        seen.update(facts)
        return "Your application is at the RCU stage and awaits sign-off."

    monkeypatch.setattr(copilot_api.grounding, "gather",
                        lambda *a, **k: _Confident())
    monkeypatch.setattr(grounding, "_generate", wrong_stage)

    body = ask(client, "What is my application status?", *demo["CREDIT"])

    assert seen["current_stage"] == "Credit"        # the stage is GIVEN
    assert "RCU" not in body["answer"]
    assert body["answer"].startswith("Your application is currently at the "
                                     "Credit stage")
    assert body["response_source"] == "STRUCTURED"


def test_the_validator_rejects_a_stage_not_on_record():
    from app.agents.applicant.validate import check_composed

    structured = "Your application is currently at the Credit stage."
    ok, reason = check_composed("Your case is now in CPA.",
                                structured=structured, stage="CREDIT")
    assert not ok and "stage not on record" in reason
    assert check_composed("Your case is at the Credit stage.",
                          structured=structured, stage="CREDIT")[0]


def test_the_question_never_sets_the_stage(client, demo):
    """Asking about RCU on a CREDIT case describes RCU in general; the
    case is still at CREDIT."""
    body = ask(client, "What does RCU check?", *demo["CREDIT"])

    assert body["stage"] == "CREDIT"


# ==========================================================================
# D. A STAGE TRANSITION -- the next question uses the new stage
# ==========================================================================

def test_fos_to_cpa_changes_what_is_pending(client, repo):
    fos_case()
    enter(repo, "FOS", 1)

    before = ask(client, "What is pending now?", CASE, APP)
    assert before["stage"] == "FOS"
    assert "Income Proof" not in before["answer"]

    enter(repo, "CPA", 2)

    after = ask(client, "What is pending now?", CASE, APP)
    assert after["stage"] == "CPA"
    assert "Income Proof" in after["answer"]


def test_a_follow_up_uses_the_current_stage_not_the_conversation(client, repo):
    fos_case()
    enter(repo, "FOS", 1)
    first = ask(client, "What is my application status?", CASE, APP)

    enter(repo, "CPA", 2)
    second = ask(client, "what now?", CASE, APP, context=first["context"])

    assert second["stage"] == "CPA"
    assert second["followed_up"] is not None


def test_a_hold_from_an_earlier_stage_is_not_this_stages(client, demo):
    """DEMO-CASE-004: REVIEW recorded at FOS, then CPA, then CREDIT."""
    status = ask(client, "What is my application status?", *demo["CREDIT"])

    assert "under review" not in status["answer"]
    # The history is still there for whoever asks why.
    why = ask(client, "why is my application under review?", *demo["CREDIT"])
    assert why["answer"].startswith("Your application is under review because")


# ==========================================================================
# E. THE TAXONOMY -- what COULD evidence a section
# ==========================================================================

def test_the_taxonomy_has_the_lenders_six_sections():
    sections = taxonomy.sections()

    assert set(sections) == {"AGE_PROOF", "SIGNATURE_VERIFICATION",
                             "IDENTITY_PROOF", "INCOME_PROOF",
                             "PROPERTY_OWNERSHIP_PROOF",
                             "BUSINESS_PHOTOGRAPHS"}
    assert sections["INCOME_PROOF"] == ("ITR", "BANK_STATEMENT", "SALARY_SLIP")
    assert sections["BUSINESS_PHOTOGRAPHS"] == ("BUSINESS_PROOF_1",
                                                "BUSINESS_PROOF_2")


def test_a_generic_category_is_never_an_uploadable_document():
    accepted = taxonomy.accepted_for("Identity Proof")

    assert "PAN" in accepted and "AADHAAR" in accepted
    assert "GOVT_ISSUED_DOCUMENT" not in accepted
    assert "ADDRESS_PROOF" not in accepted


def test_the_taxonomy_is_not_the_fos_checklist():
    """Salary slip, ITR, sale deed and business proofs are in the taxonomy
    and in NO FOS checklist for a case that does not need them."""
    fos = {r.slot for r in engine.resolve(None, stage="FOS").requirements}

    assert fos == {"PAN", "ADDRESS_PROOF"}


# ==========================================================================
# F + G. STAGE-AWARE REQUIREMENTS, AND ONE_OF
# ==========================================================================

@pytest.mark.parametrize("product,employment,stage,expected", [
    (None, None, "FOS", {"PAN", "ADDRESS_PROOF"}),
    (None, None, "CPA", {"PAN", "ADDRESS_PROOF", "INCOME_PROOF"}),
    ("HOME_LOAN", None, "CPA", {"PROPERTY_OWNERSHIP_PROOF", "INCOME_PROOF"}),
    (None, "SELF_EMPLOYED", "CPA", {"BUSINESS_PROOF_1", "BUSINESS_PROOF_2"}),
    (None, None, "CREDIT", {"INCOME_PROOF", "BANK_STATEMENT"}),
    (None, None, "RCU", {"SIGNATURE_VERIFICATION"}),
])
def test_requirements_follow_stage_product_and_profile(product, employment,
                                                       stage, expected):
    resolution = engine.resolve(
        product, stage=stage,
        attributes={"employment_type": employment} if employment else {})

    assert expected <= {r.slot for r in resolution.requirements}


def test_a_business_case_and_a_salaried_case_differ():
    salaried = engine.resolve(None, stage="CPA",
                              attributes={"employment_type": "SALARIED"})
    business = engine.resolve(None, stage="CPA",
                              attributes={"employment_type": "SELF_EMPLOYED"})

    assert "BUSINESS_PROOF_1" not in {r.slot for r in salaried.requirements}
    assert "BUSINESS_PROOF_1" in {r.slot for r in business.requirements}
    income = {r.slot: r.accepts for r in business.requirements}["INCOME_PROOF"]
    assert income == ("ITR",)          # narrowed by the self-employed rule


def test_an_uncaptured_attribute_is_reported_not_guessed():
    resolution = engine.resolve(None, stage="CPA")

    assert "BUSINESS_PROOF_1" not in {r.slot for r in resolution.requirements}
    assert "CPA_BUSINESS" in {u.rule_id for u in resolution.unevaluated_rules}


def test_one_of_is_one_slot_and_any_alternative_satisfies_it(client, repo):
    fos_case(documents=("PAN", "DRIVING_LICENCE", "ITR"))
    enter(repo, "FOS", 1)
    enter(repo, "CPA", 2)

    pending = ask(client, "What is pending?", CASE, APP)["answer"]
    required = ask(client, "What documents are required?", CASE, APP)["answer"]

    # ITR alone satisfies Income Proof: no salary slip is asked for.
    assert "Income Proof" not in pending
    assert "Salary Slip" not in pending
    assert "Income Proof — VERIFIED" in required


def test_the_stage_that_added_a_requirement_is_recorded():
    resolution = engine.resolve(None, stage="RCU")
    by_slot = {r.slot: r.stage for r in resolution.requirements}

    assert by_slot["PAN"] is None                  # the product policy's
    assert by_slot["INCOME_PROOF"] == "CPA"
    assert by_slot["SIGNATURE_VERIFICATION"] == "RCU"
    assert resolution.provenance()["stage"] == "RCU"


def test_an_unknown_stage_adds_nothing():
    assert ({r.slot for r in engine.resolve(None, stage="UNDERWRITING").requirements}
            == {"PAN", "ADDRESS_PROOF"})


# ==========================================================================
# H. REQUIRED vs PENDING vs SUBMITTED vs VERIFIED vs REVIEW vs REJECTED
# ==========================================================================

def _mixed_documents():
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-docs", "applicant_id": APP, "case_id": CASE,
        "status": "PARTIAL", "decision": "REVIEW", "next_action": "MANUAL_REVIEW",
        "documents": [
            {"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
             "verification": "PASS", "reason_codes": []},
            {"source_id": "dl.jpg", "type": "DRIVING_LICENCE", "party_id": APP,
             "verification": "REVIEW", "reason_codes": ["REQUIRED_FIELD_MISSING"]},
            {"source_id": "bank.pdf", "type": "BANK_STATEMENT", "party_id": APP,
             "verification": "FAIL", "reason_codes": ["DOCUMENT_TYPE_MISMATCH"]},
        ],
    })


@pytest.mark.parametrize("question,intent,has,lacks", [
    ("What documents are required?", "DOCUMENTS_REQUIRED",
     ["PAN", "Address Proof"], []),
    ("What documents have I submitted?", "DOCUMENTS_UPLOADED",
     ["PAN", "Driving Licence", "Bank Statement"], []),
    ("What is the status of my documents?", "DOCUMENTS_UPLOADED",
     ["PAN", "Driving Licence"], []),
    ("Which documents are verified?", "DOCUMENTS_UPLOADED",
     ["PAN"], ["Driving Licence", "Bank Statement"]),
    ("Which documents are under review?", "DOCUMENTS_PENDING",
     ["Driving Licence"], ["PAN", "Bank Statement"]),
    ("Which documents were rejected?", "DOCUMENTS_UPLOADED",
     ["Bank Statement"], ["PAN", "Driving Licence"]),
    ("which docs r verified", "DOCUMENTS_UPLOADED", ["PAN"], ["Bank Statement"]),
])
def test_each_document_question_gets_its_own_answer(client, repo, question,
                                                    intent, has, lacks):
    _mixed_documents()

    body = ask(client, question, CASE, APP)

    assert body["intent"] == intent, body
    for word in has:
        assert word in body["answer"], body["answer"]
    for word in lacks:
        assert word not in body["answer"], body["answer"]


def test_a_rejected_document_question_is_not_a_lending_decision(client, repo):
    _mixed_documents()

    body = ask(client, "Which documents were rejected?", CASE, APP)

    assert body["category"] == "CASE_ONLY"
    assert body["intent"] == "DOCUMENTS_UPLOADED"
    assert body["response_source"] == "STRUCTURED"   # not ROUTED
    assert body["answer"] == "Bank Statement is rejected."


# ==========================================================================
# I + J. THE TAXONOMY'S NAMES, SHORT FORMS AND TYPOS
# ==========================================================================

@pytest.mark.parametrize("question,document", [
    ("sal slip status", "SALARY_SLIP"),
    ("is my ITR verified?", "ITR"),
    ("is my DL verified", "DRIVING_LICENCE"),
    ("sale deed pending?", "SALE_DEED"),
    ("what is my bank stmt status?", "BANK_STATEMENT"),
    ("is my salry slip uploded", "SALARY_SLIP"),
])
def test_every_taxonomy_document_is_recognised(question, document):
    from app.agents.applicant.intents import understand

    assert understand(question, has_case=True).document_type == document


@pytest.mark.parametrize("question,intent", [
    ("what does this stage mean?", "STAGE_PROCESS"),
    ("what do I need to fix?", "CASE_HISTORY"),
    ("what happened to my application?", "CASE_HISTORY"),
    ("what is pending now?", "PENDING_ITEMS"),
    ("where is my case?", "APPLICATION_STAGE"),
    ("where does my application stand?", "APPLICATION_STATUS"),
    ("what do I need to do?", "NEXT_ACTION"),
])
def test_natural_questions_resolve(question, intent):
    from app.agents.applicant.intents import understand

    assert understand(question, has_case=True).intent.value == intent


def test_this_stage_means_the_cases_stage(client, demo):
    body = ask(client, "what does this stage mean?", *demo["RCU"])

    assert body["intent"] == "STAGE_PROCESS"
    assert body["stage"] == "RCU"


# ==========================================================================
# CONFIGURATION IS WIRED
# ==========================================================================

def test_a_capability_switched_off_in_configuration_is_not_served(monkeypatch):
    from app.agents.applicant import config

    real = config.chatbot

    def narrowed(section):
        if section != "stages":
            return real(section)
        value = dict(real("stages"))
        value["capabilities"] = {"CPA": {"capabilities": ["stage_status"]}}
        return value

    monkeypatch.setattr(config, "chatbot", narrowed)
    registered = stage_registry.capabilities_for(stages.LosStage.CPA)

    assert registered.capabilities == frozenset({"stage_status"})


def test_configuration_cannot_claim_a_capability_nothing_provides(monkeypatch):
    from app.agents.applicant import config

    real = config.chatbot

    def invented(section):
        if section != "stages":
            return real(section)
        value = dict(real("stages"))
        value["capabilities"] = {"RCU": {"capabilities": [
            "stage_status", "rcu_investigation_result"]}}
        return value

    monkeypatch.setattr(config, "chatbot", invented)
    registered = stage_registry.capabilities_for(stages.LosStage.RCU)

    assert "rcu_investigation_result" not in registered.capabilities


def test_a_disabled_stage_serves_nothing(monkeypatch):
    from app.agents.applicant import config

    real = config.chatbot

    def disabled(section):
        if section != "stages":
            return real(section)
        value = dict(real("stages"))
        value["capabilities"] = {"HOPS": {"enabled": False}}
        return value

    monkeypatch.setattr(config, "chatbot", disabled)
    registered = stage_registry.capabilities_for(stages.LosStage.HOPS)

    assert not registered.supported


def test_the_stage_requirements_file_is_what_the_engine_applies(monkeypatch,
                                                                tmp_path):
    from app.agents.policy import loader

    path = tmp_path / "stages.yaml"
    path.write_text("stages:\n  FOS: []\n  CPA:\n    - rule_id: X\n"
                    "      section: AGE_PROOF\n      requirement_type: ONE_OF\n",
                    encoding="utf-8")
    monkeypatch.setenv("LOS_STAGE_REQUIREMENTS_PATH", str(path))
    loader.reload()
    try:
        slots = {r.slot for r in engine.resolve(None, stage="CPA").requirements}
        assert "AGE_PROOF" in slots and "INCOME_PROOF" not in slots
    finally:
        monkeypatch.delenv("LOS_STAGE_REQUIREMENTS_PATH")
        loader.reload()
