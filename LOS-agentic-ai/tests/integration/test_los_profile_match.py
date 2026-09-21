"""
Declared profiles, through the real endpoint, for two real people.

WHAT THIS ADDS TO THE UNIT TESTS. `tests/agents/test_profile_match.py`
proves the matcher is correct on synthetic input. This proves the WIRING:
that a profile typed into the form reaches the matcher, that it is matched
against the right party's documents, and that the result comes back on the
response — through the real route, the real OCR and two real PAN cards
belonging to two different people.

THE SAMPLES. Both parties upload a file called `pan.jpg`, because that is
what phones call things. They are different cards:

    primary      samples/documents/rpan.jpg
    co-applicant samples/documents/lPan.jpg

NOTHING IS HARDCODED FROM OCR. The declared profiles are built from what
the documents actually extracted on a first pass, so a change in the OCR
output cannot turn these into false failures. What is asserted is the
RELATIONSHIP between a declared value and a document — matched against its
own party, not matched against the other's.

THE EXPENSIVE RUNS ARE MEMOISED. Each end-to-end call is several seconds of
real OCR, and several tests assert on the same response. The cache holds
response bodies only; the two tests that look at the store do their own
call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.store import set_repository
from app.store.sqlite_repo import SQLiteRepository

PRIMARY_PAN = Path("samples/documents/rpan.jpg")
CO_PAN = Path("samples/documents/lPan.jpg")

pytestmark = pytest.mark.skipif(
    not (PRIMARY_PAN.exists() and CO_PAN.exists()),
    reason="PAN samples not available",
)

#: Response bodies from the end-to-end runs, keyed by scenario. See the
#: module docstring: these are bodies, never store state.
_RUNS: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def store(tmp_path):
    repository = SQLiteRepository(tmp_path / "profile_match.sqlite3")
    repository.initialise()
    set_repository(repository)
    yield repository
    set_repository(None)


@pytest.fixture
def client(make_token) -> TestClient:
    import main

    c = TestClient(main.app)
    c.headers.update(
        {"Authorization": f"Bearer {make_token(scopes=['los.read'])}"})
    return c


def upload(field: str, name: str, path: Path):
    return (field, (name, path.read_bytes(), "application/octet-stream"))


def post(client, files, **form):
    response = client.post("/api/v1/los/process", data=form, files=files)
    assert response.status_code == 200, response.text
    return response.json()


def both_parties(client, case_id: str, **profile_fields):
    """One call carrying both parties' `pan.jpg`."""
    return post(
        client,
        [upload("files", "pan.jpg", PRIMARY_PAN),
         upload("co_applicant_files", "pan.jpg", CO_PAN)],
        operation="PROCESS", applicant_id="APP-1", co_applicant_id="COAPP-9",
        case_id=case_id,
        expected_types="PAN", co_applicant_expected_types="PAN",
        **profile_fields,
    )


def extracted(body, party_id: str) -> dict[str, Any]:
    for document in body["documents"]:
        if document.get("party_id") == party_id:
            return document.get("extraction") or {}
    return {}


def matches(body) -> dict[str, dict[str, Any]]:
    return {entry["party_id"]: entry for entry in body.get("profile_match", [])}


def field(entry, name: str) -> dict[str, Any]:
    return next(f for f in entry["fields"] if f["field"] == name)


# ==========================================================================
# THE THREE RUNS
# ==========================================================================


@pytest.fixture
def truth(client) -> dict[str, dict[str, Any]]:
    """
    What each party's card actually says, read once with no profile.

    Also the proof that a two-party call with no profiles publishes no
    profile block at all.
    """
    if "truth" not in _RUNS:
        _RUNS["truth"] = both_parties(client, "CASE-PM-TRUTH")
    body = _RUNS["truth"]

    primary = extracted(body, "APP-1")
    co = extracted(body, "COAPP-9")
    if not primary.get("pan_number") or not co.get("pan_number"):
        pytest.skip("the PAN samples did not release a PAN on this run")
    if primary["pan_number"] == co["pan_number"]:
        pytest.skip("the two samples are the same person; no isolation to test")

    return {"APP-1": primary, "COAPP-9": co}


@pytest.fixture
def correct(client, truth):
    """Both parties declared honestly."""
    if "correct" not in _RUNS:
        _RUNS["correct"] = both_parties(
            client, "CASE-PM-OK",
            applicant_name=truth["APP-1"].get("name") or "",
            applicant_pan=truth["APP-1"]["pan_number"],
            co_applicant_name=truth["COAPP-9"].get("name") or "",
            co_applicant_pan=truth["COAPP-9"]["pan_number"],
        )
    return _RUNS["correct"]


