"""
The verification gate, and what documents.yaml actually controls.

There are TWO things called the verification gate, and conflating them is
what produced the bug these tests pin:

  ENFORCED   verification/agent.py::enabled_for(), driven by the
             VERIFICATION_ENABLED / VERIFICATION_ENABLED_<TYPE> environment
             variables. The document workflow and the API routes obey this.

  REPORTED   documents.yaml -> verification.documents.<TYPE>.enabled, read
             only by the MCP policy.get tool.

The YAML cannot switch verification off. It can describe it wrongly, and it
did: ITR and SALARY_SLIP were recorded as disabled, with a comment claiming
no extractor existed, while the pipeline verified both and both extractors
were validated against real samples.
"""

from __future__ import annotations

import pytest

from app.agents.verification.agent import enabled_for
from app.services import verification_config


@pytest.fixture(autouse=True)
def _fresh_config():
    """The YAML is cached; drop it around every test here."""
    verification_config.reload()
    yield
    verification_config.reload()


# ==========================================================================
# THE CORRECTED ENTRIES
# ==========================================================================


@pytest.mark.parametrize("document_type", ["ITR", "SALARY_SLIP"])
def test_itr_and_salary_slip_are_recorded_as_verified(document_type):
    """
    The regression.

    Both were recorded as disabled because no extractor existed. Both
    extractors now exist and are real-sample validated, so the record has to
    say so -- policy.get is how an agent finds out why a document was or was
    not verified.
    """
    assert verification_config.is_enabled(document_type) is True


@pytest.mark.parametrize("document_type", ["ITR", "SALARY_SLIP"])
def test_the_extractors_those_entries_describe_actually_exist(document_type):
    """
    The claim in the config has to be backed by code.

    Re-enabling the record without the extractor behind it would just be the
    old bug pointing the other way.
    """
    from app.agents.financial.schemas import FinancialDocumentType

    assert document_type in {t.value for t in FinancialDocumentType}


def test_the_salary_slip_extractor_is_importable():
    from app.agents.salary_slip import extract_salary_slip

    assert callable(extract_salary_slip)


def test_the_itr_extractor_is_importable():
    from app.agents.itr.extract import extract_itr

    assert callable(extract_itr)


# ==========================================================================
# THE TWO GATES MUST AGREE
# ==========================================================================


HANDLED_CLASSES = (
    "PAN",
    "DRIVING_LICENCE",
    "VOTER_ID",
    "PASSPORT",
    "BANK_STATEMENT",
    "ITR",
    "SALARY_SLIP",
    "SALE_DEED",
    "AADHAAR",
    "MARK_SHEET",
)


@pytest.mark.parametrize("document_type", HANDLED_CLASSES)
def test_the_reported_gate_matches_the_enforced_gate(document_type, monkeypatch):
    """
    The invariant that was broken.

    With no environment override in play, what policy.get reports must match
    what the pipeline does. A mismatch is not a harmless documentation slip:
    it is the system giving a false answer about its own behaviour.
    """
    monkeypatch.delenv("VERIFICATION_ENABLED", raising=False)
    monkeypatch.delenv(f"VERIFICATION_ENABLED_{document_type}", raising=False)

    assert verification_config.is_enabled(document_type) == enabled_for(
        document_type
    ), f"{document_type}: reported gate disagrees with the enforced gate"


def test_every_class_the_agent_handles_is_recorded_explicitly():
    """
    An absent entry defaults to enabled and makes a claim by omission.

    Listing every handled class means the record says something deliberate
    about each one instead of falling through to a default.
    """
    import yaml

    from app.services.verification_config import config_path

    with config_path().open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    recorded = set((data.get("verification") or {}).get("documents") or {})

    from app.agents.verification.agent import ALL_CLASSES

    missing = set(ALL_CLASSES) - recorded
    assert not missing, f"not recorded in documents.yaml: {sorted(missing)}"


def test_the_stale_extractor_comment_is_gone():
    """The comment claimed something that had stopped being true."""
    from app.services.verification_config import config_path

    text = config_path().read_text(encoding="utf-8")

    assert "extractor not built yet" not in text


# ==========================================================================
# THE ENFORCED GATE STILL ENFORCES
# ==========================================================================


