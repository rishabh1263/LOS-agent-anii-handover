"""
The document policy engine: what a case needs, and why it needs it.

WHAT THESE TESTS ARE GUARDING. Two things, and they pull in opposite
directions.

The first is that NO REQUIREMENT IS INVENTED. Every slot has to trace to a
rule in a configuration file. A test that asserted "a personal loan over X
needs an ITR" would be writing lender policy into the test suite, which is
exactly what this engine exists to stop -- so almost every test here builds
its own policy fixture and asserts the ENGINE'S BEHAVIOUR, not any bank's
matrix. The only assertions made against the shipped file are that it is
well-formed and that it declares itself unconfirmed.

The second is that A RULE THAT CANNOT BE EVALUATED MUST NOT VANISH. A case
with no loan amount captured cannot have an amount band applied to it. The
engine could impose the strictest band (and send a field officer to collect
documents the customer may not owe), impose the loosest (and hand back a
checklist that looks complete and is not), or say so. It says so, and a
third of this file is about that.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from app.agents.policy import engine, loader
from app.agents.policy.engine import CONDITIONAL, OPTIONAL, REQUIRED

# ==========================================================================
# FIXTURES -- a policy that exists only for these tests
# ==========================================================================

#: Deliberately not a real lender's matrix. Round numbers, invented slots.
FIXTURE_POLICY = {
    "policy_id": "TEST_POLICY",
    "policy_version": "9.9.9-TEST",
    "product": "TEST_LOAN",
    "status": "UNCONFIRMED",
    "base": {
        "rule_id": "T_BASE",
        "required": [
            {"slot": "IDENTITY", "accepts": ["PAN"],
             "reason": "Identity is needed for every application."},
            {"slot": "ADDRESS_PROOF",
             "accepts": ["PASSPORT", "VOTER_ID", "DRIVING_LICENCE"]},
        ],
        "optional": [{"slot": "PHOTO", "accepts": ["PHOTO"]}],
    },
    "amount_rules": [
        {"rule_id": "T_LOW", "min_amount": 0, "max_amount": 100,
         "required": []},
        {"rule_id": "T_MID", "min_amount": 101, "max_amount": 200,
         "required": [{"slot": "INCOME_PROOF",
                       "accepts": ["SALARY_SLIP", "ITR"],
                       "reason": "Income evidence above the first band."}]},
        {"rule_id": "T_HIGH", "min_amount": 201, "max_amount": None,
         "required": [
             {"slot": "INCOME_PROOF", "accepts": ["SALARY_SLIP", "ITR"]},
             {"slot": "EMPLOYMENT_PROOF", "accepts": ["EMPLOYMENT_PROOF"]},
         ]},
    ],
    "conditional_rules": [
        {"rule_id": "T_SELF_EMPLOYED",
         "when": {"employment_type": "SELF_EMPLOYED"},
         "required": [{"slot": "INCOME_PROOF", "accepts": ["ITR"],
                       "reason": "A self-employed applicant files an ITR."}]},
    ],
    "documents": {
        "PAN": {"required_fields": ["pan_number", "name"],
                "verification_required": True},
        "BANK_STATEMENT": {"required_fields": ["period_start"],
                           "min_months": 3, "min_months_status": "UNCONFIRMED"},
    },
}


@pytest.fixture
def policy_dir(tmp_path, monkeypatch):
    """A policy directory containing only the fixture policy."""
    directory = tmp_path / "policies"
    directory.mkdir()

    def write(document, name="test_loan.yaml"):
        (directory / name).write_text(yaml.safe_dump(document),
                                      encoding="utf-8")
        loader.reload()

    monkeypatch.setenv("FOS_POLICY_DIR", str(directory))
    loader.reload()
    write(FIXTURE_POLICY)
    yield write
    loader.reload()


def slots(resolution):
    return {r.slot: r for r in resolution.requirements}


# ==========================================================================
# A. NOTHING IS INVENTED
# ==========================================================================


def test_every_requirement_names_the_rule_that_produced_it(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=150)

    assert resolution.requirements
    for requirement in resolution.requirements:
        assert requirement.rule_ids, f"{requirement.slot} has no rule"
        for rule_id in requirement.rule_ids:
            assert rule_id in resolution.applied_rules


def test_an_empty_policy_produces_no_requirements(policy_dir):
    """Not a default set. Nothing."""
    policy_dir({"policy_id": "EMPTY", "policy_version": "1",
                "product": "TEST_LOAN", "status": "UNCONFIRMED"})

    resolution = engine.resolve("TEST_LOAN", loan_amount=150)

    assert resolution.requirements == ()
    assert resolution.policy_id == "EMPTY"


def test_the_resolution_carries_its_policy_version(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=50)

    assert resolution.policy_id == "TEST_POLICY"
    assert resolution.policy_version == "9.9.9-TEST"
    assert resolution.provenance()["policy_version"] == "9.9.9-TEST"


def test_an_unconfirmed_policy_says_so_on_every_requirement(policy_dir):
    """
    A placeholder threshold that renders identically to a signed-off one is
    a placeholder somebody will eventually mistake for policy.
    """
    resolution = engine.resolve("TEST_LOAN", loan_amount=150)

    assert resolution.policy_status == "UNCONFIRMED"
    assert all(r.policy_status == "UNCONFIRMED"
               for r in resolution.requirements)


# ==========================================================================
# B. AMOUNT BANDS
# ==========================================================================


@pytest.mark.parametrize("amount,expected_rule", [
    (0, "T_LOW"), (50, "T_LOW"), (100, "T_LOW"),
    (101, "T_MID"), (150, "T_MID"), (200, "T_MID"),
    (201, "T_HIGH"), (10_000, "T_HIGH"),
])
def test_the_band_containing_the_amount_is_the_one_that_applies(
        policy_dir, amount, expected_rule):
    resolution = engine.resolve("TEST_LOAN", loan_amount=amount)

    bands = [r for r in resolution.applied_rules if r.startswith("T_")
             and r != "T_BASE"]
    assert bands == [expected_rule]


def test_boundaries_belong_to_exactly_one_band(policy_dir):
    """An amount on a boundary must not collect two bands' documents."""
    for amount in (100, 101, 200, 201):
        resolution = engine.resolve("TEST_LOAN", loan_amount=amount)
        bands = [r for r in resolution.applied_rules
                 if r in {"T_LOW", "T_MID", "T_HIGH"}]
        assert len(bands) == 1, f"{amount} matched {bands}"


