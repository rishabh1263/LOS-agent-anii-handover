"""
The demonstration bank statement, and the shape of what comes back.

TWO THINGS ARE PINNED HERE.

  THE DEMO FIXTURE STAYS READABLE. `samples/documents/demo_bank_statement.pdf`
  is generated, one page, with a real text layer, and every value in it is
  invented. The repository's other statements have text layers too and are
  real people's -- names, email addresses, dozens of pages of their
  transactions -- which is why none of them is used for a demonstration. If
  the fixture stops parsing, the demo silently falls back to "requires
  manual review" and looks like a broken pipeline rather than a stale file.

  NOTHING IDENTIFYING LEAVES THE BUILDING. The account number, the IFSC and
  the holder's name are extracted -- the pipeline needs them -- and none of
  them belongs in a Copilot answer, in a source, or in the vector index.
  Extracted identity values are deliberately never indexed, so a question
  cannot retrieve them; these tests hold that line.

THE SCANNED CASE IS NOT WORKED AROUND. A scanned multi-page statement still
returns DOCUMENT_REQUIRES_OCR, because the synchronous pipeline declines to
run heavy OCR on one. That rule is production behaviour and is asserted, not
bypassed.
"""

from __future__ import annotations

import pathlib

import pytest

from app.agents.bank_statement.extract import extract_bank_statement

FIXTURE = pathlib.Path("samples/documents/demo_bank_statement.pdf")
SCANNED = pathlib.Path("samples/documents/SBI Bank Statement.pdf")

#: Values invented for the fixture. They must never surface in an
#: answer, a source, or an indexed chunk.
ACCOUNT_NUMBER = "30123456789"
IFSC = "SBIN0001234"


@pytest.fixture(scope="module")
def extracted():
    if not FIXTURE.exists():
        pytest.skip(f"{FIXTURE} not generated")
    return extract_bank_statement(str(FIXTURE))


# ==========================================================================
# A. THE FIXTURE PARSES
# ==========================================================================


def test_the_demo_statement_exists_and_has_one_page(extracted):
    assert extracted.pages == 1
    assert extracted.pages_with_text == 1


def test_the_demo_statement_extracts_successfully(extracted):
    assert str(extracted.status).endswith("SUCCESS")
    assert extracted.errors == []


def test_the_transactions_are_read_and_the_balance_reconciles(extracted):
    """
    A statement whose running balance does not add up is rejected
    downstream, so a typo in the fixture would surface as a failed
    demo rather than as a wrong number.
    """
    assert extracted.transaction_count == 8
    assert extracted.balance_reconciles is True


def test_the_account_holder_matches_the_seeded_applicant(extracted):
    """
    DEMO-APP-002 is VIKRAM SINGH CHAUHAN. The holder has to match, or
    the cross-document identity check compares two different people
    and the demo shows a mismatch nobody intended.
    """
    assert extracted.account_holder == "VIKRAM SINGH CHAUHAN"


def test_the_account_number_is_reported_masked(extracted):
    assert extracted.account_number_masked
    assert ACCOUNT_NUMBER not in str(extracted.account_number_masked)


@pytest.mark.skipif(not SCANNED.exists(), reason="scanned sample absent")
def test_a_scanned_statement_is_still_declined():
    """
    THE SAFETY RULE IS NOT WORKED AROUND. The synchronous pipeline
    does not run heavy OCR on a scanned multi-page statement, and the
    fixture exists because of that rule, not in spite of it.
    """
    result = extract_bank_statement(str(SCANNED))

    assert result.pages_with_text == 0
    assert not str(result.status).endswith("SUCCESS")


# ==========================================================================
# B. WHAT IS INDEXED CARRIES NO IDENTITY VALUES AND NO FILE NAMES
# ==========================================================================


def test_a_document_sentence_names_the_kind_not_the_file():
    """
    The indexed sentence is what a model paraphrases. With the file
    name in it, an officer asking why a case was under review was
    told about "4b543335-47f4-41fb-9458-49fcaf27a983_4.pdf".
    """
    from app.knowledge import indexing

    class Document:
        document_id = "CASE-1:APP-1:4b543335-47f4-41fb_4.pdf"
        source_id = "4b543335-47f4-41fb_4.pdf"
        document_type = "BANK_STATEMENT"
        verification_status = "PASS"
        reason_codes: list[str] = []
        party_id = "APP-1"
        party_role = "PRIMARY_APPLICANT"
        uploaded_at = None

    repository = type("Repo", (), {
        "list_documents": lambda self, case_id: [Document()]})()

    derived = indexing._from_documents(repository, "CASE-1", {})

    assert "BANK_STATEMENT" in derived[0].text
    assert "4b543335" not in derived[0].text
    # Still traceable, in the metadata rather than the prose.
    assert derived[0].payload["source_id"] == Document.source_id


