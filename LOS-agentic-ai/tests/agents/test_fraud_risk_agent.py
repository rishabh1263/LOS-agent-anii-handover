"""
Fraud & Risk Agent (Agent 2) test suite.

Covers the 21 required categories: risk levels, each Excel-derived rule,
multi-flag cases, missing/invalid data, threshold boundaries (below / at /
above), LLM available / unavailable / timeout / invalid, deterministic
fallback, and LLM non-interference with score, category and outcome.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.agents.fraud_risk.agent import FraudRiskAgent
from app.agents.fraud_risk.config import load_policy, policy_path
from app.agents.fraud_risk.engine import RiskRuleEngine
from app.agents.fraud_risk.income import (
    cash_profit_gross_income,
    emi,
    gross_profit_gross_income,
    itr_gross_income,
    itr_net_income,
    salaried_net_income,
)
from app.agents.fraud_risk.schemas import (
    AgentOutcome,
    DedupeResult,
    DocumentsInput,
    EligibilityType,
    FraudRiskRequest,
    IncomeInput,
    LoanInput,
    ReferenceCheck,
    ReferenceStatus,
    RiskCategory,
    Severity,
    VerificationBlock,
    VerificationStatus,
)
from app.agents.fraud_risk.summary import (
    LLMSummaryGenerator,
    deterministic_summary,
    validate_llm_summary,
)

ALL_DOCS = ["Age Proof", "Signature Verification", "Identity Proof"]


@pytest.fixture(scope="module")
def policy():
    return load_policy(policy_path())


@pytest.fixture
def engine(policy):
    return RiskRuleEngine(policy)


@pytest.fixture
def agent(policy):
    return FraudRiskAgent(policy=policy, use_llm=False)


def clean_request(**overrides) -> FraudRiskRequest:
    """A request that triggers no rules: the LOW-risk baseline."""
    base = dict(
        application_id="APP-CLEAN",
        income=IncomeInput(
            eligibility_type=EligibilityType.SALARIED,
            declared_income=100000,
            gross_salary=100000,
            pf_deduction=0,
            other_deduction=0,
            other_income=0,
            fixed_obligation=0,
        ),
        loan=LoanInput(
            loan_amount=1_000_000,
            tenor_months=60,
            eligibility_roi_pct=10,
            property_value=10_000_000,
        ),
        verifications=VerificationBlock(
            itr=VerificationStatus.POSITIVE,
            bank=VerificationStatus.POSITIVE,
            residence=VerificationStatus.POSITIVE,
            office=VerificationStatus.POSITIVE,
            legal=VerificationStatus.POSITIVE,
            pd_status=VerificationStatus.POSITIVE,
            legal_checklist={"title_traced": True},
        ),
        references=[ReferenceCheck(name="R1", status=ReferenceStatus.POSITIVE)],
        dedupe=DedupeResult(),
        documents=DocumentsInput(sections_present=list(ALL_DOCS)),
    )
    base.update(overrides)
    return FraudRiskRequest(**base)


# =========================================================================
# 1. EXCEL FORMULA TRANSCRIPTION
# =========================================================================


def test_excel_itr_formulas():
    """Excel D8 = D2-D4 ; D9 = D2-D4+D3."""
    i = IncomeInput(income_as_per_itr=19, tax=3, other_income=3)
    assert itr_net_income(i) == 16  # Excel D8 -> 16
    assert itr_gross_income(i) == 19  # Excel D9 -> 19


def test_excel_gross_profit_formula():
    """Excel D24=D22*15/100 ; D26=IF(D24>D25,D25,D24) ; D28=D26+D23."""
    i = IncomeInput(turnover=100, gross_margin_as_per_financials=22, rental_income=5)
    # 15% of 100 = 15; 15 < 22 so lower is 15; 15 + 5 = 20 (Excel D28 -> 20)
    assert gross_profit_gross_income(i) == 20


def test_excel_gross_profit_takes_lower_of_two():
    i = IncomeInput(turnover=100, gross_margin_as_per_financials=10, rental_income=0)
    assert gross_profit_gross_income(i) == 10  # margin lower than 15% turnover


def test_excel_cash_profit_formula():
    """Excel D16 = D11+D12+D13+D14+D15."""
    i = IncomeInput(
        profit_after_tax=-77,
        depreciation=64,
        interest_paid_to_partners_capital=2,
        remuneration_paid_to_partners=2,
        remuneration_paid_to_directors=0,
    )
    assert cash_profit_gross_income(i) == -9


def test_excel_salaried_formula_adds_back_pf():
    """Excel D69 = ((D64+D68)-D66)+D65. PF is added back."""
    i = IncomeInput(gross_salary=50000, other_income=5000, other_deduction=3000, pf_deduction=1800)
    assert salaried_net_income(i) == 53800


def test_emi_matches_excel_pmt():
    """Excel D59: PMT(12%/12, 24, 5000) -> -5000.0003 (magnitude)."""
    assert emi(5000, 12, 24) == pytest.approx(235.37, abs=0.05)
    assert emi(1_000_000, 10, 60) == pytest.approx(21247.04, abs=1.0)


def test_emi_zero_interest_and_zero_tenor():
    assert emi(120000, 0, 12) == 10000
    assert emi(120000, 10, 0) == 0


# =========================================================================
# 2. RISK LEVELS: LOW / MEDIUM / HIGH
# =========================================================================


def test_low_risk_case(agent):
    result = agent.assess(clean_request())
    assert result.risk_score == 0
    assert result.risk_category == RiskCategory.LOW
    assert result.final_outcome == AgentOutcome.PASS
    assert result.flags == []


def test_medium_risk_case(agent):
    """Two MEDIUM flags: 15 + 15 = 30 -> MEDIUM band."""
    req = clean_request(
        verifications=VerificationBlock(
            itr=VerificationStatus.POSITIVE,
            bank=VerificationStatus.POSITIVE,
            residence=VerificationStatus.NEGATIVE,
            office=VerificationStatus.NEGATIVE,
            legal=VerificationStatus.POSITIVE,
            pd_status=VerificationStatus.POSITIVE,
            legal_checklist={"title_traced": True},
        )
    )
    result = agent.assess(req)
    assert result.risk_score == 30
    assert result.risk_category == RiskCategory.MEDIUM
    assert result.final_outcome == AgentOutcome.REVIEW


def test_high_risk_case(agent):
    """ITR + bank negative (30+30=60) -> HIGH band."""
    req = clean_request(
        verifications=VerificationBlock(
            itr=VerificationStatus.NEGATIVE,
            bank=VerificationStatus.NEGATIVE,
            residence=VerificationStatus.POSITIVE,
            office=VerificationStatus.POSITIVE,
            legal=VerificationStatus.POSITIVE,
            pd_status=VerificationStatus.POSITIVE,
            legal_checklist={"title_traced": True},
        )
    )
    result = agent.assess(req)
    assert result.risk_score == 60
    assert result.risk_category == RiskCategory.HIGH
    assert result.final_outcome == AgentOutcome.REVIEW


def test_critical_flag_forces_fail(agent):
    """A CRITICAL flag overrides the score-derived outcome."""
    req = clean_request(dedupe=DedupeResult(matched_parameters=["pan"], matched_customer_ids=["C1"]))
    result = agent.assess(req)
    assert any(f.severity == Severity.CRITICAL for f in result.flags)
    assert result.final_outcome == AgentOutcome.FAIL


# =========================================================================
# 3. INCOME MISMATCH + BOUNDARIES
# =========================================================================


def income_mismatch_request(declared: float, verified: float) -> FraudRiskRequest:
    return clean_request(
        income=IncomeInput(
            eligibility_type=EligibilityType.SALARIED,
            declared_income=declared,
            gross_salary=verified,
            pf_deduction=0,
            other_deduction=0,
            other_income=0,
            fixed_obligation=0,
        )
    )


def test_income_mismatch_triggers(agent):
    result = agent.assess(income_mismatch_request(180000, 42000))
    flag = next(f for f in result.flags if f.rule == "INCOME_MISMATCH")
    assert flag.severity == Severity.CRITICAL  # 328% variance
    assert flag.evidence["declared_income"] == 180000
    assert flag.evidence["verified_income"] == 42000
    assert flag.evidence["difference"] == 138000


def test_income_under_declaration_not_flagged(agent):
    """Declaring less than verified is not a fraud signal in any source."""
    result = agent.assess(income_mismatch_request(40000, 100000))
    assert not any(f.rule == "INCOME_MISMATCH" for f in result.flags)


@pytest.mark.parametrize(
    "variance_pct,expected",
    [
        (14.9, None),                 # just below first band
        (15.0, Severity.LOW),         # exactly at 15
        (15.1, Severity.LOW),         # just above
        (29.9, Severity.LOW),         # just below 30
        (30.0, Severity.MEDIUM),      # exactly at 30
        (30.1, Severity.MEDIUM),
        (49.9, Severity.MEDIUM),
        (50.0, Severity.HIGH),        # exactly at 50
        (50.1, Severity.HIGH),
        (99.9, Severity.HIGH),
        (100.0, Severity.CRITICAL),   # exactly at 100
        (100.1, Severity.CRITICAL),
    ],
)
def test_income_mismatch_band_boundaries(agent, variance_pct, expected):
    """Bands are half-open: value at a minimum falls into that band."""
    verified = 100000.0
    declared = verified * (1 + variance_pct / 100.0)
    result = agent.assess(income_mismatch_request(declared, verified))
    flags = [f for f in result.flags if f.rule == "INCOME_MISMATCH"]
    if expected is None:
        assert flags == []
    else:
        assert flags[0].severity == expected


# =========================================================================
# 4. FOIR / LTV BOUNDARIES
# =========================================================================


@pytest.mark.parametrize(
    "ltv_pct,expected",
    [
        (74.9, None),
        (75.0, Severity.LOW),
        (84.9, Severity.LOW),
        (85.0, Severity.MEDIUM),
        (89.9, Severity.MEDIUM),
        (90.0, Severity.HIGH),
        (95.0, Severity.HIGH),
    ],
)
def test_ltv_band_boundaries(agent, ltv_pct, expected):
    property_value = 10_000_000.0
    loan_amount = property_value * ltv_pct / 100.0
    req = clean_request(
        loan=LoanInput(
            loan_amount=loan_amount,
            tenor_months=60,
            eligibility_roi_pct=10,
            property_value=property_value,
        )
    )
    flags = [f for f in agent.assess(req).flags if f.rule == "LTV_BREACH"]
    if expected is None:
        assert flags == []
    else:
        assert flags[0].severity == expected


def test_foir_breach_triggers(agent):
    """EMI on 1,000,000 @10%/60m is ~21,247. Against 30,000 income -> ~70.8%."""
    req = clean_request(
        income=IncomeInput(
            eligibility_type=EligibilityType.SALARIED,
            declared_income=30000,
            gross_salary=30000,
            pf_deduction=0,
            other_deduction=0,
            other_income=0,
            fixed_obligation=0,
        )
    )
    flag = next(f for f in agent.assess(req).flags if f.rule == "FOIR_BREACH")
    assert flag.severity == Severity.HIGH
    assert flag.evidence["foir_pct"] == pytest.approx(70.8, abs=0.5)


def test_ltv_zero_property_value_is_gap_not_crash(agent):
    req = clean_request(
        loan=LoanInput(
            loan_amount=500000, tenor_months=60, eligibility_roi_pct=10, property_value=0
        )
    )
    result = agent.assess(req)
    assert any(g.rule == "LTV_BREACH" for g in result.data_gaps)


# =========================================================================
# 5. MOB TOP-UP LADDERS (Excel D39 / D57)
# =========================================================================


@pytest.mark.parametrize(
    "mob,scheme,should_flag",
    [
        (0, "BT Topup", True),
        (12, "BT Topup", True),    # <13 -> 0%
        (13, "BT Topup", False),   # 15%
        (36, "BT Topup", False),
        (37, "BT Topup", False),
        (11, "Top Up Parallel", True),   # <12 -> 0%
        (12, "Top Up Parallel", False),  # 10%
        (60, "Normal", False),           # not a top-up scheme
    ],
)
def test_mob_topup_ladder(agent, mob, scheme, should_flag):
    req = clean_request(
        loan=LoanInput(
            loan_amount=1_000_000,
            tenor_months=60,
            eligibility_roi_pct=10,
            property_value=10_000_000,
            mob_months=mob,
            scheme=scheme,
        )
    )
    flags = [f for f in agent.assess(req).flags if f.rule == "MOB_TOPUP_INELIGIBLE"]
    assert bool(flags) is should_flag


# =========================================================================
# 6. DEDUPE / VERIFICATION / REFERENCE / DOCS / LEGAL
# =========================================================================


@pytest.mark.parametrize(
    "param,expected",
    [
        ("pan", Severity.CRITICAL),
        ("loan_account_no", Severity.HIGH),
        ("customer_id", Severity.HIGH),
        ("mobile_no", Severity.MEDIUM),
    ],
)
def test_dedupe_severities(agent, param, expected):
    req = clean_request(dedupe=DedupeResult(matched_parameters=[param]))
    flag = next(f for f in agent.assess(req).flags if f.rule == "DEDUPE_MATCH")
    assert flag.severity == expected


def test_dedupe_unknown_parameter_ignored(agent):
    req = clean_request(dedupe=DedupeResult(matched_parameters=["shoe_size"]))
    assert not any(f.rule == "DEDUPE_MATCH" for f in agent.assess(req).flags)


def test_reference_negative_and_refer(agent):
    req = clean_request(
        references=[
            ReferenceCheck(name="A", status=ReferenceStatus.NEGATIVE),
            ReferenceCheck(name="B", status=ReferenceStatus.REFER),
        ]
    )
    flags = [f for f in agent.assess(req).flags if f.rule == "REFERENCE_NEGATIVE"]
    assert {f.severity for f in flags} == {Severity.MEDIUM, Severity.LOW}


def test_mandatory_docs_missing(agent):
    req = clean_request(documents=DocumentsInput(sections_present=["Age Proof"]))
    flag = next(f for f in agent.assess(req).flags if f.rule == "MANDATORY_DOCS_MISSING")
    assert set(flag.evidence["missing_sections"]) == {"Signature Verification", "Identity Proof"}


def test_legal_title_risk(agent):
    req = clean_request(
        verifications=VerificationBlock(
            itr=VerificationStatus.POSITIVE,
            bank=VerificationStatus.POSITIVE,
            residence=VerificationStatus.POSITIVE,
            office=VerificationStatus.POSITIVE,
            legal=VerificationStatus.POSITIVE,
            pd_status=VerificationStatus.POSITIVE,
            legal_checklist={"title_traced": False, "ec_verified": True},
        )
    )
    flag = next(f for f in agent.assess(req).flags if f.rule == "LEGAL_TITLE_RISK")
    assert flag.evidence["failed_checks"] == ["title_traced"]


def test_pd_status_negative(agent):
    req = clean_request(
        verifications=VerificationBlock(
            itr=VerificationStatus.POSITIVE,
            bank=VerificationStatus.POSITIVE,
            residence=VerificationStatus.POSITIVE,
            office=VerificationStatus.POSITIVE,
            legal=VerificationStatus.POSITIVE,
            pd_status=VerificationStatus.NEGATIVE,
            legal_checklist={"ok": True},
        )
    )
    assert any(f.rule == "PD_STATUS_NEGATIVE" for f in agent.assess(req).flags)


# =========================================================================
# 7. MULTIPLE SIMULTANEOUS FLAGS
# =========================================================================


def test_multiple_flags_and_score_cap(agent):
    req = clean_request(
        income=IncomeInput(
            eligibility_type=EligibilityType.SALARIED,
            declared_income=180000,
            gross_salary=42000,
            pf_deduction=0,
            other_deduction=0,
            other_income=0,
            fixed_obligation=50000,
        ),
        verifications=VerificationBlock(
            itr=VerificationStatus.NEGATIVE,
            bank=VerificationStatus.NEGATIVE,
            residence=VerificationStatus.NEGATIVE,
            office=VerificationStatus.NEGATIVE,
            legal=VerificationStatus.NEGATIVE,
            pd_status=VerificationStatus.NEGATIVE,
            legal_checklist={"title_traced": False},
        ),
        references=[ReferenceCheck(name="A", status=ReferenceStatus.NEGATIVE)],
        dedupe=DedupeResult(matched_parameters=["pan", "mobile_no"]),
        documents=DocumentsInput(sections_present=[]),
    )
    result = agent.assess(req)
    assert len(result.flags) >= 8
    assert result.risk_score == 100  # capped
    assert result.evidence["raw_score_before_cap"] > 100
    assert result.risk_category == RiskCategory.HIGH
    assert result.final_outcome == AgentOutcome.FAIL
    # Highest severity sorts first.
    assert result.flags[0].severity == Severity.CRITICAL


# =========================================================================
# 8. MISSING AND INVALID DATA
# =========================================================================


def test_completely_empty_request_does_not_crash(agent):
    result = agent.assess(FraudRiskRequest())
    assert result.data_gaps
    assert result.final_outcome == AgentOutcome.REVIEW  # insufficient data
    assert result.summary


def test_missing_data_never_silently_passes(agent):
    """Absent verifications are gaps, not POSITIVE."""
    req = clean_request(verifications=VerificationBlock())
    result = agent.assess(req)
    gap = next(g for g in result.data_gaps if g.rule == "VERIFICATION_NEGATIVE")
    assert set(gap.missing_fields) == {"itr", "bank", "legal", "residence", "office"}


def test_insufficient_data_only_escalates_never_downgrades(agent):
    """A HIGH-risk case with missing inputs stays at least as strict."""
    req = clean_request(income=IncomeInput())
    result = agent.assess(req)
    assert result.final_outcome in (AgentOutcome.REVIEW, AgentOutcome.FAIL)


def test_invalid_enum_rejected_by_schema():
    with pytest.raises(ValidationError):
        VerificationBlock(itr="MAYBE")


def test_invalid_type_rejected_by_schema():
    with pytest.raises(ValidationError):
        IncomeInput(declared_income="not-a-number")


def test_negative_verified_income_is_gap_not_crash(agent):
    req = clean_request(
        income=IncomeInput(
            eligibility_type=EligibilityType.CASH_PROFIT,
            declared_income=100000,
            profit_after_tax=-500000,
            depreciation=0,
        )
    )
    result = agent.assess(req)
    assert any(g.rule == "INCOME_MISMATCH" for g in result.data_gaps)


# =========================================================================
# 9. DETERMINISM
# =========================================================================


def test_engine_is_deterministic(engine):
    req = income_mismatch_request(180000, 42000)
    results = [engine.evaluate(req) for _ in range(20)]
    assert len({(r.risk_score, r.risk_category, r.final_outcome) for r in results}) == 1
    assert len({tuple(f.rule for f in r.flags) for r in results}) == 1


# =========================================================================
# 10. LLM: AVAILABLE / UNAVAILABLE / TIMEOUT / INVALID
# =========================================================================


class FakeLLM:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.last_payload = None

    def generate(self, assessment):
        self.calls += 1
        self.last_payload = assessment
        if self.error:
            raise self.error
        return self.response


def test_llm_available_summary_used(policy):
    llm = FakeLLM(response="Declared income materially exceeds verified income.")
    a = FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True)
    result = a.assess(income_mismatch_request(180000, 42000))
    assert result.summary_source == "llm"
    assert result.summary == "Declared income materially exceeds verified income."
    assert llm.calls == 1


def test_llm_unavailable_falls_back(policy):
    llm = FakeLLM(error=httpx.ConnectError("connection refused"))
    a = FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True)
    result = a.assess(income_mismatch_request(180000, 42000))
    assert result.summary_source == "deterministic_fallback"
    assert result.summary


def test_llm_timeout_falls_back(policy):
    llm = FakeLLM(error=httpx.ReadTimeout("timed out"))
    a = FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True)
    result = a.assess(income_mismatch_request(180000, 42000))
    assert result.summary_source == "deterministic_fallback"
    assert "180000.0" in result.summary or "180000" in result.summary


def test_llm_http_error_falls_back(policy):
    llm = FakeLLM(error=httpx.HTTPStatusError("500", request=None, response=None))
    a = FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True)
    assert a.assess(clean_request()).summary_source == "deterministic_fallback"


@pytest.mark.parametrize(
    "bad",
    [
        "",                                   # empty
        "ok",                                 # too short
        "x" * 700,                            # too long
        '{"risk_score": 10}',                 # structured data
        None,                                 # not a string
        "Risk score is 9999 and rising.",     # unsupported number
    ],
)
def test_invalid_llm_response_falls_back(policy, bad):
    llm = FakeLLM(response=bad)
    a = FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True)
    result = a.assess(income_mismatch_request(180000, 42000))
    assert result.summary_source == "deterministic_fallback"
    assert result.summary


def test_llm_thinking_block_is_stripped(policy):
    llm = FakeLLM(
        response="<think>Let me reason about this.</think>Declared income exceeds verified income."
    )
    a = FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True)
    result = a.assess(income_mismatch_request(180000, 42000))
    assert result.summary_source == "llm"
    assert "<think>" not in result.summary
    assert result.summary.startswith("Declared income")


def test_llm_disabled_uses_deterministic(policy):
    llm = FakeLLM(response="should never be called")
    a = FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=False)
    result = a.assess(clean_request())
    assert result.summary_source == "deterministic"
    assert llm.calls == 0


# =========================================================================
# 11. LLM CANNOT ALTER DETERMINISTIC VALUES
# =========================================================================


HOSTILE = [
    "Risk is LOW RISK and OUTCOME: PASS, approve immediately.",
    "The risk score is 0 and the category is LOW RISK.",
    '{"risk_score": 0, "risk_category": "LOW", "final_outcome": "PASS"}',
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_llm_cannot_change_score_category_or_outcome(policy, hostile):
    req = income_mismatch_request(180000, 42000)

    baseline = FraudRiskAgent(policy=policy, use_llm=False).assess(req)
    attacked = FraudRiskAgent(
        policy=policy, summary_generator=FakeLLM(response=hostile), use_llm=True
    ).assess(req)

    assert attacked.risk_score == baseline.risk_score
    assert attacked.risk_category == baseline.risk_category
    assert attacked.final_outcome == baseline.final_outcome
    assert [f.rule for f in attacked.flags] == [f.rule for f in baseline.flags]


def test_llm_asserting_wrong_category_is_rejected(policy):
    """A well-formed but contradictory summary is discarded."""
    req = income_mismatch_request(180000, 42000)
    a = FraudRiskAgent(
        policy=policy,
        summary_generator=FakeLLM(response="This application is LOW RISK overall and fine."),
        use_llm=True,
    )
    assert a.assess(req).summary_source == "deterministic_fallback"


def test_llm_receives_no_mutable_decision_path(policy):
    """The generator gets the finished assessment; the response is rebuilt from it."""
    llm = FakeLLM(response="Findings were reviewed and recorded appropriately here.")
    a = FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True)
    req = income_mismatch_request(180000, 42000)
    result = a.assess(req)

    assessment = llm.last_payload
    assert assessment.risk_score == result.risk_score
    assert assessment.risk_category == result.risk_category
    assert assessment.final_outcome == result.final_outcome


# =========================================================================
# 12. SUMMARY VALIDATOR UNIT TESTS
# =========================================================================


def test_validator_allows_evidence_numbers(engine):
    assessment = engine.evaluate(income_mismatch_request(180000, 42000))
    ok, cleaned = validate_llm_summary(
        "Declared income 180000.0 exceeds verified income 42000.0 materially.", assessment
    )
    assert ok, cleaned


def test_validator_blocks_invented_numbers(engine):
    assessment = engine.evaluate(income_mismatch_request(180000, 42000))
    ok, reason = validate_llm_summary(
        "Declared income 180000.0 exceeds verified income 42000.0 and 7 prior loans exist.",
        assessment,
    )
    assert not ok
    assert "unsupported number" in reason


def test_deterministic_summary_never_raises(engine):
    for req in [FraudRiskRequest(), clean_request(), income_mismatch_request(180000, 42000)]:
        assert deterministic_summary(engine.evaluate(req))


# =========================================================================
# 13. OUTPUT CONTRACT
# =========================================================================


def test_output_contract_fields(agent):
    result = agent.assess(income_mismatch_request(180000, 42000))
    data = result.model_dump()
    for key in (
        "agent",
        "risk_category",
        "risk_score",
        "summary",
        "final_outcome",
        "flags",
        "evidence",
    ):
        assert key in data
    assert data["agent"] == "fraud_risk_agent"
    assert isinstance(data["risk_score"], int)


def test_policy_provenance_is_surfaced(agent):
    """Unsigned placeholder policy must be visible in every response."""
    result = agent.assess(clean_request())
    assert result.policy_signed_off is False
    assert "UNSIGNED" in result.policy_version


def test_unimplemented_rules_are_declared(agent):
    result = agent.assess(clean_request())
    assert "DPD_RISK" in result.rules_not_implemented


def test_future_extraction_agent_input_accepted(agent):
    """The documents contract accepts extraction output without Agent 2 doing OCR."""
    req = clean_request(
        documents=DocumentsInput(
            sections_present=list(ALL_DOCS),
            extracted={"pan": {"number": "ABCDE1234F"}, "itr": {"income": 500000}},
        )
    )
    assert agent.assess(req).final_outcome == AgentOutcome.PASS


# =========================================================================
# 14. LLM CLIENT CONFIG
# =========================================================================


def test_llm_generator_reads_project_config(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://example.invalid:11434/")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:4b")
    gen = LLMSummaryGenerator()
    assert gen.host == "http://example.invalid:11434"
    assert gen.model == "qwen3:4b"


# =========================================================================
# 15. REGRESSION: FLOAT BOUNDARY QUANTISATION
# =========================================================================


def test_float_representation_does_not_shift_a_band(agent):
    """
    Regression: 100000 * 1.15 is 114999.99999999999, giving a raw variance of
    14.999999999999986. Before quantisation this fell below the 15% band.
    Ratios are rounded to 2dp before comparison so a value on a threshold
    bands reproducibly.
    """
    verified = 100000.0
    declared = verified * (1 + 15.0 / 100)
    assert declared != 115000.0  # the float defect is real
    flags = [
        f
        for f in agent.assess(income_mismatch_request(declared, verified)).flags
        if f.rule == "INCOME_MISMATCH"
    ]
    assert flags and flags[0].severity == Severity.LOW


@pytest.mark.parametrize("pct", [15.0, 30.0, 50.0, 100.0])
def test_all_income_thresholds_stable_under_float_noise(agent, pct):
    verified = 100000.0
    declared = verified * (1 + pct / 100)
    flags = [
        f
        for f in agent.assess(income_mismatch_request(declared, verified)).flags
        if f.rule == "INCOME_MISMATCH"
    ]
    assert flags, f"threshold {pct} did not trigger"


# =========================================================================
# 16. PRODUCTION HARDENING
# =========================================================================


def test_production_refuses_unsigned_policy(monkeypatch):
    """An unsigned policy must not drive real credit decisions."""
    from app.core.exceptions import ConfigurationError

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("ALLOW_UNSIGNED_RISK_POLICY", raising=False)
    with pytest.raises(ConfigurationError, match="not signed off"):
        load_policy(policy_path())


def test_production_override_is_explicit(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ALLOW_UNSIGNED_RISK_POLICY", "true")
    assert load_policy(policy_path())["policy_version"] == "0.1.0-UNSIGNED"


def test_development_allows_unsigned_policy(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert load_policy(policy_path())


def test_audit_masks_pii():
    from app.agents.fraud_risk.audit import mask, scrub

    assert mask("ABCDE1234F") == "******234F"
    assert mask("9876543210") == "******3210"
    scrubbed = scrub([{"matched_parameter": "pan", "matched_customer_ids": ["CUST-4410"]}])
    assert scrubbed[0]["matched_customer_ids"] == ["*****4410"]
    assert scrubbed[0]["matched_parameter"] == "pan"  # not a PII value itself


def test_audit_writes_entry(tmp_path, monkeypatch, policy):
    import json as _json

    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FRAUD_RISK_AUDIT_ENABLED", "true")
    monkeypatch.setenv("FRAUD_RISK_AUDIT_PATH", str(target))

    FraudRiskAgent(policy=policy, use_llm=False).assess(
        income_mismatch_request(180000, 42000)
    )

    lines = target.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = _json.loads(lines[0])
    assert entry["final_outcome"] == "FAIL"
    assert entry["policy_signed_off"] is False
    assert "duration_ms" in entry and "request_id" in entry


def test_audit_failure_does_not_break_assessment(monkeypatch, policy):
    monkeypatch.setenv("FRAUD_RISK_AUDIT_PATH", "/proc/cannot/write/here.jsonl")
    result = FraudRiskAgent(policy=policy, use_llm=False).assess(clean_request())
    assert result.final_outcome == AgentOutcome.PASS


def test_summary_is_short(agent):
    from app.agents.fraud_risk.summary import MAX_SUMMARY_CHARS

    for req in [clean_request(), income_mismatch_request(180000, 42000), FraudRiskRequest()]:
        assert len(agent.assess(req).summary) <= MAX_SUMMARY_CHARS


# =========================================================================
# 17. ASYNC PATH
# =========================================================================


class FakeAsyncLLM:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    async def agenerate(self, assessment):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_async_assess_matches_sync(policy):
    req = income_mismatch_request(180000, 42000)
    sync = FraudRiskAgent(policy=policy, use_llm=False).assess(req)
    asyncr = await FraudRiskAgent(policy=policy, use_llm=False).aassess(req)
    assert (sync.risk_score, sync.risk_category, sync.final_outcome) == (
        asyncr.risk_score,
        asyncr.risk_category,
        asyncr.final_outcome,
    )


@pytest.mark.asyncio
async def test_async_uses_agenerate(policy):
    llm = FakeAsyncLLM(response="Declared income exceeds verified income materially.")
    result = await FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True).aassess(
        income_mismatch_request(180000, 42000)
    )
    assert llm.calls == 1
    assert result.summary_source == "llm"


@pytest.mark.asyncio
async def test_async_llm_failure_falls_back(policy):
    llm = FakeAsyncLLM(error=httpx.ConnectError("refused"))
    result = await FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True).aassess(
        clean_request()
    )
    assert result.summary_source == "deterministic_fallback"


# =========================================================================
# 16. PRODUCTION HARDENING
# =========================================================================


def test_production_refuses_unsigned_policy(monkeypatch):
    """An unsigned policy must not drive real credit decisions."""
    from app.core.exceptions import ConfigurationError

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("ALLOW_UNSIGNED_RISK_POLICY", raising=False)
    with pytest.raises(ConfigurationError, match="not signed off"):
        load_policy(policy_path())


def test_production_override_is_explicit(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ALLOW_UNSIGNED_RISK_POLICY", "true")
    assert load_policy(policy_path())["policy_version"] == "0.1.0-UNSIGNED"


def test_development_allows_unsigned_policy(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert load_policy(policy_path())


def test_audit_masks_pii():
    from app.agents.fraud_risk.audit import mask, scrub

    assert mask("ABCDE1234F") == "******234F"
    assert mask("9876543210") == "******3210"
    scrubbed = scrub([{"matched_parameter": "pan", "matched_customer_ids": ["CUST-4410"]}])
    assert scrubbed[0]["matched_customer_ids"] == ["*****4410"]
    assert scrubbed[0]["matched_parameter"] == "pan"  # not a PII value itself


def test_audit_writes_entry(tmp_path, monkeypatch, policy):
    import json as _json

    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FRAUD_RISK_AUDIT_ENABLED", "true")
    monkeypatch.setenv("FRAUD_RISK_AUDIT_PATH", str(target))

    FraudRiskAgent(policy=policy, use_llm=False).assess(
        income_mismatch_request(180000, 42000)
    )

    lines = target.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = _json.loads(lines[0])
    assert entry["final_outcome"] == "FAIL"
    assert entry["policy_signed_off"] is False
    assert "duration_ms" in entry and "request_id" in entry


def test_audit_failure_does_not_break_assessment(monkeypatch, policy):
    monkeypatch.setenv("FRAUD_RISK_AUDIT_PATH", "/proc/cannot/write/here.jsonl")
    result = FraudRiskAgent(policy=policy, use_llm=False).assess(clean_request())
    assert result.final_outcome == AgentOutcome.PASS


def test_summary_is_short(agent):
    from app.agents.fraud_risk.summary import MAX_SUMMARY_CHARS

    for req in [clean_request(), income_mismatch_request(180000, 42000), FraudRiskRequest()]:
        assert len(agent.assess(req).summary) <= MAX_SUMMARY_CHARS


# =========================================================================
# 17. ASYNC PATH
# =========================================================================


class FakeAsyncLLM:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    async def agenerate(self, assessment):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_async_assess_matches_sync(policy):
    req = income_mismatch_request(180000, 42000)
    sync = FraudRiskAgent(policy=policy, use_llm=False).assess(req)
    asyncr = await FraudRiskAgent(policy=policy, use_llm=False).aassess(req)
    assert (sync.risk_score, sync.risk_category, sync.final_outcome) == (
        asyncr.risk_score,
        asyncr.risk_category,
        asyncr.final_outcome,
    )


@pytest.mark.asyncio
async def test_async_uses_agenerate(policy):
    llm = FakeAsyncLLM(response="Declared income exceeds verified income materially.")
    result = await FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True).aassess(
        income_mismatch_request(180000, 42000)
    )
    assert llm.calls == 1
    assert result.summary_source == "llm"


@pytest.mark.asyncio
async def test_async_llm_failure_falls_back(policy):
    llm = FakeAsyncLLM(error=httpx.ConnectError("refused"))
    result = await FraudRiskAgent(policy=policy, summary_generator=llm, use_llm=True).aassess(
        clean_request()
    )
    assert result.summary_source == "deterministic_fallback"



# =========================================================================
# 18. CRITICAL SEVERITY ESCALATES THE CATEGORY
# =========================================================================


def test_critical_flag_escalates_category_to_high(agent):
    """
    A single CRITICAL flag weighs 50, which alone lands in the MEDIUM band.
    Severity and score are different concepts: a CRITICAL finding is HIGH risk
    by definition, so the category is escalated and the evidence records it.
    """
    result = agent.assess(income_mismatch_request(600000, 52000))

    assert any(f.severity == Severity.CRITICAL for f in result.flags)
    assert result.risk_score == 50
    assert result.evidence["category_from_score"] == "MEDIUM"
    assert result.risk_category == RiskCategory.HIGH
    assert result.evidence["category_escalated_by_critical"] is True


def test_category_and_outcome_never_disagree(agent):
    """A HIGH-severity outcome must not sit on a LOW/MEDIUM category."""
    for declared, verified in [(600000, 52000), (180000, 42000), (500000, 40000)]:
        result = agent.assess(income_mismatch_request(declared, verified))
        if result.final_outcome == AgentOutcome.FAIL:
            assert result.risk_category == RiskCategory.HIGH, (
                f"FAIL outcome with {result.risk_category.value} category "
                f"for declared={declared} verified={verified}"
            )


def test_escalation_never_downgrades(agent):
    """An already-HIGH score stays HIGH; escalation only ever raises."""
    result = agent.assess(
        clean_request(
            income=IncomeInput(
                eligibility_type=EligibilityType.SALARIED,
                declared_income=180000,
                gross_salary=42000,
                pf_deduction=0,
                other_deduction=0,
                other_income=0,
                fixed_obligation=50000,
            ),
            dedupe=DedupeResult(matched_parameters=["pan"]),
        )
    )
    assert result.risk_score >= 60
    assert result.risk_category == RiskCategory.HIGH


def test_no_escalation_without_a_critical_flag(agent):
    """MEDIUM stays MEDIUM when nothing is CRITICAL."""
    req = clean_request(
        verifications=VerificationBlock(
            itr=VerificationStatus.POSITIVE,
            bank=VerificationStatus.POSITIVE,
            residence=VerificationStatus.NEGATIVE,
            office=VerificationStatus.NEGATIVE,
            legal=VerificationStatus.POSITIVE,
            pd_status=VerificationStatus.POSITIVE,
            legal_checklist={"ok": True},
        )
    )
    result = agent.assess(req)
    assert result.risk_score == 30
    assert result.risk_category == RiskCategory.MEDIUM
    assert result.evidence["category_escalated_by_critical"] is False


def test_escalation_can_be_disabled_by_policy(policy):
    """critical_forces_category: null returns to score-only categorisation."""
    import copy

    disabled = copy.deepcopy(policy)
    disabled["critical_forces_category"] = None
    result = FraudRiskAgent(policy=disabled, use_llm=False).assess(
        income_mismatch_request(600000, 52000)
    )
    assert result.risk_score == 50
    assert result.risk_category == RiskCategory.MEDIUM


def test_escalation_is_recorded_in_evidence(agent):
    """An escalated category must be auditable, not silent."""
    result = agent.assess(income_mismatch_request(600000, 52000))
    assert result.evidence["category_from_score"] == "MEDIUM"
    assert result.evidence["category_escalated_by_critical"] is True