def test_a_higher_band_adds_documents_it_does_not_replace_them(policy_dir):
    low = slots(engine.resolve("TEST_LOAN", loan_amount=50))
    high = slots(engine.resolve("TEST_LOAN", loan_amount=10_000))

    assert set(low) < set(high)
    assert "IDENTITY" in high and "ADDRESS_PROOF" in high


def test_an_unbounded_top_band_has_no_ceiling(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=Decimal("1e12"))
    assert "T_HIGH" in resolution.applied_rules


# ==========================================================================
# C. AN AMOUNT THE ENGINE IS NOT SURE OF
# ==========================================================================


@pytest.mark.parametrize("raw,expected", [
    (500000, Decimal(500000)),
    ("500000", Decimal(500000)),
    ("5,00,000", Decimal(500000)),
    ("500000.00", Decimal("500000.00")),
    ("  500000  ", Decimal(500000)),
    ("INR 500000", Decimal(500000)),
    ("Rs 500000", Decimal(500000)),
    (0, Decimal(0)),
])
def test_amounts_that_are_unambiguous_parse(raw, expected):
    assert engine.parse_amount(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ", "approx 500000", "5 lakh", "five hundred thousand",
    "-500000", -1, "500000-600000", "TBD", ".", True, False, [],
])
def test_amounts_that_are_not_unambiguous_do_not_parse(raw):
    """
    THE POINT OF THIS TEST. "5 lakh" cleaning down to 5 would select the
    lowest band for a case that belongs in the highest. A number the engine
    is not certain of is not a number.
    """
    assert engine.parse_amount(raw) is None


def test_no_amount_means_no_band_and_the_gap_is_reported(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=None)

    assert not [r for r in resolution.applied_rules
                if r in {"T_LOW", "T_MID", "T_HIGH"}]
    gap = next(u for u in resolution.unevaluated_rules
               if u.rule_id == "AMOUNT_BANDS")
    assert gap.missing_attributes == ("loan_amount",)
    assert "INCOME_PROOF" in gap.would_require


