"""
The Universal Copilot, end to end: understanding, stage, grounding,
composition, validation and fallback.

Every route test runs the real HTTP route, the real agent, the real MCP
tools and a real SQLite store fed by the real ingest path. The model and
retrieval are replaced only where a test is ABOUT them (failure,
validation, JEV); everywhere else they are simply unavailable, which is
the condition the deterministic path must already handle.

The seeded case is the one the brief describes: PAN and bank statement both
verified, the names on them different (NAME_MISMATCH, decision REVIEW), and
Address Proof missing from the checklist.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents.applicant import normalize, semantic, status_facts
from app.agents.applicant.intents import Intent, understand
from app.agents.applicant.validate import check_composed, required_facts
from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

COPILOT = "/api/v1/copilot/query"
CASE = "case_0123456789abcdef0123456789abcdef"
APP = "APP-ABCDEF123456"
PAN_NAME = "RISHABH AJIT SINGH"
BANK_NAME = "PRIYANKAROHANMORE"

#: Every tool a case answer may name. A tool_invoked entry outside this
#: set would be a fabricated tool.
KNOWN_TOOLS = {"application.get", "applicant.get", "applicant.360",
               "applications.list", "documents.get", "documents.checklist",
               "documents.verification", "workflow.pending_items",
               "workflow.next_action", "workflow.readiness"}

_CODE = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")
_ANY_ID = re.compile(r"case_[0-9a-f]{8,}|APP-[A-Z0-9]{6,}|DEMO-(CASE|APP)-\d+",
                     re.IGNORECASE)


# ==========================================================================
# FIXTURES
# ==========================================================================


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "universal.sqlite3")
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
    monkeypatch.delenv("JEV_ENABLED", raising=False)
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


def mismatch_case(decision="REVIEW"):
    """PAN vs bank statement name mismatch; Address Proof missing."""
    from app.store.ingest import persist_los_result

    held = decision != "PASS"
    persist_los_result({
        "request_id": "r-universal", "applicant_id": APP, "case_id": CASE,
        "status": "PARTIAL" if held else "SUCCESS", "decision": decision,
        "reason_codes": ["NAME_MISMATCH"] if held else [],
        "next_action": "MANUAL_REVIEW" if held else "PROCEED",
        "documents": [
            {"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
             "verification": "PASS", "reason_codes": [],
             "extraction": {"name": PAN_NAME, "father_name": "AJIT SINGH",
                            "date_of_birth": "2002-06-12",
                            "pan_number": "NUHPS4875K"}},
            {"source_id": "bank.pdf", "type": "BANK_STATEMENT",
             "party_id": APP, "verification": "PASS", "reason_codes": [],
             "extraction": {"account_number_masked": "XXXX1015"}},
        ],
        **({"kyc": {
            "status": "REVIEW", "overall_score": 10, "overall_confidence": 90,
            "reason_codes": ["NAME_MISMATCH"],
            "fields": [{"field": "NAME", "status": "FAIL", "match_score": 10,
                        "confidence": 90, "reason_code": "NAME_MISMATCH",
                        "sources": [
                            {"source_id": "pan.jpg", "document_type": "PAN",
                             "value": PAN_NAME},
                            {"source_id": "bank.pdf",
                             "document_type": "BANK_STATEMENT",
                             "value": BANK_NAME}]}]}} if held else {}),
    })


def ask(client, message, *, case_id=CASE, applicant_id=APP, **extra) -> dict:
    response = client.post(COPILOT, json={"applicant_id": applicant_id,
                                          "case_id": case_id,
                                          "message": message, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def assert_user_facing(body: dict) -> None:
    """No internal id, no internal code, no internal vocabulary."""
    answer = body["answer"]
    assert not _ANY_ID.search(answer), answer
    assert not _CODE.search(answer), answer
    for word in ("PARTIAL", "application.get", "documents.checklist", "MCP"):
        assert word not in answer, answer
    assert set(body["tool_invoked"]) <= KNOWN_TOOLS, body["tool_invoked"]


# ==========================================================================
# A + B. CASE QUESTIONS, INCLUDING TYPOS AND SHORT FORMS
# ==========================================================================

CASE_QUESTIONS = [
    ("What is my application status?", "APPLICATION_STATUS"),
    ("where does my application stand?", "APPLICATION_STATUS"),
    ("what stage am I in?", "APPLICATION_STAGE"),
    ("why is my application under review?", "CASE_HISTORY"),
    ("what exactly is wrong?", "CASE_HISTORY"),
    ("what documents are pending?", "DOCUMENTS_PENDING"),
    ("which doc is pending?", "DOCUMENTS_PENDING"),
    ("what do I need to upload?", "DOCUMENTS_MISSING"),
    ("is my PAN verified?", "DOCUMENT_VERIFICATION"),
    ("is my bank stmt verified?", "DOCUMENT_VERIFICATION"),
    ("what is my next action?", "NEXT_ACTION"),
    # B -- typos and short forms
    ("what docs r pending", "DOCUMENTS_PENDING"),
    ("why my app is review", "CASE_HISTORY"),
    ("what's wrong with my doc", "CASE_HISTORY"),
    ("bank stmt status", "DOCUMENT_VERIFICATION"),
    ("addr proof pending?", "DOCUMENTS_PENDING"),
    ("app status", "APPLICATION_STATUS"),
    ("mera application ka status kya hai", "APPLICATION_STATUS"),
    ("documnets pending?", "DOCUMENTS_PENDING"),
    ("is my pan verifed", "DOCUMENT_VERIFICATION"),
]


@pytest.mark.parametrize("question,intent", CASE_QUESTIONS)
def test_a_case_question_is_answered_from_the_case(client, repo, question,
                                                   intent):
    mismatch_case()

    body = ask(client, question)

    assert body["category"] == "CASE_ONLY", body
    assert body["intent"] == intent, body
    assert body["response_source"] == "STRUCTURED"
    assert body["grounded"] is True
    assert body["tool_invoked"], "a case answer ran no tool"
    assert body["stage"] == "FOS"
    assert_user_facing(body)


@pytest.mark.parametrize("question", [
    "What is my application status?", "where does my application stand?",
    "app status"])
def test_the_status_states_the_hold_and_its_concrete_reason(client, repo,
                                                            question):
    mismatch_case()

    answer = ask(client, question)["answer"]

    assert "under review because" in answer
    assert PAN_NAME in answer and BANK_NAME in answer


@pytest.mark.parametrize("question", [
    "why is my application under review?", "what exactly is wrong?",
    "why my app is review", "what's wrong with my doc"])
def test_why_names_the_problem_and_the_evidence(client, repo, question):
    mismatch_case()

    answer = ask(client, question)["answer"]

    assert answer.startswith("Your application is under review because")
    assert PAN_NAME in answer and BANK_NAME in answer
    assert "document issue" not in answer


@pytest.mark.parametrize("question", [
    "what documents are pending?", "which doc is pending?",
    "what docs r pending", "addr proof pending?", "what do I need to upload?"])
def test_pending_questions_name_the_pending_document(client, repo, question):
    mismatch_case()

    assert "Address Proof" in ask(client, question)["answer"]


def test_a_named_document_is_the_one_answered(client, repo):
    mismatch_case()

    pan = ask(client, "is my PAN verified?")["answer"]
    bank = ask(client, "bank stmt status")["answer"]

    assert pan.startswith("PAN ")
    assert bank.startswith("Bank Statement ")


def test_the_next_action_is_stated_when_asked(client, repo):
    mismatch_case()

    answer = ask(client, "what is my next action?")["answer"]

    # Phase 3: said as a person would, the recorded action verbatim after it.
    assert answer.startswith("Your next step is to ")


# ==========================================================================
# C. KNOWLEDGE -- no case tool, no case fact
# ==========================================================================

@pytest.mark.parametrize("question,category", [
    ("What is FOIR?", "KNOWLEDGE_ONLY"),
    ("What is LTV?", "KNOWLEDGE_ONLY"),
    ("What documents are required for a personal loan?", "KNOWLEDGE_ONLY"),
    ("What is RCU?", "PROCESS_KNOWLEDGE"),
    ("What is an application status?", "KNOWLEDGE_ONLY"),
    ("What are application statuses?", "KNOWLEDGE_ONLY"),
])
def test_a_knowledge_question_reads_no_case(client, repo, question, category):
    mismatch_case()

    body = ask(client, question)

    assert body["category"] == category, body
    assert body["tool_invoked"] == []
    assert PAN_NAME not in body["answer"]
    assert "under review" not in body["answer"]
    assert body["status"] is None


# ==========================================================================
# D. MIXED -- both halves, each labelled for what it is
# ==========================================================================

def test_mixed_why_and_what_to_upload_answers_both_from_the_case(client, repo):
    mismatch_case()

    answer = ask(client, "What is wrong with my application and what "
                         "should I upload?")["answer"]

    first = answer.split("\n\n")[0]
    assert first.startswith("Your application is under review because")
    assert PAN_NAME in first
    assert "Address Proof is also pending; please upload it to continue." in first
    assert not _CODE.search(answer), answer
    assert "|" not in answer


def test_mixed_with_policy_keeps_case_facts_and_policy_apart(client, repo):
    mismatch_case()

    body = ask(client, "Why is my application under review and what "
                       "documents are required?")
    halves = body["answer"].split("\n\n")

    assert PAN_NAME in halves[0]
    assert "Address Proof is also pending" in halves[0]
    if body["category"] == "MIXED":
        assert halves[-1].startswith("In general:")
        assert PAN_NAME not in halves[-1]
    assert not _CODE.search(body["answer"]), body["answer"]


def test_mixed_policy_question_keeps_the_case_reason(client, repo):
    mismatch_case()

    answer = ask(client, "Why is my case pending and what does the policy "
                         "say?")["answer"]

    assert PAN_NAME in answer.split("\n\n")[0]


# ==========================================================================
# E. STAGE -- read from the case, never assumed to be FOS
# ==========================================================================

@pytest.fixture
def demo(repo):
    from app.store import demo_seed

    demo_seed.seed(repo, force=True)
    return {c["case_id"]: (c["applicant_id"], c["stage"])
            for c in demo_seed._CASES}


def test_every_stage_is_answered_as_that_stage(client, demo):
    from app.agents.applicant import config

    for case_id, (applicant_id, stage) in demo.items():
        status = ask(client, "What is my application status?",
                     case_id=case_id, applicant_id=applicant_id)
        where = ask(client, "what stage am I in?",
                    case_id=case_id, applicant_id=applicant_id)

        assert status["stage"] == stage, (case_id, status)
        assert where["stage"] == stage
        assert_user_facing(status)
        assert_user_facing(where)
        label = config.stage_label(stage)
        if stage == "FOS":
            assert status["answer"].startswith("Your application is currently under ")
            assert where["answer"].startswith("Your application is in the FOS stage")
        else:
            assert status["answer"].startswith(
                f"Your application is currently at the {label} stage.")
            assert "not currently available" in status["answer"]
            assert "FOS" not in status["answer"]
            assert where["answer"] == f"Your application is at the {label} stage."


def test_a_stage_with_case_capability_gets_full_detail():
    assert status_facts.stage_has_case_detail("FOS")
    for stage in ("CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT"):
        assert not status_facts.stage_has_case_detail(stage)
    # An unknown stage is never answered as if it had FOS detail.
    assert not status_facts.stage_has_case_detail("UNDERWRITING")


def test_the_stage_label_comes_from_configuration(monkeypatch):
    from app.agents.applicant import config

    assert config.stage_label("CREDIT") == "Credit"
    monkeypatch.setattr(config, "chatbot",
                        lambda section: {"labels": {"CREDIT": "Credit Desk"}}
                        if section == "stages" else {})
    assert config.stage_label("CREDIT") == "Credit Desk"
    # A stage nobody labelled is still named, from its own name.
    assert config.stage_label("POST_APPROVAL") == "Post Approval"


# ==========================================================================
# F. FOLLOW-UPS -- the context block, round-tripped
# ==========================================================================

def test_a_follow_up_about_the_document_is_understood(client, repo):
    """After "what is pending?", "the document" is the pending one."""
    mismatch_case()

    first = ask(client, "what documents are pending?")
    assert first["context"]["last_slot"] == "ADDRESS_PROOF"

    second = ask(client, "what about the document?", context=first["context"])

    assert second["followed_up"] is not None
    assert second["followed_up"]["original_message"] == "what about the document?"
    assert second["followed_up"]["interpreted_as"] == "Is address proof still pending?"
    assert second["category"] == "CASE_ONLY"
    assert "Address Proof" in second["answer"]


def test_a_follow_up_resolves_to_the_one_implicated_document(client, repo):
    """A review on ONE document: "it" is that document."""
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-one", "applicant_id": APP, "case_id": CASE,
        "status": "PARTIAL", "decision": "REVIEW",
        "reason_codes": ["REQUIRED_FIELD_MISSING"],
        "next_action": "MANUAL_REVIEW",
        "documents": [{"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
                       "verification": "REVIEW",
                       "reason_codes": ["REQUIRED_FIELD_MISSING"]}],
    })

    first = ask(client, "Why is my application under review?")
    second = ask(client, "what about it?", context=first["context"])

    assert first["context"]["last_slot"] == "PAN"
    assert second["followed_up"]["interpreted_as"] == "Has the PAN been verified?"
    assert "PAN" in second["answer"]
    assert second["intent"] == "DOCUMENT_VERIFICATION"


def test_the_document_is_not_guessed_when_two_are_implicated(client, repo):
    """PAN vs bank statement: "the document" is ambiguous, so not resolved."""
    mismatch_case()

    first = ask(client, "Why is my application under review?")
    second = ask(client, "what about the document?", context=first["context"])

    assert first["context"]["last_slot"] is None
    assert second["followed_up"] is None


def test_a_follow_up_never_changes_whose_case_it_is(client, repo):
    mismatch_case()

    forged = {"last_intent": "CASE_HISTORY", "last_slot": "CIBIL_SCORE"}
    body = ask(client, "what about the document?", context=forged)

    # An unrecognised slot is not resolved against; nothing leaks.
    assert body["followed_up"] is None
    assert "CIBIL" not in body["answer"].upper()


def test_what_do_i_need_to_upload_needs_no_context(client, repo):
    mismatch_case()

    body = ask(client, "what do i need to upload?")

    assert body["intent"] == "DOCUMENTS_MISSING"
    assert "Address Proof" in body["answer"]


# ==========================================================================
# G. QWEN FAILURE -- the structured answer, every time
# ==========================================================================

class _Model:
    """A stand-in model client: replies, raises, hangs or returns junk."""

    def __init__(self, *replies, behaviour="reply"):
        self.replies = list(replies)
        self.behaviour = behaviour
        self.calls: list[dict] = []

    async def get_response(self, messages, stream=False, options=None):
        self.calls.append({"messages": messages, "options": options or {}})
        if self.behaviour == "raise":
            raise ConnectionError("model provider is not reachable")
        if self.behaviour == "hang":
            await asyncio.sleep(5)
        if self.behaviour == "junk":
            return SimpleNamespace(text=None)
        return SimpleNamespace(text=self.replies.pop(0) if self.replies else "")


@pytest.fixture
def model_on(monkeypatch):
    from app.agents.applicant import config as agent_config

    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "true")
    agent_config.reload()
    monkeypatch.setattr("app.llm.availability.provider_reachable", lambda: True)
    monkeypatch.setattr("app.llm.availability.mark_slow", lambda *_a, **_k: None)

    def install(model):
        monkeypatch.setattr("app.llm.provider.create_ollama_client",
                            lambda *a, **k: model)
        return model
    return install


def _text(message) -> str:
    return " ".join(getattr(c, "text", None) or str(c)
                    for c in getattr(message, "contents", []) or [])


EXPECTED_PLAIN = ("Your application is currently under Basic Document "
                  "Verification. Address Proof is still pending.")


@pytest.mark.parametrize("behaviour", ["raise", "junk"])
def test_a_failed_model_costs_only_the_phrasing(client, repo, model_on,
                                               behaviour):
    mismatch_case(decision="PASS")
    model = model_on(_Model(behaviour=behaviour))

    body = ask(client, "What is my application status?")

    assert model.calls, "the model was not asked"
    assert body["answer"] == EXPECTED_PLAIN
    assert body["response_source"] == "STRUCTURED"
    assert body["grounded"] is True


def test_a_slow_model_times_out_to_the_structured_answer(client, repo,
                                                         model_on,
                                                         monkeypatch):
    monkeypatch.setenv("APPLICANT_AGENT_LLM_SECONDS", "0.2")
    mismatch_case(decision="PASS")
    model_on(_Model(behaviour="hang"))

    body = ask(client, "What is my application status?")

    assert body["answer"] == EXPECTED_PLAIN
    assert body["response_source"] == "STRUCTURED"


def test_malformed_model_output_is_refused(client, repo, model_on):
    mismatch_case(decision="PASS")
    model_on(_Model('{"answer": "approved"}'))

    body = ask(client, "What is my application status?")

    assert body["answer"] == EXPECTED_PLAIN
    assert body["response_source"] == "STRUCTURED"


def test_a_complete_model_answer_is_published_as_llm(client, repo, model_on):
    mismatch_case(decision="PASS")
    phrased = ("Your application is in Basic Document Verification, and "
               "Address Proof is still pending.")
    model = model_on(_Model(phrased))

    body = ask(client, "What is my application status?")

    assert body["answer"] == phrased
    assert body["response_source"] == "LLM"
    # The configured output budget reached the model.
    from app.agents.applicant import config
    assert model.calls[0]["options"]["max_tokens"] == config.max_output_tokens()


def test_the_model_never_sees_an_identifier(client, repo, model_on):
    mismatch_case(decision="PASS")
    model = model_on(_Model("Address Proof is still pending."))

    ask(client, "What is my application status?")

    sent = " ".join(_text(m) for m in model.calls[0]["messages"])
    assert "Address Proof" in sent or "ADDRESS_PROOF" in sent, sent[:200]
    assert CASE not in sent and APP not in sent


def test_a_recorded_hold_is_never_handed_to_the_model(client, repo, model_on):
    mismatch_case()
    model = model_on(_Model("Your application is fine."))

    body = ask(client, "What is my application status?")

    assert model.calls == []
    assert "under review because" in body["answer"]


# ==========================================================================
# H. RAG FAILURE -- case questions still answered
# ==========================================================================

@pytest.mark.parametrize("failure", ["raise", "empty"])
def test_a_retrieval_outage_does_not_cost_a_case_answer(client, repo,
                                                        monkeypatch, failure):
    from app.api.routes import copilot_api
    from app.knowledge.grounding import GroundedContext

    def broken(*_a, **_k):
        if failure == "raise":
            raise RuntimeError("vector store is down")
        return GroundedContext()

    monkeypatch.setattr(copilot_api.grounding, "gather", broken)
    mismatch_case()

    for question in ("What is my application status?",
                     "why is my application under review?",
                     "what docs are pending?"):
        body = ask(client, question)
        assert body["category"] == "CASE_ONLY"
        assert body["response_source"] == "STRUCTURED"
        assert body["grounded"] is True
        assert body["answer"]


# ==========================================================================
# I. THE VALIDATOR
# ==========================================================================

STRUCTURED = ("Your application is currently under Basic Document "
              "Verification and is under review because the name on the PAN, "
              f"{PAN_NAME}, does not match the bank account holder name, "
              f"{BANK_NAME}. Address Proof is pending.")


def test_the_required_facts_are_read_from_the_structured_answer():
    required = required_facts(STRUCTURED)

    assert required["names"] == {PAN_NAME, BANK_NAME}
    assert required["pending"] == {"address proof"}
    assert required["holds"] == {"under review"}


def test_a_complete_rephrasing_passes():
    ok, text = check_composed(
        f"Your application is under review: the PAN says {PAN_NAME} but the "
        f"bank account holder is {BANK_NAME}. Address Proof is pending.",
        structured=STRUCTURED, identifiers=(CASE, APP))
    assert ok, text


@pytest.mark.parametrize("generated,why", [
    ("Your application is being processed.", "dropped"),
    (f"Your application is under review because {PAN_NAME} differs. "
     "Address Proof is pending.", "dropped a recorded value"),
    (f"Your application is under review: {PAN_NAME} vs {BANK_NAME}.",
     "pending document"),
    (f"Your application is fine: {PAN_NAME} and {BANK_NAME}. Address Proof "
     "is pending.", "hold"),
    (f"Application {CASE} is under review: {PAN_NAME} vs {BANK_NAME}. "
     "Address Proof is pending.", "identifier"),
    (f"Under review (NAME_MISMATCH): {PAN_NAME} vs {BANK_NAME}. Address "
     "Proof is pending.", "internal code"),
    (f"Under review: {PAN_NAME} vs {BANK_NAME} and SURESH KUMAR. Address "
     "Proof is pending.", "not on record"),
])
def test_the_validator_rejects(generated, why):
    ok, reason = check_composed(generated, structured=STRUCTURED,
                                identifiers=(CASE, APP))
    assert not ok
    assert why in reason, reason


def test_the_validator_rejects_an_invented_hold():
    ok, reason = check_composed(
        "Your application is under review.",
        structured="Your application is currently under Document Collection.")
    assert not ok and "not on record" in reason


def test_the_validator_rejects_excessive_length():
    long = ". ".join(["Address Proof is pending"] * 6) + "."
    ok, reason = check_composed(
        long, structured="Address Proof is pending.")
    assert not ok and ("too many sentences" in reason or "too long" in reason)


def test_the_validator_limits_follow_configuration(monkeypatch):
    from app.agents.applicant import config

    text = "Address Proof is pending. Please upload it. Thank you."
    monkeypatch.setattr(config, "max_sentences", lambda: 3)
    assert check_composed(text, structured="Address Proof is pending.")[0]
    monkeypatch.setattr(config, "max_sentences", lambda: 2)
    assert not check_composed(text, structured="Address Proof is pending.")[0]


def test_validation_can_be_switched_off(monkeypatch):
    from app.agents.applicant import config

    monkeypatch.setattr(config, "validation", lambda *_a, **_k: False)
    ok, _ = check_composed("Your application is being processed.",
                           structured=STRUCTURED)
    assert ok


def test_a_rejected_composition_publishes_the_record(client, repo,
                                                     monkeypatch):
    """Through the route: the Copilot composer's output is validated too."""
    from app.api.routes import copilot_api
    from app.knowledge import grounding
    from app.knowledge.grounding import GroundedContext

    class _Confident(GroundedContext):
        @property
        def grounded(self):
            return True

    async def lazy(*_a, **_k):
        return "Your application is currently being processed."

    monkeypatch.setattr(copilot_api.grounding, "gather",
                        lambda *a, **k: _Confident())
    monkeypatch.setattr(grounding, "_generate", lazy)
    mismatch_case()

    body = ask(client, "What is my application status?")

    assert "under review because" in body["answer"]
    assert PAN_NAME in body["answer"]
    assert body["response_source"] == "STRUCTURED"


