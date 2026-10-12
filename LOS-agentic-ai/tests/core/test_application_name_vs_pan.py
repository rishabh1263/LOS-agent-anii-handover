"""
APPLICATION NAME vs VERIFIED PAN (kyc_policies.yaml application_name, app/agents/los/kyc_gate.py).

The application's name is compared with the holder name read from the party's VERIFIED PAN by the existing
profile-match NAME comparator. Synthetic records only (an in-memory repository double).
"""
from types import SimpleNamespace

import pytest

from app.agents.los import kyc_gate


def _repo(*, declared="Arjun Mehta", pans=(("pan-1", "VERIFIED", "ARJUN MEHTA"),), party="APP-1",
          co=None, kyc_payload=None):
    documents, findings = [], []
    for doc_id, status, name in pans:
        owner = party
        if isinstance(status, tuple):
            status, owner = status
        documents.append(SimpleNamespace(document_id=doc_id, document_type="PAN", status=status, party_id=owner,
                                         party_role="CO_APPLICANT" if owner != party else "PRIMARY_APPLICANT"))
        if name is not None:
            findings.append(SimpleNamespace(document_id=doc_id, party_id=owner, finding_kind="EXTRACTION",
                                            payload={"fields": {"name": name}}))
    kyc = [SimpleNamespace(party_id=party, payload=kyc_payload or {
        "passed_checks": ["NAME", "DOB", "ADDRESS", "PAN", "FATHER_NAME"]})]

    def current(case_id, kind=None):
        return kyc if kind == "KYC" else findings if kind == "EXTRACTION" else kyc + findings

    return SimpleNamespace(
        list_documents=lambda case_id: documents,
        get_current_findings=current,
        get_application=lambda case_id: SimpleNamespace(applicant_id=party, co_applicant_id=co),
        get_applicant=lambda applicant_id: SimpleNamespace(full_name=declared) if applicant_id == party else None,
    )


def _match(repo, party="APP-1", role="PRIMARY_APPLICANT"):
    return kyc_gate.application_name_match("CASE-1", party, role, repo)


@pytest.mark.parametrize("declared, pan_name, outcome", [
    ("Arjun Mehta", "ARJUN MEHTA", "MATCH"),                    # case
    ("  arjun   mehta ", "ARJUN MEHTA", "MATCH"),               # whitespace
    ("Arjun Mehta", "KARAN SETHI", "MISMATCH"),                 # a different person's name
])
def test_the_outcome_follows_the_existing_comparator(declared, pan_name, outcome):
    assert _match(_repo(declared=declared, pans=(("pan-1", "VERIFIED", pan_name),)))["outcome"] == outcome


def test_a_middle_name_difference_is_never_silently_a_match():
    result = _match(_repo(declared="Arjun Kumar Mehta", pans=(("pan-1", "VERIFIED", "ARJUN MEHTA"),)))
    assert result["outcome"] in ("PARTIAL", "MISMATCH", "MATCH")
    # whatever the comparator decides, the evidence is kept, and a PARTIAL goes to a person (gate REVIEW)
    assert result["pan_name"] == "ARJUN MEHTA" and result["declared"] == "Arjun Kumar Mehta"
    if result["outcome"] == "PARTIAL":
        assert kyc_gate._NAME_OUTCOME["PARTIAL"] == kyc_gate.REVIEW


def test_a_pan_not_yet_verified_keeps_the_name_check_pending():
    result = _match(_repo(pans=(("pan-1", "PROCESSING", "ARJUN MEHTA"),)))
    assert result["outcome"] == "PENDING" and "not passed verification" in result["reason"]


def test_a_failed_pan_never_verifies_the_name():
    result = _match(_repo(pans=(("pan-1", "REJECTED", "ARJUN MEHTA"),)))
    assert result["outcome"] == "PENDING" and result["pan_verification"] == "REJECTED"


def test_no_pan_at_all_is_pending():
    assert _match(_repo(pans=()))["outcome"] == "PENDING"


def test_an_unreadable_pan_name_is_unverifiable_not_a_pass():
    result = _match(_repo(pans=(("pan-1", "VERIFIED", None),)))
    assert result["outcome"] == "UNVERIFIABLE" and kyc_gate._NAME_OUTCOME["UNVERIFIABLE"] != kyc_gate.PASS


def test_no_name_on_the_application_is_unverifiable():
    assert _match(_repo(declared=None))["outcome"] == "UNVERIFIABLE"