def test_no_amount_still_produces_the_base_checklist(policy_dir):
    """
    A case with no amount is not a case with no requirements. Identity
    evidence does not depend on how much is being borrowed.
    """
    resolution = engine.resolve("TEST_LOAN", loan_amount=None)
    assert "IDENTITY" in slots(resolution)


def test_an_unparseable_amount_is_treated_as_no_amount_not_as_zero(policy_dir):
    """Zero would silently select the lowest band."""
    resolution = engine.resolve("TEST_LOAN", loan_amount="about 5 lakh")

    assert "T_LOW" not in resolution.applied_rules
    assert any(u.rule_id == "AMOUNT_BANDS"
               for u in resolution.unevaluated_rules)


# ==========================================================================
# D. CONDITIONS ON CASE ATTRIBUTES
# ==========================================================================


def test_a_condition_that_matches_fires_and_records_why(policy_dir):
    resolution = engine.resolve(
        "TEST_LOAN", loan_amount=50,
        attributes={"employment_type": "SELF_EMPLOYED"})

    income = slots(resolution)["INCOME_PROOF"]
    assert income.requirement == CONDITIONAL
    assert income.applicable_conditions == ("employment_type=SELF_EMPLOYED",)
    assert "T_SELF_EMPLOYED" in income.rule_ids


def test_a_condition_that_does_not_match_does_not_fire(policy_dir):
    resolution = engine.resolve(
        "TEST_LOAN", loan_amount=50,
        attributes={"employment_type": "SALARIED"})

    assert "INCOME_PROOF" not in slots(resolution)
    assert "T_SELF_EMPLOYED" not in resolution.applied_rules


def test_a_condition_that_does_not_match_is_not_reported_as_a_gap(policy_dir):
    """It was evaluated. It simply did not apply."""
    resolution = engine.resolve(
        "TEST_LOAN", loan_amount=50,
        attributes={"employment_type": "SALARIED"})

    assert not [u for u in resolution.unevaluated_rules
                if u.rule_id == "T_SELF_EMPLOYED"]


def test_a_missing_attribute_does_not_impose_the_requirement(policy_dir):
    """
    THE CHOICE THIS TEST PINS DOWN. An uncaptured employment type could
    default to self-employed (and send an officer for an ITR the customer
    does not owe) or default to salaried (and produce a checklist that is
    quietly short). It does neither.
    """
    resolution = engine.resolve("TEST_LOAN", loan_amount=50, attributes={})

    assert "INCOME_PROOF" not in slots(resolution)


def test_a_missing_attribute_is_reported_rather_than_dropped(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=50, attributes={})

    gap = next(u for u in resolution.unevaluated_rules
               if u.rule_id == "T_SELF_EMPLOYED")
    assert gap.missing_attributes == ("employment_type",)
    assert "INCOME_PROOF" in gap.would_require
    assert "employment_type" in gap.reason


def test_a_blank_attribute_counts_as_missing(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=50,
                                attributes={"employment_type": ""})
    assert any(u.rule_id == "T_SELF_EMPLOYED"
               for u in resolution.unevaluated_rules)


@pytest.mark.parametrize("value", ["SELF_EMPLOYED", "self_employed",
                                   " Self_Employed "])
def test_attribute_matching_ignores_case_and_padding(policy_dir, value):
    resolution = engine.resolve("TEST_LOAN", loan_amount=50,
                                attributes={"employment_type": value})
    assert "T_SELF_EMPLOYED" in resolution.applied_rules


def test_a_condition_may_list_several_accepted_values(policy_dir):
    policy = dict(FIXTURE_POLICY)
    policy["conditional_rules"] = [{
        "rule_id": "T_EITHER",
        "when": {"employment_type": ["SELF_EMPLOYED", "BUSINESS"]},
        "required": [{"slot": "INCOME_PROOF", "accepts": ["ITR"]}],
    }]
    policy_dir(policy)

    for value in ("SELF_EMPLOYED", "BUSINESS"):
        resolution = engine.resolve("TEST_LOAN", loan_amount=50,
                                    attributes={"employment_type": value})
        assert "T_EITHER" in resolution.applied_rules

    resolution = engine.resolve("TEST_LOAN", loan_amount=50,
                                attributes={"employment_type": "SALARIED"})
    assert "T_EITHER" not in resolution.applied_rules


