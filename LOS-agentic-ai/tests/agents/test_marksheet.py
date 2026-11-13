"""
10th / 12th marksheet API (app/agents/marksheet): the verdict, the class, the candidate's name, and the fast refusals.
Synthetic samples only (scripts/make_marksheet_samples.py -> samples/marksheet/).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.agents.marksheet import config, quality, read, service

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "samples" / "marksheet" / "dev"
LABELS = json.loads((DEV / "labels.json").read_text(encoding="utf-8")) if (DEV / "labels.json").exists() else {}
URL = "/api/v1/marksheet/verify-marksheet"

pytestmark = pytest.mark.skipif(not LABELS, reason="run scripts/make_marksheet_samples.py first")


@pytest.fixture(scope="module", autouse=True)
def warm_engine():
    """The OCR model loaded once before anything is timed (a cold load is seconds; the server does it at startup)."""
    from app.agents.passbook import engine

    engine.warm()


def analyse(file: str):
    return service._analyse_sync((DEV / file).read_bytes(), file, "test")


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ==========================================================================
# config and the cheap checks
# ==========================================================================

def test_config_is_complete_and_has_no_yaml_booleans():
    assert config.missing() == []
    for word in config.get()["name"]["stop_words"]:
        assert isinstance(word, str), word                           # a bare NO would read as False


def test_a_blank_page_is_refused_before_ocr():
    q = quality.measure(Image.new("RGB", (1200, 1600), (255, 255, 255)), config.get()["quality"])
    assert q.blank and not q.blurry
    r = service._analyse_sync(_png(Image.new("RGB", (1200, 1600), (255, 255, 255))), "blank.png", "t")
    assert r.status == "BLANK" and r.source == "none" and "ocr_ms" not in r.timings_ms
    assert r.timings_ms["total_ms"] < 300


def test_a_smudged_page_is_unreadable_before_ocr():
    r = analyse("blurry_marksheet.jpg")
    assert r.status == "UNREADABLE" and r.reasons == ["IMAGE_TOO_BLURRY"] and "ocr_ms" not in r.timings_ms


@pytest.mark.parametrize("data, name, code", [
    (b"", "a.jpg", "EMPTY_FILE"),
    (b"not an image at all", "a.jpg", None),
    (b"%PDF-1.4 broken", "a.pdf", None),
])
def test_bad_files_are_refused(data, name, code):
    with pytest.raises(service.Refused) as refused:
        service._analyse_sync(data, name, "t")
    assert refused.value.status in (400, 415, 422) and (code is None or refused.value.code == code)


def test_a_tiny_image_is_refused():
    with pytest.raises(service.Refused) as refused:
        service._analyse_sync(_png(Image.new("RGB", (120, 90), (255, 255, 255))), "tiny.png", "t")
    assert refused.value.code == "IMAGE_TOO_SMALL"


# ==========================================================================
# reading the text
# ==========================================================================

def _rows(*lines):
    from app.agents.document_agent.schemas import OCRToken

    tokens = [OCRToken(text=t, confidence=0.95, x0=50, y0=40 * i, x1=900, y1=40 * i + 30) for i, t in enumerate(lines)]
    return read.rows_of(tokens)


@pytest.mark.parametrize("lines, cls", [
    (["CENTRAL BOARD OF SECONDARY EDUCATION", "SENIOR SCHOOL CERTIFICATE EXAMINATION 2021"], "12TH"),
    (["CENTRAL BOARD OF SECONDARY EDUCATION", "SECONDARY SCHOOL EXAMINATION 2019"], "10TH"),
    (["SHTRA STATE BOARD OF SECONDARY AND HIGHER SECONDARY EDUCATI", "SECONDARY SCHOOL CERTIFICATE EXAMINATION"], "10TH"),
    (["MAHARASHTRA STATE BOARD OF SECONDARY AND HIGHER SECONDARY EDUCATION", "HIGHER SECONDARY CERTIFICATE EXAMINATION"], "12TH"),
    (["BOARD OF HIGH SCHOOL AND INTERMEDIATE EDUCATION UTTAR PRADESH", "INTERMEDIATE EXAMINATION 2018"], "12TH"),
    (["BOARD OF HIGH SCHOOL AND INTERMEDIATE EDUCATION UTTAR PRADESH", "HIGH SCHOOL EXAMINATION 2016"], "10TH"),
    (["KARNATAKA SECONDARY EDUCATION EXAMINATION BOARD", "S.S.L.C EXAMINATION"], "10TH"),
    (["CLASS XII"], "12TH"),
    (["CLASS X"], "10TH"),
    (["SOME SCHOOL", "ANNUAL REPORT"], None),
])
def test_the_class_is_read_and_a_board_name_never_counts(lines, cls):
    assert read.school_class(_rows(*lines), config.get()) == cls


@pytest.mark.parametrize("lines, name", [
    (["This is to certify that RAHUL KUMAR SHARMA", "Mother's Name SUNITA SHARMA"], "RAHUL KUMAR SHARMA"),
    (["Name of Candidate : PRIYA NAIR Roll No 1234"], "PRIYA NAIR"),
    (["Father's Name : RAMESH PATIL", "Name of Candidate : ANIKET PATIL"], "ANIKET PATIL"),
    (["CANDIDATE'S FULL NAME (SURNAME FIRST) PATIL ANIKET", "CANDIDATE'S MOTHER'S NAME SUNITA"], "PATIL ANIKET"),
    (["Candidate Name", "MEERA MENON"], "MEERA MENON"),
    (["Mother's Name SUNITA", "Father's Name RAMESH"], None),
    (["Name of School : DPS RK PURAM"], None),
])
def test_the_candidates_name_never_a_parents(lines, name):
    assert read.candidate_name(_rows(*lines), config.get()["name"]).value == name


@pytest.mark.parametrize("text, kind", [
    ("INCOME TAX DEPARTMENT PERMANENT ACCOUNT NUMBER", "PAN"),
    ("STATE BANK OF INDIA PASSBOOK IFSC SBIN0001234", "PASSBOOK"),
    ("UNIVERSITY OF MUMBAI BACHELOR OF COMMERCE", "DEGREE"),
    ("CENTRAL BOARD OF SECONDARY EDUCATION MARKS STATEMENT", None),
])
def test_another_document_is_named(text, kind):
    assert read.other_document(text, config.get()["other_documents"]) == kind


# ==========================================================================
# end to end on the synthetic samples (one OCR each)
# ==========================================================================

@pytest.mark.parametrize("file", ["ms_cbse_12th_1.jpg", "ms_up_10th_1.jpg", "digital_cbse_12th.pdf"])
def test_marksheets_are_verified_with_class_and_name(file):
    want = LABELS[file]
    r = analyse(file)
    assert (r.status, r.school_class) == ("VERIFIED", want["class"])
    assert r.name.value.replace(" ", "") == want["name"].replace(" ", "") and r.name.status == "FOUND"
    assert r.timings_ms["total_ms"] < 2500


def test_a_digital_pdf_needs_no_ocr():
    r = analyse("digital_cbse_12th.pdf")
    assert r.source == "native_pdf_text" and "ocr_ms" not in r.timings_ms and r.timings_ms["total_ms"] < 300


@pytest.mark.parametrize("file, kind", [("other_pan_0.jpg", "PAN"), ("other_passbook_1.jpg", "PASSBOOK"),
                                        ("other_degree_0.jpg", "DEGREE"), ("other_letter_0.jpg", None)])
def test_wrong_documents_are_not_marksheets(file, kind):
    r = analyse(file)
    assert r.status == "NOT_MARKSHEET" and r.detected_as == kind and r.name.value is None


# ==========================================================================
# the API
# ==========================================================================

@pytest.fixture
def client():
    import main

    return TestClient(main.app)


def _auth(make_token, scopes=("documents:write",)):
    return {"Authorization": f"Bearer {make_token(subject='hrms-user', scopes=list(scopes))}"}


def test_api_verifies_and_reads_the_name(client, make_token):
    service.clear_cache()
    file = "ms_cbse_12th_1.jpg"
    r = client.post(URL, files={"file": (file, (DEV / file).read_bytes(), "image/jpeg")}, headers=_auth(make_token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "VERIFIED" and out["is_valid"] is True and out["document"]["class"] == "12TH"
    assert out["name"]["status"] == "FOUND" and out["name"]["value"]
    assert out["message"].startswith("This is a 12TH marksheet") and out["request_id"]
    again = client.post(URL, files={"file": (file, (DEV / file).read_bytes(), "image/jpeg")}, headers=_auth(make_token))
    assert again.json()["reused_result"] is True and again.json()["time_ms"] == 0.0


def test_api_answers_a_wrong_document_and_a_blank_page_plainly(client, make_token):
    pan = client.post(URL, files={"file": ("p.jpg", (DEV / "other_pan_0.jpg").read_bytes(), "image/jpeg")},
                      headers=_auth(make_token)).json()
    assert pan["status"] == "NOT_MARKSHEET" and pan["document"] == {"detected_as": "PAN"} and "PAN card" in pan["message"]
    blank = client.post(URL, files={"file": ("b.png", _png(Image.new("RGB", (900, 1200), "white")), "image/png")},
                        headers=_auth(make_token)).json()
    assert blank["status"] == "BLANK" and blank["is_valid"] is False and blank["time_ms"] < 300


@pytest.mark.parametrize("name, body, status, code", [
    ("a.docx", b"PK..", 415, "UNSUPPORTED_FILE_TYPE"),
    ("a.jpg", b"", 400, "EMPTY_FILE"),
    ("a.jpg", b"garbage bytes", 422, None),
])
def test_api_refusals(client, make_token, name, body, status, code):
    r = client.post(URL, files={"file": (name, body, "application/octet-stream")}, headers=_auth(make_token))
    assert r.status_code == status, r.text
    assert code is None or r.json()["detail"]["code"] == code


def test_api_needs_a_token_and_an_upload_scope(client, make_token):
    files = {"file": ("b.png", _png(Image.new("RGB", (900, 1200), "white")), "image/png")}
    assert client.post(URL, files=files).status_code == 401
    assert client.post(URL, files=files, headers=_auth(make_token, scopes=("read_applicant",))).status_code == 403


# ==========================================================================
# the class-specific calls: /verify-10th and /verify-12th
# ==========================================================================

@pytest.mark.parametrize("route, file, cls", [("verify-10th", "ms_cbse_10th_1.jpg", "10TH"),
                                              ("verify-12th", "ms_cbse_12th_1.jpg", "12TH")])
def test_class_api_verifies_its_own_class_and_reads_the_name(client, make_token, route, file, cls):
    service.clear_cache()
    r = client.post(f"/api/v1/marksheet/{route}", files={"file": (file, (DEV / file).read_bytes(), "image/jpeg")},
                    headers=_auth(make_token))
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["status"], out["is_valid"], out["expected_class"], out["document"]["class"]) == ("VERIFIED", True, cls, cls)
    assert out["name"]["status"] == "FOUND" and out["name"]["value"]


@pytest.mark.parametrize("route, file, cls, expected", [("verify-10th", "ms_cbse_12th_1.jpg", "12TH", "10TH"),
                                                        ("verify-12th", "ms_cbse_10th_1.jpg", "10TH", "12TH")])
def test_class_api_names_the_wrong_class_and_extracts_nothing(client, make_token, route, file, cls, expected):
    r = client.post(f"/api/v1/marksheet/{route}", files={"file": (file, (DEV / file).read_bytes(), "image/jpeg")},
                    headers=_auth(make_token))
    out = r.json()
    assert (out["status"], out["is_valid"], out["document"]["class"]) == ("WRONG_CLASS", False, cls)
    assert out["message"] == f"This is a {cls} marksheet, not a {expected} marksheet. Please upload the {expected} marksheet."
    assert f"EXPECTED_{expected}" in out["reason_codes"]
    assert out["name"] == {"status": "NOT_EXTRACTED"}                   # verification failed: nothing extracted


def test_class_api_refuses_another_document(client, make_token):
    r = client.post("/api/v1/marksheet/verify-12th",
                    files={"file": ("p.jpg", (DEV / "other_pan_0.jpg").read_bytes(), "image/jpeg")}, headers=_auth(make_token))
    assert r.status_code == 200 and r.json()["status"] == "NOT_MARKSHEET" and r.json()["is_valid"] is False
    assert r.json()["name"] == {"status": "NOT_EXTRACTED"}


def test_scope_check_works_without_the_applicant_agent(client, make_token, monkeypatch):
    """The standalone zip ships without app.agents.applicant: a caller without the upload scope gets a clean 403,
    never a 500 from a missing import."""
    import sys

    monkeypatch.setitem(sys.modules, "app.agents.applicant.config", None)
    r = client.post("/api/v1/marksheet/verify-10th", files={"file": ("a.jpg", b"x", "image/jpeg")},
                    headers=_auth(make_token, scopes=("read_applicant",)))
    assert r.status_code == 403