def test_a_rejected_phrasing_is_never_retried(client, repo, model_on,
                                              monkeypatch):
    # Slice 11 (no retries): even with a retry configured, a rejected
    # phrasing costs ONE model call and falls back to the recorded answer --
    # a second call is a second wait for nothing the record does not say.
    from app.agents.applicant import config

    monkeypatch.setattr(config, "regenerate_attempts", lambda: 1)
    mismatch_case(decision="PASS")
    good = ("Your application is at Basic Document Verification, and "
            "Address Proof is still pending.")
    model = model_on(_Model("Your application is progressing.", good))

    body = ask(client, "What is my application status?")

    assert len(model.calls) == 1
    assert body["response_source"] != "LLM"
    assert body["answer_basis"]["composition"]["outcome"] == "REJECTED"


def test_without_retries_a_rejection_falls_back_at_once(client, repo,
                                                        model_on):
    mismatch_case(decision="PASS")
    model = model_on(_Model("Your application is progressing.",
                            "never asked"))

    body = ask(client, "What is my application status?")

    assert len(model.calls) == 1
    assert body["answer"] == EXPECTED_PLAIN
    assert body["response_source"] == "STRUCTURED"


# ==========================================================================
# JEV -- additive, optional, never authoritative
# ==========================================================================

class _Jev:
    def __init__(self, notes=None, fail=False):
        self.notes, self.fail, self.seen = notes or [], fail, []

    def annotate(self, packet):
        self.seen.append(packet)
        if self.fail:
            raise RuntimeError("jev down")
        return self.notes


