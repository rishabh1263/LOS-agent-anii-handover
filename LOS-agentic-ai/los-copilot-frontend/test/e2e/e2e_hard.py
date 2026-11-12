"""HARD end-to-end test of the Dedupe Agent, positive and negative, in a real browser against 5183 -> 8010.
SYNTHETIC demo identifiers only (app/config/demo/mifin_demo.yaml). Prints PASS / FAIL per check; exit code = failures."""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

from playwright.sync_api import sync_playwright

# Run:  python los-copilot-frontend/test/e2e/e2e_hard.py [screenshot-dir]   (frontend + backend running, demo MIFIN/LMS)
BASE = os.getenv("E2E_BASE", "http://127.0.0.1:5183/")
EDGE = os.getenv("E2E_BROWSER", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
results = []
FULL_IDS = ["BCDPM5678N", "ABCPK4321L", "DEFPR1357S", "9876502222", "9876505555"]


def check(name, ok, info=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   [{str(info)[:300]}]"), flush=True)


def http(method, path, body=None):
    req = urllib.request.Request(BASE.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EDGE, headless=True, args=["--disable-gpu"])
    page = b.new_page(viewport={"width": 1440, "height": 900})
    errors, calls, dialogs = [], [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.on("request", lambda r: calls.append((r.method, r.url.replace(BASE, "/"))) if "/api/" in r.url else None)

    def home():
        page.goto(BASE, timeout=90000)
        page.wait_for_selector("#apply-form:not([hidden])", timeout=60000)

    def fill(customer="Existing", param=None, value=None, want=None):
        for _ in range(60):
            page.click("#sample-fill")
            if want is None or want(page):
                break
        page.select_option("#customer-type", customer)
        page.wait_for_timeout(150)
        if param:
            page.select_option("#dedupe-parameter", param)
            page.fill("#dedupe-value", value)

    def submit_and_wait():
        page.click("#submit")
        page.wait_for_selector("dialog[open]", timeout=10000)
        page.click("#confirm-ok")
        page.wait_for_selector("section[data-page=result] .hero", timeout=90000)
        page.wait_for_timeout(700)
        return page.inner_text("section[data-page=result]")

    def posts(path):
        return sum(1 for m, u in calls if m == "POST" and u.startswith(path))

    # =========================================================== the form
    home()
    kinds = dict(x.split(":") for x in page.eval_on_selector_all("#fields-top [data-field]", "x => x.map(e => e.dataset.field + ':' + (e.querySelector('select') ? 'select' : e.querySelector('input[type=radio]') ? 'radio' : e.querySelector('input[type=checkbox]') ? 'checkbox' : e.querySelector('input[type=date]') ? 'date' : 'text'))"))
    check("P01 form: choice fields are drop-downs",
          all(kinds.get(k) == "select" for k in ("product", "property_type", "tenure_months", "loan_purpose", "applicant_type", "employment_type")), kinds)
    check("P02 form: Yes/No radio, Aadhaar checkbox; no Document Received Date",
          (kinds.get("co_origination"), kinds.get("mobile_aadhaar_linked")) == ("radio", "checkbox")
          and "document_received_date" not in kinds, kinds)
    opts = page.eval_on_selector_all("#f-loan_purpose option", "x => x.map(o => o.value)")
    check("P03 form: Marriage is a loan purpose", "Marriage" in opts, opts)
    check("P04 form: no Loan Details section, no code box", page.locator("#loan-card").is_hidden() and page.locator("text=Assigned by MIFIN").count() == 0)

    # ---- negatives on the form: nothing is sent
    before = posts("/api/v1/fos/applicants")
    page.click("#reset") if page.locator("#reset").is_visible() else None
    page.click("#submit")
    page.wait_for_timeout(500)
    check("N01 empty form: errors shown, no case created",
          posts("/api/v1/fos/applicants") == before and page.locator(".field.invalid").count() >= 3,
          page.locator(".field.invalid").count())
    fill("Existing", "PAN", "ABC123")
    page.click("#submit"); page.wait_for_timeout(400)
    check("N02 bad PAN format refused before sending", posts("/api/v1/fos/applicants") == before
          and page.inner_text("#err-dedupeValue").strip() != "", page.inner_text("#err-dedupeValue"))
    fill("Existing", "Mobile No", "12345")
    page.click("#submit"); page.wait_for_timeout(400)
    check("N03 bad mobile refused before sending", posts("/api/v1/fos/applicants") == before
          and page.inner_text("#err-dedupeValue").strip() != "")
    fill("Existing", "PAN", "BCDPM5678N")
    page.fill("#f-loan_amount", "abc")
    page.click("#submit"); page.wait_for_timeout(400)
    check("N04 a non-number loan amount refused", posts("/api/v1/fos/applicants") == before
          and page.inner_text("[data-field=loan_amount] .field-error").strip() != "")
    page.fill("#f-mobile", "")
    page.click("#submit"); page.wait_for_timeout(400)
    check("N05 a required field emptied is refused", posts("/api/v1/fos/applicants") == before)

    # ---- PAN / mobile boxes masked while not being edited; the real value is still what is sent
    home(); fill("Existing", "PAN", "BCDPM5678N",
                 want=lambda pg: pg.locator("#coapp-list fieldset").count() >= 1 and pg.input_value("#co-0-pan") != "")
    page.click("#f-full_name")                                   # leave the box
    co_pan = page.input_value("#co-0-pan")
    check("P27 co-applicant PAN box masked when not being edited", co_pan.startswith("*") and len(co_pan) == 10, co_pan)
    check("P28 Dedupe Parameter Value (PAN) masked when not being edited", page.input_value("#dedupe-value") == "******678N",
          page.input_value("#dedupe-value"))
    page.click("#dedupe-value")
    check("P29 the full value is shown while editing", page.input_value("#dedupe-value") == "BCDPM5678N")
    page.click("#f-full_name")
    t = submit_and_wait()
    check("P30 the real (unmasked) PAN was sent: customer found", "AP00745218" in t)

    # =========================================================== existing customer by PAN, live + closed + cancelled loans
    home(); fill("Existing", "PAN", "BCDPM5678N")
    page.fill("#f-address", "<img src=x onerror=alert(1)> Pune")
    t = submit_and_wait()
    page.screenshot(path=f"{OUT}/e2e_existing_pan.png", full_page=True)
    check("P05 existing by PAN: our customer, full AP code", "Yes. They are our customer AP00745218." in t)
    check("P06 3 previous loans with each status", "Yes, 3: 1 active, 1 closed, 1 canceled." in t)
    check("P07 PAN the same on every loan", "The same PAN is on all 3 loan(s)." in t)
    check("P08 the applicant line agrees with the match (checked by PAN)",
          not re.search(r"Applicant\s*\n?\s*No duplicate found – no SBFC customer record · checked by mobile", t),
          [l for l in t.splitlines() if l.startswith("No duplicate found")][:2])
    check("P09 'Why' has exactly 3 points", page.locator(".reason-points li").count() == 3)
    check("P10 removed: 'What to do next', 'not a credit decision' line, code box",
          "What to do next" not in t and "What do I do next" not in t and "It is not a credit decision" not in t)
    check("P11 no full PAN / mobile anywhere on the result", not any(x in t for x in FULL_IDS), [x for x in FULL_IDS if x in t])
    check("P12 PAN shown masked in 'Field by field'", "******678N" in t)
    check("P13 loan table fits (no sideways scroll)",
          page.evaluate("() => { const w = document.querySelector('.eld'); return w.scrollWidth <= w.clientWidth + 1 }"))
    check("N06 HTML in the address never runs", not dialogs, dialogs)
    rows = page.locator(".eld tbody tr:not(.pr-detail)")
    cancelled = [i for i in range(rows.count()) if "Cancel" in rows.nth(i).inner_text()]
    reason_cell = rows.nth(cancelled[0]).locator("td.reason").inner_text().strip() if cancelled else ""
    check("P14 a cancelled loan shows its reason (or 'Not given by source')", bool(reason_cell) and reason_cell != "—", reason_cell)
    rows.nth(cancelled[0]).click(); page.wait_for_timeout(400)
    check("P31 clicking a loan opens its side panel; the table stays one line per loan",
          page.locator("#pr-drawer").is_visible() and page.locator(".eld tr.pr-detail").count() == 0)
    cards = page.locator(".pr-card h5").all_inner_texts()
    detail = page.inner_text(".pr-cards")
    check("P15 ▸ opens 4 cards: Customer, Loan, Repayment, Status", [c.upper() for c in cards[:4]] == ["CUSTOMER", "LOAN", "REPAYMENT", "STATUS"], cards)
    check("P16 Customer card: role, common applicant, PAN, name, DOB",
          all(k in detail for k in ("Role in MIFIN", "Common Applicant", "PAN Match", "Name Match", "DOB Match")))
    check("P17 cancelled loan: LMS fields 'N/A – cancelled', reason in Status card",
          "N/A – cancelled" in detail and "Cancellation Reason" in detail)
    page.screenshot(path=f"{OUT}/e2e_pr_panel.png")
    page.keyboard.press("Escape"); page.wait_for_timeout(300)
    check("P32 Esc closes the panel", page.locator("#pr-drawer").count() == 0)
    check("P18 every rule runs: none 'can't run yet'", page.locator(".brules-na").count() == 0)
    hist = page.evaluate("() => document.querySelector('.hist-checks').textContent")
    check("P19 every loan-history check has its limit: none 'Limit not set'", "Limit not set" not in hist, hist[:200])
    # the model's reason: the spinner always ends (AI wording or the standard points), never hangs
    try:
        page.wait_for_selector(".copilot .reason-wait", state="detached", timeout=70000)
        settled = True
    except Exception:
        settled = False
    check("P20 the AI reason finishes or falls back within 70 s", settled and page.locator(".reason-points li").count() == 3)

    # =========================================================== 3 cancelled loans
    home(); fill("Existing", "PAN", "ABCPK4321L")
    t = submit_and_wait()
    check("P21 3 cancelled cases: common applicant on all 3", "all 3 cases are under one customer" in t)
    reasons = [c.strip() for c in page.locator(".eld td.reason").all_inner_texts()]
    check("P22 each cancelled case shows a reason", len(reasons) == 3 and all(r and r != "—" for r in reasons), reasons)

    # =========================================================== by mobile, by customer ID, by loan account
    home(); fill("Existing", "Mobile No", "9876502222")
    t = submit_and_wait()
    check("P23 by mobile: found, mobile shown masked", "AP00745218" in t and "98******22" in t and "9876502222" not in t)
    home(); fill("Existing", "AP Code", "AP00745218")
    t = submit_and_wait()
    check("P24 by Customer ID: found with 3 loans", "Yes, 3:" in t)
    home(); fill("Existing", "PR Code", "PR02407753")
    t = submit_and_wait()
    check("P25 by loan account: 'This loan account' marked", page.locator(".mf-this").count() == 1)

    # =========================================================== new customer
    home(); fill("New", want=lambda pg: pg.locator("#coapp-list fieldset").count() == 0)   # a co-applicant may be existing
    t = submit_and_wait()
    check("P26 new customer: no dedupe needed, go ahead", "No. This is a new customer." in t and "Go ahead" in t)

    # =========================================================== negatives with an answer
    home(); fill("Existing", "PAN", "DEFPR1357S")
    t = submit_and_wait()
    check("N07 possible match: not 'go ahead', loans hidden, confirm box shown",
          "Go ahead" not in t and page.locator(".hero-confirm").count() == 1 and "Hidden until you confirm" in t)
    home(); fill("Existing", "Mobile No", "9876505555")
    t = submit_and_wait()
    check("N08 a mobile on two customers: never 'go ahead'", "Go ahead" not in t and "More than one customer" in t)
    home(); fill("Existing", "PAN", "EFGPS9753T")
    t = submit_and_wait()
    check("N09 MIFIN error: never 'go ahead'", "Go ahead" not in t and "could not check" in t.lower())
    home(); fill("Existing", "PAN", "ZZZPZ1234Z")
    t = submit_and_wait()
    check("N10 declared existing but not found: said, no crash", "No. This is a new customer." in t or "not found" in t.lower())

    # ---- double click: one case only
    home(); fill("New")
    n0 = posts("/api/v1/fos/applicants")
    page.click("#submit"); page.wait_for_selector("dialog[open]", timeout=10000)
    page.click("#confirm-ok"); page.wait_for_timeout(50)
    try:
        page.click("#submit", timeout=1000)
    except Exception:
        pass
    page.wait_for_selector("section[data-page=result] .hero", timeout=90000)
    check("N11 a double click creates one case", posts("/api/v1/fos/applicants") - n0 == 1, posts("/api/v1/fos/applicants") - n0)

    # ---- lookup page: a case that does not exist
    home()
    page.click("button[data-page=lookup]")
    page.fill("#lk-case", "CASE-DOESNOTEXIST")
    page.select_option("#lk-parameter", "PAN"); page.fill("#lk-value", "BCDPM5678N")
    page.click("#lk-submit"); page.wait_for_timeout(2500)
    shown = page.inner_text("#result-error") if page.locator("#result-error").is_visible() else ""
    check("N12 unknown case: a clear error, not a result", bool(shown.strip()) and not page.locator("section[data-page=result] .hero").is_visible(), shown[:120])

    check("N13 no page errors in the whole run", not errors, errors)
    b.close()

# =========================================================== API through the proxy
s, body = http("GET", "/api/v1/admin/users")
check("N14 proxy refuses a route the page does not use", s == 404 and body.get("detail", {}).get("code") == "ROUTE_NOT_PROXIED", (s, body))
s, body = http("GET", "/api/v1/credit/dedupe/ddp_" + "0" * 32 + "/reason")
check("N15 a reason for an unknown check: 404", s == 404, (s, body))
s, body = http("POST", "/api/v1/credit/dedupe", {"case_id": "CASE-X", "dedupe_parameter": "PAN", "dedupe_parameter_value": "<script>"})
check("N16 a malformed dedupe request is refused (4xx)", 400 <= s < 500, s)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(len(failed))
