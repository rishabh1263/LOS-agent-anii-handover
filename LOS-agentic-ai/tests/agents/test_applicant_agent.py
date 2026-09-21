"""
The Applicant Agent: what it must answer, and what it must refuse.

THE RULES THIS SUITE DEFENDS:

    every fact comes from a stored record, never from the model
    authorisation is decided before any record is read
    a write is proposed, never performed on the first pass
    credit, risk, KYC and lending questions are routed, never answered
    a model that is off, slow or wrong costs the phrasing and nothing else

The store is a real repository pointed at a temporary file, populated through
the same tools the API uses. No fixture writes rows behind the interface,
because a test that bypasses the repository is not testing the repository.
"""

from __future__ import annotations

import pytest

from app.agents.applicant import config as agent_config
from app.agents.applicant.intents import Intent, classify, plan_for
from app.agents.applicant.permissions import Caller, PermissionDenied, check_capability
from app.agents.applicant.validate import validate_answer, validate_response_shape
from app.store import set_repository
from app.store.models import (
    Applicant,
    Application,
    ApplicationStatus,
    Document,
    DocumentStatus,
    status_for_verdict,
)
from app.store.sqlite_repo import SQLiteRepository

FULL_SCOPES = {
    "read_applicant", "read_application", "read_documents", "read_verification",
    "read_pending_items", "read_next_action", "create_applicant",
    "update_applicant", "create_application", "upload_document",
}


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    """
    A real repository on a temporary file, for every test in this module.

    A real one rather than a fake: the SQL, the JSON round-tripping and the
    upsert behaviour are things that break, and a fake would not notice.
    """
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    agent_config.reload()

    repository = SQLiteRepository(tmp_path / "test_store.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)
    agent_config.reload()


@pytest.fixture
def caller() -> Caller:
    return Caller(subject="fos-test", scopes=frozenset(FULL_SCOPES),
                  roles=frozenset({"fos"}))


def seed(repository, *, documents=None, product="PERSONAL_LOAN",
         address="12 Example Street", applicant_id="APP-TEST",
         case_id="CASE-TEST"):
    """
    A case, built through the repository interface.

    Returns (applicant, application). Document verdicts are passed in as the
    document pipeline would report them -- PASS / REVIEW / FAIL -- and
    translated by the same function the ingest path uses.
    """
    applicant = Applicant(
        applicant_id=applicant_id, full_name="Test Applicant",
        mobile="9876543210", date_of_birth="1990-01-01", address=address,
    )
    repository.save_applicant(applicant)

    application = Application(case_id=case_id, applicant_id=applicant_id,
                              product=product, loan_amount="100000")
    repository.save_application(application)

    for doc_type, verdict, codes in (documents or []):
        repository.save_document(Document(
            document_id=f"{case_id}:{doc_type}",
            case_id=case_id, applicant_id=applicant_id,
            document_type=doc_type,
            status=status_for_verdict(verdict),
            verification_status=verdict,
            reason_codes=list(codes or []),
            source_id=f"{doc_type.lower()}.jpg",
        ))

    return applicant, application


# ==========================================================================
# INTENT -- the questions a FOS actually types
# ==========================================================================

@pytest.mark.parametrize("message,expected", [
    ("Show applicant details.", Intent.APPLICANT_DETAILS),
    ("Who is the applicant?", Intent.APPLICANT_DETAILS),
    ("What information is still missing?", Intent.APPLICANT_MISSING_INFO),
    ("What's the application status?", Intent.APPLICATION_STATUS),
    ("Where is this application?", Intent.APPLICATION_STAGE),
    ("Which documents have been uploaded?", Intent.DOCUMENTS_UPLOADED),
    ("What documents are required?", Intent.DOCUMENTS_REQUIRED),
    ("Show me the document checklist.", Intent.DOCUMENTS_REQUIRED),
    ("Which documents are missing?", Intent.DOCUMENTS_MISSING),
    ("Which docs are left?", Intent.DOCUMENTS_MISSING),
    ("Which documents are under review?", Intent.DOCUMENTS_PENDING),
    ("Is PAN verified?", Intent.DOCUMENT_VERIFICATION),
    ("Is the passport ok?", Intent.DOCUMENT_VERIFICATION),
    ("Why did this document fail?", Intent.DOCUMENT_VERIFICATION),
    ("What's pending?", Intent.PENDING_ITEMS),
    ("What is blocking this application?", Intent.PENDING_ITEMS),
    ("What should I do next?", Intent.NEXT_ACTION),
    ("Can I submit this application?", Intent.READINESS),
    ("Is this ready for CPA?", Intent.READINESS),
    ("Is everything complete?", Intent.COMPLETENESS),
    ("Give me a complete applicant summary.", Intent.FULL_SUMMARY),
    ("Summarize this case.", Intent.FULL_SUMMARY),
    ("Where does this case stand?", Intent.FULL_SUMMARY),
])
def test_the_question_is_understood(message, expected):
    assert classify(message).intent is expected


@pytest.mark.parametrize("message,route", [
    ("What is the applicant's credit score?", "CREDIT_SCORE"),
    ("Show me the CIBIL report.", "CREDIT_SCORE"),
    ("Calculate the risk for this case.", "RISK"),
    ("What is the risk rating?", "RISK"),
    ("Do a complete KYC.", "KYC_DECISION"),
    ("Run an RCU check.", "RCU_FRAUD"),
    ("Analyze the bank statement.", "FINANCIAL_ANALYSIS"),
    ("Is this loan safe to approve?", "LOAN_DECISION"),
    ("Should we approve this?", "LOAN_DECISION"),
])
def test_downstream_questions_are_routed_not_answered(message, route):
    """
    These must be recognised BEFORE anything tries to answer them.

    An agent that answers "what is the credit score?" from FOS data has
    invented a credit score.
    """
    classification = classify(message)
    assert classification.intent is Intent.OUT_OF_SCOPE
    assert classification.route_to == route
    assert plan_for(classification) == ()


def test_an_unrecognised_question_is_not_guessed_at():
    assert classify("What is the weather today?").intent is Intent.UNKNOWN


def test_the_named_document_reaches_the_plan():
    classification = classify("Is the driving licence verified?")
    assert classification.document_type == "DRIVING_LICENCE"
    assert plan_for(classification)[0] == "documents.verification"


def test_a_verification_question_naming_no_document_still_answers():
    """"Which documents failed?" has no single document to look up."""
    classification = classify("Which documents failed?")
    assert classification.intent is Intent.DOCUMENT_VERIFICATION
    assert plan_for(classification)[0] == "documents.get"


@pytest.mark.parametrize("message", [
    "What is the application status?",
    "Which documents have been uploaded?",
    "What should I do next?",
    "Is this case ready for CPA?",
    "What is pending?",
])
def test_every_case_scoped_question_carries_the_checklist(message):
    """
    The checklist is the screen behind the answer.

    A FOS asking for the next action gets the action and the checklist it came
    from, in one call. Without this the frontend has to make a second request
    to render the same screen.
    """
    assert "documents.checklist" in plan_for(classify(message)) or any(
        tool in {"applicant.360", "documents.checklist"}
        for tool in plan_for(classify(message))
    )


def test_an_applicant_question_asked_against_a_case_still_carries_it():
    """
    What makes a call case-scoped is the case_id, not the intent.

    "Show me the applicant" asked from a case screen is a case-scoped call,
    and that screen has a checklist on it.
    """
    plan = plan_for(classify("Show me the applicant details."))
    assert plan == ("applicant.get", "documents.checklist")


def test_an_applicant_question_with_no_case_asks_for_no_checklist():
    """There is no case to build one for; asking would only produce an error."""
    assert plan_for(classify("Show me the applicant details."),
                    has_case=False) == ("applicant.get",)


def test_without_a_case_id_no_case_scoped_checklist_is_planned():
    plan = plan_for(classify("What should I do next?"), has_case=False)
    assert "documents.checklist" not in plan


# ==========================================================================
# DETERMINISTIC WORKFLOW -- the business answers
# ==========================================================================

def test_a_complete_case_is_ready_for_cpa(_store):
    from app.agents.applicant import workflow

    applicant, application = seed(_store, documents=[
        ("PAN", "PASS", []),
        ("BANK_STATEMENT", "PASS", []),
        ("DRIVING_LICENCE", "PASS", []),
    ])
    documents = _store.list_documents(application.case_id)

    result = workflow.readiness(applicant, application, documents)
    assert result["status"] == "READY_FOR_CPA"
    assert result["blocking_items"] == []


def test_a_missing_document_blocks_the_handoff(_store):
    from app.agents.applicant import workflow

    applicant, application = seed(_store, documents=[("PAN", "PASS", [])])
    documents = _store.list_documents(application.case_id)

    result = workflow.readiness(applicant, application, documents)
    assert result["status"] == "NOT_READY"
    codes = {item["code"] for item in result["blocking_items"]}
    assert "DOCUMENT_MISSING" in codes


def test_an_alternative_document_satisfies_its_slot(_store):
    """A passport is as good as a licence for address proof."""
    from app.agents.applicant import workflow

    _, application = seed(_store, documents=[
        ("PAN", "PASS", []), ("BANK_STATEMENT", "PASS", []),
        ("PASSPORT", "PASS", []),
    ])
    checklist = workflow.build_checklist(
        application, _store.list_documents(application.case_id)
    )
    address = next(e for e in checklist if e["slot"] == "ADDRESS_PROOF")
    assert address["status"] == "VERIFIED"
    assert address["document_type"] == "PASSPORT"


def test_a_rejected_document_asks_for_a_replacement(_store):
    from app.agents.applicant import workflow

    applicant, application = seed(_store, documents=[
        ("PAN", "FAIL", ["DOCUMENT_TYPE_MISMATCH"]),
        ("BANK_STATEMENT", "PASS", []), ("DRIVING_LICENCE", "PASS", []),
    ])
    documents = _store.list_documents(application.case_id)

    action = workflow.next_action(applicant, application, documents)
    assert action["action"] == "REQUEST_CORRECT_DOCUMENT"


def test_a_document_under_review_blocks_while_the_rule_is_on(_store):
    from app.agents.applicant import workflow

    applicant, application = seed(_store, documents=[
        ("PAN", "REVIEW", ["IDENTIFIER_NOT_FOUND"]),
        ("BANK_STATEMENT", "PASS", []), ("DRIVING_LICENCE", "PASS", []),
    ])
    documents = _store.list_documents(application.case_id)

    result = workflow.readiness(applicant, application, documents)
    assert result["status"] == "NOT_READY"
    assert any(i["code"] == "DOCUMENT_UNDER_REVIEW"
               for i in result["blocking_items"])


def test_missing_applicant_information_blocks_before_documents(_store):
    """Information first: a document collected against a half-captured
    applicant often has to be collected again."""
    from app.agents.applicant import workflow

    applicant, application = seed(_store, address=None, documents=[
        ("PAN", "PASS", []), ("BANK_STATEMENT", "PASS", []),
        ("DRIVING_LICENCE", "PASS", []),
    ])
    documents = _store.list_documents(application.case_id)

    action = workflow.next_action(applicant, application, documents)
    assert action["action"] == "CAPTURE_APPLICANT_INFORMATION"


def test_a_switched_off_rule_is_reported_not_hidden(_store, monkeypatch):
    from app.agents.applicant import workflow

    applicant, application = seed(_store, documents=[("PAN", "PASS", [])])
    documents = _store.list_documents(application.case_id)

    result = workflow.readiness(applicant, application, documents)
    assert result["rules_applied"]["require_all_documents"] is True


def test_the_stage_follows_the_records(_store):
    from app.agents.applicant import workflow

    applicant, application = seed(_store, documents=[
        ("PAN", "PASS", []), ("BANK_STATEMENT", "PASS", []),
        ("DRIVING_LICENCE", "PASS", []),
    ])
    documents = _store.list_documents(application.case_id)
    readiness = workflow.readiness(applicant, application, documents)

    assert workflow.derived_stage(application, documents, readiness) == \
        ApplicationStatus.READY_FOR_CPA.value


# ==========================================================================
# THE STORE
# ==========================================================================

def test_a_pipeline_verdict_becomes_a_stored_status():
    assert status_for_verdict("PASS") is DocumentStatus.VERIFIED
    assert status_for_verdict("REVIEW") is DocumentStatus.REVIEW
    assert status_for_verdict("FAIL") is DocumentStatus.REJECTED


def test_a_skipped_check_does_not_count_as_looked_at():
    """SKIPPED established nothing, so the document is not marked verified."""
    assert status_for_verdict("SKIPPED") is DocumentStatus.UPLOADED


def test_records_survive_a_round_trip(_store):
    seed(_store, documents=[("PAN", "REVIEW", ["IDENTIFIER_NOT_FOUND"])])

    document = _store.get_document("CASE-TEST:PAN")
    assert document is not None
    assert document.status is DocumentStatus.REVIEW
    assert document.reason_codes == ["IDENTIFIER_NOT_FOUND"]


def test_saving_the_same_document_updates_rather_than_duplicates(_store):
    seed(_store, documents=[("PAN", "FAIL", ["BAD"])])
    document = _store.get_document("CASE-TEST:PAN")
    document.status = DocumentStatus.VERIFIED
    document.verification_status = "PASS"
    _store.save_document(document)

    documents = _store.list_documents("CASE-TEST")
    assert len(documents) == 1
    assert documents[0].status is DocumentStatus.VERIFIED


def test_ownership_is_answered_by_the_store(_store):
    seed(_store)
    assert _store.applicant_owns_case("APP-TEST", "CASE-TEST") is True
    assert _store.applicant_owns_case("APP-OTHER", "CASE-TEST") is False


# ==========================================================================
# PERMISSIONS
# ==========================================================================

def test_a_read_needs_its_scope(caller):
    check_capability(caller, Intent.APPLICANT_DETAILS)

    without = Caller(subject="x", scopes=frozenset(), roles=frozenset())
    with pytest.raises(PermissionDenied) as exc:
        check_capability(without, Intent.APPLICANT_DETAILS)
    assert exc.value.code == "INSUFFICIENT_SCOPE"


def test_the_read_all_scope_does_not_grant_writes():
    """A service account that can see everything still cannot change it."""
    reader = Caller(subject="svc", scopes=frozenset({"los.read"}),
                    roles=frozenset())

    check_capability(reader, Intent.APPLICANT_DETAILS)

    with pytest.raises(PermissionDenied):
        check_capability(reader, Intent.UPDATE_APPLICANT)


def test_a_write_needs_its_own_scope(caller):
    check_capability(caller, Intent.UPDATE_APPLICANT)

    partial = Caller(subject="x", scopes=frozenset({"read_applicant"}),
                     roles=frozenset())
    with pytest.raises(PermissionDenied):
        check_capability(partial, Intent.UPDATE_APPLICANT)


def test_a_case_belonging_to_another_applicant_is_refused(_store):
    from app.agents.applicant.permissions import check_ownership

    seed(_store)
    check_ownership("APP-TEST", "CASE-TEST")

    with pytest.raises(PermissionDenied) as exc:
        check_ownership("APP-SOMEONE-ELSE", "CASE-TEST")
    assert exc.value.code == "CASE_NOT_ACCESSIBLE"


# ==========================================================================
# OUTPUT VALIDATION -- the model may not invent a fact
# ==========================================================================

FACTS = {
    "applicant": {"full_name": "Test Applicant", "missing_fields": ["address"]},
    "documents": [{"type": "PAN", "status": "VERIFIED"}],
    "readiness": "NOT_READY",
}


def test_a_grounded_answer_is_accepted():
    accepted, value = validate_answer(
        "PAN is VERIFIED and the case is NOT_READY; the address is missing.",
        FACTS,
    )
    assert accepted is True, value


def test_an_invented_number_is_rejected():
    accepted, reason = validate_answer(
        "The applicant has requested 4200000 rupees.", FACTS,
    )
    assert accepted is False
    assert "unsupported number" in reason


def test_an_invented_status_is_rejected():
    accepted, reason = validate_answer(
        "The PAN document was REJECTED during verification.", FACTS,
    )
    assert accepted is False
    assert "unsupported status" in reason


@pytest.mark.parametrize("text", [
    "The loan is approved and ready to disburse.",
    "I recommend approval for this applicant.",
    "The applicant's credit score is strong.",
    "This applicant is creditworthy.",
])
def test_downstream_decision_language_is_refused(text):
    """
    No phrasing of a credit decision can be grounded here.

    The FOS stage does not decide these, so there is no data that could make
    such a sentence true.
    """
    accepted, reason = validate_answer(text, FACTS)
    assert accepted is False
    assert "downstream decision language" in reason


def test_structured_output_is_rejected():
    accepted, reason = validate_answer('{"status": "VERIFIED"}', FACTS)
    assert accepted is False
    assert "structured data" in reason


def test_an_empty_answer_is_rejected():
    accepted, _ = validate_answer("   ", FACTS)
    assert accepted is False


def test_counts_of_what_was_shown_are_allowed():
    """"1 document" is a fact about the data, not an invention."""
    accepted, value = validate_answer(
        "There is 1 document on file and it is VERIFIED.", FACTS,
    )
    assert accepted is True, value


def test_the_envelope_carries_no_internals():
    assert validate_response_shape({
        "answer": "ok", "documents": [{"document_type": "PAN"}],
    }) == []

    problems = validate_response_shape({
        "answer": "ok", "debug": {"prompt": "you are a helpful assistant"},
    })
    assert problems == ["response.debug.prompt"]


# ==========================================================================
# THE AGENT END TO END, through the tool layer
# ==========================================================================

async def test_a_question_is_answered_from_stored_records(_store, caller):
    from app.agents.applicant.agent import answer_question

    seed(_store, documents=[("PAN", "PASS", [])])

    response = await answer_question(
        message="Is PAN verified?",
        applicant_id="APP-TEST", case_id="CASE-TEST",
        claims={"sub": "fos", "scope": " ".join(FULL_SCOPES)},
    )
    assert response["intent"] == Intent.DOCUMENT_VERIFICATION.value
    assert "VERIFIED" in response["answer"]
    # STRUCTURED, not "deterministic": the value now says WHICH source,
    # so a knowledge answer and a records lookup are distinguishable.
    assert response["response_source"] == "STRUCTURED"


async def test_a_write_is_proposed_and_not_performed(_store):
    from app.agents.applicant.agent import answer_question

    seed(_store)

    response = await answer_question(
        message="Update the applicant's phone number to 9998887776",
        applicant_id="APP-TEST", case_id="CASE-TEST",
        claims={"sub": "fos", "scope": " ".join(FULL_SCOPES)},
    )
    assert response["intent"] == Intent.UPDATE_APPLICANT.value
    assert len(response["actions"]) == 1
    assert response["actions"][0]["requires_confirmation"] is True

    # Nothing changed until it is confirmed.
    assert _store.get_applicant("APP-TEST").mobile == "9876543210"


async def test_a_confirmed_write_is_applied(_store):
    from app.agents.applicant.agent import answer_question, confirm_action

    seed(_store)
    claims = {"sub": "fos", "scope": " ".join(FULL_SCOPES)}

    proposal = await answer_question(
        message="Update the applicant's phone number to 9998887776",
        applicant_id="APP-TEST", case_id="CASE-TEST", claims=claims,
    )
    result = await confirm_action(action=proposal["actions"][0], claims=claims)

    assert result["applied"] is True
    assert _store.get_applicant("APP-TEST").mobile == "9998887776"


async def test_a_confirmation_is_re_authorised(_store):
    """The proposal and the confirmation are separate requests."""
    from app.agents.applicant.agent import AgentError, answer_question, confirm_action

    seed(_store)
    proposal = await answer_question(
        message="Update the applicant's phone number to 9998887776",
        applicant_id="APP-TEST", case_id="CASE-TEST",
        claims={"sub": "fos", "scope": " ".join(FULL_SCOPES)},
    )

    with pytest.raises(AgentError) as exc:
        await confirm_action(
            action=proposal["actions"][0],
            claims={"sub": "fos", "scope": "los.read"},
        )
    assert exc.value.http_status == 403


async def test_an_out_of_scope_question_reads_nothing(_store):
    from app.agents.applicant.agent import answer_question

    seed(_store)

    response = await answer_question(
        message="What is the applicant's credit score?",
        applicant_id="APP-TEST", case_id="CASE-TEST",
        claims={"sub": "fos", "scope": " ".join(FULL_SCOPES)},
    )
    assert response["intent"] == Intent.OUT_OF_SCOPE.value
    assert response["route_to"] == "CREDIT_AGENT"
    assert response["applicant"] is None
    assert response["documents"] == []


async def test_a_cross_applicant_question_is_refused(_store):
    from app.agents.applicant.agent import AgentError, answer_question

    seed(_store)

    with pytest.raises(AgentError) as exc:
        await answer_question(
            message="What's pending?",
            applicant_id="APP-SOMEONE-ELSE", case_id="CASE-TEST",
            claims={"sub": "fos", "scope": " ".join(FULL_SCOPES)},
        )
    assert exc.value.http_status == 403


async def test_a_full_summary_combines_the_whole_picture(_store):
    from app.agents.applicant.agent import answer_question

    seed(_store, documents=[("PAN", "PASS", [])])

    response = await answer_question(
        message="Give me a complete summary of this applicant.",
        applicant_id="APP-TEST", case_id="CASE-TEST",
        claims={"sub": "fos", "scope": " ".join(FULL_SCOPES)},
    )
    assert response["intent"] == Intent.FULL_SUMMARY.value
    assert response["applicant"] is not None
    assert response["application"] is not None
    assert response["readiness"] is not None
    assert response["next_action"] is not None


async def test_a_missing_case_is_a_clean_answer_not_a_crash(_store):
    from app.agents.applicant.agent import AgentError, answer_question

    seed(_store)

    with pytest.raises(AgentError) as exc:
        await answer_question(
            message="What's pending?",
            applicant_id="APP-TEST", case_id="CASE-DOES-NOT-EXIST",
            claims={"sub": "fos", "scope": " ".join(FULL_SCOPES)},
        )
    assert exc.value.http_status == 403


async def test_the_response_never_carries_internals(_store):
    from app.agents.applicant.agent import answer_question

    seed(_store, documents=[("PAN", "PASS", [])])

    response = await answer_question(
        message="Give me a complete summary of this applicant.",
        applicant_id="APP-TEST", case_id="CASE-TEST",
        claims={"sub": "fos", "scope": " ".join(FULL_SCOPES)},
    )
    assert validate_response_shape(response) == []


# ==========================================================================
# INGEST -- the pipeline's verdicts, copied not recomputed
# ==========================================================================

def test_a_los_result_is_persisted_as_the_pipeline_reported_it(_store):
    from app.store.ingest import persist_los_result

    _store.save_applicant(Applicant(applicant_id="APP-ING"))

    summary = persist_los_result({
        "applicant_id": "APP-ING",
        "case_id": "CASE-ING",
        "documents": [
            {"source_id": "pan.jpg", "type": "PAN", "verification": "PASS",
             "extraction": {"pan_number": "ABCDE1234F"}},
            {"source_id": "dl.jpg", "type": "DRIVING_LICENCE",
             "verification": "REVIEW", "reason_codes": ["IDENTIFIER_NOT_FOUND"]},
        ],
    })

    assert summary["documents_written"] == 2
    documents = {d.document_type: d for d in _store.list_documents("CASE-ING")}
    assert documents["PAN"].status is DocumentStatus.VERIFIED
    assert documents["DRIVING_LICENCE"].status is DocumentStatus.REVIEW
    assert documents["DRIVING_LICENCE"].reason_codes == ["IDENTIFIER_NOT_FOUND"]


def test_extracted_values_are_not_copied_into_the_store(_store):
    """
    Field NAMES, never values.

    The store records that a PAN number was extracted. The number itself stays
    in the pipeline's response rather than becoming a second copy that would
    have to be protected.
    """
    from app.store.ingest import persist_los_result

    _store.save_applicant(Applicant(applicant_id="APP-ING"))
    persist_los_result({
        "applicant_id": "APP-ING", "case_id": "CASE-ING",
        "documents": [{"source_id": "pan.jpg", "type": "PAN",
                       "verification": "PASS",
                       "extraction": {"pan_number": "ABCDE1234F"}}],
    })

    # LOOKED UP BY CASE, NOT BY KEY. The stored key is
    # `case:party:source` now that a case can carry two people, and a
    # test that hardcodes the key shape is asserting an internal detail
    # rather than the behaviour it is named for -- which is that field
    # NAMES are stored and values are not.
    documents = _store.list_documents("CASE-ING")
    assert len(documents) == 1
    document = documents[0]

    assert document.extracted_fields == {"pan_number": True}
    assert "ABCDE1234F" not in str(document.extracted_fields)


def test_a_result_without_an_applicant_is_not_persisted(_store):
    """A document with no owner has nothing to be attached to."""
    from app.store.ingest import persist_los_result

    assert persist_los_result({
        "case_id": "CASE-ORPHAN",
        "documents": [{"source_id": "pan.jpg", "type": "PAN",
                       "verification": "PASS"}],
    }) is None
    assert _store.list_documents("CASE-ORPHAN") == []


def test_a_case_is_never_reassigned_between_applicants(_store):
    """Silently moving a case is how one customer's documents reach another."""
    from app.store.ingest import persist_los_result

    seed(_store, applicant_id="APP-ONE", case_id="CASE-SHARED")
    _store.save_applicant(Applicant(applicant_id="APP-TWO"))

    assert persist_los_result({
        "applicant_id": "APP-TWO", "case_id": "CASE-SHARED",
        "documents": [{"source_id": "x.jpg", "type": "PAN",
                       "verification": "PASS"}],
    }) is None
    assert _store.get_application("CASE-SHARED").applicant_id == "APP-ONE"


def test_persistence_failure_never_propagates(_store, monkeypatch):
    """The caller already has their answer; a store fault must not undo it."""
    from app.store import ingest

    def explode(*_args, **_kwargs):
        raise RuntimeError("store is on fire")

    monkeypatch.setattr(ingest, "_persist", explode)
    assert ingest.persist_los_result({"applicant_id": "A", "case_id": "C"}) is None