@pytest.fixture
def jev_provider():
    from app.agents.applicant import jev

    yield jev.register
    jev.register(None)


def test_jev_off_is_a_no_op(jev_provider):
    from app.agents.applicant import jev

    provider = _Jev(["note"])
    jev_provider(provider)

    assert jev.annotate({"question": "x"}) == []
    assert provider.seen == []


def test_jev_notes_reach_the_composer_labelled_as_annotations(
        client, repo, monkeypatch, jev_provider):
    from app.api.routes import copilot_api
    from app.knowledge import grounding
    from app.knowledge.grounding import GroundedContext

    class _Confident(GroundedContext):
        @property
        def grounded(self):
            return True

    seen: dict = {}

    async def generate(question, facts, *_a, **_k):
        seen.update(facts)
        return None

    monkeypatch.setenv("JEV_ENABLED", "true")
    jev_provider(_Jev(["the two names share no token"]))
    monkeypatch.setattr(copilot_api.grounding, "gather",
                        lambda *a, **k: _Confident())
    monkeypatch.setattr(grounding, "_generate", generate)
    # A question a composer may phrase: the case summary quotes no recorded
    # hold or names (Slice 11 never sends those to a model) -- with case
    # composition switched ON, which the policy now respects on every path.
    from app.agents.applicant import config as agent_config

    monkeypatch.setattr(agent_config, "compose_case_answers", lambda: True)
    mismatch_case()
    expected = ask(client, "Give me a complete summary of this case")["answer"]
    seen.clear()

    body = ask(client, "Give me a complete summary of this case")

    assert seen["annotations_not_authoritative"] == ["the two names share no token"]
    assert seen["current_stage"] == "FOS"
    # The notes changed nothing the records established.
    assert body["answer"] == expected


