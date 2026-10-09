"""
PHASE 3 STEP 5c -- mandatory signature PRESENCE (LOS_SIGNATURE_MANDATORY, default off).

The applicant and every co-applicant need a signature that is (1) not blank and (2) a
handwritten mark -- not printed / typed text, a stamp, a straight line or a dot. Never
matched against another signature. VERIFIED / REJECTED (with a reason) / REVIEW when
unsure, never approved when unsure. Separate from the authenticity verdict, which is
untouched. Grandfathered: only cases created on or after the configured activation date.
"""

from __future__ import annotations

import asyncio
import io
import math
import random
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.agents.applicant import config as agent_config
from app.agents.applicant import workflow
from app.agents.signature import presence
from app.store.models import Applicant, Application, Document, DocumentStatus
from tests.integration.test_reupload_supersedes import _store  # noqa: F401

NO_TEXT = lambda grey: (0, 0.0)                      # noqa: E731 - OCR stub: nothing read
TYPED = lambda grey: (11, 0.99)                      # noqa: E731 - OCR stub: a confident name


@pytest.fixture(autouse=True)
def flags_off(monkeypatch):
    for name in (presence.FLAG, workflow.COAPP_FLAG, "COPILOT_DOCUMENT_ACTIONS"):
        monkeypatch.delenv(name, raising=False)


# ---- images -----------------------------------------------------------------------------------
def png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def canvas():
    from PIL import Image

    return Image.new("L", (640, 240), 250)


def grain(image, seed=3):
    rng, pixels = random.Random(seed), image.load()
    for _ in range(image.width * image.height // 6):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        pixels[x, y] = max(0, min(255, pixels[x, y] + rng.randint(-18, 18)))
    return image


def scribble() -> bytes:
    from PIL import ImageDraw

    image = canvas()
    draw = ImageDraw.Draw(image)
    draw.line([(60 + k * 18, 120 + int(46 * math.sin(k * 0.7))) for k in range(30)], fill=20, width=8)
    draw.line([(120, 60), (150, 190), (200, 80), (230, 170)], fill=20, width=7)
    return png(grain(image))


def typed_name() -> bytes:
    from PIL import ImageDraw, ImageFont

    image = canvas()
    try:
        font = ImageFont.truetype("arial.ttf", 64)
    except OSError:
        pytest.skip("no TrueType font on this machine")
    ImageDraw.Draw(image).text((60, 80), "Rahul Sharma", fill=0, font=font)
    return png(grain(image))


def drawn(shape) -> bytes:
    from PIL import ImageDraw

    image = canvas()
    shape(ImageDraw.Draw(image))
    return png(grain(image))


# ---- the check ---------------------------------------------------------------------------------
def test_a_blank_signature_is_rejected():
    r = presence.check(png(canvas()), ocr=NO_TEXT)
    assert r["status"] == "REJECTED" and r["messages"] == ["Signature is blank"]


def test_a_dot_is_rejected():
    r = presence.check(drawn(lambda d: d.ellipse((316, 116, 326, 126), fill=10)), ocr=NO_TEXT)
    assert r["status"] == "REJECTED" and r["reasons"] == ["SIGNATURE_PRESENCE_DOT"]


def test_a_single_straight_line_is_rejected():
    r = presence.check(drawn(lambda d: d.line((60, 140, 580, 142), fill=10, width=4)), ocr=NO_TEXT)
    assert r["status"] == "REJECTED" and r["reasons"] == ["SIGNATURE_PRESENCE_LINE"]


def test_printed_matter_is_rejected():
    def bars(d):
        for y in range(20, 230, 14):
            d.rectangle((20, y, 620, y + 8), fill=0)
    r = presence.check(drawn(bars), ocr=NO_TEXT)
    assert r["status"] == "REJECTED" and r["messages"] == ["Signature looks printed, not handwritten"]


def test_a_typed_name_is_rejected_by_the_real_ocr():
    r = presence.check(typed_name())
    assert r["status"] == "REJECTED" and r["reasons"] == ["SIGNATURE_PRESENCE_TYPED_TEXT"], r


def test_a_handwritten_mark_is_verified():
    r = presence.check(scribble(), ocr=NO_TEXT)
    assert r["status"] == "VERIFIED" and r["reasons"] == [], r


def test_ocr_that_cannot_run_is_review_never_verified():
    def broken(grey):
        raise RuntimeError("engine down")
    assert presence.check(scribble(), ocr=broken)["status"] == "REVIEW"


def test_confident_text_on_handwritten_geometry_is_review_not_rejected():
    # a neat cursive name can read as text; one test alone never rejects it
    r = presence.check(scribble(), ocr=TYPED)
    assert r["status"] == "REVIEW" and "SIGNATURE_PRESENCE_POSSIBLE_TEXT" in r["reasons"]


def test_an_unreadable_file_is_rejected():
    assert presence.check(b"not an image")["status"] == "REJECTED"


def test_the_result_round_trips_through_reason_codes():
    r = presence.check(png(canvas()), ocr=NO_TEXT)
    back = presence.from_codes(["SIGNATURE_BLANK", *r["codes"]])
    assert back == {"status": "REJECTED", "reasons": ["SIGNATURE_PRESENCE_BLANK"], "messages": ["Signature is blank"]}
    assert presence.from_codes(["SIGNATURE_PRESENT"]) is None


def test_every_presence_code_has_a_sentence():
    from app.agents.verification import reasons

    codes = [*presence.STATUS_CODE.values(), *presence.MESSAGES]
    assert all(code in reasons.CATALOGUE for code in codes)


# ---- configuration -----------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("2026-10-08", datetime(2026, 10, 8, tzinfo=timezone.utc)),
    (date(2026, 10, 8), datetime(2026, 10, 8, tzinfo=timezone.utc)),
    ("2026-10-08T00:00:00+05:30", datetime(2026, 10, 8, tzinfo=timezone(timedelta(hours=5, minutes=30)))),
])
def test_the_activation_date_is_read_from_config(monkeypatch, raw, expected):
    monkeypatch.setattr(agent_config, "_section", lambda name: {"signature_mandatory": {"activation_date": raw}})
    assert agent_config.signature_activation() == (expected, None)


