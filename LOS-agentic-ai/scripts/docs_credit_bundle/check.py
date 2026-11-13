"""
CREDIT DOCUMENT DESK -- the acceptance check. Positive + negative, against the RUNNING backend (and dashboard).

    python check.py                                    # backend http://127.0.0.1:8020, dashboard http://127.0.0.1:5182
    python check.py --backend http://127.0.0.1:8010 --frontend http://127.0.0.1:5181

Standard library only. Read-only: it never changes a case (every call is an evaluation; the audit record of the same
answer is written once). Needs the 14 demo cases (backend/scripts/seed_docs_credit_demo.py, run by start.ps1).
Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES = [f"RULE-{n:02d}" for n in range(1, 12)]
POLICY_FOR = {"PERSONAL_LOAN": "PL-", "HOME_LOAN": "HL-", "LOAN_AGAINST_PROPERTY": "LAP-"}
WHOSE = {"PRIMARY_APPLICANT": "the applicant", "CO_APPLICANT": "the co-applicant"}
LEAKS = ("traceback", "c:\\\\users", "psycopg", "select ", "sqlstate", "/home/", "file \"")
problems: list[str] = []
passed = 0


def ok(cond: bool, what: str) -> None:
    global passed
    if cond:
        passed += 1
    else:
        problems.append(what)


def call(base: str, path: str, method: str = "POST", body=None, raw: bytes | None = None,
         headers: dict | None = None, timeout: float = 60) -> tuple[int, dict | str, dict]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read()
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            try:
                return r.status, json.loads(text or b"{}"), hdrs
            except ValueError:
                return r.status, text.decode(errors="replace"), hdrs
    except urllib.error.HTTPError as e:
        text = e.read()
        hdrs = {k.lower(): v for k, v in e.headers.items()}
        try:
            return e.code, json.loads(text or b"{}"), hdrs
        except ValueError:
            return e.code, text[:300].decode(errors="replace"), hdrs


# ======================================================================================================== positive
def positive(base: str) -> None:
    api = "/api/v1/credit/document-readiness"
    expected = json.loads((HERE / "expected_demo.json").read_text(encoding="utf-8")) \
        if (HERE / "expected_demo.json").exists() else {}
    code, health, _ = call(base, "/health", "GET")
    ok(code == 200, f"/health answered {code}")
    for case, want in sorted(expected.items()):
        code, d, _ = call(base, f"{api}/{case}/rules")
        if code != 200:
            ok(False, f"{case}: HTTP {code} {str(d)[:160]}")
            continue
        a, s, rules, docs, c = d["answer"], d["answer"]["summary"], d["rules"], d["documents"], d["case"]
        req, route = a["requirements"], s["route"]
        got = {"status": a["overall_status"], "route": route["decision"], "critical": route["critical"],
               "to_collect": route["non_critical"], "allowed": a["credit_underwriting_allowed"],
               "open": sorted(f"{q['check_id']}|{q.get('party_role')}" for q in req)}
        for k in want:
            ok(got[k] == want[k], f"{case}: {k} is {got[k]!r}, expected {want[k]!r}")

        blocking = [q for q in req if q["severity"] == "BLOCKING"]
        crit = [q for q in blocking if q["status"] != "EXCEPTION_PENDING"]
        minor = [q for q in req if q["severity"] != "BLOCKING"]
        exp_route = ("REFER_TO_AUTHORITY" if crit else "AWAIT_APPROVAL" if blocking else
                     "MOVE_AHEAD_WITH_NOTES" if minor else "MOVE_AHEAD")
        ok(route["decision"] == exp_route, f"{case}: route {route['decision']} but the items say {exp_route}")
        ok((route["critical"], route["non_critical"]) == (len(crit), len(minor)), f"{case}: route counts off")
        ok(a["credit_underwriting_allowed"] == (a["overall_status"] == "COMPLETE" and a["stage"] == "CREDIT"),
           f"{case}: underwriting gate does not follow COMPLETE + CREDIT")
        ok(a["audit"]["requirements_open"] == len(req), f"{case}: audit open count off")
        ok(a["policy_version"].startswith(POLICY_FOR.get(a["product"], "?")),
           f"{case}: {a['product']} answered by {a['policy_version']}")
        ok(a["audit"]["policy_status"] in {"SAMPLE", "APPROVED"} and a["audit"]["policy_file_sha256"],
           f"{case}: audit lacks the policy status / file hash")
        # critical ONLY for missing / not accepted / rejected / mismatch / exception (policy critical_when)
        for q in blocking:
            ok(q["rule_id"] not in ("RULE-06", "RULE-07", "RULE-11"), f"{case}: {q['rule_id']} blocks: {q['reason']}")
            ok(q["status"] not in ("PENDING_VERIFICATION", "UNDER_REVIEW"), f"{case}: being verified but blocks")
        # every reason: a full sentence, no template leak, names WHOSE document
        for q in req:
            r = q["reason"]
            ok(r and r[0].isupper() and r.rstrip().endswith((".", ")")), f"{case}: reason not a sentence: {r!r}")
            ok(not re.search(r"[{}]|\bNone\b|[a-z]_[a-z]", r), f"{case}: template leak in reason: {r!r}")
            if q.get("party_role") in WHOSE and q["rule_id"] in ("RULE-02", "RULE-03", "RULE-05", "RULE-06", "RULE-11"):
                ok(WHOSE[q["party_role"]] in r.lower(), f"{case}: reason does not say whose: {r!r}")
        # the rules board: 11 rules once each, in the 3 sections once each, statuses follow the checks
        ids = [r["rule_id"] for r in rules]
        ok(sorted(ids) == RULES, f"{case}: board rules {ids}")
        ok(sorted(x for sec in d["sections"] for x in sec["rules"]) == RULES, f"{case}: sections do not cover 11")
        for r in rules:
            open_in = [ch for ch in r["checks"] if ch["status"] not in ("SATISFIED", "WAIVED")]
            if r["status"] in ("N/A", "OFF") or r["rule_id"] == "RULE-10":
                continue
            exp = "FAIL" if any(ch["severity"] == "BLOCKING" for ch in open_in) else "WARN" if open_in else "PASS"
            ok(r["status"] == exp, f"{case}: {r['rule_id']} {r['status']} expected {exp}")
            ok(r["passed"] + len(open_in) == r["total"], f"{case}: {r['rule_id']} passed + open != total")
            ok(bool(r.get("name")) and bool(r.get("help")), f"{case}: {r['rule_id']} has no name / help")
        # the checklist: people, cells agree with the open items
        roles = ["PRIMARY_APPLICANT"] + (["CO_APPLICANT"] if c.get("co_applicant_name") else [])
        ok(docs["people"] == roles, f"{case}: checklist people {docs['people']}")
        for row in docs["rows"]:
            for role, cell in row["cells"].items():
                open_here = [q for q in req if q["check_id"] == f"{role}:{row['slot']}"]
                if cell["state"] in ("OK", "WAIVED"):
                    ok(not open_here, f"{case}: {role}:{row['slot']} {cell['state']} but open")
                if cell["state"] in ("MISSING", "REPLACE", "IN_REVIEW", "AWAITING_APPROVAL", "CHECK"):
                    ok(bool(open_here), f"{case}: {role}:{row['slot']} {cell['state']} but nothing open")
                if cell["state"] == "MISSING":
                    ok(not cell.get("file"), f"{case}: {role}:{row['slot']} missing but has a file")
        co_slots = {r["slot"] for r in docs["rows"] if "CO_APPLICANT" in r["cells"]}
        ok(not co_slots & {"PROPERTY_DOCUMENT", "PROPERTY_OWNERSHIP_PROOF"}, f"{case}: co-applicant asked for deed")
        # the AI / rules note never contradicts the route
        note = (s["ai_summary"]["text"] or "").lower()
        if route["decision"] == "REFER_TO_AUTHORITY":
            ok("can move ahead" not in note and "can start" not in note, f"{case}: note says go ahead: {note}")
        if route["decision"].startswith("MOVE_AHEAD"):
            ok("higher authority" not in note and "cannot start" not in note, f"{case}: note contradicts: {note}")
        # determinism: the same case again -> the same evaluation
        code2, d2, _ = call(base, f"{api}/{case}")
        ok(code2 == 200 and d2["audit"]["evaluation_id"] == a["audit"]["evaluation_id"],
           f"{case}: a second call gave another evaluation")
        ok(d2["requirements"] == req, f"{case}: plain and /rules answers differ")
    if not expected:
        problems.append("expected_demo.json missing: the demo answers were not checked")

    # concurrency: 36 calls on 4 cases at once -> one answer per case
    cases = ["CASE-D0C500000003", "CASE-D0C500000006", "CASE-D0C500000011", "CASE-D0C500000013"] * 9
    with cf.ThreadPoolExecutor(12) as pool:
        results = list(pool.map(lambda cid: call(base, f"{api}/{cid}"), cases))
    seen: dict[str, set] = {}
    for cid, (code, out, _) in zip(cases, results):
        seen.setdefault(cid, set()).add((code, out.get("overall_status") if isinstance(out, dict) else None,
                                         out.get("audit", {}).get("evaluation_id") if isinstance(out, dict) else None))
    ok(all(len(v) == 1 and next(iter(v))[0] == 200 for v in seen.values()), f"concurrency: {seen}")

    # speed: warm answers are fast
    times = []
    for _ in range(5):
        t = time.perf_counter()
        call(base, f"{api}/CASE-D0C500000009")
        times.append(time.perf_counter() - t)
    ok(sorted(times)[2] < 2.0, f"median warm answer {sorted(times)[2]:.2f} s (> 2 s)")


# ======================================================================================================== negative
def negative(base: str) -> None:
    api = "/api/v1/credit/document-readiness"
    good = f"{api}/CASE-D0C500000001"
    cases = [
        # name, path, method, body, raw, headers, allowed codes
        ("unknown case", f"{api}/CASE-DOESNOTEXIST", "POST", None, None, None, {403, 404}),
        ("unknown case on /rules", f"{api}/CASE-DOESNOTEXIST/rules", "POST", None, None, None, {403, 404}),
        ("lower-case id (ids are exact)", f"{api}/case-d0c500000001", "POST", None, None, None, {403, 404}),
        ("SQL-looking id", f"{api}/CASE'%20OR%201=1--", "POST", None, None, None, {422}),
        ("quote in id", f"{api}/CASE%22%3B", "POST", None, None, None, {422}),
        ("path traversal", f"{api}/..%2F..%2Fetc%2Fpasswd", "POST", None, None, None, {404, 422}),
        ("encoded slash in id", f"{api}/CASE%2FX", "POST", None, None, None, {404, 422}),
        ("null byte in id", f"{api}/CASE%00X", "POST", None, None, None, {400, 422}),
        ("space in id", f"{api}/CASE%20D0C5", "POST", None, None, None, {422}),
        ("unicode id", f"{api}/CASE-%E0%A4%85", "POST", None, None, None, {422}),
        ("emoji id", f"{api}/CASE-%F0%9F%98%80", "POST", None, None, None, {422}),
        ("id 129 chars", f"{api}/" + "A" * 129, "POST", None, None, None, {422}),
        ("id 5000 chars", f"{api}/" + "A" * 5000, "POST", None, None, None, {414, 422, 431}),
        ("script in id", f"{api}/%3Cscript%3E", "POST", None, None, None, {422}),
        ("caller sends a status", good, "POST", {"overall_status": "COMPLETE"}, None, None, {422}),
        ("caller sends documents", good, "POST", {"documents": [{"type": "PAN"}]}, None, None, {422}),
        ("caller sends a stage", good, "POST", {"stage": "CREDIT"}, None, None, {422}),
        ("caller sends a policy", good, "POST", {"policy_version": "X"}, None, None, {422}),
        ("correlation id with spaces / tags", good, "POST", {"correlation_id": "a b<script>"}, None, None, {422}),
        ("correlation id 129 chars", good, "POST", {"correlation_id": "x" * 129}, None, None, {422}),
        ("correlation id a number", good, "POST", {"correlation_id": 12345}, None, None, {422}),
        ("correlation id a list", good, "POST", {"correlation_id": ["a"]}, None, None, {422}),
        ("JSON array body", good, "POST", None, b"[1,2]", None, {422}),
        ("JSON string body", good, "POST", None, b'"hello"', None, {422}),
        ("broken JSON", good, "POST", None, b"{bad", None, {422}),
        ("deeply nested JSON", good, "POST", None, b'{"a":' * 200 + b"1" + b"}" * 200, None, {422}),
        ("1 MB body", good, "POST", None, b'{"correlation_id":"' + b"x" * 1_000_000 + b'"}', None, {413, 422}),
        ("form body", good, "POST", None, b"correlation_id=abc", {"Content-Type": "application/x-www-form-urlencoded"},
         {415, 422}),
        ("GET not allowed", good, "GET", None, None, None, {405}),
        ("PUT not allowed", good, "PUT", {}, None, None, {405}),
        ("DELETE not allowed", good, "DELETE", None, None, None, {405}),
        ("PATCH on /rules", good + "/rules", "PATCH", {}, None, None, {405}),
        ("unknown sub-route", good + "/anything", "POST", None, None, None, {404, 405}),
        # still answers, but with exactly what the caller may send
        ("valid call with a correlation id", good, "POST", {"correlation_id": "check-001"}, None, None, {200}),
        ("valid call with no body", good, "POST", None, None, None, {200}),
    ]
    for name, path, method, body, raw, headers, allowed in cases:
        code, out, _ = call(base, path, method, body, raw, headers)
        text = json.dumps(out).lower() if not isinstance(out, str) else out.lower()
        leak = next((w for w in LEAKS if w in text), None)
        ok(code in allowed and not leak,
           f"negative '{name}': HTTP {code}, allowed {sorted(allowed)}{f', leaks {leak!r}' if leak else ''}: {text[:140]}")
        if code == 200 and name.startswith("valid call with a correlation"):
            ok(out.get("request_id") == "check-001", "the correlation id is not echoed as request_id")
    # no case list is ever served (the dashboard opens one case by its id): whatever answers this path (the LOS
    # server routes it elsewhere), it must not list cases
    code, out, _ = call(base, f"{api}/", "GET")
    text = json.dumps(out) if not isinstance(out, str) else out
    ok(code in (404, 405) or "CASE-D0C5" not in text, f"negative 'case list': HTTP {code} lists cases: {text[:140]}")


# ======================================================================================================== dashboard
def dashboard(front: str, base: str) -> None:
    code, html, h = call(front, "/", "GET")
    ok(code == 200 and isinstance(html, str) and "Credit Document Desk" in html, f"dashboard / answered {code}")
    csp = h.get("content-security-policy", "")
    ok("script-src 'self'" in csp and "frame-ancestors 'none'" in csp, f"dashboard CSP weak: {csp[:120]}")
    ok(h.get("x-content-type-options") == "nosniff", "dashboard: no nosniff")
    for asset in ("/app.js", "/styles.css", "/theme.js"):
        c, _, hh = call(front, asset, "GET")
        ok(c == 200 and hh.get("etag"), f"dashboard {asset}: {c}")
    c, page, _ = call(front, "/?case=CASE-D0C500000005", "GET")
    ok(c == 200, f"dashboard deep link answered {c}")
    # the proxy gives the backend's answer, untouched
    c1, via, _ = call(front, "/api/v1/credit/document-readiness/CASE-D0C500000009/rules")
    c2, direct, _ = call(base, "/api/v1/credit/document-readiness/CASE-D0C500000009/rules")
    ok(c1 == 200 and isinstance(via, dict) and via.get("answer", {}).get("audit", {}).get("evaluation_id")
       == direct.get("answer", {}).get("audit", {}).get("evaluation_id"), "dashboard proxy answer differs")
    for name, path, method, raw, allowed in [
        ("static path traversal", "/..%2f..%2fserver.js", "GET", None, {400, 403, 404}),
        ("static dot-dot", "/../server.js", "GET", None, {200, 400, 403, 404}),
        ("POST to a page", "/", "POST", b"{}", {405}),
        ("other API not proxied", "/api/v1/applicants", "GET", None, {404, 405}),
        ("body over the proxy limit", "/api/v1/credit/document-readiness/CASE-D0C500000001", "POST",
         b'{"correlation_id":"' + b"x" * 70_000 + b'"}', {413}),
    ]:
        c, out, _ = call(front, path, method, raw=raw)
        text = out if isinstance(out, str) else json.dumps(out)
        ok(c in allowed and "require(" not in text, f"dashboard negative '{name}': {c}, allowed {sorted(allowed)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Credit Document Desk acceptance check")
    ap.add_argument("--backend", default="http://127.0.0.1:8020")
    ap.add_argument("--frontend", default="http://127.0.0.1:5182", help="'' to skip the dashboard checks")
    args = ap.parse_args()
    started = time.perf_counter()
    positive(args.backend)
    negative(args.backend)
    if args.frontend:
        dashboard(args.frontend, args.backend)
    print(f"{passed} checks passed, {len(problems)} failed ({time.perf_counter() - started:.1f} s)")
    for p in problems:
        print("  FAIL", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