def test_a_failing_jev_changes_nothing(client, repo, monkeypatch,
                                       jev_provider):
    monkeypatch.setenv("JEV_ENABLED", "true")
    jev_provider(_Jev(fail=True))
    mismatch_case()

    body = ask(client, "What is my application status?")

    assert "under review because" in body["answer"]
    assert body["response_source"] == "STRUCTURED"


# ==========================================================================
# UNDERSTANDING -- normalisation and the semantic fallback
# ==========================================================================

@pytest.mark.parametrize("typed,normalised", [
    ("what docs r pending", "what documents are pending"),
    ("bank stmt status", "bank statement status"),
    ("addr proof pending?", "address proof pending?"),
    ("why my app is review", "why my application is review"),
    ("documnets pending?", "documents pending?"),
    ("is my pan verifed", "is my pan verified"),
    ("sal slip uploaded?", "salary slip uploaded?"),
])
def test_short_forms_and_typos_become_full_words(typed, normalised):
    assert normalize.normalise(typed).text == normalised


@pytest.mark.parametrize("left_alone", [
    "What is the name on my PAN?",
    "Is the name RISHABH AJIT SINGH on my PAN?",
    "What are application statuses?",
    "approved",
])
def test_names_acronyms_and_inflections_are_left_alone(left_alone):
    assert normalize.normalise(left_alone).text == left_alone


