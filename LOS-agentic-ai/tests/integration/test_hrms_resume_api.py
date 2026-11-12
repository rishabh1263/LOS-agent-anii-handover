"""
HRMS RESUME API -- one call, verify + extract (2026-10-16). Positive and negative, end to end, real OCR, real auth.

    POST /api/v1/hrms/resume/verify-extract

Any document that is not a resume is refused (UNSUPPORTED, with what it looks like), in milliseconds for a Word or
native PDF file; a resume's fields are returned only when it is VERIFIED or MANUAL_REVIEW.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SET = ROOT / "samples" / "synthetic_resume" / "heldout"
LEAKS = ("Traceback", "File \"", "site-packages", "C:\\")
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-" + "TEST-FILE!$H+H*").encode()
pytestmark = pytest.mark.skipif(not (SET / "ground_truth.json").is_file(),
                                reason="python -m evals.resume.synthetic --split heldout")


def _truth():
    return {k: v for k, v in json.loads((SET / "ground_truth.json").read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


def _pick(scenario, fmt=None, layout=None):
    for name, t in _truth().items():
        if t["scenario"] == scenario and (fmt is None or t["format"] == fmt) and (layout is None or t.get("layout") == layout):
            return name, t
    pytest.skip(f"no {scenario}/{fmt}/{layout}")


@pytest.fixture(scope="module", autouse=True)
def _warm_ocr_workers():
    """As the server does at startup (main.py): every OCR worker loads the model before the first request is timed."""
    from app.agents.document_agent.ocr import get_ocr_executor
    from app.agents.passbook import engine

    pool = get_ocr_executor()
    for future in [pool.submit(engine.warm) for _ in range(pool._max_workers * 2)]:
        future.result(timeout=120)


@pytest.fixture
def hr(make_token):
    import main
    from app.agents.resume import service

    service.clear_cache()
    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {make_token(subject='hr-resume', scopes=['hrms:verify'])}"})
    return c


def _post(client, name, data, **form):
    started = time.perf_counter()
    r = client.post("/api/v1/hrms/resume/verify-extract", files={"file": (name, data)},
                    data={k: str(v) for k, v in form.items() if v is not None})
    assert not [x for x in LEAKS if x in r.text], r.text[:300]
    return r, time.perf_counter() - started


# ---- positive ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt,limit", [("docx", 0.5), ("pdf", 0.5), ("jpg", 3.0)])
@pytest.mark.parametrize("layout", ["experienced", "fresher", "cv"])
def test_a_genuine_resume_is_verified_with_its_name_only(hr, fmt, layout, limit):
    try:
        name, t = _pick("GENUINE", fmt, layout)
    except pytest.skip.Exception:
        name, t = _pick("GENUINE", fmt)
    r, seconds = _post(hr, name, (SET / name).read_bytes(), expected_name=t["expected_input"]["name"])
    body = r.json()
    assert r.status_code == 200 and body["status"] == "VERIFIED", body["reason_codes"]
    # owner 2026-10-21 (resume.yaml api_fields): the API gives ONLY the candidate's name -- no e-mail, phone, ...
    assert body["fields"] == {"name": t["fields"]["name"]}
    assert t["fields"]["email"] not in r.text and t["fields"]["phone"] not in r.text
    assert seconds < limit, (fmt, seconds)


def test_a_word_resume_is_read_without_ocr(hr, monkeypatch):
    from app.agents.passbook import engine

    monkeypatch.setattr(engine, "read", lambda *_a: (_ for _ in ()).throw(AssertionError("OCR ran on a .docx")))
    name, _t = _pick("GENUINE", "docx")
    body = _post(hr, name, (SET / name).read_bytes())[0].json()
    assert body["status"] == "VERIFIED" and body["source"] == "docx"


def test_a_surname_change_is_a_review_not_a_rejection(hr):
    name, t = _pick("GENUINE", "docx")
    first = t["fields"]["name"].split()[0]
    body = _post(hr, name, (SET / name).read_bytes(), expected_name=f"{first} KAPOOR")[0].json()
    assert body["status"] == "MANUAL_REVIEW" and "NAME_PARTLY_MATCHES" in body["reason_codes"]


def test_records_supplied_by_hr_are_cross_checked(hr):
    name, t = _pick("GENUINE", "pdf")
    ok = _post(hr, name, (SET / name).read_bytes(), year_10th=t["fields"]["year_10th"],
               year_12th=t["fields"]["year_12th"])[0].json()
    assert ok["status"] == "VERIFIED"
    off = _post(hr, name, (SET / name).read_bytes(), year_12th=t["fields"]["year_12th"] - 3)[0].json()
    assert off["status"] == "MANUAL_REVIEW" and "12TH_YEAR_MISMATCH" in off["reason_codes"]


# ---- negative: the resume itself ---------------------------------------------------------------------------------
@pytest.mark.parametrize("scenario,status,reason", [
    ("OVERLAP", "MANUAL_REVIEW", "OVERLAPPING_JOBS"),
    ("FUTURE_DATE", "MANUAL_REVIEW", "FUTURE_DATE"),
    ("EDU_ORDER", "MANUAL_REVIEW", "EDUCATION_TIMELINE_INCONSISTENT"),
    ("NO_CONTACT", "MANUAL_REVIEW", "CONTACT_MISSING"),
    ("NAME_MISMATCH", "REJECTED", "NAME_MISMATCH"),
])
@pytest.mark.parametrize("fmt", ["docx", "pdf", "jpg"])
def test_each_problem_is_caught_and_never_verified(hr, scenario, status, reason, fmt):
    name, t = _pick(scenario, fmt)
    body = _post(hr, name, (SET / name).read_bytes(), expected_name=t["expected_input"].get("name"))[0].json()
    assert body["status"] == status, (scenario, fmt, body["status"], body["reason_codes"])
    assert reason in body["reason_codes"]
    if status == "REJECTED":
        assert body["fields"] is None


# ---- negative: not a resume -- refused, fast ---------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["offer_letter", "marksheet", "article"])
@pytest.mark.parametrize("fmt,limit", [("docx", 0.1), ("pdf", 0.2), ("jpg", 3.0)])
def test_a_document_that_is_not_a_resume_is_refused(hr, kind, fmt, limit):
    name = next(k for k, v in _truth().items() if v["scenario"] == "NOT_RESUME" and v.get("kind") == kind
                and v["format"] == fmt)
    r, seconds = _post(hr, name, (SET / name).read_bytes())
    body = r.json()
    assert body["status"] == "UNSUPPORTED" and body["fields"] is None, (kind, fmt, body["status"])
    assert seconds < limit, (kind, fmt, seconds)


@pytest.mark.parametrize("path,looks_like", [("samples/documents/lPan.jpg", "PAN_CARD"),
                                             ("samples/real_test/voter4.jpg", None),
                                             ("samples/passports/passport_samples0_10.jpg", None)])
def test_an_identity_card_is_not_a_resume(hr, path, looks_like):
    p = ROOT / path
    if not p.is_file():
        pytest.skip("sample not present")
    r, _ = _post(hr, p.name, p.read_bytes())
    body = r.json() if r.status_code == 200 else {"status": "REFUSED"}
    assert body["status"] in ("UNSUPPORTED", "UNREADABLE", "REFUSED")
    assert r.status_code != 200 or body["fields"] is None
    if looks_like and r.status_code == 200:
        assert body["looks_like"] == looks_like


def test_a_passbook_is_not_a_resume(hr):
    folder = ROOT / "samples" / "synthetic_passbook" / "heldout"
    p = sorted(folder.glob("*genuine_clean*.jpg"))[0]
    body = _post(hr, p.name, p.read_bytes())[0].json()
    assert body["status"] == "UNSUPPORTED" and body["looks_like"] == "BANK_PASSBOOK"


def test_a_blank_photo_is_refused_before_any_ocr(hr, monkeypatch):
    from PIL import Image

    from app.agents.passbook import engine

    monkeypatch.setattr(engine, "read", lambda *_a: (_ for _ in ()).throw(AssertionError("OCR ran on a blank")))
    buffer = io.BytesIO()
    Image.new("RGB", (1240, 1754), (255, 255, 255)).save(buffer, "JPEG")
    r, seconds = _post(hr, "blank.jpg", buffer.getvalue())
    assert r.json()["status"] == "UNREADABLE" and seconds < 0.2


# ---- negative: bad and hostile files -----------------------------------------------------------------------------
def _docx(extra: dict[str, bytes] | None = None, body: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        if body:
            z.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/"
                                            "wordprocessingml/2006/main'><w:body/></w:document>")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buffer.getvalue()


def _encrypted_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "RESUME", fontsize=12)
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")


@pytest.mark.parametrize("name,data,code", [
    ("cv.pdf", b"", 400),
    ("cv.exe", b"MZ\x90\x00", 415),
    ("cv.doc", b"\xd0\xcf\x11\xe0", 415),
    ("cv.pdf", EICAR, 422),
    ("cv.docx", EICAR, 422),
    ("cv.docx", _docx({"word/vbaProject.bin": b"\x00" * 64}), 422),
    ("cv.docx", _docx(body=False), 422),
    ("cv.docx", _docx({"word/media/payload.exe": b"MZ" + b"\x00" * 64}), 422),
    ("cv.docx", b"not a zip at all", 422),
    ("cv.pdf", b"%PDF-1.4 garbage", 422),
    ("cv.pdf", _encrypted_pdf(), 422),
    ("cv.jpg", b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4, 422),
])
def test_bad_files_are_clean_4xx(hr, name, data, code):
    r, _ = _post(hr, name, data)
    assert r.status_code == code, (name, r.status_code, r.text[:200])


def test_a_zip_bomb_word_file_is_refused(hr):
    big = _docx({"word/media/huge.bin": b"\x00" * (70 * 2**20)})
    assert len(big) < 1 * 2**20
    r, _ = _post(hr, "cv.docx", big)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ZIP_BOMB"


def test_an_oversized_upload_is_413(hr):
    assert _post(hr, "cv.pdf", b"%PDF-1.4" + b"\x00" * (10 * 2**20 + 10))[0].status_code == 413


def test_a_tiny_image_is_refused(hr):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 150), (240, 240, 240)).save(buffer, "PNG")
    ImageDraw = __import__("PIL.ImageDraw", fromlist=["x"])
    r, _ = _post(hr, "cv.png", buffer.getvalue())
    assert r.status_code in (200, 422) and (r.status_code == 422 or r.json()["status"] == "UNREADABLE")


# ---- security, reuse, recovery -----------------------------------------------------------------------------------
def test_no_token_is_401_and_a_weak_token_is_403(make_token, monkeypatch):
    import main

    monkeypatch.setenv("AUTH_ENABLED", "true")
    c = TestClient(main.app)
    name, _t = _pick("GENUINE", "docx")
    data = (SET / name).read_bytes()
    assert c.post("/api/v1/hrms/resume/verify-extract", files={"file": (name, data)}).status_code == 401
    for scopes in (["read_applicant"], ["pan:verify"]):
        h = {"Authorization": f"Bearer {make_token(scopes=scopes)}"}
        assert c.post("/api/v1/hrms/resume/verify-extract", files={"file": (name, data)}, headers=h).status_code == 403


def test_one_callers_result_is_never_served_to_another(make_token):
    import main
    from app.agents.resume import service

    service.clear_cache()
    c = TestClient(main.app)
    name, _t = _pick("GENUINE", "pdf")
    data = (SET / name).read_bytes()
    a = {"Authorization": f"Bearer {make_token(subject='hr-a', scopes=['hrms:verify'])}"}
    b = {"Authorization": f"Bearer {make_token(subject='hr-b', scopes=['hrms:verify'])}"}
    post = lambda h: c.post("/api/v1/hrms/resume/verify-extract", files={"file": (name, data)}, headers=h).json()
    assert post(a)["reused_result"] is False
    assert post(b)["reused_result"] is False
    assert post(a)["reused_result"] is True


def test_no_resume_content_reaches_the_logs(hr, caplog):
    import logging

    name, t = _pick("GENUINE", "pdf")
    with caplog.at_level(logging.DEBUG):
        _post(hr, name, (SET / name).read_bytes())
    for value in (t["fields"]["email"], t["fields"]["phone"], t["fields"]["name"]):
        assert value not in caplog.text


def test_a_failure_is_a_clean_500_and_the_next_request_works(hr, monkeypatch):
    from app.agents.resume import parse

    original = parse.parse
    monkeypatch.setattr(parse, "parse", lambda *_a: (_ for _ in ()).throw(RuntimeError("parser exploded")))
    name, _t = _pick("GENUINE", "pdf")
    r, _ = _post(hr, name, (SET / name).read_bytes())
    assert r.status_code == 500 and r.json()["detail"]["status"] == "ERROR" and "exploded" not in r.text
    monkeypatch.setattr(parse, "parse", original)
    assert _post(hr, name, (SET / name).read_bytes())[0].json()["status"] == "VERIFIED"


# ---- gaps found on REAL sample resumes (University of Baltimore career centre, 2026-10-16) -----------------------
@pytest.mark.parametrize("written", ["+91 98765 43210", "987-654-3210", "(987) 654 3210", "09876543210",
                                     "+91-98765-43210", "9876543210", "Mobile: 98765.43210"])
def test_every_common_way_of_writing_an_indian_mobile_is_read(written):
    from app.agents.resume import parse

    assert parse.parse(["RAHUL SHARMA", written, "EDUCATION", "B.Com, Mumbai University, 2018"]).phone == "9876543210"


@pytest.mark.parametrize("not_a_phone", ["12/05/2019", "Pune - 411045", "2019-2021", "Tel: 91-20-2721 8080"])
def test_dates_pins_and_landlines_are_not_mobiles(not_a_phone):
    from app.agents.resume import parse

    assert parse.parse(["RAHUL SHARMA", not_a_phone]).phone is None


@pytest.mark.parametrize("heading", ["RELEVANT EXPERIENCE", "Relavent Experience", "ADDITIONAL EXPERIENCE",
                                     "Leadership Experience", "Technical Skills:", "Skills & Abilities"])
def test_heading_variants_are_sections(heading):
    from app.agents.resume import parse

    assert parse.heading_of(heading) in ("experience", "skills")


def test_a_sentence_mentioning_experience_is_not_a_heading():
    from app.agents.resume import parse

    assert parse.heading_of("Handled customer experience for 3 years") is None


def _jobs_verdict(lines):
    from app.agents.resume import parse, verify

    resume = parse.parse(lines)
    return verify.decide("\n".join(lines) + " " * 200, resume)


BASE = ["RAHUL SHARMA", "rahul.sharma@gmail.com", "EDUCATION", "B.Com, Mumbai University, 2015", "WORK EXPERIENCE"]


def test_a_promotion_inside_one_employer_is_not_two_jobs():
    v = _jobs_verdict(BASE + ["EMG Corporation, Pune 11/2018-Present", "Portfolio Coordinator 01/2022-Present",
                              "Program Coordinator 11/2018-01/2022"])
    assert "OVERLAPPING_JOBS" not in v.reasons, v.reasons


def test_a_volunteer_or_intern_role_beside_a_job_is_not_an_overlap():
    v = _jobs_verdict(BASE + ["Sales Associate, Big Bazaar Jun 2014 - Mar 2016",
                              "Volunteer, City Hospital May 2014 - Aug 2015", "Intern, Infosys Jan 2015 - Dec 2015"])
    assert "OVERLAPPING_JOBS" not in v.reasons, v.reasons


def test_two_real_jobs_at_once_are_still_flagged():
    v = _jobs_verdict(BASE + ["Credit Analyst, HDFC Bank Jan 2019 - Dec 2021",
                              "Sales Officer, ICICI Bank Mar 2020 - Present"])
    assert "OVERLAPPING_JOBS" in v.reasons and v.status == "MANUAL_REVIEW"


def test_coursework_is_not_a_qualification_and_placeholders_are_not_years():
    from app.agents.resume import parse

    r = parse.parse(["JANE SMITH", "jane@x.com", "EDUCATION", "Bachelor of Science in Accounting, University of X",
                     "• Coursework: Intermediate Accounting I, II and III", "Master of Business Administration, 20XX"])
    levels = {e.level: e.year for e in r.education}
    assert "12th" not in levels and levels.get("post_graduation") is None


def test_a_graduation_date_line_is_not_a_bachelors_degree():
    from app.agents.resume import parse

    r = parse.parse(["JOHN DOE", "j@x.com", "EDUCATION", "Doctor of Philosophy, University of Baltimore",
                     "Graduation 12/2020", "Master of Public Health, 2015", "Bachelor of Arts, 2010"])
    assert [e.level for e in r.education if e.level == "graduation"] == ["graduation"]
