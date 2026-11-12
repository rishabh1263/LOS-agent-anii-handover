// ALL PARAMETERS, LIVE (sir, 2026-10-24): POST /api/v1/credit/dedupe/all on E2E_API (default 8010) for a synthetic
// case, then the page's own view (core.allView). Checks: the conflict holds the case, agreeing identifiers say so,
// missing parameters are listed, nothing is shown in full, the combined decision is the most serious row.
// Run: node los-copilot-frontend/test/e2e/all_sweep.js
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
const ORDER = ["BLOCK_APPLICATION", "UNABLE_TO_DECIDE", "HOLD_FOR_REVIEW", "PROCEED_TO_NEXT_STAGE"];

(async () => {
  const made = await post("/api/v1/fos/applicants", { applicant: { full_name: "Priya More", mobile: `9${crypto.randomInt(100000000, 999999999)}`,
    date_of_birth: "1988-11-02", address: "Synthetic Road, Pune" }, application: { product: "PERSONAL_LOAN", loan_amount: 500000 } });
  const caseId = made.data.case_id;
  const runs = [
    ["one customer: PAN + its own mobile", { pan: "BCDPM5678N", mobile: "9876502222" }, "SAME_CUSTOMER"],
    ["two customers: PAN of one, mobile of another", { pan: "BCDPM5678N", mobile: "9876501111" }, "CONFLICT"],
    ["all four, one customer", { pan: "BCDPM5678N", mobile: "9876502222", ap_code: "AP00745218", pr_code: "PR02407753" }, "SAME_CUSTOMER"],
    ["PAN only (mobile from the case record)", { pan: "BCDPM5678N" }, null],
    ["nobody in MIFIN", { pan: "ZZZPZ1111Z" }, "NO_CUSTOMER_FOUND"],
  ];
  const problems = [];
  for (const [name, values, expect] of runs) {
    const r = await post("/api/v1/credit/dedupe/all", { case_id: caseId, customer_type: "Existing", party_role: "Applicant", ...values });
    if (r.status !== 200) { problems.push(`${name}: HTTP ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`); continue; }
    const v = C.allView(r.data, UI);
    console.log(`\n== ${name}\n   ${v.combined.label} · ${v.consistency.text}`);
    v.rows.forEach((x) => console.log(`   ${x.label.padEnd(24)} ${String(x.value).padEnd(12)} ${x.from.padEnd(12)} ${x.match.padEnd(16)} ${String(x.customer || "—").padEnd(11)} ${x.decision}`));
    v.missing.forEach((m) => console.log(`   ${m.padEnd(24)} not checked`));
    if (expect && r.data.consistency.status !== expect) problems.push(`${name}: consistency ${r.data.consistency.status}, expected ${expect}`);
    if (r.data.consistency.status === "CONFLICT" && r.data.combined.recommendation === "PROCEED_TO_NEXT_STAGE") problems.push(`${name}: conflict yet PROCEED`);
    const worst = r.data.rows.map((x) => x.recommendation).sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b))[0];
    if (ORDER.indexOf(r.data.combined.recommendation) > ORDER.indexOf(worst)) problems.push(`${name}: combined lighter than its worst row`);
    const shown = JSON.stringify(r.data.rows.map(({ result, ...x }) => x));
    if (/BCDPM5678N|9876502222|9876501111/.test(shown)) problems.push(`${name}: a full PAN / mobile in the rows`);
  }
  console.log(`\nproblems: ${problems.length}`);
  problems.forEach((p) => console.log("  ! " + p));
  process.exit(problems.length ? 1 : 0);
})().catch((e) => { console.error("ERROR", e.message); process.exit(99); });