def test_two_verified_pans_the_worst_counts():
    result = _match(_repo(pans=(("pan-1", "VERIFIED", "ARJUN MEHTA"), ("pan-2", "VERIFIED", "KARAN SETHI"))))
    assert result["outcome"] == "MISMATCH" and result["pan_document_id"] == "pan-2"


def test_a_superseded_pan_is_ignored():
    result = _match(_repo(pans=(("pan-old", "SUPERSEDED", "KARAN SETHI"), ("pan-1", "VERIFIED", "ARJUN MEHTA"))))
    assert result["outcome"] == "MATCH"


def test_the_co_applicants_pan_is_never_matched_against_the_applicant():
    repo = _repo(pans=(("pan-co", ("VERIFIED", "CO-1"), "KARAN SETHI"),), co="CO-1")
    assert _match(repo)["outcome"] == "PENDING"                 # the applicant has no verified PAN of their own


def test_the_application_name_is_never_changed():
    repo = _repo(declared="Arjun Mehta", pans=(("pan-1", "VERIFIED", "KARAN SETHI"),))
    _match(repo)
    assert repo.get_applicant("APP-1").full_name == "Arjun Mehta"


def test_the_gate_counts_a_mismatch_and_shows_the_evidence(monkeypatch):
    repo = _repo(pans=(("pan-1", "VERIFIED", "KARAN SETHI"),))
    result = kyc_gate.evaluate("CASE-1", repo)
    (party,) = result["parties"]
    check = next(c for c in party["checks"] if c["check"] == kyc_gate.APPLICATION_NAME)
    assert check["status"] == kyc_gate.BLOCKED and party["status"] == kyc_gate.BLOCKED
    assert {v["value"] for v in check["values"]} == {"Arjun Mehta", "KARAN SETHI"}


def test_a_match_lets_the_other_checks_decide():
    result = kyc_gate.evaluate("CASE-1", _repo())
    assert result["parties"][0]["status"] == kyc_gate.PASS


def test_shown_but_not_counted_when_the_gate_does_not_require_it(monkeypatch):
    monkeypatch.setattr(kyc_gate, "_name_spec", lambda: {"enabled": True, "cpa_gate": False})
    result = kyc_gate.evaluate("CASE-1", _repo(pans=(("pan-1", "VERIFIED", "KARAN SETHI"),)))
    party = result["parties"][0]
    check = next(c for c in party["checks"] if c["check"] == kyc_gate.APPLICATION_NAME)
    assert check["status"] == kyc_gate.BLOCKED and check["counted"] is False
    assert party["status"] == kyc_gate.PASS                     # advisory only


@pytest.mark.parametrize("declared, pan_name", [("Arjun Mehra", "ARJUN MEHTA"),      # a different surname, FUZZY 91
                                                ("A Mehta", "ARJUN MEHTA"),          # initials
                                                ("Arjun Kumar Mehta", "ARJUN MEHTA")])  # a middle name
def test_a_lenient_comparator_pass_is_never_a_verified_match(declared, pan_name):
    result = _match(_repo(declared=declared, pans=(("pan-1", "VERIFIED", pan_name),)))
    assert result["outcome"] == "PARTIAL", result
    assert kyc_gate._NAME_OUTCOME["PARTIAL"] == kyc_gate.REVIEW          # a person decides


def test_word_order_is_a_match():
    assert _match(_repo(declared="Mehta Arjun", pans=(("pan-1", "VERIFIED", "ARJUN MEHTA"),)))["outcome"] == "MATCH"


def test_a_corrected_application_name_is_re_checked_with_no_cached_result():
    """Master prompt §8/§9: the check reads the CURRENT application name every time -- a misspelling corrected on
    the form flips the outcome on the next read, with nothing to invalidate by hand."""
    person = SimpleNamespace(full_name="Arjun Mehra")
    repo = _repo(pans=(("pan-1", "VERIFIED", "ARJUN MEHTA"),))
    repo.get_applicant = lambda applicant_id: person if applicant_id == "APP-1" else None
    before = _match(repo)
    assert before["outcome"] != "MATCH" and before["declared"] == "Arjun Mehra"
    person.full_name = "Arjun Mehta"                               # the officer's correction, as stored
    after = _match(repo)
    assert after["outcome"] == "MATCH" and after["declared"] == "Arjun Mehta"
    assert after["pan_name"] == "ARJUN MEHTA"                       # the PAN name never replaced the application's