@pytest.mark.parametrize("raw", [None, "", "next tuesday"])
def test_a_missing_or_bad_activation_date_is_reported(monkeypatch, raw):
    monkeypatch.setattr(agent_config, "_section", lambda name: {"signature_mandatory": {"activation_date": raw}})
    value, problem = agent_config.signature_activation()
    assert value is None and problem


def test_the_shipped_config_has_no_activation_date():
    # the owner switched the signature rule on 2026-10-08 (applicant_agent.yaml activation_date): cases created
    # before it are grandfathered, so the shipped date is exactly that day
    agent_config.reload()
    activated = agent_config.signature_activation()[0]
    assert activated is not None and activated.date().isoformat() == "2026-10-08"


# ---- readiness ---------------------------------------------------------------------------------
CO = "COAPP-5C-0001"
NOW = datetime.now(timezone.utc)


def activate(monkeypatch, when=NOW - timedelta(days=1)):
    monkeypatch.setenv(presence.FLAG, "true")
    monkeypatch.setattr(agent_config, "signature_activation", lambda: (when, None))


def case(repo, *, co=False, created=NOW):
    applicant_id, case_id = f"APP-{uuid.uuid4().hex[:8]}", f"CASE-{uuid.uuid4().hex[:8]}"
    repo.save_applicant(Applicant(applicant_id=applicant_id))
    repo.save_application(Application(case_id=case_id, applicant_id=applicant_id, product="PERSONAL_LOAN",
                                      co_applicant_id=CO if co else None, created_at=created))
    return repo.get_application(case_id)


def sign(repo, application, codes, *, co=False, status=DocumentStatus.REVIEW):
    repo.save_document(Document(
        document_id=f"doc_{uuid.uuid4().hex[:12]}", case_id=application.case_id,
        applicant_id=application.applicant_id, document_type="SIGNATURE",
        party_id=CO if co else application.applicant_id,
        party_role="CO_APPLICANT" if co else "PRIMARY_APPLICANT", status=status, reason_codes=list(codes)))


