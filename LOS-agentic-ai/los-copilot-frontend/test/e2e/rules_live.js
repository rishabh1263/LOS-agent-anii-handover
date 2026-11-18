// R01-R11 on the LIVE backend (through this page's proxy): synthetic cases built to trigger each rule that the LOS
// can evaluate, then the page's own rule logic (core.businessRules) on the real answers.
// Run: node los-copilot-frontend/test/e2e/rules_live.js   (frontend on E2E_BASE, backend with demo MIFIN / LMS)
"use strict";

const C = require("../../public/core.js");
const UI = require("../../public/ui_text.json");
const crypto = require("node:crypto");

const BASE = (process.env.E2E_BASE || "http://127.0.0.1:5183").replace(/\/$/, "");
const results = [];
const check = (name, ok, info) => {
  results.push(ok);
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${ok ? "" : `   [${String(info).slice(0, 300)}]`}`);
};

async function call(method, path, body) {
  const res = await fetch(BASE + path, { method, headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: body ? JSON.stringify(body) : undefined });
  return { status: res.status, data: await res.json().catch(() => ({})) };
}

const mobile = () => `9${crypto.randomInt(100000000, 999999999)}`;              // a fresh synthetic mobile per run
const applicant = (name, m) => ({ full_name: name, mobile: m, date_of_birth: "1990-05-10", address: "Synthetic Road, Pune" });

async function openCase(name, m, product = "PERSONAL_LOAN") {
  const r = await call("POST", "/api/v1/fos/applicants", { applicant: applicant(name, m), application: { product, loan_amount: 500000 } });
  if (r.status >= 300) throw new Error(`create ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`);
  return r.data.case_id;
}

async function rules(caseId, parameter, value) {
  const r = await call("POST", "/api/v1/credit/dedupe", { case_id: caseId, party_role: "APPLICANT", customer_type: "Existing",
    dedupe_parameter: parameter, dedupe_parameter_value: value });
  if (r.status !== 200) throw new Error(`dedupe ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`);
  return { R: Object.fromEntries(C.businessRules(r.data, UI).map((b) => [b.id, b])), raw: r.data };
}

(async () => {
  // ---- a clean, unique customer: nothing found anywhere
  const solo = mobile();
  const soloCase = await openCase("Test Solo", solo);
  let { R } = await rules(soloCase, "Mobile No", solo);
  check("clean customer: R01 clear (no other LOS case)", R.R01.tone === "pass", R.R01.reason);
  check("clean customer: R06 clear (no other application)", R.R06.tone === "pass", R.R06.reason);
  check("clean customer: R04 clear (no role change)", R.R04.tone === "pass", R.R04.reason);
  check("clean customer: R03 clear (no document reuse)", R.R03.tone === "pass", R.R03.reason);
  check("R05 runs against its limits (new customer, nothing to count): clear", R.R05.tone === "pass", R.R05.reason);
  check("R08-R11 run (no system missing): clear for a new customer", ["R08", "R09", "R10", "R11"].every((k) => R[k].tone === "pass"),
    ["R08", "R09", "R10", "R11"].map((k) => `${k}:${R[k].tone}`));

  // ---- the same customer applying twice for the same product -> R01 + R06 found
  const twice = mobile();
  await openCase("Test Twice", twice);
  const second = await openCase("Test Twice", twice);
  ({ R } = await rules(second, "Mobile No", twice));
  check("same mobile on another LOS case: R01 Found", R.R01.tone === "fail", R.R01.reason);
  // a shared mobile does not prove the same customer (a PAN does): R06 says so -- never "clear"; the dedupe holds
  check("same mobile, same product, again: R06 partly checked (not proven), never clear", R.R06.tone === "partial"
    && /not proven/.test(R.R06.reason), R.R06.reason);
  check("...and R04 / R03 stay clear (not mixed up)", R.R04.tone === "pass" && R.R03.tone === "pass", [R.R04.reason, R.R03.reason]);

  // ---- a different product is not a simultaneous application
  const other = mobile();
  await openCase("Test Other", other, "PERSONAL_LOAN");
  const homeLoan = await openCase("Test Other", other, "HOME_LOAN");
  ({ R } = await rules(homeLoan, "Mobile No", other));
  check("same customer, different product: R01 Found, R06 not Found", R.R01.tone === "fail" && R.R06.tone !== "fail", [R.R01.tone, R.R06.reason]);

  // ---- an existing MIFIN customer whose name / DOB on this case differ -> R07 Found
  const mism = await openCase("Test Mismatch", mobile());
  ({ R } = await rules(mism, "PAN", "BCDPM5678N"));
  check("PAN matches MIFIN but name / DOB differ: R07 Found", R.R07.tone === "fail" && /name|date of birth/.test(R.R07.reason), R.R07.reason);
  check("existing customer with clean history: R02 clear so far (DPD limit unset)", R.R02.tone === "partial", R.R02.reason);

  // ---- EMI bounces + an open fraud case: R08 and R09 Found
  const dpd = await openCase("Test Dpd", mobile());
  ({ R } = await rules(dpd, "PAN", "FGHPT8642U"));
  check("3 EMI bounces: R08 Found", R.R08.tone === "fail", R.R08.reason);
  check("an open RCU investigation: R09 Found", R.R09.tone === "fail", R.R09.reason);
  check("conflicting records: R02 never shown Clear", R.R02.tone !== "pass", R.R02.reason);

  // ---- a possible match confirmed: R10 (shared property) and R11 (not eligible for a top-up) Found
  const pm = await openCase("Test Possible", mobile());
  const first = await call("POST", "/api/v1/credit/dedupe", { case_id: pm, party_role: "APPLICANT", customer_type: "Existing",
    dedupe_parameter: "PAN", dedupe_parameter_value: "DEFPR1357S" });
  const cand = first.data.history.customer.confirmation.candidate;
  const conf = await call("POST", "/api/v1/credit/dedupe", { case_id: pm, party_role: "APPLICANT", customer_type: "Existing",
    dedupe_parameter: "PAN", dedupe_parameter_value: "DEFPR1357S", confirm_match: "SAME_CUSTOMER", candidate: cand });
  R = Object.fromEntries(C.businessRules(conf.data, UI).map((b) => [b.id, b]));
  check("a property also pledged by another customer: R10 Found", R.R10.tone === "fail", R.R10.reason);
  check("a running loan not eligible for a top-up: R11 Found", R.R11.tone === "fail", R.R11.reason);

  const failed = results.filter((x) => !x).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed);
})().catch((e) => { console.error("ERROR", e.message); process.exit(99); });
