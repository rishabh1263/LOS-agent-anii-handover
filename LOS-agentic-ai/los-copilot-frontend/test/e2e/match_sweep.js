// THE MATCHING TABLE AND RULE ORDER, LIVE, FOR EVERY DEMO VALUE (sir's review, 2026-10-23). Each demo identifier
// against two synthetic cases -- an applicant who differs from MIFIN and one who is MIFIN's own customer -- through the
// real API on E2E_API (default 8010), then the page's own logic (core.matchTable, core.businessRules, the colour order).
// Flags: a result with a side missing, "Matches" on different values / "Does not match" on equal ones, a full PAN or
// mobile anywhere in the table, rules out of red -> yellow -> green order, an identity mismatch that still proceeds.
// Run: node los-copilot-frontend/test/e2e/match_sweep.js
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
const ORDER = { fail: 0, partial: 1, skip: 1, pass: 2 };
const FULL = [/\b[A-Z]{5}\d{4}[A-Z]\b/, /(?<!\d)[6-9]\d{9}(?!\d)/];
const norm = (x) => String(x || "").toUpperCase().replace(/[^A-Z0-9*]/g, "");

function nameAgrees(a, b) {                     // the backend's rule: first word and last word (mifin.compare_identity)
  const x = String(a || "").toUpperCase().split(/\s+/).filter(Boolean);
  const y = String(b || "").toUpperCase().split(/\s+/).filter(Boolean);
  return x.length >= 2 && y.length >= 1 && x[0] === y[0] && x[x.length - 1] === y[y.length - 1];
}

(async () => {
  const mk = async (name, dob) => (await post("/api/v1/fos/applicants", { applicant: { full_name: name, mobile: `9${crypto.randomInt(100000000, 999999999)}`,
    date_of_birth: dob, address: "Synthetic Road, Pune" }, application: { product: "PERSONAL_LOAN", loan_amount: 500000 } })).data.case_id;
  const cases = { "differs from MIFIN (Sweep Person 1990-05-10)": await mk("Sweep Person", "1990-05-10"),
                  "MIFIN's own customer (Priya More 1988-11-02)": await mk("Priya More", "1988-11-02") };
  const runs = [["PAN", "BCDPM5678N"], ["Mobile No", "9876502222"], ["AP Code", "AP00745218"], ["PR Code", "PR02407753"],
    ["PAN", "ABCPK4321L"], ["PAN", "CDEPN2468Q"], ["PAN", "DEFPR1357S"], ["Mobile No", "9876505555"], ["PAN", "EFGPS9753T"],
    ["PAN", "FGHPT8642U"], ["AP Code", "AP00768031"], ["PAN", "ZZZPZ1111Z"]];
  const problems = [];
  for (const [who, caseId] of Object.entries(cases)) {
    console.log(`\n======== case: ${who}`);
    for (const [parameter, value] of runs) {
      const r = await post("/api/v1/credit/dedupe", { case_id: caseId, party_role: "APPLICANT", customer_type: "Existing",
        dedupe_parameter: parameter, dedupe_parameter_value: value });
      const tag = `${who.split(" (")[0]} / ${parameter} ${value}`;
      if (r.status !== 200) { console.log(`${tag}: HTTP ${r.status}`); problems.push(`${tag}: HTTP ${r.status}`); continue; }
      const v = C.viewResult(r.data, UI);
      const rows = C.matchTable(v, { value }, UI);
      const rec = ((r.data.dedupe || {}).workflow_recommendation || {}).recommendation || "-";
      console.log(`\n${tag}  -> match ${v.match.code} · ${rec}`);
      for (const x of rows) {
        console.log(`   ${x.field.padEnd(22)} | ${String(x.entered ?? "—").padEnd(16)} | ${String(x.mifin ?? "—").padEnd(16)} | ${x.result}`);
        const both = x.entered != null && x.mifin != null;
        if (["Matches", "Does not match"].includes(x.result) && !both) problems.push(`${tag} ${x.field}: "${x.result}" with a side missing`);
        if (both && x.field === "Name") {
          const agree = nameAgrees(x.entered, x.mifin);
          if (x.result === "Matches" && !agree) problems.push(`${tag} Name: "Matches" but ${x.entered} vs ${x.mifin}`);
          if (x.result === "Does not match" && agree) problems.push(`${tag} Name: "Does not match" but ${x.entered} vs ${x.mifin}`);
        } else if (both && x.result === "Matches" && norm(x.entered) !== norm(x.mifin)) {
          problems.push(`${tag} ${x.field}: "Matches" but ${x.entered} vs ${x.mifin}`);
        } else if (both && x.result === "Does not match" && norm(x.entered) === norm(x.mifin)) {
          problems.push(`${tag} ${x.field}: "Does not match" but both read ${x.entered}`);
        }
        if (FULL.some((re) => re.test(`${x.entered} ${x.mifin}`))) problems.push(`${tag} ${x.field}: full PAN / mobile shown`);
      }
      const shown = v.business.filter((b) => b.tone !== "na").map((b, i) => [b, i])
        .sort(([a, i], [b, j]) => ((ORDER[a.tone] ?? 9) - (ORDER[b.tone] ?? 9)) || i - j).map(([b]) => b);
      console.log(`   rules: ${shown.map((b) => `${b.id}:${b.tone}`).join(" ")}`);
      for (let i = 1; i < shown.length; i++) {
        if ((ORDER[shown[i - 1].tone] ?? 9) > (ORDER[shown[i].tone] ?? 9)) problems.push(`${tag}: rule order broken at ${shown[i].id}`);
      }
      const mismatch = rows.some((x) => ["Name", "Date of birth", "PAN"].includes(x.field) && x.result === "Does not match");
      if (mismatch && rec === "PROCEED_TO_NEXT_STAGE") problems.push(`${tag}: identity differs from MIFIN yet PROCEED`);
      const found = v.business.some((b) => b.tone === "fail");
      if (found && rec === "PROCEED_TO_NEXT_STAGE") problems.push(`${tag}: a rule found something yet PROCEED`);
    }
  }
  console.log(`\nproblems: ${problems.length}`);
  problems.forEach((p) => console.log("  ! " + p));
  process.exit(problems.length ? 1 : 0);
})().catch((e) => { console.error("ERROR", e.message); process.exit(99); });