def test_extracted_identity_values_are_never_indexed():
    """
    `_from_documents` indexes what HAPPENED to a document, never what
    was read out of it. An account number in the index is retrievable
    by similarity, which is not a property anybody asked for.
    """
    import inspect

    from app.knowledge import indexing

    source = inspect.getsource(indexing._from_documents)

    for field in ("account_number", "ifsc", "extracted_fields", "pan_number",
                  "date_of_birth"):
        assert field not in source


# ==========================================================================
# C. THE PUBLISHED SOURCE SHAPE
# ==========================================================================


def test_every_source_says_what_kind_it_is():
    from app.api.routes.copilot_api import _normalised

    published = _normalised(
        [{"kind": "case_finding", "document_id": "d1"},
         {"kind": "process_knowledge", "source_type": "PROCESS_KNOWLEDGE"}],
        case_id="DEMO-CASE-005", stage="RCU", documents={"d1": "SALE_DEED"})

    assert published[0]["type"] == "CASE_FINDING"
    assert published[1]["type"] == "PROCESS_KNOWLEDGE"


def test_a_case_source_is_given_the_case_and_the_stage():
    from app.api.routes.copilot_api import _normalised

    published = _normalised([{"kind": "case_finding", "document_id": "d1"}],
                            case_id="DEMO-CASE-005", stage="RCU",
                            documents={})

    assert published[0]["case_id"] == "DEMO-CASE-005"
    assert published[0]["stage"] == "RCU"


def test_process_knowledge_belongs_to_no_case():
    """
    A stage guide describes a desk. Stamping the case onto it would
    claim the guidance was recorded against this file.
    """
    from app.api.routes.copilot_api import _normalised

    published = _normalised(
        [{"kind": "process_knowledge", "source_type": "PROCESS_KNOWLEDGE"}],
        case_id="DEMO-CASE-005", stage="RCU", documents={})

    assert published[0].get("case_id") is None
    assert published[0]["stage"] == "RCU"


def test_a_finding_borrows_the_document_kind_from_the_document_source():
    """
    Retrieval labels a document chunk with its type; case memory cites
    a finding on the same document without one. A reader should see
    BANK_STATEMENT on both.
    """
    from app.api.routes.copilot_api import _normalised

    published = _normalised(
        [{"kind": "case_finding", "document_id": "d1",
          "reason_code": "ADDRESS_MISMATCH"}],
        case_id="C1", stage="RCU", documents={"d1": "BANK_STATEMENT"})

    assert published[0]["document_type"] == "BANK_STATEMENT"


def test_the_lowercase_kind_is_kept_for_existing_callers():
    from app.api.routes.copilot_api import _normalised

    published = _normalised([{"kind": "case_finding"}], case_id="C1",
                            stage="RCU", documents={})

    assert published[0]["kind"] == "case_finding"
    assert published[0]["type"] == "CASE_FINDING"


# ==========================================================================
# D. THE COMPACT OVERALL BLOCK
# ==========================================================================


def test_a_document_issue_reads_as_a_sentence():
    """
    DOCUMENT_TYPE_MISMATCH reached a reader as a bare enum name until
    the issue builder consulted the verification catalogue.
    """
    from app.agents.los import overview

    published = overview.verification_of([
        {"source_id": "a.pdf", "type": "BANK_STATEMENT",
         "verification": "REVIEW", "reason_codes": ["DOCUMENT_REQUIRES_OCR"]},
    ])

    issue = published["issues"][0]
    assert issue["code"] == "DOCUMENT_REQUIRES_OCR"
    assert issue["document_type"] == "BANK_STATEMENT"

    # THE SENTENCE IS ATTACHED ONCE, where the compact block is
    # assembled -- carrying it here as well put the same string in
    # the response twice.
    from app.agents.los import overview as _overview

    compact = _overview._party_issues("PRIMARY_APPLICANT",
                                      {"verification": published})
    assert "scan" in compact[0]["message"].lower()


def test_a_code_nobody_explained_gets_no_invented_sentence():
    """
    The catalogue is consulted; the derive-from-letters fallback is
    not. An unmapped code stays visibly unmapped rather than becoming
    confident-looking prose nobody wrote.
    """
    from app.agents.los import overview

    published = overview.verification_of([
        {"source_id": "a.pdf", "verification": "REVIEW",
         "reason_codes": ["SOMETHING_NOBODY_MAPPED"]},
    ])

    assert "message" not in published["issues"][0]