# ==========================================================================
# E. RULES COMBINE
# ==========================================================================


def test_a_narrowing_rule_narrows_what_satisfies_the_slot(policy_dir):
    """
    The band asks for income proof and accepts three things. Being
    self-employed says which one. The result is the intersection, not a
    second INCOME_PROOF row.
    """
    resolution = engine.resolve(
        "TEST_LOAN", loan_amount=150,
        attributes={"employment_type": "SELF_EMPLOYED"})

    income = slots(resolution)["INCOME_PROOF"]
    assert income.accepts == ("ITR",)
    assert len([r for r in resolution.requirements
                if r.slot == "INCOME_PROOF"]) == 1


def test_the_strongest_requirement_wins(policy_dir):
    """REQUIRED from a band is not weakened by an OPTIONAL elsewhere."""
    policy = dict(FIXTURE_POLICY)
    policy["base"] = dict(FIXTURE_POLICY["base"],
                          optional=[{"slot": "INCOME_PROOF",
                                     "accepts": ["SALARY_SLIP", "ITR"]}])
    policy_dir(policy)

    resolution = engine.resolve("TEST_LOAN", loan_amount=150)
    assert slots(resolution)["INCOME_PROOF"].requirement == REQUIRED


def test_a_base_requirement_cannot_be_removed_by_any_rule(policy_dir):
    """
    No combination of amount and attributes may produce a case that needs
    less identity evidence than every other case.
    """
    for amount in (0, 100, 150, 10_000, None):
        for attributes in ({}, {"employment_type": "SELF_EMPLOYED"},
                           {"employment_type": "SALARIED"}):
            resolution = engine.resolve("TEST_LOAN", loan_amount=amount,
                                        attributes=attributes)
            present = slots(resolution)
            assert present["IDENTITY"].requirement == REQUIRED
            assert present["ADDRESS_PROOF"].requirement == REQUIRED


def test_an_optional_slot_never_becomes_mandatory_on_its_own(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=10_000)
    photo = slots(resolution)["PHOTO"]

    assert photo.requirement == OPTIONAL
    assert photo.mandatory is False


def test_a_fired_conditional_requirement_is_mandatory(policy_dir):
    """
    CONDITIONAL describes WHY it applies, not whether it binds. Once the
    condition has matched, the document is as required as any other.
    """
    resolution = engine.resolve(
        "TEST_LOAN", loan_amount=50,
        attributes={"employment_type": "SELF_EMPLOYED"})

    assert slots(resolution)["INCOME_PROOF"].mandatory is True


def test_disjoint_narrowing_is_reported_rather_than_blocking_the_case(
        policy_dir):
    """
    A policy file that narrows one slot to two non-overlapping sets
    describes a document nobody can produce. Blocking every case of that
    product punishes the customer for a configuration mistake.
    """
    policy = dict(FIXTURE_POLICY)
    policy["conditional_rules"] = [{
        "rule_id": "T_IMPOSSIBLE",
        "when": {"employment_type": "SELF_EMPLOYED"},
        "required": [{"slot": "INCOME_PROOF", "accepts": ["FORM_16"]}],
    }]
    policy_dir(policy)

    resolution = engine.resolve(
        "TEST_LOAN", loan_amount=150,
        attributes={"employment_type": "SELF_EMPLOYED"})

    income = slots(resolution)["INCOME_PROOF"]
    assert set(income.accepts) == {"SALARY_SLIP", "ITR", "FORM_16"}
    assert resolution.conflicts
    assert resolution.conflicts[0].slot == "INCOME_PROOF"
    assert "T_IMPOSSIBLE" in resolution.conflicts[0].rule_ids


# ==========================================================================
# F. EXPLAINING A REQUIREMENT
# ==========================================================================


def test_a_requirement_can_be_explained_in_full(policy_dir):
    resolution = engine.resolve(
        "TEST_LOAN", loan_amount=150,
        attributes={"employment_type": "SELF_EMPLOYED"})

    explanation = resolution.explain("INCOME_PROOF")

    assert explanation["rule_id"]
    assert explanation["policy_version"] == "9.9.9-TEST"
    assert explanation["policy_status"] == "UNCONFIRMED"
    assert explanation["applicable_conditions"] == [
        "employment_type=SELF_EMPLOYED"]
    assert explanation["accepts"] == ["ITR"]