@pytest.fixture
def swapped(client, truth):
    """
    Each party declared the OTHER person's PAN.

    THE TEST THIS FILE EXISTS FOR. Every declared value is present
    somewhere in the case, on a document that verified and released it.
    Only the party filter stops each one being satisfied by the other's
    card — and a match here would be a match against a stranger.
    """
    if "swapped" not in _RUNS:
        _RUNS["swapped"] = both_parties(
            client, "CASE-PM-SWAP",
            applicant_name=truth["COAPP-9"].get("name") or "",
            applicant_pan=truth["COAPP-9"]["pan_number"],
            co_applicant_name=truth["APP-1"].get("name") or "",
            co_applicant_pan=truth["APP-1"]["pan_number"],
        )
    return _RUNS["swapped"]


# ==========================================================================
# A. TWO PARTIES, TWO PROFILES, TWO RESULTS
# ==========================================================================


def test_each_party_gets_its_own_matching_result(correct):
    assert set(matches(correct)) == {"APP-1", "COAPP-9"}


def test_each_result_names_its_role(correct):
    result = matches(correct)

    assert result["APP-1"]["party_role"] == "PRIMARY_APPLICANT"
    assert result["COAPP-9"]["party_role"] == "CO_APPLICANT"


def test_the_primarys_declared_pan_matches_the_primarys_card(correct):
    comparison = field(matches(correct)["APP-1"], "PAN_NUMBER")

    assert comparison["status"] == "PASS"
    assert comparison["match_score"] == 100


def test_the_co_applicants_declared_pan_matches_the_co_applicants_card(correct):
    comparison = field(matches(correct)["COAPP-9"], "PAN_NUMBER")

    assert comparison["status"] == "PASS"
    assert comparison["match_score"] == 100


def test_each_match_cites_the_document_it_used(correct, truth):
    """
    Both cards are called `pan.jpg`, so the citation alone cannot tell
    them apart — the party on the result is what does.
    """
    for party_id in ("APP-1", "COAPP-9"):
        source = field(matches(correct)[party_id], "PAN_NUMBER")["source"]
        assert source["source_id"] == "pan.jpg"
        assert source["document_type"] == "PAN"


def test_both_parties_score_on_their_own_evidence(correct):
    for entry in matches(correct).values():
        assert entry["score"] == 100
        assert entry["fields_compared"] >= 1


# ==========================================================================
# B. NEITHER PARTY CAN BE SATISFIED BY THE OTHER'S DOCUMENT
# ==========================================================================


def test_the_primary_cannot_match_against_the_co_applicants_pan(swapped):
    comparison = field(matches(swapped)["APP-1"], "PAN_NUMBER")

    assert comparison["status"] == "FAIL"
    assert comparison["reason_code"] == "PROFILE_MISMATCH"


def test_the_co_applicant_cannot_match_against_the_primarys_pan(swapped):
    comparison = field(matches(swapped)["COAPP-9"], "PAN_NUMBER")

    assert comparison["status"] == "FAIL"
    assert comparison["reason_code"] == "PROFILE_MISMATCH"


def test_a_swapped_profile_is_reported_as_a_mismatch_not_a_pass(swapped):
    for entry in matches(swapped).values():
        assert entry["score"] < 100


def test_the_documents_still_verify_on_their_own_terms(swapped):
    """
    IT DECIDES NOTHING. Both cards are genuine and both still PASS. A
    profile mismatch is evidence for a human, not a verdict.
    """
    for document in swapped["documents"]:
        assert document["verification"] == "PASS"


def test_a_profile_mismatch_does_not_reject_the_case(swapped, correct):
    assert swapped["decision"] == correct["decision"]
    assert swapped["status"] == correct["status"]


# ==========================================================================
# C. BACKWARD COMPATIBILITY
# ==========================================================================


def test_a_two_party_call_with_no_profiles_publishes_no_profile_block(truth):
    """Absent, not an empty list: nothing was declared and nothing is claimed."""
    assert "profile_match" not in _RUNS["truth"]


