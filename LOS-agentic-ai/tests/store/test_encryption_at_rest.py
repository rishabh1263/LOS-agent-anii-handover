"""Document values in case_findings are encrypted at rest; codes and ids stay queryable."""

import json

import psycopg
import pytest

from app.store import crypto
from app.store.models import Application, Applicant, CaseFinding, FindingKind
from app.store.testing import fresh_repository


@pytest.fixture
def repo():
    r = fresh_repository()
    r.initialise()
    r.save_applicant(Applicant(applicant_id="A1", full_name="Asha Rao"))
    r.save_application(Application(case_id="C1", applicant_id="A1", product="PERSONAL_LOAN"))
    return r


def _raw(repo, kind):
    with psycopg.connect(repo._dsn) as c:
        return c.execute("SELECT payload, status FROM case_findings WHERE finding_kind = %s", (kind,)).fetchone()


def test_extraction_values_are_ciphertext_in_postgres_and_plain_through_the_repository(repo):
    repo.save_finding(CaseFinding(finding_id="F1", case_id="C1", finding_kind=FindingKind.EXTRACTION, party_id="A1",
                                  status="PASS", payload={"pan_number": "ABCDE1234F", "name": "ASHA RAO"},
                                  source_id="pan.jpg"))
    payload, status = _raw(repo, "EXTRACTION")
    assert "ABCDE1234F" not in payload and "ASHA" not in payload and json.loads(payload)["_enc"] == crypto.MARKER
    assert status == "PASS"                                            # codes stay in clear, queryable
    [f] = repo.get_case_findings("C1", kind=FindingKind.EXTRACTION)
    assert f.payload == {"pan_number": "ABCDE1234F", "name": "ASHA RAO"}


def test_kinds_without_document_values_stay_plain_and_old_plain_rows_still_read(repo):
    repo.save_finding(CaseFinding(finding_id="F2", case_id="C1", finding_kind=FindingKind.VERIFICATION,
                                  party_id="A1", status="PASS", payload={"type": "PAN"}, source_id="pan.jpg"))
    assert json.loads(_raw(repo, "VERIFICATION")[0]) == {"type": "PAN"}
    with psycopg.connect(repo._dsn, autocommit=True) as c:          # a row written before encryption
        c.execute("UPDATE case_findings SET finding_kind = 'EXTRACTION' WHERE finding_id = 'F2'")
    assert repo.get_case_findings("C1", kind=FindingKind.EXTRACTION)[0].payload == {"type": "PAN"}


def test_key_rotation_decrypts_with_an_older_key(monkeypatch):
    from cryptography.fernet import Fernet

    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv(crypto.ENV_KEY, old)
    sealed = crypto.seal("KYC", {"value": "x"})
    monkeypatch.setenv(crypto.ENV_KEY, f"{new},{old}")
    assert json.loads(crypto.open_(sealed)) == {"value": "x"}


def test_production_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    with pytest.raises(crypto.EncryptionConfigError):
        crypto.require_in_production()


def test_dev_login_refresh_tokens_live_in_postgres_and_are_single_use_under_a_race():
    from concurrent.futures import ThreadPoolExecutor

    from fastapi import HTTPException

    from app.security import dev_idp

    token = dev_idp.issue_refresh_token("dev-user")

    def rotate():
        try:
            return dev_idp.rotate_refresh_token(token)[0]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(lambda _: rotate(), range(8)))
    assert outcomes.count("dev-user") == 1 and outcomes.count(401) == 7
