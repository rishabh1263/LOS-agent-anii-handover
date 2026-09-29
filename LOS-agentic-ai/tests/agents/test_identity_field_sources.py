"""
An identity number is found where the PIPELINE actually stores it: the
extraction finding of a file whose type is recorded on the verification finding
or the document row -- not only on the extraction itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.applicant.copilot.facts import field_state
from app.store import request_cache


def _finding(kind, payload, source_id="pan.jpg", party_id="APP-1"):
    return SimpleNamespace(finding_kind=kind, payload=payload, source_id=source_id,
                           document_id=None, party_id=party_id)


class _Repo:
    def __init__(self, findings, documents):
        self._findings, self._documents = findings, documents

    def get_current_findings(self, case_id, party_id=None):
        return [f for f in self._findings if party_id is None or f.party_id in (None, party_id)]

    def list_documents(self, case_id):
        return list(self._documents)


def _resolve(monkeypatch, findings, documents, field="pan_number", party_id="APP-1"):
    repo = _Repo(findings, documents)
    monkeypatch.setattr("app.store.get_repository", lambda: repo)
    with request_cache.scoped():
        return field_state.resolve(field, {}, case_id="CASE-1", party_id=party_id)


def _pan_document(party_id="APP-1", extracted=None):
    return SimpleNamespace(document_type="PAN", source_id="pan.jpg", document_id="D-1",
                           party_id=party_id, extracted_fields=extracted)


def test_the_extraction_is_matched_through_the_verification_finding_of_the_same_file(monkeypatch):
    found = _resolve(monkeypatch, [
        _finding("EXTRACTION", {"pan_number": "ABCDE1234F", "name": "X"}),       # no "type"
        _finding("VERIFICATION", {"type": "PAN", "expected_type": "PAN"}),
    ], [_pan_document()])
    assert found.state is field_state.FieldState.PRESENT
    assert found.value == "ABCDE1234F" and found.source == "document:PAN:extraction"


def test_the_extraction_is_matched_through_the_document_row_alone(monkeypatch):
    found = _resolve(monkeypatch, [_finding("EXTRACTION", {"pan_number": "ABCDE1234F"})],
                     [_pan_document()])
    assert found.state is field_state.FieldState.PRESENT


def test_the_document_rows_extracted_fields_are_a_source(monkeypatch):
    found = _resolve(monkeypatch, [], [_pan_document(extracted='{"pan_number": "ABCDE1234F"}')])
    assert found.state is field_state.FieldState.PRESENT
    assert found.source == "document:PAN:document_record"


def test_an_uploaded_document_with_no_number_read_is_not_available(monkeypatch):
    found = _resolve(monkeypatch, [_finding("VERIFICATION", {"type": "PAN"})], [_pan_document()])
    assert found.state is field_state.FieldState.NOT_AVAILABLE


def test_no_document_at_all_is_not_provided(monkeypatch):
    found = _resolve(monkeypatch, [], [])
    assert found.state is field_state.FieldState.NOT_PROVIDED


def test_another_partys_document_never_answers(monkeypatch):
    found = _resolve(monkeypatch, [
        _finding("EXTRACTION", {"pan_number": "ZZZPQ9999Z"}, party_id="CO-1"),
        _finding("VERIFICATION", {"type": "PAN"}, party_id="CO-1"),
    ], [_pan_document(party_id="CO-1")], party_id="APP-1")
    assert found.state is not field_state.FieldState.PRESENT
