"""
CROSS-DOCUMENT CHECKS, industry defaults (2026-10-11, kyc_policies.yaml property_ownership / employer_consistency):
the property's owner on its sale deed must be a party to the application; the employer should be one organisation
across the applicant's documents. Synthetic records only (an in-memory repository double).
"""
from types import SimpleNamespace

import pytest

from app.agents.los import kyc_gate


def _repo(*, product="LOAN_AGAINST_PROPERTY", declared="Arjun Mehta", buyers=("ARJUN MEHTA",), deed=True,
          deed_status="VERIFIED", co=(), employers=()):
    documents, findings = [], []
    if deed:
        documents.append(SimpleNamespace(document_id="deed-1", document_type="SALE_DEED", status=deed_status,
                                         party_id="APP-1"))
        if buyers is not None:
            findings.append(SimpleNamespace(document_id="deed-1", party_id="APP-1", finding_kind="EXTRACTION",
                                            payload={"fields": {"buyer_names": list(buyers)}}))
    for i, (kind, name) in enumerate(employers):
        documents.append(SimpleNamespace(document_id=f"emp-{i}", document_type=kind, status="VERIFIED",
                                         party_id="APP-1"))
        findings.append(SimpleNamespace(document_id=f"emp-{i}", party_id="APP-1", finding_kind="EXTRACTION",
                                        payload={"fields": {"employer_name": name}}))

    def current(case_id, kind=None):
        return [f for f in findings if kind in (None, "EXTRACTION")]

    return SimpleNamespace(
        list_documents=lambda case_id: documents,
        get_current_findings=current,
        get_application=lambda case_id: SimpleNamespace(applicant_id="APP-1", product=product, co_applicant_id=None),
        get_applicant=lambda applicant_id: SimpleNamespace(full_name=declared),
        _co=list(co),
    )


@pytest.fixture(autouse=True)
def _co_applicants(monkeypatch):
    from app.agents.los import co_applicants

    monkeypatch.setattr(co_applicants, "list_for_case", lambda case_id, repository=None: [
        {"name": n} for n in getattr(repository, "_co", [])])


@pytest.mark.parametrize("declared, buyers, co, outcome", [
    ("Arjun Mehta", ["ARJUN MEHTA"], [], "MATCH"),                         # the applicant owns it
    ("Arjun Mehta", ["NEHA MEHTA", "ARJUN MEHTA"], [], "MATCH"),            # one of the joint owners
    ("Arjun Mehta", ["NEHA MEHTA"], ["Neha Mehta"], "MATCH"),               # owned by the co-applicant
    ("Arjun Mehta", ["KARAN SETHI"], [], "MISMATCH"),                       # someone else's property
])
def test_the_owner_on_the_deed_must_be_a_party(declared, buyers, co, outcome):
    result = kyc_gate.property_ownership_match("CASE-1", _repo(declared=declared, buyers=buyers, co=co))
    assert result["outcome"] == outcome, result
    assert result["required"] is True


def test_a_lenient_name_match_is_for_a_person_never_a_match():
    result = kyc_gate.property_ownership_match("CASE-1", _repo(declared="Arjun Mehra", buyers=["ARJUN MEHTA"]))
    assert result["outcome"] in ("PARTIAL", "MISMATCH")


def test_no_deed_yet_is_pending_where_required_and_not_applicable_elsewhere():
    assert kyc_gate.property_ownership_match("CASE-1", _repo(deed=False))["outcome"] == "PENDING"
    hl = kyc_gate.property_ownership_match("CASE-1", _repo(product="HOME_LOAN", deed=False))
    assert hl["outcome"] == "NOT_APPLICABLE" and hl["required"] is False


def test_a_superseded_deed_never_counts():
    assert kyc_gate.property_ownership_match("CASE-1", _repo(deed_status="SUPERSEDED"))["outcome"] == "PENDING"


def test_a_deed_with_no_buyer_read_is_unverifiable_not_a_pass():
    assert kyc_gate.property_ownership_match("CASE-1", _repo(buyers=[]))["outcome"] == "UNVERIFIABLE"


@pytest.mark.parametrize("employers, outcome", [
    ([("SALARY_SLIP", "ABC Technologies Pvt Ltd"), ("EMPLOYMENT_PROOF", "ABC Technologies Private Limited")],
     "MATCH"),                                                               # legal-form words differ only
    ([("SALARY_SLIP", "ABC Technologies Pvt Ltd"), ("EMPLOYMENT_PROOF", "XYZ Retail Ltd")], "MISMATCH"),
    ([("SALARY_SLIP", "ABC Technologies Pvt Ltd")], "NOT_APPLICABLE"),       # nothing to compare
])
def test_the_employer_is_one_organisation_across_documents(employers, outcome):
    assert kyc_gate.employer_consistency("CASE-1", _repo(deed=False, employers=employers))["outcome"] == outcome


def test_an_employer_difference_is_a_review_never_a_block(monkeypatch):
    repo = _repo(deed=False, employers=[("SALARY_SLIP", "ABC Ltd"), ("EMPLOYMENT_PROOF", "XYZ Ltd")])
    monkeypatch.setattr(kyc_gate, "_parties", lambda case_id, repository: {"APP-1": "PRIMARY_APPLICANT"})
    monkeypatch.setattr(kyc_gate, "_name_check_enabled", lambda: False)
    result = kyc_gate.evaluate("CASE-1", repo)
    check = next(c for c in result["parties"][0]["checks"] if c["check"] == "EMPLOYER_CONSISTENCY")
    assert check["status"] == "REVIEW" and check["counted"] is False


def test_a_lap_property_owned_by_someone_else_blocks_the_gate(monkeypatch):
    repo = _repo(buyers=["KARAN SETHI"])
    monkeypatch.setattr(kyc_gate, "_parties", lambda case_id, repository: {"APP-1": "PRIMARY_APPLICANT"})
    monkeypatch.setattr(kyc_gate, "_name_check_enabled", lambda: False)
    result = kyc_gate.evaluate("CASE-1", repo)
    check = next(c for c in result["parties"][0]["checks"] if c["check"] == "PROPERTY_OWNERSHIP")
    assert check["status"] == "BLOCKED" and check["counted"] is True
    assert "PROPERTY_OWNERSHIP" in result["required_checks"] and result["status"] == "BLOCKED"