def test_the_semantic_fallback_needs_a_case_in_hand():
    with_case = understand("how far along is my loan", has_case=True)
    without = understand("how far along is my loan", has_case=False)

    assert with_case.intent is Intent.APPLICATION_STATUS
    assert with_case.matched_on.startswith("semantic:")
    assert without.intent is Intent.UNKNOWN


def test_the_semantic_fallback_never_takes_a_definition():
    assert understand("What are the different application statuses?",
                      has_case=True).intent is Intent.FOS_KNOWLEDGE


def test_intents_are_extended_by_configuration(monkeypatch):
    from app.agents.applicant import config

    real = config.chatbot

    def extended(section):
        if section != "intents":
            return real(section)
        return {"min_score": 0.5, "examples": {
            "NEXT_ACTION": ["what is the way forward on my file"]}}

    monkeypatch.setattr(config, "chatbot", extended)
    semantic.reload()
    try:
        assert understand("way forward on my file?",
                          has_case=True).intent is Intent.NEXT_ACTION
    finally:
        semantic.reload()


async def test_a_write_is_never_normalised(repo, monkeypatch):
    """
    Normalisation rewrites short words ("kya" -> "what"), so a write --
    whose fields reach the store -- is classified on the words as typed
    and never passes through it.
    """
    from app.agents.applicant import agent, config

    assert normalize.normalise("update mobile to 9876543210 kya").changed

    def forbidden(*_a, **_k):
        raise AssertionError("a write was normalised")

    monkeypatch.setattr(agent, "understand", forbidden)
    # The officer's own applicant: a write is proposed only for one they hold.
    from app.store.models import Applicant
    repo.save_applicant(Applicant(applicant_id=APP))
    repo.grant_access("fos", "APPLICANT", APP)
    scopes = " ".join(set(config.read_scopes().values())
                      | set(config.write_scopes().values()))
    response = await agent.answer_question(
        message="update the applicant mobile number to 9876543210",
        applicant_id=APP, case_id=None,
        claims={"sub": "fos", "scope": scopes},
    )
    assert response["intent"] == "UPDATE_APPLICANT"