def test_explaining_a_slot_the_case_does_not_need_returns_nothing(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=50)
    assert resolution.explain("INCOME_PROOF") is None
    assert resolution.explain("NOT_A_SLOT") is None


def test_a_base_requirement_carries_its_configured_reason(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=50)
    assert resolution.explain("IDENTITY")["reason"] == (
        "Identity is needed for every application.")


def test_provenance_lists_the_rules_and_the_gaps(policy_dir):
    provenance = engine.resolve("TEST_LOAN").provenance()

    assert provenance["policy_id"] == "TEST_POLICY"
    assert provenance["status"] == "UNCONFIRMED"
    assert "T_BASE" in provenance["applied_rules"]
    assert {u["rule_id"] for u in provenance["unevaluated_rules"]} == {
        "AMOUNT_BANDS", "T_SELF_EMPLOYED"}


# ==========================================================================
# G. DOCUMENT EVIDENCE REQUIREMENTS
# ==========================================================================


def test_evidence_requirements_come_from_the_policy(policy_dir):
    resolution = engine.resolve("TEST_LOAN", loan_amount=50)

    assert resolution.evidence_for("PAN")["required_fields"] == [
        "pan_number", "name"]
    assert resolution.evidence_for("BANK_STATEMENT")["min_months"] == 3


def test_an_unconfigured_document_type_has_no_evidence_requirements(
        policy_dir):
    """Empty, not a guessed default."""
    assert engine.resolve("TEST_LOAN").evidence_for("PASSPORT") == {}


def test_a_placeholder_threshold_is_marked_in_the_evidence_block(policy_dir):
    evidence = engine.resolve("TEST_LOAN").evidence_for("BANK_STATEMENT")
    assert evidence["min_months_status"] == "UNCONFIRMED"


# ==========================================================================
# H. A PRODUCT WITH NO POLICY FILE
# ==========================================================================


def test_an_unknown_product_falls_back_to_the_agent_checklist(policy_dir):
    """
    A deployment that has not written a policy behaves exactly as it did
    before the engine existed.
    """
    from app.agents.applicant import config as agent_config

    resolution = engine.resolve("PERSONAL_LOAN")

    assert resolution.policy_id == engine.LEGACY_POLICY_ID
    assert resolution.policy_version == engine.LEGACY_VERSION
    assert [r.slot for r in resolution.requirements] == [
        e["slot"] for e in agent_config.checklist_for("PERSONAL_LOAN")]


def test_the_fallback_does_not_claim_a_policy_version(policy_dir):
    resolution = engine.resolve("PERSONAL_LOAN")
    assert resolution.policy_status == "UNVERSIONED"
    assert resolution.source == engine.LEGACY_SOURCE


def test_no_product_at_all_still_resolves(policy_dir):
    resolution = engine.resolve(None)
    assert resolution.policy_id == engine.LEGACY_POLICY_ID
    assert resolution.requirements


def test_a_missing_policy_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FOS_POLICY_DIR", str(tmp_path / "nothing-here"))
    loader.reload()
    try:
        assert loader.known_products() == []
        assert engine.resolve("PERSONAL_LOAN").requirements
    finally:
        loader.reload()