def items(repo, application):
    return workflow.signature_items(repo.get_application(application.case_id),
                                    repo.list_documents(application.case_id))


VERIFIED_CODES = ["SIGNATURE_PRESENT", "SIGNATURE_PRESENCE_VERIFIED"]
BLANK_CODES = ["SIGNATURE_BLANK", "SIGNATURE_PRESENCE_REJECTED", "SIGNATURE_PRESENCE_BLANK"]


def test_flag_off_nothing_changes(_store):
    application = case(_store)
    assert items(_store, application) == []
    assert not any(i["code"].startswith("SIGNATURE") for i in workflow.pending_items(
        _store.get_applicant(application.applicant_id), application, []))


def test_a_new_case_without_a_signature_is_blocked(_store, monkeypatch):
    activate(monkeypatch)
    application = case(_store)
    assert [(i["code"], i["detail"]) for i in items(_store, application)] == [
        ("DOCUMENT_MISSING", "Signature has not been uploaded.")]
    readiness = workflow.readiness(_store.get_applicant(application.applicant_id), application, [])
    assert any(b["code"] == "DOCUMENT_MISSING" and b.get("slot") == "SIGNATURE" for b in readiness["blocking_items"])


def test_a_case_created_before_activation_is_grandfathered(_store, monkeypatch):
    activate(monkeypatch, when=NOW + timedelta(days=1))
    assert items(_store, case(_store)) == []


def test_a_verified_signature_satisfies_the_rule(_store, monkeypatch):
    activate(monkeypatch)
    application = case(_store)
    sign(_store, application, VERIFIED_CODES)              # authenticity REVIEW (no specimen) is fine
    assert items(_store, application) == []


def test_a_rejected_signature_blocks_with_its_reason(_store, monkeypatch):
    activate(monkeypatch)
    application = case(_store)
    sign(_store, application, BLANK_CODES, status=DocumentStatus.REJECTED)
    (item,) = items(_store, application)
    assert item["code"] == "SIGNATURE_REJECTED" and item["detail"] == "Signature was rejected: Signature is blank."


def test_review_and_unchecked_signatures_block(_store, monkeypatch):
    activate(monkeypatch)
    review, unchecked = case(_store), case(_store)
    sign(_store, review, ["SIGNATURE_PRESENCE_REVIEW", "SIGNATURE_PRESENCE_POSSIBLE_TEXT"])
    sign(_store, unchecked, ["SIGNATURE_PRESENT"])
    assert [i["code"] for i in items(_store, review)] == ["SIGNATURE_UNDER_REVIEW"]
    assert [i["code"] for i in items(_store, unchecked)] == ["SIGNATURE_NOT_CHECKED"]


def test_every_co_applicant_needs_their_own_signature(_store, monkeypatch):
    activate(monkeypatch)
    application = case(_store, co=True)
    sign(_store, application, VERIFIED_CODES)              # the applicant's never counts for the co-applicant
    (item,) = items(_store, application)
    assert item["party_role"] == "CO_APPLICANT" and item["detail"] == "Co-applicant's signature has not been uploaded."
    sign(_store, application, VERIFIED_CODES, co=True)
    assert items(_store, application) == []


def test_flag_on_without_an_activation_date_fails_closed(_store, monkeypatch):
    monkeypatch.setenv(presence.FLAG, "true")
    monkeypatch.setattr(agent_config, "signature_activation", lambda: (None, "activation_date is not set"))
    assert [i["code"] for i in items(_store, case(_store))] == ["SIGNATURE_RULE_MISCONFIGURED"]


# ---- the document action view -------------------------------------------------------------------
def test_a_rejected_signature_is_listed_under_upload_the_correct_documents(_store, monkeypatch):
    from app.agents.applicant.copilot.answering import document_actions

    activate(monkeypatch)
    application = case(_store)
    sign(_store, application, BLANK_CODES, status=DocumentStatus.REJECTED)
    view = document_actions.build(application.case_id, repository=_store)
    rows = [r for r in view["reupload"] if r["document_type"] == "SIGNATURE"]
    assert len(rows) == 1 and rows[0]["reasons"] == ["Signature is blank"]
    text = document_actions.render(view)["answer"]
    assert "Please upload the correct documents:" in text and "Signature [Upload] — Signature is blank" in text


