"""
INDUSTRY OCR RULES (2026-10-03): capture gate, perspective flattening, two-engine
voting, confidence routing and the reviewer correction loop. Each is evidence-gated:
nothing here can turn an uncertain read into a PASS.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents.document_agent import preprocess, voting, workflow
from app.agents.document_agent.schemas import (DocumentExtractionResult, DocumentStatus, DocumentType,
                                               ExtractedField, FieldStatus, ValidationStatus)


# ---- capture gate + confidence routing ------------------------------------------------
def _report(*codes):
    return SimpleNamespace(analysed=True, findings=[SimpleNamespace(reason_code=c, severity="SEVERE") for c in codes])


def _result(fields=None, warnings=None):
    return SimpleNamespace(fields=fields or {}, warnings=warnings or [])


def test_a_clean_pass_is_never_told_to_retake_even_with_glare():
    out = workflow._capture_and_review(verdict="PASS", quality_report=_report("SEVERE_GLARE"), result=_result())
    assert out["capture"]["retake_recommended"] is False and out["capture"]["issues"] == ["SEVERE_GLARE"]


def test_a_severe_photo_that_did_not_verify_is_told_to_retake():
    out = workflow._capture_and_review(verdict="REVIEW", quality_report=_report("DOCUMENT_IMAGE_SEVERELY_BLURRY"),
                                       result=_result())
    assert out["capture"]["retake_recommended"] is True and "Retake" in out["capture"]["hint"]


def test_low_confidence_and_disagreeing_fields_are_routed_to_review(monkeypatch):
    monkeypatch.setenv("DOCUMENT_FIELD_REVIEW_CONFIDENCE", "0.8")
    fields = {"name": ExtractedField(value="A", ocr_confidence=0.95, status=FieldStatus.EXTRACTED),
              "address": ExtractedField(value="X", ocr_confidence=0.4, status=FieldStatus.EXTRACTED)}
    out = workflow._capture_and_review(verdict="PASS", quality_report=None,
                                       result=_result(fields, ["engine_disagreement=dl_number"]))
    assert {"field": "address", "reason": "LOW_OCR_CONFIDENCE"} in out["review_fields"]
    assert {"field": "dl_number", "reason": "ENGINES_DISAGREE"} in out["review_fields"]
    assert {"field": "name", "reason": "LOW_OCR_CONFIDENCE"} not in out["review_fields"]


# ---- perspective flattening -----------------------------------------------------------
def test_flatten_leaves_an_image_without_a_document_outline_unchanged():
    from PIL import Image

    blank = Image.new("RGB", (800, 600), "white")
    assert preprocess.flatten(blank) is blank


def test_flatten_straightens_a_card_photographed_on_a_desk():
    import numpy as np
    from PIL import Image, ImageDraw

    desk = Image.new("RGB", (1200, 900), (60, 50, 40))
    ImageDraw.Draw(desk).polygon([(250, 180), (980, 230), (930, 720), (200, 660)], fill=(245, 245, 240))
    flat = preprocess.flatten(desk)
    assert flat is not desk
    assert np.asarray(flat).mean() > 200            # the card fills the result; the desk is gone


# ---- two-engine voting ----------------------------------------------------------------
def _field(value, valid=True):
    return ExtractedField(value=value, status=FieldStatus.EXTRACTED,
                          validation=ValidationStatus.VALID if valid else ValidationStatus.INVALID)


def _recognition(fields):
    result = DocumentExtractionResult(document_type=DocumentType.PAN, status=DocumentStatus.PARTIAL, fields=fields)
    return SimpleNamespace(result=result, image=object())


def _second_engine(monkeypatch, fields):
    from app.agents.document_agent import ocr, pipeline

    monkeypatch.setattr(ocr, "TesseractEngine", lambda: SimpleNamespace(read_array=lambda a: ([], 0.0)))
    monkeypatch.setattr("app.agents.document_agent.preprocess.to_array", lambda image: image)
    monkeypatch.setattr(pipeline, "extract_from_tokens", lambda tokens, **kw: DocumentExtractionResult(
        document_type=DocumentType.PAN, status=DocumentStatus.SUCCESS, fields=fields))


def test_a_missing_required_field_is_filled_only_by_a_validated_second_read(monkeypatch):
    _second_engine(monkeypatch, {"pan_number": _field("ABCDE1234F"), "name": _field("A PERSON")})
    rec = _recognition({"name": _field("A PERSON")})
    vote = voting.second_opinion(rec)
    assert vote["ran"] and vote["filled"] == ["pan_number"]
    assert rec.result.fields["pan_number"].value == "ABCDE1234F"
    assert "second engine" in rec.result.fields["pan_number"].evidence


def test_an_invalid_second_read_is_never_accepted(monkeypatch):
    _second_engine(monkeypatch, {"pan_number": _field("ABCDE12345", valid=False)})
    rec = _recognition({"name": _field("A PERSON")})
    vote = voting.second_opinion(rec)
    assert vote["filled"] == [] and "pan_number" not in rec.result.fields


def test_valid_but_different_reads_are_a_disagreement_not_a_choice(monkeypatch):
    _second_engine(monkeypatch, {"pan_number": _field("ABCDE1234F"), "name": _field("OTHER NAME")})
    rec = _recognition({"pan_number": _field("ABCDE1234F", valid=False), "name": _field("A PERSON")})
    vote = voting.second_opinion(rec)
    assert "name" in vote["disagreements"] and rec.result.fields["name"].value == "A PERSON"


def test_a_complete_read_pays_for_no_second_engine(monkeypatch):
    called = []
    from app.agents.document_agent import ocr

    monkeypatch.setattr(ocr, "TesseractEngine", lambda: called.append(1))
    rec = _recognition({"pan_number": _field("ABCDE1234F"), "name": _field("A PERSON"),
                        "father_name": _field("B PERSON"), "date_of_birth": _field("01/01/1990")})
    assert voting.second_opinion(rec) == {"ran": False} and not called


# ---- the reviewer correction loop -----------------------------------------------------
APP, CASE, OWNER = "APP-FIX00000001", "CASE-FIX-000001", "fix-owner"
DOC = f"{CASE}:{APP}:pan.jpg"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.store import set_repository
    from app.store.models import Applicant, Application, Document, DocumentStatus as DS
    from app.store.sqlite_repo import SQLiteRepository

    monkeypatch.setenv("DOCUMENT_LABELS_PATH", str(tmp_path / "labels.jsonl"))
    r = SQLiteRepository(tmp_path / "fix.sqlite3")
    r.initialise()
    set_repository(r)
    r.save_applicant(Applicant(applicant_id=APP, full_name="Fix Person"))
    r.save_application(Application(case_id=CASE, applicant_id=APP, product="PERSONAL_LOAN"))
    r.save_document(Document(document_id=DOC, case_id=CASE, applicant_id=APP, document_type="PAN", party_id=APP,
                             status=DS.REVIEW, source_id="pan.jpg",
                             extracted_fields={"pan_number": {"value": "ABCDE1234X"}}))
    r.grant_access(OWNER, "APPLICANT", APP)
    yield r
    set_repository(None)


def _client(make_token, scopes):
    import main

    c = TestClient(main.app)
    c.headers["Authorization"] = "Bearer " + make_token(subject=OWNER, scopes=scopes)
    return c


def test_a_reviewer_correction_is_kept_beside_the_original_audited_and_labelled(repo, make_token, tmp_path):
    url = f"/api/v1/los/cases/{CASE}/documents/{DOC}/corrections"
    body = {"field": "pan_number", "corrected_value": "ABCDE1234F", "reason": "last character misread as X"}
    denied = _client(make_token, ["read_documents", "upload_document"]).post(url, json=body)
    assert denied.status_code == 403
    ok = _client(make_token, ["documents:review"]).post(url, json=body)
    assert ok.status_code == 200 and ok.json()["original_kept"] is True
    stored = repo.get_document(DOC).extracted_fields
    assert stored["pan_number"]["value"] == "ABCDE1234X"                       # original untouched
    assert stored["_corrections"]["pan_number"]["value"] == "ABCDE1234F"
    event = [e for e in repo.get_case_timeline(CASE) if e.event_type == "DOCUMENT_FIELD_CORRECTED"][0]
    assert "ABCDE1234" not in (event.summary or "")                              # no values on the timeline
    label = json.loads((tmp_path / "labels.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert (label["extracted"], label["corrected"], label["field"]) == ("ABCDE1234X", "ABCDE1234F", "pan_number")
