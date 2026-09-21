"""
KYC Agent: cross-document consistency.

Every case is built from values the Document Agent and Financial Agent would
have produced, because that is exactly what KYC consumes -- no images, no OCR.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.agents.kyc.agent import configuration, run_kyc
from app.agents.kyc.schemas import (
    AddressInput,
    CheckStatus,
    IncomeInput,
    KycCheck,
    KycDocumentType,
    KycRequest,
    ReasonCode,
    SourceDocument,
)


def pan_doc(name="RISHABH AJIT SINGH", dob="2002-06-12", pan="NUHPS4875K", **kw):
    return SourceDocument(
        source_id="pan", document_type=KycDocumentType.PAN,
        name=name, date_of_birth=dob, pan=pan, **kw
    )


def dl_doc(name="RISHABH AJIT SINGH", dob="2002-06-12", address=None, **kw):
    return SourceDocument(
        source_id="dl", document_type=KycDocumentType.DRIVING_LICENCE,
        name=name, date_of_birth=dob,
        address=address or AddressInput(
            raw="BLOCK NO-F/5, R.NO.3, DEONAR NEW MUNICIPAL COLONY, "
                "Greater Mumbai, Mumbai Suburban, MH",
            pincode="400043",
        ),
        **kw
    )


def salary_doc(monthly="80000", name="RISHABH AJIT SINGH"):
    return SourceDocument(
        source_id="payslip", document_type=KycDocumentType.SALARY_SLIP,
        name=name,
        income=IncomeInput(monthly_net_salary=Decimal(monthly)),
    )


def bank_doc(avg_credit="85000", name="RISHABH AJIT SINGH"):
    return SourceDocument(
        source_id="bank", document_type=KycDocumentType.BANK_STATEMENT,
        name=name,
        income=IncomeInput(average_monthly_credit=Decimal(avg_credit)),
    )


def itr_doc(annual="960000", pan="NUHPS4875K", name="RISHABH AJIT SINGH"):
    return SourceDocument(
        source_id="itr", document_type=KycDocumentType.ITR,
        name=name, pan=pan,
        income=IncomeInput(declared_annual_income=Decimal(annual)),
    )


def result_for(*documents):
    return run_kyc(KycRequest(documents=list(documents)), request_id="t")


def status_of(result, check: KycCheck) -> CheckStatus:
    found = result.check(check)
    assert found is not None, f"{check} missing from result"
    return found.status


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_complete_matching_customer_passes():
    result = result_for(pan_doc(), dl_doc(), salary_doc(), bank_doc(), itr_doc())

    assert result.status is CheckStatus.PASS
    assert result.reason_codes == []
    assert status_of(result, KycCheck.NAME) is CheckStatus.PASS
    assert status_of(result, KycCheck.DOB) is CheckStatus.PASS
    assert status_of(result, KycCheck.PAN) is CheckStatus.PASS
    assert result.sources_received == 5
    # KYC never claims a document is genuine.
    assert result.authenticity_checked is False


def test_name_ordering_and_titles_do_not_count_as_a_mismatch():
    """Documents disagree constantly on ordering; that is not identity."""
    result = result_for(
        pan_doc(name="Mr. Rishabh Ajit Singh"),
        dl_doc(name="SINGH RISHABH AJIT"),
    )
    assert status_of(result, KycCheck.NAME) is CheckStatus.PASS


# ---------------------------------------------------------------------------
# Name
# ---------------------------------------------------------------------------


def test_name_mismatch_is_not_a_pass():
    result = result_for(pan_doc(name="RISHABH AJIT SINGH"),
                        dl_doc(name="SURESH KUMAR VERMA"))

    assert status_of(result, KycCheck.NAME) is CheckStatus.FAIL
    assert ReasonCode.NAME_MISMATCH in result.reason_codes

    # The CHECK fails; the overall verdict is capped at REVIEW because the
    # name check is non-blocking in policy. Two documents naming different
    # people is a reason to involve a human, not a finding this agent can
    # make on its own -- it cannot tell a married name from a wrong bundle.
    assert result.status is CheckStatus.REVIEW
    assert result.status is not CheckStatus.FAIL


def test_near_miss_name_goes_to_review_not_rejection():
    """A dropped letter is OCR damage, not a different applicant."""
    result = result_for(pan_doc(name="RISHABH AJIT SINGH"),
                        dl_doc(name="RISHABH AJT SINGH"))

    assert status_of(result, KycCheck.NAME) in (CheckStatus.PASS, CheckStatus.REVIEW)
    assert status_of(result, KycCheck.NAME) is not CheckStatus.FAIL


def test_name_comparison_carries_its_evidence():
    result = result_for(pan_doc(name="A B C"), dl_doc(name="X Y Z"))
    check = result.check(KycCheck.NAME)

    assert check.comparisons, "a mismatch must show which pair disagreed"
    assert {e.source_id for e in check.evidence} == {"pan", "dl"}
    assert check.comparisons[0].detail


# ---------------------------------------------------------------------------
# Date of birth
# ---------------------------------------------------------------------------


def test_dob_mismatch_fails():
    result = result_for(pan_doc(dob="2002-06-12"), dl_doc(dob="1994-01-03"))

    assert status_of(result, KycCheck.DOB) is CheckStatus.FAIL
    assert ReasonCode.DOB_MISMATCH in result.reason_codes

    # A date of birth has no near-misses, so the check is a hard FAIL -- but
    # it is non-blocking, so the applicant reaches a reviewer rather than a
    # rejection.
    assert result.status is CheckStatus.REVIEW


def test_dob_formats_normalise_before_comparison():
    result = result_for(pan_doc(dob="12/06/2002"), dl_doc(dob="2002-06-12"))
    assert status_of(result, KycCheck.DOB) is CheckStatus.PASS


def test_missing_dob_is_never_inferred():
    """A document without a DOB must not borrow one from another document."""
    result = result_for(pan_doc(dob=None), dl_doc(dob="2002-06-12"))

    check = result.check(KycCheck.DOB)
    assert check.status is CheckStatus.SKIPPED
    assert ReasonCode.DOB_SINGLE_SOURCE in check.reason_codes


def test_unparseable_dob_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError):
        SourceDocument(
            source_id="x", document_type=KycDocumentType.PAN,
            date_of_birth="not a date",
        )


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------


def test_same_address_written_differently_still_matches():
    left = dl_doc()
    right = SourceDocument(
        source_id="bank2", document_type=KycDocumentType.BANK_STATEMENT,
        address=AddressInput(
            raw="Block F/5 Room 3, Deonar New Municipal Colony, "
                "Mumbai, Maharashtra - 400043",
        ),
    )
    result = run_kyc(KycRequest(documents=[left, right]), request_id="t")
    assert status_of(result, KycCheck.ADDRESS) in (CheckStatus.PASS, CheckStatus.REVIEW)


def test_partial_address_mismatch_goes_to_review():
    """Same city and state, different pincode and locality: a human decides."""
    left = dl_doc()
    right = SourceDocument(
        source_id="other", document_type=KycDocumentType.BANK_STATEMENT,
        address=AddressInput(
            raw="12 ANDHERI WEST, Mumbai, Maharashtra", pincode="400053",
        ),
    )
    result = run_kyc(KycRequest(documents=[left, right]), request_id="t")

    assert status_of(result, KycCheck.ADDRESS) in (CheckStatus.REVIEW, CheckStatus.FAIL)
    assert result.status is CheckStatus.REVIEW, (
        "address is non-blocking, so it must not reject the applicant outright"
    )


def test_address_check_explains_which_components_differed():
    left = dl_doc()
    right = SourceDocument(
        source_id="other", document_type=KycDocumentType.BANK_STATEMENT,
        address=AddressInput(raw="9 MG ROAD, Pune, Maharashtra", pincode="411001"),
    )
    check = run_kyc(KycRequest(documents=[left, right]), "t").check(KycCheck.ADDRESS)

    assert check.comparisons
    assert "compared" in check.comparisons[0].detail


# ---------------------------------------------------------------------------
# PAN
# ---------------------------------------------------------------------------


def test_pan_mismatch_fails():
    result = result_for(pan_doc(pan="NUHPS4875K"), itr_doc(pan="EVPPG6189E"))

    assert status_of(result, KycCheck.PAN) is CheckStatus.FAIL
    assert ReasonCode.PAN_MISMATCH in result.reason_codes
    assert result.status is CheckStatus.REVIEW


def test_malformed_pan_fails_on_format():
    result = result_for(pan_doc(pan="NOTAPAN123"), itr_doc(pan="NOTAPAN123"))

    assert status_of(result, KycCheck.PAN) is CheckStatus.FAIL
    assert ReasonCode.PAN_INVALID_FORMAT in result.reason_codes


def test_single_pan_is_validated_but_not_cross_checked():
    result = result_for(pan_doc(), dl_doc())
    check = result.check(KycCheck.PAN)

    assert check.status is CheckStatus.SKIPPED
    assert ReasonCode.PAN_SINGLE_SOURCE in check.reason_codes


def test_pan_case_and_whitespace_are_normalised():
    result = result_for(pan_doc(pan=" nuhps4875k "), itr_doc(pan="NUHPS4875K"))
    assert status_of(result, KycCheck.PAN) is CheckStatus.PASS


# --- holder-type semantics -------------------------------------------------
# "ABCDE1234P" was used as a synthetic PAN in a live test and rejected. It is
# five letters, four digits and a letter, so it LOOKS valid -- but the 4th
# character encodes holder type, and D is not one the Income Tax Department
# assigns. The rejection is correct; these tests pin that it stays correct and
# that the reason is legible without reading the source.


def test_well_shaped_pan_with_an_unassigned_holder_type_is_rejected():
    result = result_for(pan_doc(pan="ABCDE1234P"), itr_doc(pan="ABCDE1234P"))

    assert status_of(result, KycCheck.PAN) is CheckStatus.FAIL
    assert ReasonCode.PAN_INVALID_FORMAT in result.reason_codes


def test_rejection_explains_it_was_the_holder_type_not_the_shape():
    """
    The reason code alone cannot distinguish a malformed PAN from a
    well-formed one with a bad holder type, which is what made this
    confusing in the first place.
    """
    check = result_for(
        pan_doc(pan="ABCDE1234P"), itr_doc(pan="ABCDE1234P")
    ).check(KycCheck.PAN)

    assert "holder-type" in check.detail
    assert "'D'" in check.detail
    assert "ABCDE1234P" in check.detail


def test_shape_failure_and_holder_type_failure_read_differently():
    shape = result_for(
        pan_doc(pan="NOTAPAN123"), itr_doc(pan="NOTAPAN123")
    ).check(KycCheck.PAN)
    holder = result_for(
        pan_doc(pan="ABCDE1234P"), itr_doc(pan="ABCDE1234P")
    ).check(KycCheck.PAN)

    assert "AAAAA9999A" in shape.detail
    assert "holder-type" in holder.detail
    assert shape.detail != holder.detail


@pytest.mark.parametrize("holder_type", list("ABCEFGHJLPT"))
def test_every_assigned_holder_type_is_accepted(holder_type):
    pan = f"ABC{holder_type}E1234P"
    result = result_for(pan_doc(pan=pan), itr_doc(pan=pan))
    assert status_of(result, KycCheck.PAN) is CheckStatus.PASS, pan


@pytest.mark.parametrize("holder_type", list("DIKMNOQRSUVWXYZ"))
def test_unassigned_holder_types_stay_rejected(holder_type):
    """Guards against anyone 'fixing' this by widening the accepted set."""
    pan = f"ABC{holder_type}E1234P"
    result = result_for(pan_doc(pan=pan), itr_doc(pan=pan))
    assert status_of(result, KycCheck.PAN) is CheckStatus.FAIL, pan


def test_kyc_defers_to_the_canonical_validator_rather_than_its_own_rules():
    """
    KYC must not carry a second copy of the PAN rules. If it did, this would
    drift from the Document Agent and the two would disagree about the same
    number.
    """
    from app.agents.document_agent.validate import validate_pan
    from app.agents.document_agent.schemas import ValidationStatus

    for pan in ("ABCDE1234P", "ABCPE1234P", "NOTAPAN123", "NUHPS4875K"):
        canonical = validate_pan(pan)[0] is ValidationStatus.VALID
        kyc_passed = status_of(
            result_for(pan_doc(pan=pan), itr_doc(pan=pan)), KycCheck.PAN
        ) is CheckStatus.PASS
        assert canonical == kyc_passed, pan


def test_valid_synthetic_pan_passes_end_to_end():
    """The corrected fixture value for the live PASS payload."""
    result = result_for(
        pan_doc(pan="ABCPE1234P"), dl_doc(), itr_doc(pan="ABCPE1234P"),
        salary_doc(), bank_doc(),
    )
    assert status_of(result, KycCheck.PAN) is CheckStatus.PASS
    assert result.status is CheckStatus.PASS


# ---------------------------------------------------------------------------
# Income
# ---------------------------------------------------------------------------


def test_consistent_salary_bank_and_itr_pass():
    # 80k/month -> 960k a year; bank inflow 85k/month -> 1.02m; ITR 960k.
    result = result_for(salary_doc("80000"), bank_doc("85000"), itr_doc("960000"))
    assert status_of(result, KycCheck.INCOME) is CheckStatus.PASS


def test_wildly_inconsistent_income_is_flagged():
    # 20k/month against a declared 5m a year is not the same applicant's income.
    result = result_for(salary_doc("20000"), itr_doc("5000000"))

    check = result.check(KycCheck.INCOME)
    assert check.status in (CheckStatus.REVIEW, CheckStatus.FAIL)
    assert ReasonCode.INCOME_INCONSISTENT in check.reason_codes


def test_income_is_non_blocking_so_it_cannot_reject_alone():
    """Bank credits are not salary; a gap is a review, not a rejection."""
    result = result_for(
        pan_doc(), dl_doc(), salary_doc("20000"), itr_doc("5000000"),
    )
    assert result.status is CheckStatus.REVIEW


def test_income_result_states_its_comparison_basis():
    result = result_for(salary_doc(), bank_doc(), itr_doc())
    check = result.check(KycCheck.INCOME)

    assert check.basis
    assert "NOT guaranteed salary" in check.basis


def test_income_from_one_document_is_not_a_cross_check():
    result = result_for(salary_doc())
    check = result.check(KycCheck.INCOME)

    assert check.status is CheckStatus.SKIPPED
    assert ReasonCode.INCOME_SINGLE_SOURCE in check.reason_codes


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_missing_optional_sources_do_not_crash():
    """A document carrying nothing but its type must be tolerated."""
    bare = SourceDocument(source_id="bare", document_type=KycDocumentType.VOTER_ID)
    result = run_kyc(KycRequest(documents=[bare]), request_id="t")

    assert result.status in (CheckStatus.REVIEW, CheckStatus.PASS, CheckStatus.FAIL)
    # Every check runs and every one has nothing to work with. Asserted as a
    # set rather than a count so adding a check does not break this test for
    # a reason that has nothing to do with what it is testing.
    assert {c.check for c in result.checks} == set(KycCheck)
    assert all(c.status is CheckStatus.SKIPPED for c in result.checks)


def test_a_single_document_cannot_pass_on_its_own():
    """With nothing to compare against, 'no disagreement' is not evidence."""
    result = run_kyc(KycRequest(documents=[pan_doc()]), request_id="t")

    assert result.status is CheckStatus.REVIEW
    assert ReasonCode.INSUFFICIENT_SOURCES in result.reason_codes


def test_every_non_pass_carries_reason_codes_and_evidence():
    result = result_for(pan_doc(name="A B C"), dl_doc(name="X Y Z"))

    assert result.status is not CheckStatus.PASS
    assert result.reason_codes
    for check in result.checks:
        if check.status in (CheckStatus.REVIEW, CheckStatus.FAIL):
            assert check.reason_codes, f"{check.check} gave no reason"
            assert check.evidence, f"{check.check} gave no evidence"


def test_result_is_json_serialisable():
    result = result_for(pan_doc(), dl_doc(), salary_doc(), bank_doc(), itr_doc())
    dumped = result.model_dump(mode="json")

    assert dumped["status"] == "PASS"
    assert {c["check"] for c in dumped["checks"]} == {k.value for k in KycCheck}

    # The field-level view serialises too, and carries the two numbers that
    # must never be confused with each other.
    assert dumped["fields"], "no field-level results were serialised"
    for row in dumped["fields"]:
        assert {"field", "status", "match_score", "confidence",
                "reason_code", "reason", "sources"} <= set(row)
    assert isinstance(dumped["overall_score"], int)
    assert isinstance(dumped["overall_confidence"], int)


def test_configuration_reports_thresholds_not_hardcoded_values():
    config = configuration()

    assert config["policy_version"]
    assert config["checks"]["name"]["thresholds"]["match_threshold"]
    assert config["authenticity_checked"] is False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def test_kyc_runs_through_the_orchestrator():
    """/api/v1/agents/execute must be able to reach the KYC agent."""
    import app.orchestration.registry as registry
    from app.orchestration.graph import run_agent

    assert registry.is_registered("kyc_agent")

    state = await run_agent(
        agent_id="kyc_agent",
        payload={
            "documents": [
                {
                    "source_id": "pan", "document_type": "PAN",
                    "name": "RISHABH AJIT SINGH", "date_of_birth": "2002-06-12",
                    "pan": "NUHPS4875K",
                },
                {
                    "source_id": "dl", "document_type": "DRIVING_LICENCE",
                    "name": "RISHABH AJIT SINGH", "date_of_birth": "2002-06-12",
                },
            ]
        },
        request_id="orchestrated-kyc",
    )

    assert state.get("status") in ("SUCCESS", "success", "COMPLETED", "completed")
    assert state.get("result", {}).get("status") == "PASS"


def test_registering_kyc_did_not_displace_the_existing_agents():
    import app.orchestration.registry as registry

    for agent_id in ("document_agent", "fraud_risk_agent", "bank_statement_agent"):
        assert registry.is_registered(agent_id)


def test_a_blocking_check_still_fails_the_verdict(monkeypatch):
    """
    Policy keeps the power to block; only the default changed.

    Marking the name check blocking restores FAIL as the overall verdict,
    which is how a genuine business or risk rule is expressed.
    """
    from app.agents.kyc import config as kyc_config

    real = kyc_config.check_blocking
    monkeypatch.setattr(
        kyc_config, "check_blocking", lambda name: name == "name" or real(name)
    )

    result = result_for(pan_doc(name="RISHABH AJIT SINGH"),
                        dl_doc(name="SURESH KUMAR VERMA"))

    assert status_of(result, KycCheck.NAME) is CheckStatus.FAIL
    assert result.status is CheckStatus.FAIL