# ==========================================================================
# CONFIGURATION IS WIRED, NOT DECORATIVE
# ==========================================================================

def test_every_chatbot_setting_is_read_at_runtime():
    from app.agents.applicant import config

    snapshot = config.snapshot()
    for key in ("llm_max_output_tokens", "response_max_sentences",
                "response_max_characters", "validation_enabled",
                "regenerate_attempts", "jev_enabled"):
        assert key in snapshot
    assert config.max_output_tokens() == 180
    # Phase 3 default: at most two sentences.
    assert config.max_sentences() == 2
    assert config.max_characters() == 500
    assert config.temperature() == 0.1
    assert config.fallback("llm_failure") == "structured_answer"


# ==========================================================================
# LIVE-FOUND DEFECTS
# ==========================================================================

def _one_document_case():
    """The live case_9d6e... shape: one PAN, KYC had nothing to compare."""
    from app.store.ingest import persist_los_result

    persist_los_result({
        "request_id": "r-one-doc", "applicant_id": APP, "case_id": CASE,
        "status": "PARTIAL", "decision": "REVIEW",
        "reason_codes": ["INSUFFICIENT_SOURCES"], "next_action": "MANUAL_REVIEW",
        "documents": [{"source_id": "pan.jpg", "type": "PAN", "party_id": APP,
                       "verification": "PASS", "reason_codes": [],
                       "extraction": {"name": PAN_NAME}}],
        "kyc": {"status": "REVIEW", "overall_score": 0, "overall_confidence": 0,
                "reason_codes": ["INSUFFICIENT_SOURCES"], "fields": []},
    })


def test_why_and_status_give_the_same_reason(client, repo):
    """Both say more documents are needed; "why" adds the fact behind it."""
    _one_document_case()

    status = ask(client, "What is my application status?")["answer"]
    why = ask(client, "why is my application under review?")["answer"]

    assert "more documents are needed for verification" in status
    assert why.startswith("Your application is under review because more "
                          "documents are needed for verification")


def test_a_named_pending_document_is_the_one_answered(client, repo):
    mismatch_case()

    missing = ask(client, "addr proof pending?")["answer"]
    received = ask(client, "is my pan pending?")["answer"]

    assert missing.startswith("Address Proof is still pending.")
    assert received.startswith("PAN is not pending")
    assert "Address Proof" in received   # still to collect, said after


def test_an_income_single_source_reason_is_words():
    from app.agents.applicant import case_memory_facts

    assert "_" not in case_memory_facts._readable("INCOME_SINGLE_SOURCE")
    assert "only one document" in case_memory_facts._readable(
        "INCOME_SINGLE_SOURCE")