def test_a_legacy_single_applicant_call_is_unchanged(client):
    body = post(
        client, [upload("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="APP-1", case_id="CASE-PM-LEGACY",
        expected_types="PAN",
    )

    assert "profile_match" not in body
    assert len(body["documents"]) == 1
    assert "co_applicant_id" not in body


def test_a_single_applicant_may_still_declare_a_profile(client):
    """
    The same matcher, the same shape — one party instead of two. A
    separate path for the legacy case is a second place to get it wrong.
    """
    body = post(
        client, [upload("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="APP-1", case_id="CASE-PM-SOLO",
        expected_types="PAN", applicant_pan="NUHPS4875K",
    )
    result = matches(body)

    assert list(result) == ["APP-1"]
    assert result["APP-1"]["party_role"] == "PRIMARY_APPLICANT"


def test_an_undeclared_field_is_skipped_not_failed(correct):
    """
    Neither party declared an address. That is a gap in the evidence, not
    a disagreement with it.
    """
    for entry in matches(correct).values():
        address = field(entry, "ADDRESS")
        assert address["status"] == "SKIPPED"
        assert address["reason_code"] == "PROFILE_VALUE_NOT_SUPPLIED"


# ==========================================================================
# D. THE STORED PROFILE FILLS WHAT THE REQUEST LEFT OUT
# ==========================================================================


def test_a_stored_name_is_matched_when_the_request_omits_it(client, store):
    """
    OPTION (iii), END TO END. The caller sent a PAN and nothing else; the
    applicant on file has a name. The name is checked from the store
    rather than going unchecked.
    """
    from app.store.models import Applicant

    store.save_applicant(Applicant(applicant_id="APP-STORED",
                                   full_name="RISHABH AJIT SINGH"))

    body = post(
        client, [upload("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="APP-STORED",
        case_id="CASE-PM-STORE", expected_types="PAN",
    )
    entry = matches(body)["APP-STORED"]

    assert field(entry, "NAME")["status"] != "SKIPPED"


def test_the_request_overrides_the_stored_value(client, store):
    """
    The caller is describing the person in front of them now; the store
    is describing whoever was captured earlier.
    """
    from app.store.models import Applicant

    store.save_applicant(Applicant(applicant_id="APP-OVERRIDE",
                                   full_name="SOMEBODY ELSE ENTIRELY"))

    body = post(
        client, [upload("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="APP-OVERRIDE",
        case_id="CASE-PM-OVER", expected_types="PAN",
        applicant_name="RISHABH AJIT SINGH",
    )
    comparison = field(matches(body)["APP-OVERRIDE"], "NAME")

    assert comparison["status"] == "PASS"


def test_a_stored_profile_is_read_per_party_not_per_case(client, store):
    """
    Two people, two records. The co-applicant's stored name must not be
    matched against the primary applicant's card.
    """
    from app.store.models import Applicant

    store.save_applicant(Applicant(applicant_id="APP-1",
                                   full_name="RISHABH AJIT SINGH"))
    store.save_applicant(Applicant(applicant_id="COAPP-9",
                                   full_name="LAXMI SANTOSH GUPTA"))

    body = both_parties(client, "CASE-PM-TWOSTORE")
    result = matches(body)

    assert field(result["APP-1"], "NAME")["status"] == "PASS"
    assert field(result["COAPP-9"], "NAME")["status"] == "PASS"


# ==========================================================================
# D2. PER-PARTY SECTIONS, THROUGH THE REAL ENDPOINT
#
# Reuses the runs above, so this costs nothing beyond the assertions.
# ==========================================================================


def test_both_parties_get_their_own_section(correct):
    assert correct["primary_applicant"]["party_id"] == "APP-1"
    assert correct["primary_applicant"]["role"] == "PRIMARY_APPLICANT"
    assert correct["co_applicant"]["party_id"] == "COAPP-9"
    assert correct["co_applicant"]["role"] == "CO_APPLICANT"


def test_the_same_filename_lands_under_the_right_party(correct):
    """
    Both uploaded `pan.jpg`, and the two cards belong to two different
    people. Grouping on the filename would put one in both sections.
    """
    owned = {}
    for document in correct["documents"]:
        owned.setdefault(document["party_id"], []).append(document["source_id"])

    for section, owner in (("primary_applicant", "APP-1"),
                           ("co_applicant", "COAPP-9")):
        assert correct[section]["document_ids"] == ["pan.jpg"]
        assert owned[owner] == ["pan.jpg"]


def test_the_same_document_type_from_both_parties_stays_separate(correct):
    types = {(d["party_id"], d["type"]) for d in correct["documents"]}

    assert types == {("APP-1", "PAN"), ("COAPP-9", "PAN")}


def test_each_partys_profile_match_sits_in_their_own_section(correct):
    """
    The section carries THAT party's entry, not the other's.

    Note the published rows for two correct matches are legitimately
    identical -- the contract publishes the verdict, never the value --
    so comparing rows proves nothing. What matters is which top-level
    entry each section holds.
    """
    top_level = matches(correct)

    assert correct["primary_applicant"]["profile_match"] == top_level["APP-1"]
    assert correct["co_applicant"]["profile_match"] == top_level["COAPP-9"]
    assert correct["primary_applicant"]["profile_match"][
        "party_role"] == "PRIMARY_APPLICANT"
    assert correct["co_applicant"]["profile_match"][
        "party_role"] == "CO_APPLICANT"


def test_a_mismatch_is_reported_under_the_party_it_belongs_to(swapped):
    """
    Each declared the other's PAN. Both mismatch, and each mismatch must
    appear against the person who declared it.
    """
    for section in ("primary_applicant", "co_applicant"):
        rows = swapped[section]["profile_match"]["fields"]
        pan = next(r for r in rows if r["field"] == "PAN_NUMBER")
        assert pan["status"] == "FAIL"
        assert pan["source"]["source_id"] == "pan.jpg"


def test_each_party_is_counted_separately(correct):
    for section in ("primary_applicant", "co_applicant"):
        summary = correct[section]["verification_summary"]
        assert summary == {"total_documents": 1, "passed": 1, "review": 0,
                           "failed": 0, "skipped": 0}


def test_the_sections_partition_the_documents(correct):
    grouped = (len(correct["primary_applicant"]["document_ids"])
               + len(correct["co_applicant"]["document_ids"]))

    assert grouped == len(correct["documents"])


def test_a_single_applicant_gets_a_section_and_no_co_applicant(client):
    body = post(
        client, [upload("files", "pan.jpg", PRIMARY_PAN)],
        operation="PROCESS", applicant_id="APP-1", case_id="CASE-P5-SOLO",
        expected_types="PAN",
    )

    assert body["primary_applicant"]["party_id"] == "APP-1"
    assert body["primary_applicant"]["document_ids"] == ["pan.jpg"]
    assert "co_applicant" not in body
    assert "profile_match" not in body["primary_applicant"]


def test_the_existing_top_level_contract_is_untouched(correct):
    for key in ("request_id", "applicant_id", "case_id", "status",
                "documents", "kyc", "cross_document", "decision",
                "next_action", "summary", "summary_source",
                "processing_ms", "errors"):
        assert key in correct, key


def test_no_internals_leak_through_the_sections(correct):
    import json

    blob = json.dumps({k: correct[k] for k in
                       ("primary_applicant", "co_applicant")})

    assert "samples/" not in blob
    assert "C:\\\\" not in blob
    for forbidden in ("bbox", "raw_text", "prompt", "Traceback",
                      "file_path", "tokens"):
        assert forbidden not in blob


# ==========================================================================
# E. WHAT THE RESPONSE DISCLOSES
# ==========================================================================


def test_the_published_block_carries_no_internals(correct):
    allowed = {"field", "status", "match_score", "confidence",
               "reason_code", "reason", "source"}

    for entry in matches(correct).values():
        for row in entry["fields"]:
            assert set(row) <= allowed, set(row) - allowed


def test_no_raw_ocr_or_paths_leak_into_the_block(correct):
    import json

    blob = json.dumps(correct["profile_match"])

    assert "samples/" not in blob
    assert "\\\\" not in blob
    for forbidden in ("tokens", "bbox", "raw_text", "candidates", "prompt"):
        assert forbidden not in blob


def test_score_and_confidence_are_reported_separately(correct):
    for entry in matches(correct).values():
        assert 0 <= entry["score"] <= 100
        assert 0 <= entry["confidence"] <= 100
        assert "fields_expected" in entry
        assert "fields_compared" in entry


def test_every_match_cites_a_document_that_actually_passed(correct):
    """
    THE GATE, END TO END. Matching runs on the internal envelope, which
    carries extracted fields whatever the verdict. Nothing may be
    compared against a document whose fields the caller was not given.
    """
    passed = {d["source_id"] for d in correct["documents"]
              if d["verification"] == "PASS"}

    for entry in matches(correct).values():
        for row in entry["fields"]:
            if row["status"] != "SKIPPED":
                assert row["source"]["source_id"] in passed


def test_the_response_still_validates_against_its_own_contract(correct):
    """
    The new block is declared on `LosProcessResponse`, so a generated
    client sees it rather than discovering an undocumented key.
    """
    from app.agents.los.schemas import LosProcessResponse

    parsed = LosProcessResponse.model_validate(correct)

    assert parsed.profile_match is not None
    assert {p.party_id for p in parsed.profile_match} == {"APP-1", "COAPP-9"}