def test_a_malformed_policy_file_does_not_take_the_product_down(
        tmp_path, monkeypatch):
    """
    Broken YAML must not become "this product needs no documents". It falls
    back to the agent checklist, which is a real list.
    """
    directory = tmp_path / "policies"
    directory.mkdir()
    (directory / "personal_loan.yaml").write_text(
        "policy_id: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("FOS_POLICY_DIR", str(directory))
    loader.reload()
    try:
        resolution = engine.resolve("PERSONAL_LOAN")
        assert resolution.policy_id == engine.LEGACY_POLICY_ID
        assert resolution.requirements
    finally:
        loader.reload()


# ==========================================================================
# I. THE CHECKLIST SHAPE THE REST OF THE SERVICE CONSUMES
# ==========================================================================


def test_the_resolution_produces_the_existing_checklist_shape(policy_dir):
    """
    `slot`, `accepts` and `mandatory` are what the workflow already reads.
    Widening the entry must not move them.
    """
    entries = engine.resolve("TEST_LOAN", loan_amount=150).checklist()

    for entry in entries:
        assert set(entry) >= {"slot", "accepts", "mandatory"}
        assert isinstance(entry["accepts"], list)
        assert isinstance(entry["mandatory"], bool)


def test_the_checklist_entry_carries_its_rule(policy_dir):
    entry = next(e for e in engine.resolve("TEST_LOAN", loan_amount=150)
                 .checklist() if e["slot"] == "INCOME_PROOF")

    assert entry["rule_ids"] == ["T_MID"]
    assert entry["requirement"] == REQUIRED
    assert entry["policy_status"] == "UNCONFIRMED"


# ==========================================================================
# J. THE SHIPPED FILE
# ==========================================================================
#
# These say nothing about what a personal loan ought to require. They check
# that the file in the repository is structurally sound and honest about
# what it is.


def test_the_shipped_personal_loan_policy_loads():
    loader.reload()
    policy = loader.policy_for("PERSONAL_LOAN")
    assert policy is not None
    assert policy["policy_id"]
    assert policy["policy_version"]


def test_every_shipped_threshold_declares_itself_unconfirmed():
    """
    The demo bands must never be mistakable for a lender's policy. If
    somebody replaces them with real numbers they are expected to flip the
    status, and this test is what makes that a deliberate act.
    """
    loader.reload()
    policy = loader.policy_for("PERSONAL_LOAN")

    assert policy["status"] == "UNCONFIRMED"
    assert "DEMO" in policy["policy_version"].upper()
    for rule in policy.get("amount_rules", []):
        assert rule.get("status") == "UNCONFIRMED", rule.get("rule_id")
    for rule in policy.get("conditional_rules", []):
        assert rule.get("status") == "UNCONFIRMED", rule.get("rule_id")


def test_every_shipped_rule_has_an_identifier():
    loader.reload()
    policy = loader.policy_for("PERSONAL_LOAN")

    assert policy["base"]["rule_id"]
    ids = [policy["base"]["rule_id"]]
    ids += [r["rule_id"] for r in policy.get("amount_rules", [])]
    ids += [r["rule_id"] for r in policy.get("conditional_rules", [])]
    assert all(ids)
    assert len(ids) == len(set(ids)), "rule ids must be unique"


def test_the_shipped_bands_cover_every_amount_without_overlapping():
    """
    A gap means some amount selects no band at all. An overlap means an
    amount collects two bands' documents. Both are configuration bugs the
    engine cannot detect at runtime without a case to resolve.
    """
    loader.reload()
    rules = loader.policy_for("PERSONAL_LOAN")["amount_rules"]
    bands = sorted(((Decimal(str(r["min_amount"])),
                     None if r.get("max_amount") is None
                     else Decimal(str(r["max_amount"])),
                     r["rule_id"]) for r in rules), key=lambda b: b[0])

    assert bands[0][0] == 0, "the lowest band must start at zero"
    assert bands[-1][1] is None, "the highest band must be unbounded"
    for (_, high, rule_id), (low, _, nxt) in zip(bands, bands[1:]):
        assert high is not None, f"{rule_id} is unbounded but not last"
        assert low == high + 1, f"gap or overlap between {rule_id} and {nxt}"


def test_every_shipped_slot_accepts_a_type_the_service_can_classify():
    """
    A requirement for a document type the pipeline cannot recognise is a
    requirement no upload can ever satisfy.
    """
    from app.agents.applicant import config as agent_config

    loader.reload()
    policy = loader.policy_for("PERSONAL_LOAN")
    known = set(agent_config.document_types()) | set(policy.get("documents", {}))

    groups = [policy["base"]]
    groups += policy.get("amount_rules", [])
    groups += policy.get("conditional_rules", [])
    for group in groups:
        for key in ("required", "optional"):
            for item in (group.get(key) or []):
                for accepted in item["accepts"]:
                    assert accepted in known, (
                        f"{group['rule_id']} accepts {accepted}, which is not "
                        f"a type this service knows")