@pytest.mark.parametrize("document_type", ["ITR", "SALARY_SLIP"])
def test_the_enforced_gate_can_still_switch_a_type_off(
    document_type, monkeypatch
):
    """
    Re-enabling the record must not remove the ability to gate a class.

    An incident rollback needs to be able to switch one document type off
    without touching a file inside a container image.
    """
    monkeypatch.setenv(f"VERIFICATION_ENABLED_{document_type}", "false")

    assert enabled_for(document_type) is False


@pytest.mark.parametrize("document_type", ["ITR", "SALARY_SLIP"])
def test_a_disabled_type_reports_skipped_rather_than_passing(
    document_type, monkeypatch
):
    """
    A switched-off check must be visible in the result.

    SKIPPED with VERIFICATION_DISABLED is the point: a check that quietly
    stops running is indistinguishable from one that keeps passing.
    """
    monkeypatch.setenv(f"VERIFICATION_ENABLED_{document_type}", "false")

    from app.agents.verification.agent import VerificationStatus, verify_quick

    result = verify_quick(
        "does-not-need-to-exist.png",
        requested_type=document_type,
        request_id="GATE-TEST",
    )

    assert result.status is VerificationStatus.SKIPPED
    assert result.enabled is False
    assert "VERIFICATION_DISABLED" in result.reason_codes


def test_the_global_switch_still_wins(monkeypatch):
    monkeypatch.setenv("VERIFICATION_ENABLED", "false")
    monkeypatch.delenv("VERIFICATION_ENABLED_ITR", raising=False)

    assert enabled_for("ITR") is False


def test_a_per_type_override_beats_the_global_switch(monkeypatch):
    monkeypatch.setenv("VERIFICATION_ENABLED", "false")
    monkeypatch.setenv("VERIFICATION_ENABLED_ITR", "true")

    assert enabled_for("ITR") is True


# ==========================================================================
# WHAT policy.get REPORTS
# ==========================================================================


async def test_the_policy_tool_reports_the_corrected_values():
    """policy.get is the consumer this section exists for."""
    from app.mcp import capabilities

    envelope = await capabilities.policy_get("verification")

    assert envelope.ok is True
    by_type = envelope.result["content"]["enabled_by_document_type"]

    assert by_type["ITR"] is True
    assert by_type["SALARY_SLIP"] is True


# ==========================================================================
# ADDRESS PROOF IS UNDEFINED, DELIBERATELY
# ==========================================================================


def test_address_proof_has_no_document_type():
    """
    Address Proof is a business category with no definition behind it.

    No class, no extractor, no markers, and no sample in the real corpus.
    This pins that it stays undefined until the business names the accepted
    document types and real samples exist -- if someone adds a type without
    those, this fails and the config note needs revisiting with it.
    """
    from app.agents.document_agent.schemas import DocumentType
    from app.agents.kyc.schemas import KycDocumentType
    from app.agents.verification.basic import DocumentClass

    for enum in (DocumentClass, DocumentType, KycDocumentType):
        names = {member.value for member in enum}
        assert "ADDRESS_PROOF" not in names, (
            f"{enum.__name__} now defines ADDRESS_PROOF -- confirm the "
            "accepted document types and update the documents.yaml note"
        )


def test_the_address_proof_decision_is_recorded():
    """The gap is written down where the next person will look for it."""
    from app.services.verification_config import config_path

    text = config_path().read_text(encoding="utf-8")

    assert "ADDRESS_PROOF = DEFINITION_REQUIRED" in text


def test_the_kyc_address_check_needs_two_sources():
    """
    Why the gap matters.

    check_address compares addresses ACROSS documents. With only the licence
    and the voter card able to supply one, it usually cannot run at all.
    """
    from app.agents.kyc.checks import check_address
    from app.agents.kyc.schemas import (
        AddressInput,
        CheckStatus,
        KycDocumentType,
        ReasonCode,
        SourceDocument,
    )

    single = [
        SourceDocument(
            source_id="dl",
            document_type=KycDocumentType.DRIVING_LICENCE,
            address=AddressInput(raw="12 EXAMPLE ROAD, EXAMPLE CITY", pincode="400001"),
        )
    ]

    result = check_address(single)

    assert result.status is CheckStatus.SKIPPED
    assert ReasonCode.ADDRESS_SINGLE_SOURCE in result.reason_codes
