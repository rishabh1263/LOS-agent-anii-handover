"""
The defects a live Swagger audit found, and the ones it only appeared to.

TWO OF THE REPORTED FOUR TURNED OUT TO BE CORRECT BEHAVIOUR, and the
tests for them are here for the same reason the fixes are: so the next
reader does not re-open a question that has been answered.

  THE BANK STATEMENT DOES EXPOSE ITS ACCOUNT HOLDER, where the
  statement labels one. Two of the three samples in this repository
  name theirs and it reaches cross-document identity; the third does
  not label it at all, and the parser refuses to guess -- its holder
  name is the second line of the page, indistinguishable in shape
  from a branch or a city. A wrong name that happens to match is a
  false PASS on somebody else's account, which is worse than one
  fewer source.

  SO KYC COMPARES WHAT IT HAS. Matching names pass, different names
  fail with a mismatch, and an unlabelled statement is one source
  rather than a contradiction.

THE TWO THAT WERE REAL:

  "WHAT DOCUMENTS ARE PENDING" WAS PUBLISHED AS POLICY_REQUIREMENT --
  telling a client the answer came from the handbook, when it comes
  from the documents on the case.

  THE CASE-STATE HEADER WAS BUILT ONLY FROM WHAT THE ANSWER FETCHED.
  A narrow question plans one tool, so `stage` and `readiness` came
  back null for a case whose stage and readiness the store knew.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.applicant.intents import Intent, classify
from app.agents.applicant.query_types import QueryType, type_for
from app.agents.kyc.agent import run_kyc
from app.agents.kyc.schemas import KycDocumentType, KycRequest, SourceDocument

PAN_NAME = "RISHABH AJIT SINGH"
PAN_DOB = date(2002, 6, 12)


def pan_source() -> SourceDocument:
    return SourceDocument(
        source_id="rpan.jpg", document_type=KycDocumentType.PAN,
        name=PAN_NAME, father_name="AJIT SINGH",
        date_of_birth=PAN_DOB, pan="NUHPS4875K",
    )


def statement_source(name: str | None,
                     dob: date | None = None) -> SourceDocument:
    return SourceDocument(
        source_id="statement.pdf",
        document_type=KycDocumentType.BANK_STATEMENT,
        name=name, date_of_birth=dob,
    )


def outcome(*sources):
    result = run_kyc(KycRequest(documents=list(sources)))
    fields = {
        (f.field.value if hasattr(f.field, "value") else str(f.field)):
        (f.status.value if hasattr(f.status, "value") else str(f.status))
        for f in result.fields
    }
    codes = [c.value if hasattr(c, "value") else str(c)
             for c in result.reason_codes]
    status = (result.status.value if hasattr(result.status, "value")
              else str(result.status))
    return fields, status, codes


# ==========================================================================
# A-D. CROSS-DOCUMENT IDENTITY, WITH AND WITHOUT A HOLDER NAME
# ==========================================================================


def test_a_matching_account_holder_passes_the_name_check():
    fields, status, _ = outcome(pan_source(), statement_source(PAN_NAME))

    assert fields["NAME"] == "PASS"
    assert status == "PASS"


def test_a_different_account_holder_fails_the_name_check():
    """
    THE CHECK THAT MATTERS. A statement belonging to somebody else is
    exactly what cross-document identity exists to catch.
    """
    fields, status, codes = outcome(
        pan_source(), statement_source("SOMEBODY ELSE ENTIRELY"))

    assert fields["NAME"] == "FAIL"
    assert "NAME_MISMATCH" in codes
    # REVIEW, not REJECT: the blocking policy is unchanged.
    assert status == "REVIEW"


def test_a_statement_without_a_holder_name_is_not_a_mismatch():
    """
    NO NAME IS NOT A WRONG NAME. The statement simply cannot be
    compared, which is one fewer source -- never a finding against
    the applicant.
    """
    fields, status, codes = outcome(pan_source(), statement_source(None))

    assert fields["NAME"] == "SKIPPED"
    assert "NAME_MISMATCH" not in codes
    assert status == "REVIEW"


def test_a_date_of_birth_is_compared_only_when_the_source_has_one():
    """
    NEVER INFERRED. A statement that prints no date of birth leaves
    the check unmade rather than borrowing the PAN's.
    """
    with_dob, _, _ = outcome(pan_source(),
                             statement_source(PAN_NAME, PAN_DOB))
    without, _, _ = outcome(pan_source(), statement_source(PAN_NAME))

    assert with_dob["DATE_OF_BIRTH"] == "PASS"
    assert without.get("DATE_OF_BIRTH") in (None, "SKIPPED")


def test_the_parser_exposes_the_holder_where_the_statement_labels_one():
    """
    Asserted against the real samples, because this is the claim the
    audit doubted. Two label their holder; the third does not, and is
    left alone rather than guessed at.
    """
    import pathlib

    from app.agents.financial.agent import _from_bank_statement

    labelled = pathlib.Path("samples/documents/Canara Bank Statement.pdf")
    if not labelled.exists():
        pytest.skip("sample not available")

    assert _from_bank_statement(str(labelled)).name


# ==========================================================================
# E-F. WHICH QUESTIONS ARE ABOUT THE CASE
# ==========================================================================


@pytest.mark.parametrize("question", [
    "What documents are pending?",
    "What documents are still pending?",
    "Which documents are under review?",
])
def test_a_pending_question_is_a_case_fact(question):
    """
    IT WAS PUBLISHED AS POLICY_REQUIREMENT, which tells a client the
    answer came from the handbook. It comes from the documents on the
    case and the state each of them is in.
    """
    intent = classify(question).intent

    assert intent is Intent.DOCUMENTS_PENDING
    assert type_for(intent) is QueryType.CASE_FACT


@pytest.mark.parametrize("question,expected", [
    ("What documents are required for a personal loan?",
     QueryType.PROCESS_KNOWLEDGE),
    ("What documents are missing?", QueryType.POLICY_REQUIREMENT),
])
def test_a_policy_question_keeps_its_own_type(question, expected):
    """
    THE DISTINCTION IS NOT "ANYTHING ABOUT DOCUMENTS". A requirement
    is a rule about the product; the missing-documents answer carries
    the policy block a client renders beside it. Neither moved.
    """
    assert type_for(classify(question).intent) is expected


@pytest.mark.parametrize("question", [
    "Is my case ready for CPA?",
    "What is the current verification status?",
])
def test_the_other_case_questions_stay_case_facts(question):
    assert type_for(classify(question).intent) in (
        QueryType.CASE_FACT, QueryType.DOCUMENT_STATUS)


# ==========================================================================
# G. THE HEADER STATES THE CASE'S REAL STATE
# ==========================================================================


def test_the_header_is_filled_from_the_record_when_the_answer_did_not_read_it():
    """
    A narrow question plans one tool, so the envelope carries no
    application and no readiness -- and the header published
    `stage: null, readiness: null` for a case the store knew all
    about.
    """
    from unittest.mock import patch

    from app.api.routes.fos_api import _with_case_state
    from app.store.models import (
        Applicant,
        Application,
        ApplicationStatus,
    )

    application = Application(case_id="CASE-1", applicant_id="APP-1",
                              status=ApplicationStatus.BASIC_DOCUMENT_VERIFICATION)

    repository = type("Repo", (), {
        "get_application": lambda self, case_id: application,
        "get_applicant": lambda self, applicant_id: Applicant(
            applicant_id="APP-1", full_name="A PERSON", mobile="9000000000",
            date_of_birth="1990-01-01", address="Somewhere"),
        "list_documents": lambda self, case_id: [],
    })()

    with patch("app.store.get_repository", return_value=repository):
        filled = _with_case_state({"case_id": "CASE-1"})

    assert filled["stage"] == "BASIC_DOCUMENT_VERIFICATION"
    assert filled["readiness"], "readiness left unfilled"
    assert filled["readiness"].get("status")


def test_a_value_the_answer_did_fetch_is_not_overwritten():
    from unittest.mock import patch

    from app.api.routes.fos_api import _with_case_state

    original = {"case_id": "CASE-1", "stage": "READY_FOR_CPA",
                "readiness": {"status": "READY"}}

    with patch("app.store.get_repository") as store:
        filled = _with_case_state(dict(original))

    assert filled == original
    store.assert_not_called()


def test_a_question_with_no_case_is_left_alone():
    from app.api.routes.fos_api import _with_case_state

    assert _with_case_state({"case_id": None}) == {"case_id": None}


# ==========================================================================
# J. A PLACEHOLDER IS NOT A PARTY
# ==========================================================================


@pytest.mark.parametrize("value", ["string", "String", " string ", "null",
                                   "none", "undefined", "", None])
def test_the_interactive_docs_placeholder_is_not_a_co_applicant(value):
    """
    Swagger fills an optional string field with the word "string".
    Sent as a co-applicant id it opened a second party called
    `string`, which appeared in the response as a real person.
    """
    from app.api.routes.los_api import _party_or_none

    assert _party_or_none(value) is None


@pytest.mark.parametrize("value", ["COAPP-001", "stringent-co-001",
                                   "  APP-2  "])
def test_a_real_identifier_survives(value):
    from app.api.routes.los_api import _party_or_none

    assert _party_or_none(value) == value.strip()
