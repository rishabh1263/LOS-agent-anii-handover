"""
THE PASSBOOK API SENDS ONLY THE HOLDER'S NAME (owner 2026-10-21, passbook.yaml `api`): with the production config,
every route gives `account_holder_name` and nothing else -- no account number, IFSC, bank or branch -- and only when
the passbook is VERIFIED; any other verdict sends no field. (The reading of all five fields is checked in
test_passbook_api.py / test_passbook_one_api.py, which open them for the test.)
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.integration.test_passbook_api import SET, _pick

pytestmark = pytest.mark.skipif(not (SET / "ground_truth.json").is_file(),
                                reason="python -m evals.passbook.synthetic --split heldout")


@pytest.fixture
def client(make_token):
    import main
    from app.agents.passbook import service

    service.clear_cache()
    c = TestClient(main.app, raise_server_exceptions=False)
    c.headers.update({"Authorization": f"Bearer {make_token(scopes=['documents:write'])}"})
    return c


def _post(client, route: str, name: str):
    return client.post(route, files={"file": (name, (SET / name).read_bytes(), "image/jpeg")})


@pytest.mark.parametrize("route", ["/api/v1/passbook/verify-extract", "/api/v1/passbook/verify-passbook",
                                   "/api/v1/passbook/extract-passbook"])
def test_a_verified_passbook_gives_only_the_holder_name(client, route):
    name, truth = _pick("GENUINE", "clean")
    r = _post(client, route, name)
    body = r.json()
    assert r.status_code == 200, body
    fields = body["fields"]
    held = fields["account_holder_name"]
    value = held["value"] if isinstance(held, dict) else held
    assert set(fields) == {"account_holder_name"} and value == truth["fields"]["account_holder_name"]
    for secret in (truth["fields"]["account_number"], truth["fields"]["ifsc_code"]):
        assert secret not in r.text                          # never sent, in any form
    assert truth["fields"]["account_number"][-4:] not in json.dumps(body.get("fields"))


def test_a_rejected_passbook_gives_no_field(client):
    name, _truth = _pick("BANK_CONFLICT")
    body = _post(client, "/api/v1/passbook/verify-extract", name).json()
    assert body["status"] == "REJECTED"
    assert body["fields"] is None and body["field_status"] is None


def test_a_passbook_for_review_gives_the_holder_name(client):
    """owner 2026-10-22: MANUAL_REVIEW (an old IFSC, a blurred account number ...) still gives the holder's name."""
    name, truth = _pick("BRANCH_MISMATCH")
    body = _post(client, "/api/v1/passbook/verify-extract", name).json()
    assert body["status"] == "MANUAL_REVIEW"
    assert body["fields"] == {"account_holder_name": truth["fields"]["account_holder_name"]}


def test_the_api_fields_come_from_config(client, monkeypatch):
    from app.agents.passbook import config as passbook_config

    monkeypatch.setitem(passbook_config.document(), "api",
                        {"fields": ["account_holder_name", "ifsc_code"], "extract_statuses": ["VERIFIED"]})
    name, truth = _pick("GENUINE", "clean")
    body = _post(client, "/api/v1/passbook/verify-extract", name).json()
    assert set(body["fields"]) == {"account_holder_name", "ifsc_code"}
    assert body["fields"]["ifsc_code"] == truth["fields"]["ifsc_code"]


def test_a_long_pdf_is_read_from_its_front_page(client):
    """Any number of pages (owner 2026-10-21: max_pdf_pages 0): the front page is read, the rest never looked at."""
    import io

    from PIL import Image

    name, truth = _pick("GENUINE", "clean")
    front = Image.open(SET / name).convert("RGB")
    filler = Image.new("RGB", front.size, "white")
    buffer = io.BytesIO()
    front.save(buffer, "PDF", save_all=True, append_images=[filler] * 5)          # 6 pages
    r = client.post("/api/v1/passbook/verify-extract", files={"file": ("six.pdf", buffer.getvalue(), "application/pdf")})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "VERIFIED", body
    assert body["fields"] == {"account_holder_name": truth["fields"]["account_holder_name"]}
