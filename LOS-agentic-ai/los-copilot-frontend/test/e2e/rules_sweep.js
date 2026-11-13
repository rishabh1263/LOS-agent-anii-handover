// EVERY RULE x EVERY DEMO VALUE, live (owner 2026-10-23: "check for each value across all case"). Each demo identifier
// by each parameter type, a new customer and a confirmed possible match -> the real API on E2E_API (default 8010) ->
// the page's own rule logic (core.businessRules) and summary. Prints one row per value and every non-clear reason;
// flags any reason that reads as a gap ("no MIFIN record", "not compared") or is too long.
// Run: node los-copilot-frontend/test/e2e/rules_sweep.js
"use strict";

const C = require("../../public/core.js");
const UI = require("../../public/ui_text.json");
const crypto = require("node:crypto");

const API = (process.env.E2E_API || "http://127.0.0.1:8010").replace(/\/$/, "");
const post = async (path, body) => {
  const r = await fetch(API + path, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body) });
  return { status: r.status, data: await r.json().catch(() => ({})) };
};
const SUSPECT = [/no MIFIN record to compare/i, /not compared for this identifier/i, /unknown check/i, /not in this answer/i];
const MARK = { pass: "✓", fail: "FOUND", partial: "~part", skip: "?not", na: "-n/a" };

(async () => {
  const mob = `9${crypto.randomInt(100000000, 999999999)}`;
  const c = await post("/api/v1/fos/applicants", { applicant: { full_name: "Sweep Person", mobile: mob, date_of_birth: "1990-05-10",
    address: "Synthetic Road, Pune" }, application: { product: "PERSONAL_LOAN", loan_amount: 500000 } });
  const caseId = c.data.case_id;
  const runs = [
    ["PAN", "BCDPM5678N", "existing, live+closed+cancelled"], ["Mobile No", "9876502222", "same, by mobile"],
    ["AP Code", "AP00745218", "same, by customer ID"], ["PR Code", "PR02407753", "same, by loan account"],
    ["PAN", "ABCPK4321L", "3 cancelled"], ["Mobile No", "9876501111", "3 cancelled, by mobile"],
    ["AP Code", "AP00731942", "3 cancelled, by customer ID"], ["PR Code", "PR02184467", "3 cancelled, by loan account"],
    ["PAN", "CDEPN2468Q", "no loans"], ["PAN", "DEFPR1357S", "possible match (undecided)"],
    ["Mobile No", "9876505555", "two customers on one mobile"], ["PAN", "EFGPS9753T", "MIFIN error"],
    ["PAN", "FGHPT8642U", "conflict + bounces + fraud"], ["AP Code", "AP00783315", "same, by customer ID"],
    ["PR Code", "PR02490017", "same, by loan account"], ["PAN", "ZZZPZ1111Z", "not in MIFIN (declared existing)"],
  ];
  const suspects = [];
  const rows = [];
  for (const [parameter, value, label] of runs) {
    const r = await post("/api/v1/credit/dedupe", { case_id: caseId, party_role: "APPLICANT", customer_type: "Existing",
      dedupe_parameter: parameter, dedupe_parameter_value: value });
    if (r.status !== 200) { rows.push([label, `HTTP ${r.status}`]); continue; }
    rows.push([`${label} [${parameter}]`, r.data]);
  }
  // a new customer, and the possible match once confirmed
  const nw = await post("/api/v1/credit/dedupe", { case_id: caseId, party_role: "APPLICANT", customer_type: "New",
    dedupe_parameter: "PAN", dedupe_parameter_value: "QWEPR5678T" });
  rows.push(["new customer [PAN]", nw.data]);
  const pm = await post("/api/v1/credit/dedupe", { case_id: caseId, party_role: "APPLICANT", customer_type: "Existing",
    dedupe_parameter: "PAN", dedupe_parameter_value: "DEFPR1357S" });
  const conf = await post("/api/v1/credit/dedupe", { case_id: caseId, party_role: "APPLICANT", customer_type: "Existing",
    dedupe_parameter: "PAN", dedupe_parameter_value: "DEFPR1357S", confirm_match: "SAME_CUSTOMER",
    candidate: pm.data.history.customer.confirmation.candidate });
  rows.push(["possible match -> same customer [PAN]", conf.data]);

  console.log("value".padEnd(48) + ["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R09", "R10", "R11"].map((x) => x.padEnd(6)).join("") + " recommendation");
  for (const [label, d] of rows) {
    if (typeof d === "string") { console.log(label.padEnd(48) + d); continue; }
    const R = C.businessRules(d, UI);
    const rec = ((d.dedupe || {}).workflow_recommendation || {}).recommendation || "-";
    console.log(label.slice(0, 47).padEnd(48) + R.map((b) => (MARK[b.tone] || b.tone).padEnd(6)).join("") + " " + rec);
    for (const b of R.filter((x) => x.tone !== "pass")) {
      console.log(`      ${b.id} ${MARK[b.tone]}: ${b.reason}`);
      if (SUSPECT.some((re) => re.test(b.reason)) || b.reason.length > 240) suspects.push(`${label} ${b.id}: ${b.reason}`);
    }
  }
  console.log(`\nreasons that read as a gap or are too long: ${suspects.length}`);
  suspects.forEach((x) => console.log("  ! " + x));
  process.exit(suspects.length ? 1 : 0);
})().catch((e) => { console.error("ERROR", e.message); process.exit(99); });