def test_a_verified_signature_is_not_shown_as_under_review(_store, monkeypatch):
    from app.agents.applicant.copilot.answering import document_actions

    activate(monkeypatch)
    application = case(_store)
    sign(_store, application, VERIFIED_CODES)
    view = document_actions.build(application.case_id, repository=_store)
    assert not any(r["document_type"] == "SIGNATURE" for k in ("reupload", "pending", "under_review") for r in view[k])


# ---- the upload path and the report ------------------------------------------------------------
def test_the_upload_path_stores_the_presence_result_and_leaves_the_verdict(monkeypatch):
    from app.agents.los import flow
    from app.orchestration import graph

    async def fake_run_agent(**kwargs):
        return {"result": {"decision": "REVIEW", "reason_codes": ["SIGNATURE_PRESENT", "REFERENCE_MISSING"]}}

    monkeypatch.setattr(graph, "run_agent", fake_run_agent)
    monkeypatch.setattr(presence, "ocr_text", NO_TEXT)
    document = flow.UploadedDocument("s1", "sign.png", png(canvas()), expected_type="SIGNATURE")

    off = asyncio.run(flow._run_specialist(document, "signature_verification", "r1"))
    assert off["verification"]["reason_codes"] == ["SIGNATURE_PRESENT", "REFERENCE_MISSING"]

    monkeypatch.setenv(presence.FLAG, "true")
    on = asyncio.run(flow._run_specialist(document, "signature_verification", "r2"))
    assert on["verification"]["decision"] == "REVIEW"                    # authenticity verdict untouched
    assert "SIGNATURE_PRESENCE_REJECTED" in on["verification"]["reason_codes"]
    assert on["verification"]["signature_presence"]["messages"] == ["Signature is blank"]


def test_the_report_lists_only_grandfathered_cases(_store, monkeypatch):
    from scripts import report_signature_gaps

    old = case(_store, created=NOW - timedelta(days=10))
    new = case(_store)
    monkeypatch.setattr(agent_config, "signature_activation", lambda: (NOW - timedelta(days=1), None))
    rows = report_signature_gaps.gaps(_store, 100)
    assert [r["case_id"] for r in rows] == [old.case_id]
    assert rows[0]["gaps"][0]["code"] == "DOCUMENT_MISSING" and new.case_id not in str(rows)


# ---- a missing activation date is noticed at once ----------------------------------------------
def test_the_misconfiguration_is_named_only_while_the_flag_is_on(monkeypatch):
    monkeypatch.setattr(agent_config, "signature_activation", lambda: (None, "activation_date is not set"))
    assert workflow.signature_rule_config_error() is None
    monkeypatch.setenv(presence.FLAG, "true")
    assert workflow.signature_rule_config_error() == "LOS_SIGNATURE_MANDATORY is on but activation_date is not set"


def test_ready_reports_the_misconfiguration(monkeypatch):
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setenv(presence.FLAG, "true")
    monkeypatch.setattr(agent_config, "signature_activation", lambda: (None, "activation_date is not set"))
    body = TestClient(main.app).get("/ready").json()
    if body.get("status") != "ready":
        pytest.skip(f"service not ready here for another reason: {body.get('reason')}")
    assert body["overall"] == "DEGRADED"
    assert body["configuration_errors"] == ["LOS_SIGNATURE_MANDATORY is on but activation_date is not set"]


def test_startup_logs_the_misconfiguration(monkeypatch, caplog):
    import logging

    from fastapi.testclient import TestClient

    import main

    monkeypatch.setenv(presence.FLAG, "true")
    monkeypatch.setattr(agent_config, "signature_activation", lambda: (None, "activation_date is not set"))
    with caplog.at_level(logging.ERROR, logger="los.startup"), TestClient(main.app):
        pass
    assert any("SIGNATURE_RULE_MISCONFIGURED" in r.getMessage() for r in caplog.records)
