"""
The KYC numbers, said in words.

THE DEFECT THIS CLOSES. A response carrying `overall_score: 38` reads as
"this applicant is 38% accurate". It is not that. `fields.roll_up`
computes a weighted mean over the fields that were ACTUALLY COMPARED, so
on a bundle where only the name could be compared, 38 IS the name-match
score under a general-sounding name. The number was right and its
meaning was guessable -- which is the worst combination, because a
reviewer never learns they guessed wrong.

NOTHING HERE EVALUATES ANYTHING. Every published number is read from a
number KYC already produced, every sentence is selected by a reason code
through a lookup table, and no threshold is consulted. Delete this layer
and the verdict is identical -- which is the property the tests below
hold.
"""

from __future__ import annotations

import pytest

from app.agents.los import kyc_explain
from app.agents.los.flow import _kyc_for_party
from app.agents.los.response import public_kyc


def document(fields, source_id, document_type, party_id="APP-1"):
    return {
        "source_id": source_id, "status": "PASS", "party_id": party_id,
        "party_role": "PRIMARY_APPLICANT",
        "document": {"type": document_type},
        "verification": {"status": "PASS", "reason_codes": []},
        "extraction": {"fields": fields},
    }


def kyc_for(documents, party_id="APP-1"):
    return _kyc_for_party(documents, party_id=party_id, request_id="test")


def name_mismatch_case():
    """PAN says one person, the bank statement says another."""
    return kyc_for([
        document({"name": "SUSMITA DAS", "pan_number": "EVPPG6189E"},
                 "copan.jpg", "PAN"),
        document({"name": "GUDDI DEVI"}, "canara.pdf", "BANK_STATEMENT"),
    ])


def agreeing_case():
    return kyc_for([
        document({"name": "RISHABH AJIT SINGH", "pan_number": "NUHPS4875K",
                  "date_of_birth": "2002-06-12"}, "pan.jpg", "PAN"),
        document({"name": "RISHABH AJIT SINGH",
                  "date_of_birth": "2002-06-12"}, "dl.jpg", "DRIVING_LICENCE"),
    ])


# ==========================================================================
# 1-3. THE SCORE, NAMED
# ==========================================================================


@pytest.mark.parametrize("value,expected", [
    (100, "Exact match"),
    (99, "Very high similarity"),
    (90, "Very high similarity"),
    (89, "High similarity"),
    (75, "High similarity"),
    (74, "Moderate similarity"),
    (50, "Moderate similarity"),
    (49, "Low similarity"),
    (38, "Low similarity"),
    (25, "Low similarity"),
    (24, "Very low similarity"),
    (0, "Very low similarity"),
])
def test_a_score_is_banded_the_same_way_every_time(value, expected):
    published = kyc_explain.score_of({
        "overall_score": value, "overall_confidence": 80,
        "fields": [{"field": "NAME", "status": "FAIL"}],
    })

    assert published["interpretation"] == expected


def test_a_score_over_one_field_names_that_field():
    published = public_kyc(name_mismatch_case())["score"]

    assert published["type"] == "NAME_MATCH"
    assert published["label"] == "Name Match Score"
    assert published["value"] == 38
    assert published["interpretation"] == "Low similarity"


def test_a_score_over_several_fields_is_a_consistency_score():
    """
    NOT "accuracy". Nothing in this pipeline measures how often the
    extractor is right, and a field named accuracy would be read as
    though something did.
    """
    published = public_kyc(agreeing_case())["score"]

    assert published["type"] == "KYC_CONSISTENCY"
    assert "accuracy" not in published["label"].lower()


