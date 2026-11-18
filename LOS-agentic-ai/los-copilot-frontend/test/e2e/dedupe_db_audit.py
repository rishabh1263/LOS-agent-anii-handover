"""
DEDUPE AGENT -- END-TO-END AUDIT, CASE BY CASE: API answer -> database -> audit file (owner, 2026-10-23: "no gaps").

Runs against a LIVE backend (default http://127.0.0.1:8010, auth off, demo MIFIN + demo Leviosa) and its embedded
PostgreSQL (port from runtime/pgdata/postmaster.pid). SYNTHETIC data only: the demo identifiers of
app/config/demo/mifin_demo.yaml and random synthetic applicants. Prints PASS / FAIL per check; exit code = failures.
Never prints a PAN or mobile it checks for.

Run from LOS-agentic-ai:  python los-copilot-frontend/test/e2e/dedupe_db_audit.py
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[3]
API = os.getenv("AUDIT_API", "http://127.0.0.1:8010").rstrip("/")
AUDIT = ROOT / "runtime" / "audit" / "customer_history.jsonl"
results: list[tuple[str, bool]] = []
SECRETS: set[str] = set()                          # every PAN / mobile used: must never be stored or returned in clear
rnd = random.Random()


def check(name: str, ok: bool, info: object = "") -> bool:
    results.append((name, bool(ok)))
    text = str(info)
    for s in SECRETS:
        text = text.replace(s, "<id>")
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   [{text[:260]}]"), flush=True)
    return bool(ok)


def mobile() -> str:
    m = str(rnd.choice("6789")) + "".join(rnd.choice("0123456789") for _ in range(9))
    SECRETS.add(m)
    return m


def pan() -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    p = "".join(rnd.choice(letters) for _ in range(3)) + "P" + rnd.choice(letters) + \
        "".join(rnd.choice("0123456789") for _ in range(4)) + rnd.choice(letters)
    SECRETS.add(p)
    return p


client = httpx.Client(base_url=API, timeout=60)


def create(name: str, *, co_pans: list[str] | None = None, mob: str | None = None) -> dict:
    body = {"applicant": {"full_name": name, "mobile": mob or mobile(), "date_of_birth": "1990-05-10",
                          "address": "Synthetic Road, Pune", "mobile_aadhaar_linked": True},
            "application": {"product": "PERSONAL_LOAN", "loan_amount": 500000, "loan_purpose": "Marriage",
                            "applicant_type": "Individual", "co_origination": False, "tenure_months": 36}}
    if co_pans:
        body["co_applicants"] = [{"full_name": f"Co Person {'ABCDEFGH'[i]}", "date_of_birth": "1992-01-01", "pan": p,
                                  "relationship": "SPOUSE"} for i, p in enumerate(co_pans)]
    r = client.post("/api/v1/fos/applicants", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code < 300, r.text[:300]
    return {"case_id": r.json()["case_id"], "applicant_id": r.json()["applicant_id"], "body": body, "requests": []}


def dedupe(case: dict, parameter: str, value: str, *, customer_type: str = "Existing", status: int = 200, **extra):
    if parameter in ("PAN", "Mobile No"):
        SECRETS.add(value)
    r = client.post("/api/v1/credit/dedupe", json={"case_id": case["case_id"], "party_role": "APPLICANT",
                                                     "customer_type": customer_type, "dedupe_parameter": parameter,
                                                     "dedupe_parameter_value": value, **extra})
    if r.status_code != status:
        check(f"{case['case_id']} {parameter} -> HTTP {status}", False, (r.status_code, r.text[:200]))
        return None
    if status != 200:
        return r.json()
    d = r.json()
    case["requests"].append(d["request_id"])
    return d


def loans(d: dict) -> dict:
    return {x["prospect_code"]: x for x in d["history"]["loans"]}


def no_secret_in(d: dict, label: str) -> None:
    text = json.dumps(d)
    leaked = [s for s in SECRETS if s in text]
    check(f"{label}: no PAN / mobile in the answer", not leaked, f"{len(leaked)} value(s)")
    # a PR code in full only in its own fields (prospect_code / record_ref)
    def bad_pr(o, key=None):
        if isinstance(o, dict):
            return any(bad_pr(v, k) for k, v in o.items())
        if isinstance(o, list):
            return any(bad_pr(v, key) for v in o)
        return isinstance(o, str) and key not in ("prospect_code", "record_ref") and bool(re.search(r"\bPR\d{8}\b", o))
    check(f"{label}: PR codes only in their own field", not bad_pr(d))


def rec(d: dict) -> str | None:
    return ((d.get("dedupe") or {}).get("workflow_recommendation") or {}).get("recommendation")


cases: dict[str, dict] = {}

# ============================================================== 1. existing customer, every search parameter
for label, (param, value) in {"E1 PAN": ("PAN", "BCDPM5678N"), "E2 mobile": ("Mobile No", "9876502222"),
                              "E3 AP code": ("AP Code", "AP00745218"), "E4 PR code": ("PR Code", "PR02407753")}.items():
    c = cases[label] = create(f"Audit {label.split()[0]}")
    d = dedupe(c, param, value)
    if not d:
        continue
    L = loans(d)
    check(f"{label}: confirmed customer AP00745218", d["history"]["customer"]["match"] == "CONFIRMED_MATCH"
          and d["history"]["customer"]["customer_code"] == "AP00745218", d["history"]["customer"]["match"])
    check(f"{label}: 3 loans, right statuses and sources",
          {k: (v["effective_status"], v["status_source"]) for k, v in L.items()} ==
          {"PR02407753": ("ACTIVE", "LMS"), "PR02251346": ("CLOSED", "LMS"), "PR02190028": ("CANCELED", "MIFIN")},
          {k: v["effective_status"] for k, v in L.items()})
    a = d["history"]["aggregates"]
    check(f"{label}: exposure 3,12,400 and EMI 11,200, complete",
          (a["active_exposure"]["status"], a["active_exposure"]["value"], a["monthly_emi_burden"]["value"]) ==
          ("COMPLETE", "312400", "11200"), (a["active_exposure"], a["monthly_emi_burden"]))
    check(f"{label}: Leviosa answered (provider LEVIOSA)",
          any(s["source"] == "LMS" and s.get("provider") == "LEVIOSA" and s["status"] == "AVAILABLE" for s in d["history"]["sources"]))
    check(f"{label}: Leviosa dates read (15-03-2024 -> 2024-03-15)", L["PR02407753"]["lms"]["disbursed_on"] == "2024-03-15")
    check(f"{label}: common applicant on all 3, PAN compared",
          d["history"]["common_applicant"]["cases"] == 3 and d["history"]["common_applicant"]["same_customer_on_all"])
    check(f"{label}: 3 reason points", len(d["explanation"].get("points") or []) == 3)
    st = (d.get("dedupe") or {}).get("status")
    findings = (d.get("dedupe") or {}).get("rule_findings") or []
    # proceed only with no duplicate AND nothing found by any rule (owner 2026-10-23)
    check(f"{label}: recommendation follows the dedupe status and the rule findings",
          (rec(d) == "PROCEED_TO_NEXT_STAGE") == (st == "NO_DUPLICATE_FOUND" and not findings), (st, findings, rec(d)))
    if label == "E4 PR code":
        check(f"{label}: the searched loan account is marked", L["PR02407753"]["searched_loan_account"] is True
              and sum(x["searched_loan_account"] for x in L.values()) == 1)
    no_secret_in(d, label)

# ============================================================== 2. cancelled loans, reasons
c = cases["E5 cancelled"] = create("Audit Cancelled")
d = dedupe(c, "PAN", "ABCPK4321L")
if d:
    L = loans(d)
    check("E5: 3 cancelled loans, every reason shown", {k: (v["effective_status"], v["cancel_reason"]) for k, v in L.items()} == {
        "PR02184467": ("CANCELED", "Customer withdrew"), "PR02236105": ("CANCELED", "Property title defect"),
        "PR02311890": ("CANCELED", "Income not verified")}, {k: v["cancel_reason"] for k, v in L.items()})
    check("E5: nothing live: exposure not applicable (never a zero total)",
          d["history"]["aggregates"]["active_exposure"]["status"] == "NOT_APPLICABLE")
    no_secret_in(d, "E5")

# ============================================================== 3. a customer with no loans
c = cases["E6 no loans"] = create("Audit Noloans")
d = dedupe(c, "PAN", "CDEPN2468Q")
if d:
    check("E6: strong match, no previous loans (an authoritative none)",
          (d["history"]["customer"]["match"], d["history"]["history_status"]) == ("CONFIRMED_MATCH", "NO_PREVIOUS_LOANS"))
    check("E6: role in MIFIN is co-applicant", d["history"]["customer"]["role_in_source"] == "CO_APPLICANT")

# ============================================================== 4. possible match -> SAME customer (persisted)
c = cases["E7 possible->same"] = create("Audit Possible")
d = dedupe(c, "PAN", "DEFPR1357S")
if d:
    cust = d["history"]["customer"]
    check("E7: possible match, loans withheld, decision asked", cust["match"] == "POTENTIAL_MATCH"
          and d["history"]["history_status"] == "NOT_ATTRIBUTED" and d["history"]["loans"] == []
          and cust["confirmation"]["required"] is True)
    check("E7: AP number shown in full, candidate opaque", cust["customer_code"] == "AP00768031"
          and "AP00768031" not in str(cust["confirmation"]["candidate"]))
    check("E7: never 'proceed' while undecided", rec(d) != "PROCEED_TO_NEXT_STAGE", rec(d))
    cand = cust["confirmation"]["candidate"]
    d2 = dedupe(c, "PAN", "DEFPR1357S", confirm_match="SAME_CUSTOMER", candidate=cand)
    if d2:
        check("E7: after 'same customer': confirmed, loans shown", d2["history"]["customer"]["match"] == "CONFIRMED_MATCH"
              and "PR02455102" in loans(d2) and d2["history"]["customer"]["confirmation"]["decided_by"] == "OFFICER")
    d3 = dedupe(c, "PAN", "DEFPR1357S")
    if d3:
        check("E7: the decision is remembered on the case", d3["history"]["customer"]["match"] == "CONFIRMED_MATCH")
    other = cases["E7b other case"] = create("Audit Possible Other")
    d4 = dedupe(other, "PAN", "DEFPR1357S")
    if d4:
        check("E7: another case is NOT affected by that decision", d4["history"]["customer"]["match"] == "POTENTIAL_MATCH")
    r = dedupe(other, "PAN", "DEFPR1357S", status=422, confirm_match="SAME_CUSTOMER")
    check("E7: a decision without the candidate is refused (422)", r is not None)

# ============================================================== 5. possible match -> NOT the same customer
c = cases["E8 possible->not same"] = create("Audit Notsame")
d = dedupe(c, "PAN", "DEFPR1357S")
if d:
    d2 = dedupe(c, "PAN", "DEFPR1357S", confirm_match="NOT_SAME_CUSTOMER",
                candidate=d["history"]["customer"]["confirmation"]["candidate"])
    if d2:
        check("E8: 'not the same' -> treated as no match, no loans", d2["history"]["customer"]["match"] == "NO_MATCH"
              and d2["history"]["loans"] == [])

# ============================================================== 6. negatives with an answer
c = cases["E9 ambiguous"] = create("Audit Ambiguous")
d = dedupe(c, "Mobile No", "9876505555")
if d:
    check("E9: a mobile on two customers -> ambiguous, never proceed",
          d["history"]["customer"]["match"] == "AMBIGUOUS" and rec(d) != "PROCEED_TO_NEXT_STAGE", rec(d))
c = cases["E10 MIFIN error"] = create("Audit Mifinerror")
d = dedupe(c, "PAN", "EFGPS9753T")
if d:
    check("E10: MIFIN error -> history unavailable, never proceed",
          d["history"]["history_status"] == "UNAVAILABLE" and rec(d) != "PROCEED_TO_NEXT_STAGE", (d["history"]["history_status"], rec(d)))
    check("E10: nothing counted as zero", d["history"]["aggregates"] is None and d["history"]["loans"] == [])
c = cases["E11 conflicting"] = create("Audit Conflict")
d = dedupe(c, "PAN", "FGHPT8642U")
if d:
    L = loans(d)
    check("E11: conflicting duplicate + bad amount: outcome data insufficient",
          d["outcome"] == "DATA_INSUFFICIENT" and "CONFLICTING_LOAN_RECORDS" in d["reason_codes"], d["reason_codes"])
    check("E11: the disbursed loan Leviosa has not confirmed is never counted",
          L["PR02490231"]["effective_status"] == "DISBURSED" and L["PR02490231"]["lms_status"] == "NOT_FOUND"
          and d["history"]["aggregates"]["active_exposure"]["status"] != "COMPLETE")

# ============================================================== 7. new customer; existing co-applicant; same mobile twice
c = cases["E12 new"] = create("Audit New")
d = dedupe(c, "PAN", pan(), customer_type="New")
if d:
    check("E12: new customer -> dedupe not needed, proceed", d["dedupe"]["invoked"] is False
          and rec(d) == "PROCEED_TO_NEXT_STAGE", (d["dedupe"].get("invoked"), rec(d)))
SECRETS.add("ABCPK4321L")
c = cases["E13 existing co-applicant"] = create("Audit Coapp", co_pans=["ABCPK4321L"])
d = dedupe(c, "PAN", pan(), customer_type="New")
if d:
    co = [p for p in d["dedupe"].get("parties") or [] if p["role"] == "CO_APPLICANT"]
    check("E13: a new applicant with an EXISTING co-applicant still runs dedupe", d["dedupe"]["invoked"] is True
          and d["dedupe"]["trigger"].get("customer_lookup_parties") == "CO_APPLICANT_NOT_NEW")
    check("E13: the co-applicant is checked on its own and found", len(co) == 1 and co[0]["existing_customer"] is True
          and co[0]["mifin"]["customer_code"] == "AP00731942", co)
shared = mobile()
first = cases["E14a same mobile"] = create("Audit Twice", mob=shared)
second = cases["E14b same mobile"] = create("Audit Twice", mob=shared)
d = dedupe(second, "Mobile No", shared)
if d:
    check("E14: the same mobile on another LOS case is found and held",
          (d["los_dedupe"]["search"]["status"] in ("MATCH", "POSSIBLE_MATCH") or d["los_dedupe"]["search"].get("matches"))
          and rec(d) != "PROCEED_TO_NEXT_STAGE", (d["los_dedupe"]["search"]["status"], rec(d)))

# ============================================================== 8. API negatives
c = cases["E15 negatives"] = create("Audit Negative")
check("E15: a malformed PAN is refused (422)", dedupe(c, "PAN", "ABC123", status=422) is not None)
check("E15: an unknown parameter is refused (422)", dedupe(c, "Passport", "X1234567", status=422) is not None)
r = client.post("/api/v1/credit/dedupe", json={"case_id": "CASE-DOESNOTEXIST", "dedupe_parameter": "PAN",
                                                 "dedupe_parameter_value": "BCDPM5678N"})
check("E15: an unknown case is refused (403/404)", r.status_code in (403, 404), r.status_code)
r = client.post("/api/v1/credit/dedupe", json={"case_id": c["case_id"], "dedupe_parameter": "PAN",
                                                 "dedupe_parameter_value": "BCDPM5678N",
                                                 "confirm_match": "SAME_CUSTOMER", "candidate": "dev1:" + "0" * 64})
check("E15: confirming when nothing is to be confirmed is refused (409/422)", r.status_code in (409, 422), r.status_code)
rid = cases["E1 PAN"]["requests"][0] if cases["E1 PAN"]["requests"] else None
if rid:
    r = client.get(f"/api/v1/credit/dedupe/{rid}/reason")
    check("E15: the 3-point reason is served for a real check", r.status_code == 200 and len(r.json().get("points") or []) == 3,
          r.status_code)
r = client.get("/api/v1/credit/dedupe/ddp_" + "0" * 32 + "/reason")
check("E15: a reason for an unknown check is 404", r.status_code == 404, r.status_code)

# ============================================================== 9. THE DATABASE, case by case
port = (ROOT / "runtime" / "pgdata" / "postmaster.pid").read_text().splitlines()[3].strip()
with psycopg.connect(f"host=127.0.0.1 port={port} user=postgres dbname=postgres") as db:
    def one(sql, args=()):
        return db.execute(sql, args).fetchone()

    def rows(sql, args=()):
        return db.execute(sql, args).fetchall()

    for label, c in cases.items():
        cid, aid = c["case_id"], c["applicant_id"]
        app = one("SELECT loan_purpose, applicant_type, co_origination, document_received_date, product, loan_amount, "
                  "tenure_months FROM applications WHERE case_id = %s", (cid,))
        check(f"DB {label}: application row with the form's fields", app is not None and app[0] == "Marriage"
              and app[1] == "Individual" and app[2] is False and app[3] is None and app[4] == "PERSONAL_LOAN"
              and str(app[6]) == "36", app)
        ap = one("SELECT mobile_aadhaar_linked FROM applicants WHERE applicant_id = %s", (aid,))
        check(f"DB {label}: applicant row, Aadhaar-linked = yes (no Aadhaar number)", ap is not None and ap[0] is True, ap)
        co_sent = c["body"].get("co_applicants") or []
        co_rows = rows("SELECT pan FROM co_applicants WHERE case_id = %s", (cid,))
        check(f"DB {label}: co-applicants stored ({len(co_sent)})", len(co_rows) == len(co_sent), len(co_rows))
        if co_rows:
            check(f"DB {label}: co-applicant PAN encrypted, not in clear",
                  all(r[0] and not any(s in str(r[0]) for s in SECRETS) for r in co_rows))
        events = rows("SELECT event_type, ref_id, summary FROM case_events WHERE case_id = %s ORDER BY sequence", (cid,))
        assessed = [e for e in events if e[0] == "CUSTOMER_HISTORY_ASSESSED"]
        check(f"DB {label}: one timeline event per dedupe check ({len(c['requests'])})",
              sorted(e[1] for e in assessed) == sorted(c["requests"]), (len(assessed), len(c["requests"])))
        decisions = [e for e in events if e[0] == "CUSTOMER_MATCH_DECISION"]
        want = 1 if label in ("E7 possible->same", "E8 possible->not same") else 0
        check(f"DB {label}: officer decisions recorded ({want})", len(decisions) == want
              and all("officer:" in (e[2] or "") for e in decisions), len(decisions))
        # nothing identifying stored in clear for this case, anywhere it lives
        texts = []
        for table, key in (("applications", "case_id"), ("case_events", "case_id"), ("co_applicants", "case_id"),
                           ("case_findings", "case_id")):
            try:
                texts += [json.dumps([str(v) for v in r]) for r in rows(f"SELECT * FROM {table} WHERE {key} = %s", (cid,))]
            except Exception:  # noqa: BLE001 - a table this build does not have
                db.rollback()
        leaked = sorted({s for s in SECRETS if s.isalnum() and not s.isdigit() and any(s in t for t in texts)})
        check(f"DB {label}: no PAN in clear in its rows", not leaked, f"{len(leaked)} value(s)")
    stored_mobile = one("SELECT mobile FROM applicants WHERE applicant_id = %s", (cases["E1 PAN"]["applicant_id"],))
    mobile_plain = bool(stored_mobile and stored_mobile[0] and stored_mobile[0] == cases["E1 PAN"]["body"]["applicant"]["mobile"])
    print(f"INFO applicants.mobile is stored {'IN CLEAR' if mobile_plain else 'protected'} (the applicant's own declared mobile)")

# ============================================================== 10. THE AUDIT FILE
time.sleep(0.5)
lines = [json.loads(x) for x in AUDIT.read_text(encoding="utf-8").splitlines() if x.strip()] if AUDIT.exists() else []
by_id = {x.get("request_id"): x for x in lines}
all_ids = [r for c in cases.values() for r in c["requests"]]
check(f"AUDIT: one line per dedupe check ({len(all_ids)})", all(r in by_id for r in all_ids),
      [r for r in all_ids if r not in by_id][:3])
audit_text = json.dumps([by_id[r] for r in all_ids if r in by_id])
check("AUDIT: no PAN / mobile in the audit lines", not any(s in audit_text for s in SECRETS))
check("AUDIT: no full AP / PR code in the audit lines", not re.search(r"\b(AP|PR)\d{8}\b", audit_text))
check("AUDIT: every line carries case, outcome and policy version",
      all(by_id[r].get("case_id") and by_id[r].get("outcome") and by_id[r].get("policy_version") for r in all_ids if r in by_id))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"\nFAILED:\n  " + "\n  ".join(failed) if failed else ""))
sys.exit(len(failed))
