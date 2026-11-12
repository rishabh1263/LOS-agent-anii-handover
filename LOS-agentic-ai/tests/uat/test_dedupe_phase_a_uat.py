"""
DEDUPE PHASE A -- CONTROLLED UAT (owner-approved 2026-10-22, synthetic data only).

ISOLATION, CHECKED BY EVERY TEST BEFORE ANYTHING RUNS (`uat` fixture):
  - a FRESH Postgres database per test (a `los_t_*` clone of the migrated test template, never the dev store under
    runtime/pgdata), verified EMPTY before seeding;
  - an EMPTY document store in the test's own temp directory;
  - DEDUPE_ENABLED=true and a RANDOM per-run UAT HMAC secret set ONLY inside this process (monkeypatch); the secret is
    never written to disk, and UAT-12 checks it appears in no response, log record or audit line;
  - the policy file is the repository's (conditionally approved for UAT only); no production setting is touched; no
    migration is applied (UAT-00 checks party_identity / file_sha256 do not exist).

SYNTHETIC DATA: generated PANs (UATxx####x), mobiles 90000000xx, names "UAT PERSON n". No real customer data.
UNAVAILABLE SOURCES (AP / PR code, loan history, DPD, top-up, RCU) are NOT mocked as data: the agent's explicit
unavailable statuses are what is tested.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.agents.dedupe import config as C
from app.agents.dedupe import service as S
from app.security import access
from app.store import set_repository
from app.store.documents import LocalDocumentStore, set_document_store
from app.store.models import Applicant, Application, CaseFinding, Document, FindingKind
from app.store.testing import fresh_repository

FOS_SCOPES = ["read_applicant", "read_application", "read_documents", "create_applicant", "create_application",
              "upload_document"]
OFFICER = "uat-officer-1"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def pan(n: int) -> str:
    return f"UATP{chr(65 + n % 26)}{n:04d}Z"          # 5 letters + 4 digits + 1 letter, synthetic


def mobile(n: int) -> str:
    return f"90000{n:05d}"


class Uat:
    def __init__(self, repo, docs: Path, secret: str, audit: Path, make_token):
        self.repo, self.docs, self.secret, self.audit, self._make_token = repo, docs, secret, audit, make_token
        self.store = LocalDocumentStore(docs)

    # -- synthetic seeding (the store's own write path) --------------------------------------------------------
    def case(self, case_id, applicant_id, *, pan_=None, mobile_=None, product="PERSONAL_LOAN", minutes=0,
             co_applicant_id=None, co_pan=None):
        self.repo.save_applicant(Applicant(applicant_id=applicant_id, full_name=f"UAT PERSON {applicant_id}",
                                           mobile=mobile_))
        self.repo.save_application(Application(case_id=case_id, applicant_id=applicant_id, product=product,
                                               co_applicant_id=co_applicant_id,
                                               created_at=T0 + timedelta(minutes=minutes)))
        if pan_:
            self.extraction(case_id, applicant_id, pan_)
        if co_applicant_id and co_pan:
            self.extraction(case_id, co_applicant_id, co_pan)

    def extraction(self, case_id, party_id, pan_):
        self.repo.save_finding(CaseFinding(finding_id=uuid.uuid4().hex, case_id=case_id, party_id=party_id,
                                           finding_kind=FindingKind.EXTRACTION, status="PASS",
                                           payload={"pan_number": pan_, "name": "UAT PERSON"},
                                           source_id=f"pan-{party_id}.jpg"))

    def file(self, case_id, applicant_id, name, content: bytes):
        from app.store import documents as store_module

        doc_id = f"{case_id}:{applicant_id}:{name}"
        self.repo.save_document(Document(document_id=doc_id, case_id=case_id, applicant_id=applicant_id,
                                         document_type="BANK_STATEMENT", party_id=applicant_id, source_id=name))
        self.store.put(store_module.storage_key(doc_id), content)

    def client(self, scopes, subject=OFFICER) -> TestClient:
        from main import app

        c = TestClient(app)
        c.headers.update({"Authorization": f"Bearer {self._make_token(subject=subject, scopes=scopes)}"})
        return c

    def grant(self, case_id, subject=OFFICER):
        self.repo.grant_access(subject, access.CASE, case_id)


@pytest.fixture
def uat(tmp_path, monkeypatch, make_token):
    repo = fresh_repository()
    repo.initialise()
    # ---- isolation checks: a fresh, empty, separate database; an empty, private document store ----
    db = repo._dsn.rsplit("/", 1)[-1].split("?")[0]
    assert db.startswith("los_t_"), f"not an isolated test clone: {db}"
    assert repo._one("SELECT COUNT(*) AS n FROM applicants", ())["n"] == 0
    assert repo._one("SELECT COUNT(*) AS n FROM applications", ())["n"] == 0
    docs = tmp_path / "uat_documents"
    docs.mkdir()
    assert not any(docs.iterdir())
    # ---- UAT-only switches, this process only ----
    secret = secrets.token_hex(32)
    monkeypatch.setenv("DEDUPE_ENABLED", "true")
    monkeypatch.setenv("LOS_IDENTITY_HMAC_KEY", f"uat1:{secret}")
    monkeypatch.setenv("DEDUPE_AUDIT_PATH", str(tmp_path / "uat_dedupe_audit.jsonl"))
    monkeypatch.setenv("APPLICANT_AGENT_LLM_ENABLED", "false")
    C.reload()
    set_repository(repo)
    u = Uat(repo, docs, secret, tmp_path / "uat_dedupe_audit.jsonl", make_token)
    set_document_store(u.store)
    yield u
    set_document_store(None)
    set_repository(None)
    C.reload()


def _rule(r, rid):
    return next(x for x in r["rules"] if x["rule_id"] == rid)


# ---- UAT-00: the environment really is the approved one -----------------------------------------------------
def test_uat_00_no_migration_and_the_uat_secret_is_in_use(uat):
    assert not uat.repo.table_exists("party_identity")
    assert uat.repo._one("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_name = 'documents' "
                         "AND column_name = 'file_sha256'", ())["n"] == 0
    from app.agents.dedupe import identity as I

    assert I.token("PAN", pan(1)).startswith("uat1:")       # the UAT key, not the development key
    assert C.get_policy()["signed_off"] is False            # conditionally approved for UAT only, not signed off


# ---- UAT-01 matching ---------------------------------------------------------------------------------------
def test_uat_01_exact_pan_on_another_customer_record_is_an_identity_conflict(uat):
    uat.case("C-01A", "APP-01A", pan_=pan(1), minutes=0)
    uat.case("C-01B", "APP-01B", pan_=pan(1).lower(), minutes=30)          # normalisation: case / spacing
    r = S.assess("C-01B")
    assert r["status"] == "MATCH" and r["recommendation"] == "IDENTITY_CONFLICT_REVIEW"
    assert r["dedupe_result"]["matched_parameters"] == ["pan"]


# ---- UAT-02 false positives --------------------------------------------------------------------------------
@pytest.mark.parametrize("label, a, b, expected", [
    ("same mobile, different PAN (family)", dict(pan_=pan(2), mobile_=mobile(2)),
     dict(pan_=pan(3), mobile_=mobile(2)), "NO_MATCH"),
    ("PAN one character apart", dict(pan_=pan(4)), dict(pan_=pan(5)), "NO_MATCH"),
    ("same name, different PAN", dict(pan_=pan(6)), dict(pan_=pan(7)), "NO_MATCH"),
    ("mobile only, no PAN either side", dict(mobile_=mobile(8)), dict(mobile_=mobile(8)), "POSSIBLE_MATCH"),
])
def test_uat_02_false_positives_are_not_identity_matches(uat, label, a, b, expected):
    uat.case("C-02A", "APP-02A", **a)
    uat.case("C-02B", "APP-02B", **b)
    r = S.assess("C-02B")
    assert r["status"] == expected, label
    assert r["dedupe_result"]["matched_parameters"] == [], label
    assert _rule(r, "P01_IDENTITY_UNIQUENESS")["outcome"] == "NOT_TRIGGERED", label


# ---- UAT-03 role swaps -------------------------------------------------------------------------------------
def test_uat_03_role_swaps_both_ways_are_reported_never_a_fraud_match(uat):
    uat.case("C-03A", "APP-03A", pan_=pan(9))                                              # X primary
    uat.case("C-03B", "APP-03B", pan_=pan(10), co_applicant_id="COAPP-03B", co_pan=pan(9))  # X co-applicant
    uat.case("C-03C", "APP-03C", pan_=pan(11), co_applicant_id="COAPP-03C", co_pan=pan(10))  # B's primary as co-app
    for case_id in ("C-03B", "C-03C"):
        r = S.assess(case_id)
        assert _rule(r, "P02_ROLE_HISTORY")["outcome"] == "TRIGGERED", case_id
        assert r["dedupe_result"]["matched_parameters"] == [], case_id
        assert any(m["role_change"] for p in r["parties"] for m in p["matches"]), case_id


# ---- UAT-04 document reuse ---------------------------------------------------------------------------------
def test_uat_04_document_reuse_across_customers_only(uat):
    shared = b"%PDF-1.4 UAT synthetic statement " + uuid.uuid4().bytes
    uat.case("C-04A", "APP-04A", pan_=pan(12))
    uat.case("C-04B", "APP-04B", pan_=pan(13))
    uat.case("C-04C", "APP-04A", pan_=pan(12), minutes=5)              # the same customer's second case
    uat.file("C-04A", "APP-04A", "stmt.pdf", shared)
    uat.file("C-04B", "APP-04B", "stmt.pdf", shared)                   # another customer: reuse
    uat.file("C-04C", "APP-04A", "stmt.pdf", shared)                   # same customer: a resubmission
    r = S.assess("C-04B")
    assert r["documents"]["reused_on_other_customers_cases"] == 2
    assert _rule(r, "P06_DOCUMENT_REUSE")["outcome"] == "TRIGGERED"
    own = S.assess("C-04C")
    assert own["documents"]["reused_on_same_customer_cases"] == 1
    assert own["documents"]["reused_on_other_customers_cases"] == 1    # still on C-04B, another customer


# ---- UAT-05 / UAT-06 masking and isolation through the real API --------------------------------------------
def test_uat_05_06_api_masks_everything_and_isolates_other_customers(uat):
    uat.case("C-05A", "APP-05A", pan_=pan(14), mobile_=mobile(14))
    uat.case("C-05B", "APP-05B", pan_=pan(14), mobile_=mobile(14))
    uat.grant("C-05B")
    officer = uat.client(["los.dedupe.read"])
    # THE ONE DEDUPE API (POST /api/v1/credit/dedupe): the LOS dedupe and its search in a single answer
    body = {"customer_type": "New", "dedupe_parameter": "PAN", "dedupe_parameter_value": pan(14)}
    mine = officer.post("/api/v1/credit/dedupe", json={"case_id": "C-05B", **body})
    theirs = officer.post("/api/v1/credit/dedupe", json={"case_id": "C-05A", **body})
    assert mine.status_code == 200 and theirs.status_code == 403
    for leaked in (pan(14), mobile(14), "C-05A", "APP-05A", "uat1:", uat.secret):
        assert leaked not in mine.text, leaked
    assert mine.json()["los_dedupe"]["search"]["customer_type_check"]["result"] == "DECLARED_NEW_BUT_FOUND"


# ---- UAT-07 partial analysis -------------------------------------------------------------------------------
def test_uat_07_partial_analysis_on_a_scan_bound_or_a_failed_source(uat, tmp_path, monkeypatch):
    for i in range(4):
        uat.case(f"C-07{i}", f"APP-07{i}", pan_=pan(20 + i), minutes=i)
    data = yaml.safe_load(C.DEFAULT_PATH.read_text(encoding="utf-8"))
    data["limits"]["max_scan_cases"] = 2
    path = tmp_path / "uat_policy_bound.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("DEDUPE_POLICY_PATH", str(path))
    C.reload()
    bounded = S.assess("C-070")
    assert bounded["coverage"]["truncated"] and "PARTIAL_ANALYSIS" in bounded["recommendations"]
    monkeypatch.delenv("DEDUPE_POLICY_PATH")
    C.reload()

    def store_down():
        raise OSError("document store unavailable")

    monkeypatch.setattr("app.store.documents.get_document_store", store_down)
    failed = S.assess("C-071")
    assert failed["coverage"]["failed_sources"][0]["source"] == "LOS_DOCUMENT_STORE"
    assert _rule(failed, "P06_DOCUMENT_REUSE")["outcome"] == "NOT_EVALUATED"
    assert failed["completeness"]["complete"] is False


# ---- UAT-08 audit and timeline -----------------------------------------------------------------------------
def test_uat_08_every_assessment_is_audited_and_on_the_timeline(uat):
    uat.case("C-08A", "APP-08A", pan_=pan(30), mobile_=mobile(30))
    S.assess("C-08A")
    S.assess("C-08A")
    lines = [json.loads(x) for x in uat.audit.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and {x["case_id"] for x in lines} == {"C-08A"}
    text = uat.audit.read_text(encoding="utf-8")
    for leaked in (pan(30), mobile(30), "uat1:", uat.secret):
        assert leaked not in text
    events = [e for e in uat.repo.get_case_timeline("C-08A") if e.event_type == "DEDUPE_ASSESSED"]
    assert len(events) == 2


# ---- UAT-09 fraud agent ------------------------------------------------------------------------------------
async def test_uat_09_fraud_agent_through_the_los_flow_hook(uat, monkeypatch):
    from types import SimpleNamespace

    from app.agents.fraud_risk.agent import FraudRiskAgent
    from app.agents.fraud_risk.schemas import FraudRiskRequest
    from app.agents.los import flow

    uat.case("C-09A", "APP-09A", pan_=pan(31))
    uat.case("C-09B", "APP-09B")                                     # this run's PAN not stored yet (pre-ingest)
    monkeypatch.setattr(flow, "_released_for_matching",
                        lambda owned: [{"extraction": {"fields": {"pan_number": p}}} for p in owned])
    dedupe = await flow._dedupe_for("C-09B", [(SimpleNamespace(party_id="APP-09B"), [pan(31)])])
    assert dedupe is not None and dedupe.matched_parameters == ["pan"]
    response = FraudRiskAgent(use_llm=False).assess(FraudRiskRequest(dedupe=dedupe))
    assert any(f.rule == "DEDUPE_MATCH" and f.severity == "CRITICAL" for f in response.flags)

    monkeypatch.setattr(S, "assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("UAT outage")))
    assert await flow._dedupe_for("C-09B", [(SimpleNamespace(party_id="APP-09B"), [pan(31)])]) is None
    gap = FraudRiskAgent(use_llm=False).assess(FraudRiskRequest(dedupe=None))
    assert any(g.rule == "DEDUPE_MATCH" for g in gap.data_gaps)          # the old behaviour, unchanged


# ---- UAT-10 concurrent application creation through the real FOS API ---------------------------------------
def test_uat_10_concurrent_creates_are_both_detected_not_prevented(uat):
    creator = uat.client(FOS_SCOPES, subject="uat-fos-1")
    made: list[dict] = []

    def create(n):
        r = creator.post("/api/v1/fos/applicants", json={
            "applicant": {"full_name": f"UAT PERSON {n}", "mobile": mobile(40), "date_of_birth": "1990-01-01",
                          "address": "UAT Street, Pune 411001"},
            "application": {"product": "PERSONAL_LOAN", "loan_amount": 300000}})
        made.append({"code": r.status_code, **(r.json() if r.status_code == 201 else {})})

    threads = [threading.Thread(target=create, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert [m["code"] for m in made] == [201, 201]              # both created: creation is NOT guarded (limitation)
    for m in made:                                              # their PAN documents arrive later
        uat.extraction(m["case_id"], m["applicant_id"], pan(41))
    for m in made:
        r = S.assess(m["case_id"])
        assert r["status"] == "MATCH" and r["recommendation"] == "IDENTITY_CONFLICT_REVIEW"
        assert _rule(r, "P15_CONCURRENT_CREATE")["outcome"] == "TRIGGERED"          # created within 60 s
        assert r["completeness"]["complete"] is False


# ---- UAT-11 unavailable sources --------------------------------------------------------------------------
def test_uat_11_unavailable_sources_are_explicit(uat):
    uat.case("C-11A", "APP-11A", pan_=pan(50))
    r = S.assess("C-11A")
    params = {p["parameter"]: p for p in r["parameters"]}
    assert params["AP_CODE"]["status"] == params["PR_CODE"]["status"] == "INSUFFICIENT_DATA"
    for rid in ("P03_ACTIVE_LOANS_EXPOSURE", "P04_REPAYMENT_HISTORY", "P11_TOP_UP", "P07_FRAUD_CASE_OUTCOMES",
                "P05_DUPLICATE_WINDOW"):
        assert _rule(r, rid)["outcome"] == "NOT_EVALUATED", rid
    for parameter, source in (("PR Code", "LOAN_MANAGEMENT_SYSTEM"), ("AP Code", "CUSTOMER_MASTER")):
        s = S.search(parameter, {"PR Code": "PR02311890", "AP Code": "AP00731958"}[parameter], customer_type="Existing")
        assert (s["status"], s["needs_source"], s["customer_type_check"]["result"]) == (
            "INSUFFICIENT_DATA", source, "NOT_VERIFIABLE")


# ---- UAT-12 the UAT secret never leaves the process ------------------------------------------------------
def test_uat_12_the_hmac_secret_is_in_no_log_record(uat, caplog):
    caplog.set_level(logging.DEBUG)
    uat.case("C-12A", "APP-12A", pan_=pan(60), mobile_=mobile(60))
    uat.case("C-12B", "APP-12B", pan_=pan(60), mobile_=mobile(60))
    S.assess("C-12B")
    S.search("Mobile No", mobile(60))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for leaked in (uat.secret, "uat1:", pan(60), mobile(60)):
        assert leaked not in logged, leaked