@pytest.mark.parametrize("field,expected_type", [
    ("NAME", "NAME_MATCH"),
    ("DATE_OF_BIRTH", "DOB_MATCH"),
    ("ADDRESS", "ADDRESS_MATCH"),
    ("PAN_NUMBER", "PAN_MATCH"),
    ("FATHER_NAME", "FATHER_NAME_MATCH"),
])
def test_each_single_field_score_names_its_own_basis(field, expected_type):
    published = kyc_explain.score_of({
        "overall_score": 70, "overall_confidence": 80,
        "fields": [{"field": field, "status": "FAIL"},
                   {"field": "INCOME", "status": "SKIPPED"}],
    })

    assert published["type"] == expected_type


def test_no_score_object_when_nothing_was_comparable():
    """
    `roll_up` returns 0 when nothing was compared, and its docstring is
    explicit that this is NOT a score of zero in the sense of
    "everything disagreed". Publishing `{value: 0, interpretation:
    "Very low similarity"}` would assert exactly what the algorithm
    refuses to assert.
    """
    published = kyc_explain.score_of({
        "overall_score": 0, "overall_confidence": 0,
        "fields": [{"field": "NAME", "status": "SKIPPED"},
                   {"field": "ADDRESS", "status": "SKIPPED"}],
    })

    assert published is None


# ==========================================================================
# 4-5. THE EXISTING NUMBERS ARE UNTOUCHED
# ==========================================================================


def test_the_existing_score_and_confidence_are_republished_not_recomputed():
    payload = name_mismatch_case()
    published = public_kyc(payload)

    assert published["overall_score"] == payload["overall_score"]
    assert published["overall_confidence"] == payload["overall_confidence"]
    assert published["score"]["value"] == payload["overall_score"]
    assert published["score"]["confidence"] == payload["overall_confidence"]


def test_the_verdict_and_reason_codes_are_untouched():
    payload = name_mismatch_case()
    published = public_kyc(payload)

    assert published["status"] == payload["status"]
    assert published["reason_codes"] == payload["reason_codes"]


def test_the_explanation_layer_changes_no_verdict():
    """
    The property that makes this safe to add: remove every explanatory
    key and the response is what it was.
    """
    published = public_kyc(name_mismatch_case())
    without = {k: v for k, v in published.items()
               if k not in {"score", "overall_score_basis",
                            "verification_summary", "result"}}

    assert set(without) == {"status", "reason_codes", "overall_score",
                            "overall_confidence", "fields"}


# ==========================================================================
# 6. THE SUMMARY COUNTS
# ==========================================================================


def test_the_summary_counts_the_checks_that_actually_ran():
    summary = public_kyc(name_mismatch_case())["verification_summary"]

    assert summary == {
        "checks_evaluated": 1,
        "checks_passed": 0,
        "checks_failed": 1,
        "checks_skipped": 5,
        "primary_issue": "NAME_MISMATCH",
    }


def test_the_summary_numbers_always_add_up():
    payload = {
        "status": "REVIEW",
        "checks": [
            {"check": "NAME", "status": "PASS", "reason_codes": []},
            {"check": "DOB", "status": "FAIL", "reason_codes": ["DOB_MISMATCH"]},
            {"check": "ADDRESS", "status": "REVIEW",
             "reason_codes": ["ADDRESS_PARTIAL_MATCH"]},
            {"check": "PAN", "status": "SKIPPED", "reason_codes": []},
        ],
    }
    summary = kyc_explain.verification_summary(payload)

    assert (summary["checks_passed"] + summary["checks_failed"]
            + summary["checks_review"] + summary["checks_skipped"]) == 4
    assert summary["checks_evaluated"] == 3


def test_the_primary_issue_is_the_worst_one():
    summary = kyc_explain.verification_summary({"checks": [
        {"check": "ADDRESS", "status": "REVIEW",
         "reason_codes": ["ADDRESS_PARTIAL_MATCH"]},
        {"check": "NAME", "status": "FAIL", "reason_codes": ["NAME_MISMATCH"]},
    ]})

    assert summary["primary_issue"] == "NAME_MISMATCH"


def test_a_clean_case_reports_no_issue():
    summary = kyc_explain.verification_summary({"checks": [
        {"check": "NAME", "status": "PASS", "reason_codes": []},
    ]})

    assert "primary_issue" not in summary
    assert summary["checks_passed"] == 1


# ==========================================================================
# 7-9. THE SENTENCES
# ==========================================================================


def test_a_name_mismatch_names_the_documents_it_compared():
    result = public_kyc(name_mismatch_case())["result"]

    assert result["title"] == "Identity verification requires review"
    assert result["action"] == "Manual review required"
    assert result["message"] == (
        "The applicant's PAN name does not match the bank account "
        "holder name.")


def test_a_clean_case_asks_for_nothing():
    result = public_kyc(agreeing_case())["result"]

    assert result["title"] == "Identity verified across documents"
    assert result["action"] == "No action required"


@pytest.mark.parametrize("code,fragment", [
    ("NAME_SINGLE_SOURCE", "Only one name source was available"),
    ("ADDRESS_SINGLE_SOURCE", "Only one address-bearing document"),
    ("ADDRESS_MISSING", "No address was available"),
    ("NAME_MISMATCH", "do not sufficiently match"),
    ("DOB_MISMATCH", "dates of birth"),
])
def test_a_field_message_explains_its_reason_code(code, fragment):
    assert fragment in kyc_explain.field_message(code)


def test_an_unknown_reason_code_produces_no_sentence():
    """
    A sentence guessed from a code that does not carry the fact is a
    fabrication with a citation.
    """
    assert kyc_explain.field_message("SOMETHING_NEW_NOBODY_MAPPED") is None
    assert kyc_explain.field_message(None) is None


def test_every_field_row_keeps_its_existing_keys():
    rows = public_kyc(name_mismatch_case())["fields"]
    name_row = {r["field"]: r for r in rows}["NAME"]

    assert name_row["status"] == "FAIL"
    assert name_row["reason_code"] == "NAME_MISMATCH"
    assert name_row["match_score"] == 38


def test_no_per_row_sentence_is_published():
    """
    THE BUDGET DECIDED THIS. A sentence on every row cost 451 bytes per
    party -- half this phase's growth, and enough on a two-party case to
    threaten the 6000-byte guard in test_los_production_e2e. It is also
    the second copy: the `reason` string was removed from these rows for
    the same reason. The prose a reviewer needs is the one issue that
    decided the case, and `result.message` carries it.
    """
    rows = public_kyc(name_mismatch_case())["fields"]

    assert all("message" not in row for row in rows)
    assert all("reason" not in row for row in rows if row.get("reason_code"))


def test_the_sentence_for_a_code_is_still_available_to_render_one():
    assert "could not be cross-checked" in kyc_explain.field_message(
        "PAN_SINGLE_SOURCE")


# ==========================================================================
# 10-12. COMPATIBILITY AND SAFETY
# ==========================================================================


def test_a_single_applicant_response_keeps_every_field_it_had():
    published = public_kyc(agreeing_case())

    for key in ("status", "reason_codes", "overall_score",
                "overall_confidence", "fields"):
        assert key in published, key


def test_the_compact_case_level_object_still_drops_the_field_rows():
    published = public_kyc(name_mismatch_case(), compact=True)

    assert "fields" not in published
    assert published["overall_score"] == 38


def test_the_documented_schema_accepts_the_published_shape():
    from app.agents.los.schemas import KycSummary

    published = public_kyc(name_mismatch_case())
    model = KycSummary.model_validate(published)

    assert model.score.type == "NAME_MATCH"
    assert model.verification_summary.checks_failed == 1
    assert model.result.action == "Manual review required"


def test_the_new_fields_are_all_optional():
    """Every existing caller keeps working."""
    from app.agents.los.schemas import KycSummary

    minimal = KycSummary.model_validate(
        {"status": "SKIPPED", "reason_codes": []})

    assert minimal.score is None
    assert minimal.verification_summary is None
    assert minimal.result is None


def test_nothing_in_this_layer_calls_a_model_or_the_network():
    """
    Deterministic by construction: a lookup table and a band. Asserted
    on the parsed module rather than on its text, so the word "LLM"
    appearing in a comment that explains why there ISN'T one does not
    fail the check -- and so that adding a real call does.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(kyc_explain))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    # `typing` and `__future__` only. Anything that could reach a model,
    # a socket or a disk would have to be imported to be used.
    assert imported <= {"typing", "__future__"}, imported


def test_the_same_input_always_produces_the_same_words():
    first = public_kyc(name_mismatch_case())
    second = public_kyc(name_mismatch_case())

    assert first == second


# ==========================================================================
# THE CASE-LEVEL ROLL-UP ON A JOINT APPLICATION
#
# THREE DEFECTS, ALL THE SAME MISTAKE: reading a case-level number or
# code without asking WHOSE it was.
#
#   1. `overall_score` is a MINIMUM over the parties, but the label was
#      computed from the MERGED field rows -- so a co-applicant's
#      name-match score of 38 was published as "KYC Consistency" for the
#      whole case.
#   2. The sentence was built from the reason code alone, so
#      NAME_MISMATCH became "the PAN does not match the driving licence"
#      on a case whose PAN and driving licence agreed perfectly.
#   3. The check tally summed both people, describing neither.
# ==========================================================================


def joint_case():
    """Primary PAN+DL agree. Co-applicant PAN and bank statement do not."""
    from app.agents.los.flow import _case_kyc

    def party_doc(fields, source_id, document_type, party_id):
        row = document(fields, source_id, document_type, party_id)
        row["party_role"] = ("CO_APPLICANT" if party_id.startswith("COAPP")
                             else "PRIMARY_APPLICANT")
        return row

    primary_fields = {"name": "RISHABH AJIT SINGH",
                      "date_of_birth": "2002-06-12",
                      "pan_number": "NUHPS4875K", "father_name": "AJIT SINGH"}

    primary = _kyc_for_party([
        party_doc(primary_fields, "pan.jpg", "PAN", "APP-1"),
        party_doc({**primary_fields,
                   "address": "BLOCK NO-F/5, DEONAR, Greater Mumbai, MH",
                   "pin_code": "400043"},
                  "dl.jpg", "DRIVING_LICENCE", "APP-1"),
    ], party_id="APP-1", request_id="t", party_role="PRIMARY_APPLICANT")

    co = _kyc_for_party([
        party_doc({"name": "SUSMITA DAS", "pan_number": "EVPPG6189E"},
                  "copan.jpg", "PAN", "COAPP-9"),
        party_doc({"name": "GUDDI DEVI"}, "canara.pdf", "BANK_STATEMENT",
                  "COAPP-9"),
    ], party_id="COAPP-9", request_id="t", party_role="CO_APPLICANT")

    case, _rank = _case_kyc([primary, co])
    return primary, co, case


def test_the_case_score_names_the_party_it_came_from():
    """
    38 is the CO-APPLICANT's name-match score. Calling it
    `KYC_CONSISTENCY` described a case-wide agreement figure that was
    never computed.
    """
    _primary, _co, case = joint_case()
    published = public_kyc(case, compact=True)

    assert published["overall_score"] == 38
    assert published["score"]["type"] == "CO_APPLICANT_NAME_MATCH"
    assert published["score"]["label"] == "Co-applicant Name Match Score"
    assert published["overall_score_basis"] == "CO_APPLICANT_NAME_MATCH"


def test_the_case_score_is_still_the_minimum_it_always_was():
    """No new scoring. The published number is the existing minimum."""
    primary, co, case = joint_case()

    assert case["overall_score"] == min(primary["overall_score"],
                                        co["overall_score"])


def test_each_partys_own_score_keeps_its_own_unqualified_basis():
    primary, co, _case = joint_case()

    assert public_kyc(primary)["score"]["type"] == "KYC_CONSISTENCY"
    assert public_kyc(primary)["score"]["value"] == 100
    assert public_kyc(co)["score"]["type"] == "NAME_MATCH"
    assert public_kyc(co)["score"]["value"] == 38


def test_the_top_level_message_names_the_co_applicant_and_the_bank_statement():
    """
    THE TEST THIS CORRECTION WAS ASKED FOR.

    The primary's PAN and driving licence PASS. The failure is the
    co-applicant's PAN against their bank statement. The sentence must
    say so, and must never blame the driving licence.
    """
    _primary, _co, case = joint_case()
    result = public_kyc(case, compact=True)["result"]

    assert result["title"] == "KYC review required"
    assert result["action"] == "Manual review required"
    assert result["message"] == (
        "Co-applicant identity verification requires review because the "
        "PAN name does not match the bank account holder name.")

    # The primary's documents are not implicated, in any wording.
    assert "driving licence" not in result["message"].lower()
    assert "primary" not in result["message"].lower()


def test_the_summary_reports_each_party_separately():
    _primary, _co, case = joint_case()
    summary = public_kyc(case, compact=True)["verification_summary"]

    assert summary["primary_applicant"]["checks_failed"] == 0
    assert summary["primary_applicant"]["checks_passed"] == 4
    assert summary["co_applicant"]["checks_failed"] == 1
    assert summary["co_applicant"]["checks_passed"] == 0
    assert summary["primary_issue"] == "NAME_MISMATCH"


def test_the_case_wide_counts_are_kept_beside_the_party_ones():
    """Backward compatibility: the flat counts did not move."""
    _primary, _co, case = joint_case()
    summary = public_kyc(case, compact=True)["verification_summary"]

    assert summary["checks_failed"] == 1
    assert summary["checks_passed"] == (
        summary["primary_applicant"]["checks_passed"]
        + summary["co_applicant"]["checks_passed"])


def test_a_single_applicant_case_gains_no_party_scoping():
    """
    Nothing about the one-party response changes: no party prefix on the
    score, no per-party block, no party named in the sentence.
    """
    published = public_kyc(name_mismatch_case())

    assert published["score"]["type"] == "NAME_MATCH"
    assert published["overall_score_basis"] == "NAME_MATCH"
    assert "co_applicant" not in published["verification_summary"]
    assert "primary_applicant" not in published["verification_summary"]
    assert published["result"]["title"] == "Identity verification requires review"


def test_the_primary_applicant_is_named_when_the_failure_is_theirs():
    """The attribution is real, not a constant that always says co-applicant."""
    from app.agents.los.flow import _case_kyc

    def party_doc(fields, source_id, document_type, party_id, role):
        row = document(fields, source_id, document_type, party_id)
        row["party_role"] = role
        return row

    primary = _kyc_for_party([
        party_doc({"name": "RISHABH AJIT SINGH"}, "pan.jpg", "PAN",
                  "APP-1", "PRIMARY_APPLICANT"),
        party_doc({"name": "SOMEBODY ELSE"}, "dl.jpg", "DRIVING_LICENCE",
                  "APP-1", "PRIMARY_APPLICANT"),
    ], party_id="APP-1", request_id="t", party_role="PRIMARY_APPLICANT")
    co = _kyc_for_party([
        party_doc({"name": "GUDDI DEVI", "date_of_birth": "1998-03-04"},
                  "copan.jpg", "PAN", "COAPP-9", "CO_APPLICANT"),
        party_doc({"name": "GUDDI DEVI", "date_of_birth": "1998-03-04"},
                  "codl.jpg", "DRIVING_LICENCE", "COAPP-9", "CO_APPLICANT"),
    ], party_id="COAPP-9", request_id="t", party_role="CO_APPLICANT")

    case, _rank = _case_kyc([primary, co])
    result = public_kyc(case, compact=True)["result"]

    assert result["message"].startswith("Primary applicant identity")
    assert "driving licence" in result["message"]
