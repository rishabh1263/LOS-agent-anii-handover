// The page's logic (public/core.js) -- node --test. The fixture is a REAL answer of POST /api/v1/credit/dedupe on the
// backend's demo providers (synthetic, masked); variants are derived from it, never invented from scratch.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const C = require("../public/core.js");

const REAL = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "dedupe_existing_complete.json"), "utf8"));
const clone = (x) => JSON.parse(JSON.stringify(x));

// the backend's GET /api/v1/fos/form-schema (shape and rules as returned on 2026-10-22)
const SCHEMA = { fields: [
  { name: "full_name", group: "applicant", label: "Applicant name", required: true, max_length: 200, choices: null, rules: { pattern: "^[A-Za-z\\u0900-\\u097F .'-]{2,}$" } },
  { name: "mobile", group: "applicant", label: "Mobile", required: true, max_length: 20, choices: null, rules: { pattern: "^[6-9]\\d{9}$", strip: "[\\s+-]|^91(?=\\d{10}$)" } },
  { name: "date_of_birth", group: "applicant", label: "Date of birth", required: true, max_length: 32, choices: null, rules: { date: true, min_age: 18, max_age: 80 } },
  { name: "address", group: "applicant", label: "Address", required: true, max_length: 500, choices: null, rules: {} },
  { name: "email", group: "applicant", label: "Email", required: false, max_length: 200, choices: null, rules: { pattern: "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$" } },
  { name: "product", group: "application", label: "Product", required: true, max_length: 64, choices: ["PERSONAL_LOAN", "HOME_LOAN", "LOAN_AGAINST_PROPERTY"], rules: {} },
  { name: "loan_amount", group: "application", label: "Loan amount (Rs)", required: true, max_length: null, choices: null, rules: { number: true } },
  { name: "employment_type", group: "application", label: "Employment type", required: false, max_length: 64, choices: ["SALARIED", "SELF_EMPLOYED"], rules: {} },
  { name: "tenure_months", group: "application", label: "Tenure (months)", required: false, max_length: null, choices: null, rules: { integer: true, min: 1, max: 480 } },
] };
const TODAY = new Date("2026-10-22T10:00:00");
const GOOD = { full_name: "Test Person", mobile: "+91 98765 43210", date_of_birth: "1990-05-01", address: "1 Synthetic Road",
  product: "PERSONAL_LOAN", loan_amount: "5,00,000", tenure_months: "36" };

function filled(customerType = "New") {
  let s = C.setCustomerType(C.initialState(), customerType);
  for (const [k, v] of Object.entries(GOOD)) s = C.setValue(s, k, v);
  return s;
}

// ---- New / Existing conditional behaviour -------------------------------------------------------------------------
test("New hides and CLEARS the dedupe fields, so a stale hidden value is never sent", () => {
  let s = C.setCustomerType(C.initialState(), "Existing");
  s = C.setDedupeParameter(s, "PAN");
  s = { ...s, dedupeValue: "ABCDE1234F" };
  s = C.setCustomerType(s, "New");
  assert.equal(s.dedupeParameter, "");
  assert.equal(s.dedupeValue, "");
  const body = C.dedupeBody("CASE-1", { ...filled("New"), dedupeParameter: s.dedupeParameter, dedupeValue: s.dedupeValue }, SCHEMA);
  assert.equal(body.dedupe_parameter, "Mobile No");                     // the applicant's own mobile, never the old PAN
  assert.equal(body.dedupe_parameter_value, "9876543210");
  assert.ok(!JSON.stringify(body).includes("ABCDE1234F"));
});

test("changing the parameter clears the value typed for the old one", () => {
  let s = C.setDedupeParameter(C.setCustomerType(C.initialState(), "Existing"), "PAN");
  s = { ...s, dedupeValue: "ABCDE1234F" };
  assert.equal(C.setDedupeParameter(s, "Mobile No").dedupeValue, "");
  assert.equal(C.setDedupeParameter(s, "PAN").dedupeValue, "ABCDE1234F");
});

test("Existing requires a parameter and a valid value before anything is sent", () => {
  const base = filled("Existing");
  assert.deepEqual(Object.keys(C.validate(base, SCHEMA, TODAY)), ["dedupeParameter"]);
  const noValue = C.setDedupeParameter(base, "AP Code");
  assert.match(C.validate(noValue, SCHEMA, TODAY).dedupeValue, /Enter the Customer ID/);
  assert.match(C.validate({ ...noValue, dedupeValue: "AP123" }, SCHEMA, TODAY).dedupeValue, /AP followed by 8 digits/);
  assert.deepEqual(C.validate({ ...noValue, dedupeValue: "ap 00731942" }, SCHEMA, TODAY), {});
});

test("no customer type chosen is an error", () => {
  assert.ok(C.validate(C.initialState(), SCHEMA, TODAY).customerType);
});

// ---- dedupe parameters -------------------------------------------------------------------------------------------
test("the dropdown: the API's four values, plain officer labels, no source text", () => {
  assert.deepEqual(C.DEDUPE_PARAMETERS.map((p) => p.value), ["PAN", "Mobile No", "AP Code", "PR Code"]);
  assert.deepEqual(C.DEDUPE_PARAMETERS.map((p) => p.label), ["PAN", "Mobile No", "Customer ID", "Loan account no."]);
  assert.ok(!C.DEDUPE_PARAMETERS.some((p) => /mifin|los records/i.test(`${p.label} ${p.hint}`)));
  assert.ok(!C.DEDUPE_PARAMETERS.some((p) => /aadhaar/i.test(p.value + p.label)));
});

test("values are normalised the way the backend normalises them", () => {
  assert.equal(C.parameter("PAN").normalise(" abcde 1234f "), "ABCDE1234F");
  assert.equal(C.parameter("Mobile No").normalise("+91 98765-43210"), "9876543210");
  assert.equal(C.parameter("Mobile No").normalise("09876543210"), "9876543210");
});

// ---- schema-driven validation ---------------------------------------------------------------------------------------
test("the backend's own rules: pattern, strip, age, integer range, choices", () => {
  const f = Object.fromEntries(SCHEMA.fields.map((x) => [x.name, x]));
  assert.equal(C.validateField(f.mobile, "+91 98765 43210"), null);
  assert.match(C.validateField(f.mobile, "12345"), /valid format/);
  assert.match(C.validateField(f.date_of_birth, "2015-01-01", TODAY), /at least 18/);
  assert.match(C.validateField(f.date_of_birth, "1930-01-01", TODAY), /at most 80/);
  assert.match(C.validateField(f.tenure_months, "500"), /at most 480/);
  assert.match(C.validateField(f.tenure_months, "12.5"), /whole number/);
  assert.match(C.validateField(f.product, "CAR_LOAN"), /listed options/);
  assert.match(C.validateField(f.loan_amount, "lots"), /must be a number/);
  assert.match(C.validateField(f.full_name, ""), /required/);
  assert.equal(C.validateField(f.email, ""), null);                     // optional
});

test("the create body carries only filled schema fields, numbers as numbers", () => {
  const body = C.createBody(filled(), SCHEMA);
  assert.deepEqual(body.application, { product: "PERSONAL_LOAN", loan_amount: 500000, tenure_months: 36 });
  assert.equal(body.applicant.mobile, "9876543210");
  assert.ok(!("email" in body.applicant));
});

test("an Existing dedupe body carries the officer's parameter, normalised, and the requested loan", () => {
  let s = C.setDedupeParameter(filled("Existing"), "PR Code");
  s = { ...s, dedupeValue: "pr02184467" };
  assert.deepEqual(C.dedupeBody("CASE-1", s, SCHEMA), { case_id: "CASE-1", customer_type: "Existing",
    requested_product: "PERSONAL_LOAN", requested_amount: 500000, dedupe_parameter: "PR Code",
    dedupe_parameter_value: "PR02184467" });
});

// ---- idempotency ------------------------------------------------------------------------------------------------
test("a retry keeps its key; an edited form gets a new one", () => {
  const body = C.createBody(filled(), SCHEMA);
  const first = C.caseSubmission(undefined, body);
  assert.equal(C.caseSubmission(first, JSON.parse(JSON.stringify(body))).key, first.key);
  const edited = C.caseSubmission(first, { ...body, application: { ...body.application, loan_amount: 600000 } });
  assert.notEqual(edited.key, first.key);
});

// ---- the API client --------------------------------------------------------------------------------------------
function fakeFetch(responses) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    const r = responses[Math.min(calls.length - 1, responses.length - 1)];
    if (r instanceof Error) throw r;
    return { ok: r.status >= 200 && r.status < 300, status: r.status, headers: new Map(Object.entries(r.headers || {})),
      text: async () => (r.body === undefined ? "" : typeof r.body === "string" ? r.body : JSON.stringify(r.body)) };
  };
  return { impl, calls };
}
const noSleep = async () => {};

test("a typed error with the backend's code, message and request id", async () => {
  const { impl } = fakeFetch([{ status: 403, body: { detail: { code: "CASE_ACCESS_DENIED", message: "Not yours.", request_id: "r1" } } }]);
  await assert.rejects(C.request("/x", { fetchImpl: impl }), (e) => e.status === 403 && e.code === "CASE_ACCESS_DENIED" && e.requestId === "r1");
});

test("GET and keyed POSTs are retried on 503; an unkeyed POST never is", async () => {
  const get = fakeFetch([{ status: 503 }, { status: 200, body: { ok: 1 } }]);
  assert.deepEqual((await C.request("/x", { retries: 2, fetchImpl: get.impl, sleep: noSleep })).data, { ok: 1 });
  assert.equal(get.calls.length, 2);
  const keyed = fakeFetch([{ status: 503 }, { status: 201, body: { case_id: "C" } }]);
  await C.request("/x", { method: "POST", body: {}, headers: { "Idempotency-Key": "k" }, retries: 1, fetchImpl: keyed.impl, sleep: noSleep });
  assert.equal(keyed.calls.length, 2);
  const plain = fakeFetch([{ status: 503 }, { status: 200, body: {} }]);
  await assert.rejects(C.request("/x", { method: "POST", body: {}, retries: 3, fetchImpl: plain.impl, sleep: noSleep }), (e) => e.status === 503);
  assert.equal(plain.calls.length, 1);
});

test("a body that is not JSON is MALFORMED_RESPONSE, a network failure NETWORK", async () => {
  const bad = fakeFetch([{ status: 200, body: "<html>oops" }]);
  await assert.rejects(C.request("/x", { fetchImpl: bad.impl }), (e) => e.code === "MALFORMED_RESPONSE");
  const down = fakeFetch([new TypeError("fetch failed")]);
  await assert.rejects(C.request("/x", { fetchImpl: down.impl }), (e) => e.code === "NETWORK" && e.retryable);
});

test("a slow backend times out; a cancelled request says so", async () => {
  const slow = (url, init) => new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(new Error("aborted"))));
  await assert.rejects(C.request("/x", { fetchImpl: slow, timeoutMs: 20 }), (e) => e.code === "TIMEOUT");
  const controller = new AbortController();
  const p = C.request("/x", { fetchImpl: slow, timeoutMs: 5000, signal: controller.signal });
  controller.abort();
  await assert.rejects(p, (e) => e.code === "CANCELLED");
});

test("the bearer token is sent only when held; 401 surfaces as status 401", async () => {
  const f = fakeFetch([{ status: 401, body: { detail: "Not authenticated" } }]);
  await assert.rejects(C.request("/x", { token: "t-1", fetchImpl: f.impl }), (e) => e.status === 401);
  assert.equal(f.calls[0].init.headers.Authorization, "Bearer t-1");
  const g = fakeFetch([{ status: 200, body: {} }]);
  await C.request("/x", { fetchImpl: g.impl });
  assert.ok(!("Authorization" in g.calls[0].init.headers));
});

// ---- the view of the real answer -------------------------------------------------------------------------------
test("the real answer renders: existing customer, three loans, verified exposure, review required", () => {
  const v = C.viewResult(REAL);
  assert.equal(v.outcome, "REVIEW_REQUIRED");
  assert.equal(v.outcomeView.title, "Review required");
  assert.equal(v.customer.label, "Existing customer");
  assert.equal(v.match.code, "CONFIRMED_MATCH");
  assert.equal(v.loans.length, 3);
  assert.equal(v.totals.exposure.label, "₹3,12,400");
  assert.equal(v.totals.emi.label, "₹11,200");
  assert.ok(v.isDemo);
  assert.ok(v.rules.some((r) => r.outcome === "NOT_EVALUATED" && r.reason === "THRESHOLD_UNCONFIGURED"));
});

test("READY_FOR_DECISION is never shown as an approval", () => {
  const r = clone(REAL); r.outcome = "READY_FOR_DECISION";
  const v = C.viewResult(r);
  assert.match(v.outcomeView.note, /not an approval/);
  assert.ok(!/approved/i.test(v.outcomeView.title));
});

for (const [status, label] of [["NO_PREVIOUS_LOANS", "No previous loans"], ["UNAVAILABLE", "Unavailable"],
  ["NOT_ATTRIBUTED", "Not attributed (identity not confirmed)"], ["PARTIAL", "2 of 3 loans confirmed by Leviosa"]]) {
  test(`history ${status} reads as "${label}", never as zero loans unless authoritative`, () => {
    const r = clone(REAL);
    r.history.history_status = status;
    if (status !== "PARTIAL") { r.history.loans = []; r.history.loan_count = 0; r.history.aggregates = null; }
    else r.history.loans.forEach((x, i) => { x.resolved = i !== 0; });   // the LMS confirmed 2 of 3: said exactly
    const v = C.viewResult(r, UI_TEXT);
    assert.equal(v.history.label, label);
    if (status === "UNAVAILABLE") assert.equal(v.totals, null);
  });
}

test("match states map to their labels", () => {
  for (const [code, text] of [["CONFIRMED_MATCH", "Confirmed match"], ["POTENTIAL_MATCH", "Potential match"],
    ["AMBIGUOUS", "Ambiguous match"], ["NO_MATCH", "No match"], ["SOURCE_ERROR", "Source error"]]) {
    const r = clone(REAL); r.history.customer.match = code;
    assert.equal(C.viewResult(r).match.label, text);
  }
});

test("one loan, a conflicting duplicate, a partial total and an unknown amount", () => {
  const r = clone(REAL);
  r.history.loans = [r.history.loans[0]];
  r.history.loans[0].conflicting_duplicate = true;
  r.history.loans[0].lms = null;
  r.history.aggregates.active_exposure = { status: "PARTIAL", value: "120000", loans_missing: ["PR******31"] };
  const v = C.viewResult(r);
  assert.equal(v.loans.length, 1);
  assert.equal(v.loans[0].conflict, true);
  assert.equal(v.loans[0].outstanding, null);                   // unknown, not zero
  assert.equal(v.totals.exposure.label, "₹1,20,000 · 1 loan not included");   // never a bare "partial"
  assert.deepEqual(v.totals.exposure.missing, ["PR******31"]);
});

test("loans sort by amount with unknowns last, both directions", () => {
  const rows = [{ sanctioned: "₹5,00,000" }, { sanctioned: null }, { sanctioned: "₹1,50,000" }];
  assert.deepEqual(C.sortLoans(rows, "sanctioned", "asc").map((x) => x.sanctioned), ["₹1,50,000", "₹5,00,000", null]);
  assert.deepEqual(C.sortLoans(rows, "sanctioned", "desc").map((x) => x.sanctioned), ["₹5,00,000", "₹1,50,000", null]);
});

test("a body that is not the contract is refused, never half-rendered", () => {
  for (const bad of [null, {}, { outcome: "X" }, { outcome: "X", history: {} }]) {
    assert.throws(() => C.viewResult(bad), (e) => e.code === "MALFORMED_RESPONSE");
  }
});

test("defence in depth: a PAN, mobile or PR code in free text never reaches the page; the AP number is shown", () => {
  const r = clone(REAL);
  r.history.customer.customer_ref = "AP00745218";
  r.explanation.text = "Customer AP00745218. PAN BCDPM5678N, mobile 9876502222, loan PR02407753.";
  const v = C.viewResult(r);
  const text = JSON.stringify(v);
  for (const s of ["BCDPM5678N", "9876502222", "PR02407753"]) assert.ok(!text.includes(s), s);
  assert.ok(v.explanation.text.includes("AP00745218"), "the AP number in full (owner 2026-10-23)");
});

test("the explanation's source and fallback are carried, never invented", () => {
  const r = clone(REAL);
  r.explanation = { text: "x", source: "DETERMINISTIC", fallback_reason: "LLM_ERROR:TimeoutError" };
  const v = C.viewResult(r);
  assert.equal(v.explanation.source, "DETERMINISTIC");
  assert.equal(v.explanation.fallback, "LLM_ERROR:TimeoutError");
});

// ---- Fill sample: random, realistic, every pool and range from public/sample_config.json
const SAMPLE_CFG = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "public", "sample_config.json"), "utf8"));
const FULL_SCHEMA = { fields: [...SCHEMA.fields,
  { name: "interest_rate_pct", label: "Interest rate (%)", required: false, rules: { number: true, min: 1, max: 60 } },
  { name: "declared_monthly_income", label: "Monthly income", required: false, rules: { number: true } },
  { name: "declared_monthly_obligations", label: "Monthly obligations", required: false, rules: { number: true } },
  { name: "property_value", label: "Property value", required: false, rules: { number: true } },
  // GET /form-schema since 2026-10-22: choices / required per product come from the eligibility terms
  { name: "property_type", group: "application", label: "Property type", required: false, choices: ["COMMERCIAL", "INDUSTRIAL", "RESIDENTIAL"],
    rules: { by_product: { HOME_LOAN: { choices: ["RESIDENTIAL"], required: false, default: "RESIDENTIAL" },
      LOAN_AGAINST_PROPERTY: { choices: ["COMMERCIAL", "INDUSTRIAL", "RESIDENTIAL"], required: true, default: null } } } },
] };

test("property type: hidden for a personal loan, required for LAP, optional with a default for HL; never sent where hidden", () => {
  const field = FULL_SCHEMA.fields.find((f) => f.name === "property_type");
  assert.equal(C.effectiveField(field, { product: "PERSONAL_LOAN" }).hidden, true);
  const lap = C.effectiveField(field, { product: "LOAN_AGAINST_PROPERTY" });
  assert.deepEqual([lap.hidden, lap.required, lap.choices.length], [false, true, 3]);
  const hl = C.effectiveField(field, { product: "HOME_LOAN" });
  assert.deepEqual([hl.required, hl.choices, hl.defaultValue], [false, ["RESIDENTIAL"], "RESIDENTIAL"]);
  let s = filled();
  s = C.setValue(s, "product", "LOAN_AGAINST_PROPERTY");
  assert.match(C.validate(s, FULL_SCHEMA, TODAY).property_type, /required/);
  s = C.setValue(s, "property_type", "VILLA");
  assert.ok(C.validate(s, FULL_SCHEMA, TODAY).property_type, "a type LAP does not offer is refused");
  s = C.setValue(s, "property_type", "COMMERCIAL");
  assert.equal(C.validate(s, FULL_SCHEMA, TODAY).property_type, undefined);
  assert.equal(C.createBody(s, FULL_SCHEMA).application.property_type, "COMMERCIAL");
  s = C.setValue(s, "product", "PERSONAL_LOAN");
  assert.equal(C.validate(s, FULL_SCHEMA, TODAY).property_type, undefined);
  assert.equal(C.createBody(s, FULL_SCHEMA).application.property_type, undefined, "hidden -> not sent");
});

test("Fill sample: LAP always carries a property type it offers; a personal loan never does", () => {
  let lap = 0;
  for (let i = 0; i < 400; i += 1) {
    const v = C.applySample(C.generateSample(SAMPLE_CFG, FULL_SCHEMA, seeded(20000 + i), TODAY), FULL_SCHEMA).values;
    if (v.product === "LOAN_AGAINST_PROPERTY") { lap += 1; assert.ok(["COMMERCIAL", "INDUSTRIAL", "RESIDENTIAL"].includes(v.property_type)); }
    if (v.product === "PERSONAL_LOAN") assert.equal(v.property_type, undefined);
    if (v.product === "HOME_LOAN" && v.property_type) assert.equal(v.property_type, "RESIDENTIAL");
  }
  assert.ok(lap > 50);
});
function seeded(seed) {                                                // mulberry32: a repeatable rng for the tests
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

test("Fill sample: 500 random draws all pass the schema rules, the dedupe rules and the configured ranges", () => {
  const seen = { names: new Set(), types: new Set(), products: new Set(), params: new Set() };
  for (let i = 0; i < 500; i += 1) {
    const s = C.generateSample(SAMPLE_CFG, FULL_SCHEMA, seeded(i + 1), TODAY);
    const state = C.applySample(s, FULL_SCHEMA);
    for (const f of FULL_SCHEMA.fields) {
      if (state.values[f.name] != null) assert.equal(C.validateField(f, state.values[f.name], TODAY), null, `${i} ${f.name}`);
    }
    assert.deepEqual(C.validateDedupe(state), {}, `draw ${i}`);
    const v = state.values;
    const p = SAMPLE_CFG.products[v.product];
    const amount = Number(v.loan_amount);
    assert.ok(amount >= p.amount[0] && amount <= p.amount[1], `amount ${amount} in ${v.product} range`);
    const tenure = Number(v.tenure_months);
    assert.ok(tenure >= p.tenure_months[0] && tenure <= p.tenure_months[1]);
    const rate = Number(v.interest_rate_pct);
    assert.ok(rate >= p.interest_rate_pct[0] && rate <= p.interest_rate_pct[1]);
    const r = rate / 1200;
    const emi = (amount * r) / (1 - (1 + r) ** -tenure);
    assert.ok((emi + Number(v.declared_monthly_obligations)) / Number(v.declared_monthly_income) <= SAMPLE_CFG.max_foir + 1e-9, "FOIR");
    assert.equal(v.property_value != null, Boolean(p.loan_to_value), "property value only for secured products");
    if (v.property_value) assert.ok(amount / Number(v.property_value) <= p.loan_to_value[1] + 1e-9, "LTV");
    if (state.dedupeParameter === "Mobile No") assert.equal(state.dedupeValue, v.mobile, "searched mobile is the applicant's");
    seen.names.add(v.full_name); seen.types.add(state.customerType); seen.products.add(v.product); seen.params.add(state.dedupeParameter);
  }
  assert.ok(seen.names.size > 300, "random, not a fixed record");
  assert.deepEqual([...seen.types].sort(), ["Existing", "New"]);
  assert.deepEqual([...seen.products].sort(), Object.keys(SAMPLE_CFG.product_weights).filter((k) => SAMPLE_CFG.product_weights[k] > 0).sort());
  assert.deepEqual([...seen.params].filter(Boolean).sort(), ["AP Code", "Mobile No", "PAN", "PR Code"]);
});

test("Fill sample: the schema wins over the config (fields, choices, ranges)", () => {
  const narrow = { fields: SCHEMA.fields.map((f) => (f.name === "product" ? { ...f, choices: ["PERSONAL_LOAN"] }
    : f.name === "tenure_months" ? { ...f, rules: { integer: true, min: 1, max: 24 } } : f)) };
  for (let i = 0; i < 100; i += 1) {
    const state = C.applySample(C.generateSample(SAMPLE_CFG, narrow, seeded(1000 + i), TODAY), narrow);
    assert.equal(state.values.product, "PERSONAL_LOAN");
    assert.ok(Number(state.values.tenure_months) <= 24);
    assert.equal(state.values.property_value, undefined, "not in this schema -> not filled");
    assert.equal(state.values.interest_rate_pct, undefined);
  }
});

test("Fill sample: a New sample carries no dedupe parameter; a PAN looks like a person's PAN", () => {
  for (let i = 0; i < 300; i += 1) {
    const s = C.generateSample(SAMPLE_CFG, FULL_SCHEMA, seeded(5000 + i), TODAY);
    if (s.customerType === "New") assert.equal(C.applySample(s, FULL_SCHEMA).dedupeParameter, "");
    if (s.dedupeParameter === "PAN") assert.match(s.dedupeValue, /^[A-Z]{3}P[A-Z]\d{4}[A-Z]$/);
  }
});

test("Fill sample: a name never mixes genders (first and middle from one pool)", () => {
  const { male, female } = SAMPLE_CFG.names;
  const genderOf = (w) => (male.first.includes(w) || male.middle.includes(w) ? "m" : female.first.includes(w) || female.middle.includes(w) ? "f" : null);
  let withMiddle = 0;
  for (let i = 0; i < 400; i += 1) {
    const parts = C.generateSample(SAMPLE_CFG, FULL_SCHEMA, seeded(9000 + i), TODAY).values.full_name.split(" ");
    if (parts.length === 3) {
      withMiddle += 1;
      assert.equal(genderOf(parts[0]), genderOf(parts[1]), parts.join(" "));
    }
  }
  assert.ok(withMiddle > 20);
});

test("what matched: plain sentences from the answer, no flags, scores or Y/N", () => {
  const lines = C.viewResult(REAL).matched.map((m) => m.text);
  assert.match(lines[0], /^This mobile number belongs to an existing SBFC customer in MIFIN \(Customer ID AP\*{6}18\)\.$/);
  assert.ok(lines.some((t) => /same one you searched/.test(t)));
  assert.ok(lines.some((t) => /No other case in the LOS has this mobile number/.test(t)));
  assert.ok(!lines.some((t) => /\bY\b|score|flag|strong|potential_flag/i.test(t)));
  const none = clone(REAL);
  none.history.customer = { match: "NO_MATCH", customer_ref: null, searched_identifier_matches: null, warnings: [] };
  none.history.loan_count = 0;
  none.searched_parameter = "PR_CODE";
  none.los_dedupe.search = { status: "INSUFFICIENT_DATA", matches: [] };
  const t = C.viewResult(none).matched.map((m) => m.text);
  assert.deepEqual(t, ["No customer in MIFIN has this loan account number.",
    "The LOS does not store the loan account number, so only MIFIN was checked for it."]);
});

test("next steps: limits the business has not set are counted once, not listed as officer actions", () => {
  const v = C.viewResult(REAL);
  const waiting = REAL.evaluation.rules.filter((r) => r.outcome === "NOT_EVALUATED" && r.reason === "THRESHOLD_UNCONFIGURED").length;
  assert.equal(v.actions.limitsNotSet, waiting);
  assert.ok(!v.actions.officer.some((a) => /once the business owner sets it/.test(a)));
  assert.equal(v.actions.officer.length + waiting, new Set(REAL.next_actions).size);
});

const UI_TEXT = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "public", "ui_text.json"), "utf8"));

test("policy checks: a title and what it checks for every rule; traffic-light tone; the rule id never shown", () => {
  const v = C.viewResult(REAL, UI_TEXT);
  for (const r of v.rules) {
    assert.ok(UI_TEXT.rules[r.id], `ui_text.json has words for ${r.id}`);
    assert.ok(r.view.title && r.view.what && r.view.reason, r.id);
    assert.ok(!/H\d\d_/.test(`${r.view.title} ${r.view.what} ${r.view.reason}`), r.id);
    assert.equal(r.view.tone, { NOT_TRIGGERED: "pass", TRIGGERED: "fail", NOT_EVALUATED: "skip", EVALUATION_FAILED: "skip" }[r.outcome]);
  }
  const skipped = v.rules.find((r) => r.reason === "THRESHOLD_UNCONFIGURED");
  assert.match(skipped.view.reason, /business has not set this limit/);
});

test("what to do next: one plain message picked from the answer", () => {
  const pick = (mut) => { const r = clone(REAL); mut(r); return C.guidance(r, UI_TEXT); };
  assert.equal(pick(() => {}).key, "EXISTING_CLEAN");
  assert.match(pick(() => {}).lines[0], /Customer ID AP\*{6}18/);
  const failed = pick((r) => { r.evaluation.rules[5].outcome = "TRIGGERED"; });
  assert.equal(failed.key, "EXISTING_FAILED");
  assert.match(failed.lines[0], /Found: /);
  assert.equal(pick((r) => { r.customer_status = "POSSIBLE_EXISTING"; }).key, "POSSIBLE_EXISTING");
  assert.equal(pick((r) => { r.customer_status = "UNKNOWN"; }).key, "UNKNOWN");
  assert.equal(pick((r) => { r.customer_status = "NEW"; r.customer_type_check = { declared: "EXISTING" }; }).key, "DECLARED_EXISTING_NOT_FOUND");
  assert.equal(pick((r) => { r.customer_status = "NEW"; r.customer_type_check = null; }).key, "NEW");
  assert.equal(pick((r) => { r.customer_status = "NEW"; r.customer_type_check = null; r.los_dedupe.search.matches = [{ role: "CO_APPLICANT" }]; }).key, "NEW_OTHER_CASES");
  const marked = pick((r) => { r.customer_type_check = { declared: "NEW" }; });
  assert.equal(marked.lines.length, 2, "marked New but found: told to change the customer type");
});

test("co-applicant: the checked party, MIFIN's role and each other LOS case's role reach the view", () => {
  const r = clone(REAL);
  r.checked_party = "CO_APPLICANT";
  r.history.customer.role_in_source = "CO_APPLICANT";
  r.los_dedupe.search.matches = [{ case_id: "REDACTED", visible: false, role: "PRIMARY_APPLICANT", product: null },
    { case_id: "CASE-SYN1", visible: true, role: "CO_APPLICANT", product: "HOME_LOAN" }];
  const v = C.viewResult(r, UI_TEXT);
  assert.equal(v.checkedParty, "CO_APPLICANT");
  assert.equal(v.match.role, "CO_APPLICANT");
  assert.deepEqual(v.otherCases.map((c) => [c.caseId, c.role]), [[null, "PRIMARY_APPLICANT"], ["CASE-SYN1", "CO_APPLICANT"]]);
  assert.equal(UI_TEXT.roles.PRIMARY_APPLICANT, "Applicant");
  assert.equal(UI_TEXT.roles.CO_APPLICANT, "Co-applicant");
});

test("Fill sample on Check an existing case: the last case, a party, a valid identifier from config", () => {
  const params = new Set();
  for (let i = 0; i < 200; i += 1) {
    const s = C.lookupSample(SAMPLE_CFG, "CASE-LAST", seeded(40000 + i));
    assert.equal(s.caseId, "CASE-LAST");
    assert.ok(["Applicant", "Co-Applicant"].includes(s.party));
    assert.ok(C.parameter(s.parameter).pattern.test(s.value), `${s.parameter} ${s.value}`);
    params.add(s.parameter);
  }
  assert.equal(params.size, 4);
  assert.equal(C.lookupSample(SAMPLE_CFG, "", seeded(1)).caseId, "");
});

test("a possible match carries the confirmation; a recorded decision is said in plain words", () => {
  const r = clone(REAL);
  r.history.customer.match = "POTENTIAL_MATCH";
  r.history.customer.confirmation = { required: true, candidate: "dev1:abc", decision: null };
  assert.deepEqual(C.viewResult(r, UI_TEXT).confirmation, { required: true, candidate: "dev1:abc", decision: null });
  r.history.customer.match = "CONFIRMED_MATCH";
  r.history.customer.confirmation = { required: false, candidate: "dev1:abc", decision: "SAME_CUSTOMER", decided_by: "OFFICER" };
  assert.ok(C.viewResult(r, UI_TEXT).matched.some((m) => /An officer confirmed this is the same customer/.test(m.text)));
});

test("R01-R11: every rule shown in order; a rule missing from the answer is not checked; findings and gaps are honest", () => {
  const v = C.viewResult(REAL, UI_TEXT);
  assert.deepEqual(v.business.map((b) => b.id), ["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R09", "R10", "R11"]);
  for (const id of ["R08", "R09", "R10", "R11"]) {          // an older answer without H11-H14: "not checked", never clear
    const b = v.business.find((x) => x.id === id);
    assert.equal(b.tone, "skip", id);
    assert.match(b.reason, /not in this answer/);
  }
  const r05 = v.business.find((x) => x.id === "R05");
  assert.equal(r05.tone, "skip");                                     // the fixture's limits are unset: never clear
  const found = clone(REAL);
  found.los_dedupe.search.matches = [{ case_id: "REDACTED", visible: false, role: "PRIMARY_APPLICANT" }];
  found.los_dedupe.search.status = "POSSIBLE_MATCH";
  found.evaluation.rules.find((x) => x.rule_id === "H06_ADVERSE_HISTORY").outcome = "TRIGGERED";
  const w = C.viewResult(found, UI_TEXT).business;
  assert.equal(w.find((x) => x.id === "R01").tone, "fail");
  assert.match(w.find((x) => x.id === "R01").reason, /other LOS cases with this identifier/);
  assert.equal(w.find((x) => x.id === "R02").tone, "fail");
  const mismatch = clone(REAL);
  mismatch.history.customer.searched_identifier_matches = false;
  assert.equal(C.viewResult(mismatch, UI_TEXT).business.find((x) => x.id === "R07").tone, "fail");
});

test("the one dedupe status: words and tone from config; an incomplete check names what is missing", () => {
  const r = clone(REAL);
  r.dedupe = { status: "NO_DUPLICATE_FOUND", search_complete: true, recommended_action: "CONTINUE", match_reasons: [], searches_missing: [] };
  assert.deepEqual((({ label, tone, action }) => ({ label, tone, action }))(C.viewResult(r, UI_TEXT).dedupe),
    { label: "No duplicate found", tone: "ok", action: "Continue" });
  r.dedupe = { status: "CHECK_INCOMPLETE", search_complete: false, recommended_action: "MANUAL_REVIEW", match_reasons: [],
    searches_missing: ["MIFIN_UNAVAILABLE"] };
  const v = C.viewResult(r, UI_TEXT).dedupe;
  assert.equal(v.tone, "bad");
  assert.deepEqual(v.missing, ["Mifin unavailable"]);
  delete r.dedupe;
  assert.equal(C.viewResult(r, UI_TEXT).dedupe, null);
});

test("what to do next follows the one dedupe status: review evidence or an incomplete check is never 'go ahead'", () => {
  const r = clone(REAL);
  r.dedupe = { status: "POSSIBLE_DUPLICATE", match_reasons: ["MOBILE_MATCHES_EXISTING_CUSTOMER", "IDENTIFIER_ON_OTHER_LOS_CASES"], searches_missing: [] };
  const g = C.guidance(r, UI_TEXT);
  assert.equal(g.key, "EXISTING_OTHER_RECORDS");
  assert.match(g.lines[0], /identifier on other los cases/);
  r.dedupe = { status: "CHECK_INCOMPLETE", match_reasons: [], searches_missing: ["MIFIN_UNAVAILABLE"] };
  assert.equal(C.guidance(r, UI_TEXT).key, "CHECK_INCOMPLETE");
  r.dedupe = { status: "NO_DUPLICATE_FOUND", match_reasons: ["MOBILE_MATCHES_EXISTING_CUSTOMER", "EXISTING_LOANS_ON_RECORD"], searches_missing: [] };
  assert.equal(C.guidance(r, UI_TEXT).key, "EXISTING_CLEAN");      // an existing customer with loans is not a duplicate
});

test("Existing Loan Details: the Loan Existing columns, every value from a verified source, the rest 'not available'", () => {
  const cols = UI_TEXT.existing_loan_columns.map((c) => c.key);
  assert.deepEqual(cols, ["prospect_code", "ap_code", "applicant_type", "product_name", "loan_amount", "principal_outstanding",
    "emi", "disbursement_date", "tenor", "roi", "loan_status", "cancel_reason", "common_applicant", "pan_match",
    "match_found"]);
  assert.ok(UI_TEXT.loan_detail_fields.some((f) => f.key === "overdue"), "overdue moved to the PR details, not dropped");
  assert.ok(UI_TEXT.existing_loan_columns.every((c) => c.label && c.source));
  const r = clone(REAL);
  r.history.customer.role_in_source = "APPLICANT";
  const rows = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT);
  assert.equal(rows.length, REAL.history.loans.length);
  const active = rows.find((x) => x.loan_status === "Active");
  assert.equal(active.prospect_code, "PR******53");
  assert.equal(active.ap_code, "AP******18");
  assert.equal(active.applicant_type, "Applicant");
  assert.equal(active.disbursement_date, "15-03-2024");
  assert.equal(active.tenor, "60 M 0 D");
  assert.equal(active.roi, null, "ROI has no source: never invented");
  assert.equal(active.match_found, "✓ Same customer");
  assert.match(active.match_found_why, /searched mobile number matched/);
  for (const row of rows) for (const k of cols) assert.ok(k in row, k);
});

test("Match Found says WHAT matched: the searched loan account itself, or the same customer", () => {
  const r = clone(REAL);
  r.searched_parameter = "PR_CODE";
  r.history.loans[0].searched_loan_account = true;
  const rows = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT);
  assert.equal(rows.filter((x) => x.match_found === "✓ This loan account").length, 1);
  assert.ok(rows.every((x) => ["✓ This loan account", "✓ Same customer"].includes(x.match_found)));
  const other = rows.find((x) => x.match_found === "✓ Same customer");
  assert.match(other.match_found_why, /customer the searched loan account number matched/);
});

function dd(over = {}) {
  return { status: "NO_DUPLICATE_FOUND", search_complete: true, searches_missing: [], parties_incomplete: [],
    match_reasons: ["MOBILE_MATCHES_EXISTING_CUSTOMER", "EXISTING_LOANS_ON_RECORD"],
    parties: [{ role: "PRIMARY_APPLICANT", status: "NO_DUPLICATE_FOUND", existing_customer: true, identifier_used: "MOBILE",
      match_reasons: ["MOBILE_MATCHES_EXISTING_CUSTOMER"], mifin: { customer_ref: "AP******18" } }],
    assessment: { existing_customer_or_loan: "FOUND", duplicate_application: "NOT_FOUND", conflicts: [], credit_decision: "NOT_DECIDED_BY_DEDUPE" },
    workflow_recommendation: { recommendation: "PROCEED_TO_NEXT_STAGE", reason: "r", next_action: "n", policy_question: null }, ...over };
}

test("Dedupe Assessment Summary: every line from the answer; never a credit decision; co-applicant said honestly", () => {
  const r = clone(REAL);
  r.dedupe = dd();
  const s = C.assessmentSummary(r, UI_TEXT);
  const get = (k) => s.rows.find((x) => x.key === k);
  assert.equal(get("overall").value, "No duplicate found");
  assert.equal(get("existing").value, "Found");
  assert.match(get("applicant").value, /existing customer \(AP\*{6}18\)/);
  assert.match(get("co").value, /no co-applicant on this case/);
  assert.equal(get("recommendation").value, "Proceed to next stage");
  assert.equal(s.note, "", "the 'not a credit decision' line was removed by the owner (2026-10-23)");
  // an incomplete co-applicant check: incomplete, held, and the co-applicant row says so
  r.dedupe = dd({ status: "CHECK_INCOMPLETE", search_complete: false, parties_incomplete: ["CO_APPLICANT:NO_PAN_OR_MOBILE_ON_RECORD"],
    parties: [...dd().parties, { role: "CO_APPLICANT", status: "CHECK_INCOMPLETE", existing_customer: false, identifier_used: null,
      match_reasons: ["NO_PAN_OR_MOBILE_ON_RECORD"] }],
    assessment: { ...dd().assessment, duplicate_application: "UNKNOWN" },
    workflow_recommendation: { recommendation: "HOLD_FOR_REVIEW", reason: "r", next_action: "n" } });
  const t = C.assessmentSummary(r, UI_TEXT);
  assert.match(t.rows.find((x) => x.key === "co0").value, /^Check incomplete – no SBFC customer record · no PAN or mobile on record/);
  assert.equal(t.rows.find((x) => x.key === "complete").value, "Incomplete – Co-applicant: No PAN or mobile on record");
  assert.equal(t.rows.find((x) => x.key === "recommendation").value, "Hold for review");
  // no approved rule: unable to decide, and the open question is shown
  r.dedupe = dd({ status: "DUPLICATE_FOUND", workflow_recommendation: { recommendation: "UNABLE_TO_DECIDE", policy_question: "block or hold?" } });
  const u = C.assessmentSummary(r, UI_TEXT);
  assert.match(u.rows.find((x) => x.key === "recommendation").value, /Unable to decide/);
  assert.equal(u.rows.find((x) => x.key === "question").value, "block or hold?");
  delete r.dedupe;
  assert.equal(C.assessmentSummary(r, UI_TEXT), null);
});

test("a NEW customer: the agent did not run, the page says so and proceeds -- never 'no duplicate found'", () => {
  const r = clone(REAL);
  r.dedupe = { invoked: false, status: null, search_complete: true, searches_missing: [], parties_incomplete: [], parties: [],
    match_reasons: ["NEW_CUSTOMER_NO_RECORD"],
    trigger: { customer_lookup: "NEW", dedupe_invoked: false, reason: "New customer: MIFIN has no record." },
    assessment: { existing_customer_or_loan: "NOT_FOUND", duplicate_application: "NOT_EVALUATED", conflicts: [] },
    workflow_recommendation: { recommendation: "PROCEED_TO_NEXT_STAGE", next_action: "Continue with the normal LOS checks." } };
  const v = C.viewResult(r, UI_TEXT);
  assert.equal(v.dedupe.label, "New customer – dedupe not required");
  const rows = Object.fromEntries(v.summary.rows.map((x) => [x.key, x.value]));
  assert.equal(rows.lookup, "New customer: MIFIN has no record.");
  assert.match(rows.agent, /Not invoked/);
  assert.equal(rows.duplicate, "Not evaluated – new customer");
  assert.ok(!Object.values(rows).includes("No duplicate found"));
  assert.notEqual(C.guidance(r, UI_TEXT).key, "NEW_REVIEW");      // the new-customer reason is not review evidence
});

test("Existing Loan Details: constant columns go to the strip, all-empty columns are hidden but NAMED", () => {
  const cols = UI_TEXT.existing_loan_columns;
  const r = clone(REAL);
  r.history.loans.forEach((l) => { delete l.lms; l.lms_status = "UNAVAILABLE"; });   // e.g. canceled loans: no LMS data
  const rows = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT);
  const { shown, empty, constant } = C.loanColumns(cols, rows, UI_TEXT);
  const keys = shown.map((c) => c.key);
  assert.ok(!keys.includes("ap_code") && !keys.includes("applicant_type"));
  assert.deepEqual(constant, ["AP Code", "Applicant Type"]);
  assert.ok(keys.includes("prospect_code") && keys.includes("loan_status") && keys.includes("loan_amount"));
  assert.ok(empty.includes("ROI") && empty.includes("EMI"));
  assert.ok(!keys.includes("roi"));
  assert.equal(new Set([...keys, ...cols.filter((c) => empty.includes(c.label)).map((c) => c.key), "ap_code", "applicant_type"]).size,
    cols.length, "every column is either shown, in the strip, or named as empty");
});

test("a MIFIN scheme gets a friendly name only when the business maps it; otherwise shown as sent", () => {
  const r = clone(REAL);
  const raw = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT).map((x) => x.product_name);
  assert.deepEqual(raw, REAL.history.loans.map((l) => l.scheme));
  const text = { ...UI_TEXT, scheme_labels: { "ME NEW CUSTOMER": "Synthetic label" } };
  const rows = C.existingLoanRows(C.viewResult(r, text), text);
  const row = rows.find((x) => x.product_code === "ME NEW CUSTOMER");
  assert.equal(row.product_name, "Synthetic label");
});

const CO_SCHEMA = { ...FULL_SCHEMA, co_applicants: { max: 3, fields: [
  { name: "full_name", label: "Co-applicant name", required: true, rules: { pattern: "^[A-Za-z\u0900-\u097F .'-]{2,}$" } },
  { name: "date_of_birth", label: "Date of birth", rules: { date: true, min_age: 18, max_age: 80 } },
  { name: "pan", label: "PAN", rules: { pattern: "^[A-Z]{5}[0-9]{4}[A-Z]$", upper: true } },
  { name: "relationship", label: "Relationship to applicant", rules: {} }] } };

test("co-applicants: add up to the schema max, errors named as the backend names them, sent only when declared", () => {
  let s = filled();
  assert.equal(C.createBody(s, CO_SCHEMA).co_applicants, undefined, "no co-applicant: the body is unchanged");
  for (let i = 0; i < 5; i += 1) s = C.addCoApplicant(s, CO_SCHEMA);
  assert.equal(s.coApplicants.length, 3);
  s = C.removeCoApplicant(s, 2);
  s = C.setCoApplicantValue(s, 0, "full_name", "Sneha Joshi");
  s = C.setCoApplicantValue(s, 0, "pan", "abcpk4321l");
  s = C.setCoApplicantValue(s, 1, "pan", "NOT-A-PAN");
  const errors = C.validate(s, CO_SCHEMA, TODAY);
  assert.ok(!errors["co_applicants[0].pan"], "lower-case PAN is upper-cased, then valid");
  assert.ok(errors["co_applicants[1].full_name"] && errors["co_applicants[1].pan"]);
  s = C.setCoApplicantValue(s, 1, "full_name", "Rahul Joshi");
  s = C.setCoApplicantValue(s, 1, "pan", "");
  assert.deepEqual(C.createBody(s, CO_SCHEMA).co_applicants, [{ full_name: "Sneha Joshi", pan: "ABCPK4321L" }, { full_name: "Rahul Joshi" }]);
  assert.equal(C.addCoApplicant(filled(), { fields: [] }).coApplicants.length, 0, "a backend without the block: none");
});

test("Fill sample co-applicants: within the max, same family name, valid, gendered relationship and a sensible age", () => {
  let any = 0;
  for (let i = 0; i < 300; i += 1) {
    const sample = C.generateSample(SAMPLE_CFG, CO_SCHEMA, seeded(60000 + i), TODAY);
    const st = C.applySample(sample, CO_SCHEMA);
    assert.ok(st.coApplicants.length <= 3);
    const last = st.values.full_name.split(" ").pop();
    for (const co of st.coApplicants) {
      any += 1;
      assert.equal(co.full_name.split(" ").pop(), last);
      const g = SAMPLE_CFG.names.male.first.includes(co.full_name.split(" ")[0]) ? "male" : "female";
      assert.ok(SAMPLE_CFG.co_applicants.relationships[g].includes(co.relationship), `${co.full_name} ${co.relationship}`);
    }
    assert.deepEqual(C.validateCoApplicants(st, CO_SCHEMA, TODAY), {}, `draw ${i}`);
  }
  assert.ok(any > 100);
});

test("what to do next counts a co-applicant's evidence: a possible match on co-applicant 2 is never 'continue'", () => {
  const r = clone(REAL);
  r.customer_status = "NEW";
  r.customer_type_check = null;
  r.los_dedupe.search.matches = [];
  r.dedupe = { invoked: true, status: "POSSIBLE_DUPLICATE", match_reasons: [], searches_missing: [], parties: [
    { role: "PRIMARY_APPLICANT", status: "NO_DUPLICATE_FOUND", match_reasons: [] },
    { role: "CO_APPLICANT", status: "NO_DUPLICATE_FOUND", match_reasons: ["PAN_MATCHES_EXISTING_CUSTOMER"] },
    { role: "CO_APPLICANT", status: "POSSIBLE_DUPLICATE", match_reasons: ["POSSIBLE_MATCH_NOT_CONFIRMED"] }] };
  const g = C.guidance(r, UI_TEXT);
  assert.equal(g.key, "NEW_REVIEW");
  assert.match(g.lines.join(" "), /Co-applicant 2: possible match not confirmed/);
  assert.ok(!/Co-applicant 1/.test(g.lines.join(" ")), "an existing customer alone is not review evidence");
});

test("form fields like the LOS Sourcing screen: listed optional fields removed, never a required one, never sent", () => {
  const schema = C.formSchema(FULL_SCHEMA, UI_TEXT);
  const names = schema.fields.map((f) => f.name);
  for (const gone of ["email", "interest_rate_pct", "declared_monthly_income", "declared_monthly_obligations", "property_value"]) {
    assert.ok(!names.includes(gone), gone);
  }
  for (const kept of ["full_name", "mobile", "date_of_birth", "address", "product", "loan_amount", "employment_type", "tenure_months", "property_type"]) {
    assert.ok(names.includes(kept), kept);
  }
  // a required field stays even if someone lists it as hidden
  const forced = C.formSchema(FULL_SCHEMA, { form_hidden_fields: ["full_name", "email"] });
  assert.ok(forced.fields.some((f) => f.name === "full_name"));
  // a sample that fills a removed field never sends it
  const sample = C.generateSample(SAMPLE_CFG, FULL_SCHEMA, seeded(7), TODAY);
  const body = C.createBody(C.applySample(sample, schema), schema);
  assert.ok(!("email" in body.applicant) && !("declared_monthly_income" in body.application) && !("interest_rate_pct" in body.application));
});

test("LOS Sourcing fields: Loan Purpose / Applicant Type free text, the Aadhaar-linked checkbox sends true / false", () => {
  const schema = { ...FULL_SCHEMA, fields: [...FULL_SCHEMA.fields,
    { name: "loan_purpose", group: "application", label: "Loan Purpose", required: false, rules: {} },
    { name: "applicant_type", group: "application", label: "Applicant Type", required: false, rules: {} },
    { name: "mobile_aadhaar_linked", group: "applicant", label: "Is it Aadhaar Linked Mobile No", required: false, rules: { boolean: true } }] };
  let s = filled();
  s = C.setValue(s, "loan_purpose", "Business Expansion");
  s = C.setValue(s, "applicant_type", "Individual");
  s = C.setValue(s, "mobile_aadhaar_linked", "true");
  assert.deepEqual(C.validate(s, schema, TODAY), {}, "a checkbox is never invalid");
  const body = C.createBody(s, schema);
  assert.equal(body.application.loan_purpose, "Business Expansion");
  assert.equal(body.application.applicant_type, "Individual");
  assert.equal(body.applicant.mobile_aadhaar_linked, true);
  assert.equal(C.createBody(C.setValue(s, "mobile_aadhaar_linked", "false"), schema).applicant.mobile_aadhaar_linked, false);
  // Fill sample fills them only where the schema has them, from config
  const st = C.applySample(C.generateSample(SAMPLE_CFG, schema, seeded(3), TODAY), schema);
  assert.ok(SAMPLE_CFG.sourcing.loan_purposes.includes(st.values.loan_purpose));
  assert.ok(["true", "false"].includes(st.values.mobile_aadhaar_linked));
  assert.equal(C.applySample(C.generateSample(SAMPLE_CFG, FULL_SCHEMA, seeded(3), TODAY), FULL_SCHEMA).values.loan_purpose, undefined);
});

test("AP / PR codes in full when the backend sends them (owner 2026-10-23); the masked ref otherwise", () => {
  const r = clone(REAL);
  r.history.customer.customer_code = "AP00745218";
  r.history.loans.forEach((l, i) => { l.prospect_code = `PR0240775${i}`; });
  const v = C.viewResult(r, UI_TEXT);
  assert.equal(v.match.ref, "AP00745218");
  const rows = C.existingLoanRows(v, UI_TEXT);
  assert.ok(rows.every((x) => /^PR\d{8}$/.test(x.prospect_code) && x.ap_code === "AP00745218"));
  assert.ok(v.matched.some((m) => m.text.includes("AP00745218")));
  const masked = C.viewResult(clone(REAL), UI_TEXT);
  assert.ok(masked.match.ref.includes("*"), "no customer_code: the masked ref, never a guess");
});

test("a PR's details: every listed field, tenor as 'N M 0 D', values with no source are null (never invented)", () => {
  const keys = UI_TEXT.loan_detail_fields.map((f) => f.key);
  assert.deepEqual(keys, ["ap_code", "common_applicant", "pan_match", "name_match", "dob_match", "applicant_type",
    "product_name", "product_code", "loan_amount",
    "disbursement_date", "principal_outstanding", "emi", "overdue", "foreclosure_interest", "tenor", "roi", "loan_status",
    "mifin_status", "lms_record", "status_why", "data_problems", "topup", "emi_bounces", "cancel_reason"]);
  assert.ok(UI_TEXT.loan_detail_fields.every((f) => f.label && f.source));
  const rows = C.existingLoanRows(C.viewResult(clone(REAL), UI_TEXT), UI_TEXT);
  const d = C.loanDetail(rows.find((x) => x.loan_status === "Active"), UI_TEXT);
  const get = (k) => d.find((x) => x.key === k).value;
  assert.equal(get("tenor"), "60 M 0 D");
  assert.equal(get("roi"), null);
  assert.equal(get("foreclosure_interest"), null);
  assert.equal(get("disbursement_date"), "15-03-2024");
  assert.equal(C.tenor(120), "120 M 0 D");
  assert.equal(C.tenor(null), null);
  assert.equal(C.tenor("x"), null);
});

test("What matched, field by field: what was entered AND what MIFIN holds, with the result (sir, 2026-10-23)", () => {
  const r = clone(REAL);
  r.history.customer.searched_identifier_matches = true;
  r.history.customer.identity_check = { name: "MATCH", dob: "MISMATCH", pan: "MATCH" };
  r.history.customer.identity_values = {
    entered: { name: "Test Person", dob: "1990-01-01", pan: "******678N", mobile: "98******00" },
    mifin: { name: "TEST PERSON", dob: "1990-01-02", pan: "******678N", mobile: "98******00" } };
  const v = C.viewResult(r, UI_TEXT);
  const rows = C.matchTable(v, { value: "9876500000" }, UI_TEXT);
  const by = (f) => rows.find((x) => x.field === f);
  assert.equal(by("mobile number").entered, "98******00", "shown masked (ui_text.json display_mask)");
  assert.equal(by("mobile number").mifin, "98******00");
  assert.equal(by("mobile number").tone, "ok");
  assert.equal(by("Name").entered, "Test Person", "the case's own name when nothing was typed");
  assert.equal(by("Name").mifin, "TEST PERSON", "MIFIN's name as it holds it");
  assert.equal(by("Name").result, "Matches");
  assert.equal(by("Date of birth").entered, "01-01-1990");
  assert.equal(by("Date of birth").mifin, "02-01-1990");
  assert.equal(by("Date of birth").result, "Does not match");
  assert.equal(by("Date of birth").tone, "bad");
  assert.equal(by("PAN").mifin, "******678N");
  // no values in the answer (switched off / MIFIN named no one): the sides stay empty, never guessed
  r.history.customer.searched_identifier_matches = false;
  r.history.customer.identity_check = null;
  r.history.customer.identity_values = null;
  const other = C.matchTable(C.viewResult(r, UI_TEXT), { value: "9876500000" }, UI_TEXT);
  assert.equal(other[0].mifin, null, "a differing MIFIN value is shown only when the answer carries it");
  assert.equal(other[0].result, "Does not match");
  assert.equal(other.find((x) => x.field === "Name").result, "Not compared");
  assert.equal(other.find((x) => x.field === "Name").entered, null);
});

test("every previous case (2, 3, any number): common applicant, PAN match, a cancelled loan's reason when given", () => {
  for (const n of [2, 3]) {
    const r = clone(REAL);
    r.history.loans = r.history.loans.slice(0, 1);
    while (r.history.loans.length < n) r.history.loans.push(clone(r.history.loans[0]));
    r.history.loans.forEach((l, i) => {
      l.prospect_ref = `PR******${i}${i}`;
      l.effective_status = "CANCELED";
      l.applicant_check = { common_applicant: true, pan: "MATCH" };
      l.cancel_reason = i === 0 ? "Customer not interested" : null;
    });
    r.history.common_applicant = { cases: n, same_customer_on_all: true, pan: "MATCH", cancelled_cases: n, cancelled_with_reason: 1 };
    const v = C.viewResult(r, UI_TEXT);
    const rows = C.existingLoanRows(v, UI_TEXT);
    assert.equal(rows.length, n);
    assert.ok(rows.every((x) => x.common_applicant === "✓ Yes"), "checked on every case");
    assert.ok(rows.every((x) => x.pan_match === "✓ Matches" && x.pan_tone === "ok"));
    assert.equal(rows[0].cancel_reason, "Customer not interested");
    assert.equal(rows[1].cancel_reason, "Not given by source", "no reason from the source: said so, never invented");
    const d = C.loanDetail(rows[1], UI_TEXT);
    assert.equal(d.find((x) => x.key === "cancel_reason").value, "Not given by source");
    assert.equal(v.commonApplicant.cases, n);
  }
  const live = clone(REAL);
  live.history.loans.forEach((l) => { l.applicant_check = { common_applicant: true, pan: "MISMATCH" }; });
  const rows = C.existingLoanRows(C.viewResult(live, UI_TEXT), UI_TEXT);
  assert.ok(rows.every((x) => x.pan_match === "✗ Differs" && x.pan_tone === "bad"));
  const active = rows.find((x) => x.loan_status === "Active");
  assert.ok(!C.loanDetail(active, UI_TEXT).some((x) => x.key === "cancel_reason"), "reason row only on a cancelled loan");
});

test("a cancellation reason never shows a PAN or mobile, even if the source sent one", () => {
  const r = clone(REAL);
  r.history.loans[0].effective_status = "CANCELED";
  r.history.loans[0].cancel_reason = "dup ABCPK4321L call 9876501111";
  const rows = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT);
  assert.ok(!/ABCPK4321L|9876501111/.test(rows.map((x) => x.cancel_reason).join(" ")));
});

test("What matched: PAN checked even when the search was by mobile", () => {
  const r = clone(REAL);
  r.searched_parameter = "MOBILE";
  r.history.customer.identity_check = { pan: "MATCH", name: "NOT_COMPARED", dob: "NOT_COMPARED" };
  const rows = C.matchTable(C.viewResult(r, UI_TEXT), { value: "9876500000" }, UI_TEXT);
  const pan = rows.find((x) => x.field === "PAN");
  assert.equal(pan.result, "Matches");
  assert.equal(pan.tone, "ok");
});


test("Application Details (0012): Document Received Date never in the future; Co-origination Yes / No, unanswered not sent", () => {
  const schema = { ...FULL_SCHEMA, fields: [...FULL_SCHEMA.fields,
    { name: "document_received_date", group: "application", label: "Document Received Date", required: false, rules: { date: true, not_future: true } },
    { name: "co_origination", group: "application", label: "Co-origination", required: false, rules: { boolean: true, control: "yes_no" } }] };
  let s = filled();
  s = C.setValue(s, "document_received_date", "2026-06-20");
  s = C.setValue(s, "co_origination", "true");
  assert.deepEqual(C.validate(s, schema, TODAY), {});
  const body = C.createBody(s, schema);
  assert.equal(body.application.document_received_date, "2026-06-20");
  assert.equal(body.application.co_origination, true);
  assert.equal(C.createBody(C.setValue(s, "co_origination", "false"), schema).application.co_origination, false);
  assert.ok(!("co_origination" in C.createBody(C.setValue(s, "co_origination", ""), schema).application), "unanswered: not sent, never 'no'");
  const later = new Date(TODAY); later.setDate(later.getDate() + 1);
  const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  assert.match(C.validate(C.setValue(s, "document_received_date", ymd(later)), schema, TODAY).document_received_date, /future/);
  assert.equal(C.validate(C.setValue(s, "document_received_date", ymd(TODAY)), schema, TODAY).document_received_date, undefined);
  // Fill sample: a date within the configured window, a yes / no -- only where the schema has them
  const st = C.applySample(C.generateSample(SAMPLE_CFG, schema, seeded(5), TODAY), schema);
  const daysAgo = Math.round((new Date(ymd(TODAY)) - new Date(st.values.document_received_date)) / 86400000);
  const [lo, hi] = SAMPLE_CFG.sourcing.document_received_days_ago;
  assert.ok(daysAgo >= lo && daysAgo <= hi, `${daysAgo} days ago`);
  assert.ok(["true", "false"].includes(st.values.co_origination));
  assert.equal(C.applySample(C.generateSample(SAMPLE_CFG, FULL_SCHEMA, seeded(5), TODAY), FULL_SCHEMA).values.co_origination, undefined);
});

test("the form layout: no Loan Details section; product and property type in Application Details, LOS order", () => {
  const L = UI_TEXT.form_layout;
  assert.ok(!("loan" in L));
  assert.deepEqual(L.application_details, ["loan_amount", "product", "property_type", "@customer_type",
    "@dedupe_parameter", "@dedupe_value", "@tenure_years", "loan_purpose", "co_origination",
    "applicant_type", "employment_type", "mobile", "mobile_aadhaar_linked"]);
  // Document Received Date: off the form (owner 2026-10-23) -- hidden, so never shown, filled or sent
  assert.ok(UI_TEXT.form_hidden_fields.includes("document_received_date"));
  const schema = C.formSchema({ ...FULL_SCHEMA, fields: [...FULL_SCHEMA.fields,
    { name: "document_received_date", group: "application", required: false, rules: { date: true, not_future: true } }] }, UI_TEXT);
  assert.ok(!schema.fields.some((f) => f.name === "document_received_date"));
});

test("why this recommendation: 3 points at once; the model's points replace them only when they are sane", () => {
  const r = clone(REAL);
  r.explanation = { ...r.explanation, points: ["Hold for review.", "Found: a possible match.", "Next: check the record."],
    llm_reason_pending: true, fallback_reason: "LLM_DEFERRED", source: "DETERMINISTIC" };
  const v = C.viewResult(r, UI_TEXT);
  assert.equal(v.explanation.points.length, 3);
  assert.equal(v.explanation.pending, true);
  const llm = C.applyReason(v, { points: ["One.", "Two.", "Three."], source: "LLM" });
  assert.deepEqual(llm.explanation.points, ["One.", "Two.", "Three."]);
  assert.equal(llm.explanation.pointsSource, "LLM");
  assert.equal(llm.explanation.pending, false);
  for (const bad of [null, {}, { points: "x" }, { points: [] }, { points: ["1", "2", "3", "4"] }]) {
    const kept = C.applyReason(v, bad);
    assert.deepEqual(kept.explanation.points, v.explanation.points, JSON.stringify(bad));
    assert.equal(kept.explanation.pending, false);
  }
  const masked = C.applyReason(v, { points: ["PAN ABCPK4321L.", "b.", "c."], source: "LLM" });
  assert.ok(!masked.explanation.points.join(" ").includes("ABCPK4321L"), "never a PAN, even from the model");
});

test("a cancelled loan always shows its reason column; a live loan's is blank", () => {
  const r = clone(REAL);
  r.history.loans[0].effective_status = "CANCELED";
  r.history.loans[0].cancel_reason = null;
  const rows = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT);
  const { shown } = C.loanColumns(UI_TEXT.existing_loan_columns, rows, UI_TEXT);
  assert.ok(shown.some((c) => c.key === "cancel_reason"), "never hidden when a loan is cancelled");
  assert.ok(rows.filter((x) => !x.is_cancelled).every((x) => x.cancel_reason === null));
});

test("the applicant fully evaluated in a PR's details: role, common applicant, PAN, name, DOB", () => {
  const r = clone(REAL);
  r.history.customer.role_in_source = "APPLICANT";
  r.history.loans.forEach((l) => { l.applicant_check = { common_applicant: true, pan: "MATCH", name: "MISMATCH", dob: "NOT_COMPARED" }; });
  const row = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT)[0];
  const d = Object.fromEntries(C.loanDetail(row, UI_TEXT).map((x) => [x.key, x]));
  assert.equal(d.name_match.value, "✗ Differs");
  assert.equal(d.name_match.tone, "bad");
  assert.equal(d.dob_match.value, "Not compared");
  assert.equal(d.pan_match.tone, "ok");
  assert.equal(d.applicant_type.value, "Applicant");
  const customer = UI_TEXT.loan_detail_groups.find((g) => g.title === "Customer").keys;
  assert.deepEqual(customer, ["ap_code", "applicant_type", "common_applicant", "pan_match", "name_match", "dob_match"]);
});

test("the summary in simple words: plain questions, answers only from the facts", () => {
  const r = clone(REAL);
  r.history.common_applicant = { cases: r.history.loans.length, pan: "MATCH" };
  r.dedupe = { invoked: true, status: "POSSIBLE_DUPLICATE", assessment: { duplicate_application: "POSSIBLE" },
    workflow_recommendation: { recommendation: "HOLD_FOR_REVIEW", next_action: "Check the other application." } };
  const v = C.viewResult(r, UI_TEXT);
  const by = Object.fromEntries(v.simple.map((x) => [x.key, x]));
  assert.match(by.customer.a, /^Yes\. They are our customer/);
  assert.match(by.loans.a, new RegExp(`^Yes, ${r.history.loans.length}: `));
  assert.match(by.pan.a, /same PAN/);
  assert.equal(by.duplicate.tone, "warn");
  assert.equal(by.decision.a, "Wait. Someone must check it first.");
  assert.ok(!("next" in by), "no 'what do I do next' row (owner 2026-10-23): point 3 of the reason says it");
  assert.ok(v.simple.every((x) => x.q && x.a));
  const fresh = clone(REAL);
  fresh.history.customer.match = "NO_MATCH"; fresh.history.loans = []; fresh.history.history_status = "NOT_APPLICABLE";
  fresh.dedupe = { invoked: false, workflow_recommendation: { recommendation: "PROCEED_TO_NEXT_STAGE" } };
  const s2 = Object.fromEntries(C.viewResult(fresh, UI_TEXT).simple.map((x) => [x.key, x.a]));
  assert.equal(s2.customer, "No. This is a new customer.");
  assert.equal(s2.decision, "Go ahead. Nothing here stops this application.");
  assert.ok(!("pan" in s2), "no PAN line for a new customer");
});

test("a check waiting for a business limit says 'Limit not set', not a vague 'Not checked'", () => {
  const v = C.ruleView({ rule_id: "H07_EXISTING_LOAN_COUNT", outcome: "NOT_EVALUATED", reason: "THRESHOLD_UNCONFIGURED" }, UI_TEXT);
  assert.equal(v.status, "Limit not set");
  assert.match(v.reason, /business has not set this limit/);
  assert.equal(C.ruleView({ rule_id: "H06_ADVERSE_HISTORY", outcome: "NOT_EVALUATED", reason: "DATA_UNAVAILABLE" }, UI_TEXT).status, "Not checked");
});

test("Application Details: Loan Purpose and Applicant Type are drop-downs from config; Fill sample picks from them", () => {
  const fc = UI_TEXT.form_choices;
  assert.ok(fc.loan_purpose.length >= 5 && fc.applicant_type.includes("Individual"));
  for (let i = 0; i < 20; i += 1) {
    const st = C.generateSample(SAMPLE_CFG, { ...FULL_SCHEMA, fields: [...FULL_SCHEMA.fields,
      { name: "loan_purpose", group: "application", rules: {} }, { name: "applicant_type", group: "application", rules: {} }] }, seeded(i), TODAY);
    const v = st.values || st;
    if (v.loan_purpose) assert.ok(fc.loan_purpose.includes(v.loan_purpose), v.loan_purpose);
    if (v.applicant_type) assert.ok(fc.applicant_type.includes(v.applicant_type), v.applicant_type);
  }
});

test("a PAN or mobile is shown masked, never in full; Marriage is a loan purpose", () => {
  assert.equal(C.maskShown("PAN", "BCDPM5678N", UI_TEXT), "******678N");
  assert.equal(C.maskShown("MOBILE", "9876502222", UI_TEXT), "98******22");
  assert.equal(C.maskShown("PAN", "", UI_TEXT), null);
  const r = clone(REAL);
  r.searched_parameter = "PAN";
  r.history.customer.searched_identifier_matches = true;
  const rows = C.matchTable(C.viewResult(r, UI_TEXT), { value: "BCDPM5678N", pan: "BCDPM5678N" }, UI_TEXT);
  assert.ok(!JSON.stringify(rows).includes("BCDPM5678N"), "no full PAN in the table");
  assert.ok(UI_TEXT.form_choices.loan_purpose.includes("Marriage"));
  assert.ok(SAMPLE_CFG.sourcing.loan_purposes.every((x) => UI_TEXT.form_choices.loan_purpose.includes(x)), "sample values are drop-down values");
});


// ---- R01-R11, each rule: found / clear / partly checked / not available, from the answer only ----------------
function ruleWorld({ scenarios = {}, search = "NO_MATCH", matches = 0, history = {}, ident = true, check = null,
  match = "CONFIRMED_MATCH", caseStatus = "NO_MATCH" } = {}) {
  const r = clone(REAL);
  const base = Object.fromEntries(["S02_EXISTING_CUSTOMER", "S09_ROLE_CHANGE", "S10_SIMULTANEOUS_APPLICATION",
    "S11_IDENTITY_CONFLICT", "S11_POSSIBLE_MATCH", "S12_DOCUMENT_REUSE"].map((s) => [s, "NOT_DETECTED"]));
  r.los_dedupe.case.status = caseStatus;
  r.los_dedupe.case.scenarios = Object.entries({ ...base, ...scenarios }).map(([scenario, status]) => ({ scenario, status }));
  r.los_dedupe.search = { status: search, matches: Array.from({ length: matches }, () => ({ visible: false })) };
  const all = [...r.evaluation.rules, ...["H11_TOPUP_ELIGIBILITY", "H12_EMI_BOUNCES", "H13_FRAUD_HISTORY", "H14_COLLATERAL_REUSE"].map((rule_id) => ({ rule_id, outcome: "NOT_TRIGGERED" }))];
  r.evaluation.rules = all.map((x) => (history[x.rule_id]
    ? { ...x, ...history[x.rule_id] } : { ...x, outcome: "NOT_TRIGGERED", reason: null }));
  r.history.customer.searched_identifier_matches = ident;
  r.history.customer.identity_check = check || { searched_identifier: "MATCH", pan: "MATCH", name: "MATCH", dob: "MATCH" };
  r.history.customer.match = match;
  return Object.fromEntries(C.businessRules(r, UI_TEXT).map((b) => [b.id, b]));
}

test("rules: all clear when nothing is found and every check ran", () => {
  const R = ruleWorld();
  // every rule runs now (owner 2026-10-23): R08-R11 read H12 / H13 / H14 / H11
  for (const id of ["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R09", "R10", "R11"]) {
    assert.equal(R[id].tone, "pass", `${id}: ${R[id].reason}`);
  }
});

test("rules: each one says Found for its own evidence -- and only that rule", () => {
  const cases = [
    ["R01", { search: "MATCH", matches: 2 }],
    ["R01", { scenarios: { S02_EXISTING_CUSTOMER: "DETECTED" } }],
    ["R02", { history: { H06_ADVERSE_HISTORY: { outcome: "TRIGGERED", next_action: "Review the settled loan." } } }],
    ["R02", { history: { H10_DPD: { outcome: "TRIGGERED" } } }],
    ["R03", { scenarios: { S12_DOCUMENT_REUSE: "DETECTED" } }],
    ["R04", { scenarios: { S09_ROLE_CHANGE: "DETECTED" } }],
    ["R05", { history: { H07_EXISTING_LOAN_COUNT: { outcome: "TRIGGERED" } } }],
    ["R05", { history: { H08_TOTAL_EXPOSURE: { outcome: "TRIGGERED" } } }],
    ["R05", { history: { H09_EMI_BURDEN: { outcome: "TRIGGERED" } } }],
    ["R06", { scenarios: { S10_SIMULTANEOUS_APPLICATION: "DETECTED" } }],
    ["R07", { ident: false }],
    ["R07", { scenarios: { S11_IDENTITY_CONFLICT: "DETECTED" } }],
    ["R07", { check: { pan: "MISMATCH", name: "MATCH", dob: "MATCH" } }],
    ["R07", { check: { pan: "MATCH", name: "MISMATCH", dob: "MATCH" } }],
    ["R07", { check: { pan: "MATCH", name: "MATCH", dob: "MISMATCH" } }],
    ["R08", { history: { H12_EMI_BOUNCES: { outcome: "TRIGGERED" } } }],
    ["R09", { history: { H13_FRAUD_HISTORY: { outcome: "TRIGGERED" } } }],
    ["R10", { history: { H14_COLLATERAL_REUSE: { outcome: "TRIGGERED" } } }],
    ["R11", { history: { H11_TOPUP_ELIGIBILITY: { outcome: "TRIGGERED" } } }],
  ];
  for (const [id, world] of cases) {
    const R = ruleWorld(world);
    assert.equal(R[id].tone, "fail", `${id} ${JSON.stringify(world)}: ${R[id].reason}`);
    assert.equal(R[id].status, "Found");
    assert.match(R[id].reason, /^Found: /);
    const others = Object.values(R).filter((b) => b.id !== id && b.tone === "fail").map((b) => b.id);
    assert.deepEqual(others, [], `${JSON.stringify(world)} also flagged ${others}`);
  }
  assert.deepEqual(ruleWorld({ history: { H06_ADVERSE_HISTORY: { outcome: "TRIGGERED", next_action: "Review the settled loan." } } }).R02.next,
    ["Review the settled loan."], "a found check carries its next step");
});

test("rules: a limit the business has not set, or missing data, is never shown as clear", () => {
  const limits = Object.fromEntries(["H07_EXISTING_LOAN_COUNT", "H08_TOTAL_EXPOSURE", "H09_EMI_BURDEN", "H10_DPD"]
    .map((k) => [k, { outcome: "NOT_EVALUATED", reason: "THRESHOLD_UNCONFIGURED" }]));
  const R = ruleWorld({ history: limits });
  assert.equal(R.R05.tone, "skip");                                        // all three limits unset
  assert.match(R.R05.reason, /limit not set by the business/);
  assert.equal(R.R02.tone, "partial");                                     // adverse clear, DPD limit unset
  assert.match(R.R02.reason, /^Clear so far\. Not checked: repayment delays/);
  const noData = ruleWorld({ history: { H06_ADVERSE_HISTORY: { outcome: "NOT_EVALUATED", reason: "DATA_UNAVAILABLE" } } });
  assert.match(noData.R02.reason, /data not available/);
  const noId = ruleWorld({ caseStatus: "INSUFFICIENT_DATA" });             // the case has no PAN / mobile on record
  for (const id of ["R04", "R06"]) assert.equal(noId[id].tone, "skip", id);
  assert.match(noId.R04.reason, /no PAN or mobile on record/);
  const losDown = ruleWorld({ search: "FAILED" });
  assert.notEqual(losDown.R01.tone, "pass");
});

test("rules: R07 compares the applicant only against a MIFIN record they matched", () => {
  // no SBFC customer: nothing can disagree -> the comparisons do not apply -> "Nothing found", never "not checked"
  const none = ruleWorld({ match: "NO_MATCH", ident: null, check: {} });
  assert.equal(none.R07.tone, "pass");
  assert.equal(none.R07.reason, "Nothing found.");
  // MIFIN could not answer: genuinely not checked, in ONE short line grouped by why
  const down = ruleWorld({ match: "UNAVAILABLE", ident: null, check: {} });
  assert.equal(down.R07.tone, "partial");
  assert.equal(down.R07.reason, "Clear so far. Not checked: searched PAN / mobile, PAN, name, date of birth (MIFIN could not answer).");
  // a search by customer ID / loan account: the identifier comparison does not apply
  const byAp = clone(REAL);
  byAp.searched_parameter = "AP_CODE";
  byAp.history.customer.searched_identifier_matches = null;
  byAp.history.customer.identity_check = { pan: "MATCH", name: "MATCH", dob: "MATCH" };
  byAp.los_dedupe.case.scenarios = [{ scenario: "S11_IDENTITY_CONFLICT", status: "NOT_DETECTED" }];
  byAp.los_dedupe.case.status = "NO_MATCH";
  assert.equal(C.businessRules(byAp, UI_TEXT).find((x) => x.id === "R07").tone, "pass");
  const notEntered = ruleWorld({ check: { pan: "MATCH", name: "NOT_COMPARED", dob: "NOT_COMPARED" } });
  assert.equal(notEntered.R07.tone, "partial");
  assert.match(notEntered.R07.reason, /not entered for this check/);
});

test("rules: R06 with a case sharing only the mobile is 'partly checked', never 'clear' and never 'found'", () => {
  const R = ruleWorld({ scenarios: { S11_POSSIBLE_MATCH: "DETECTED" } });
  assert.equal(R.R06.tone, "partial");
  assert.match(R.R06.reason, /shares only this mobile \(same customer not proven – needs a PAN\)/);
});

test("Fill sample never picks the demo's MIFIN-error case (it would show 'Loan history unavailable')", () => {
  const all = JSON.stringify(SAMPLE_CFG);
  assert.ok(!all.includes("EFGPS9753T"), "the error PAN is for the E2E negative test only");
  for (let i = 0; i < 200; i += 1) {
    const st = C.generateSample(SAMPLE_CFG, FULL_SCHEMA, seeded(i), TODAY);
    assert.notEqual(st.dedupeValue, "EFGPS9753T");
  }
});

test("the Status card says where a loan's status came from -- and why it is Disbursed or Unknown", () => {
  const r = clone(REAL);
  const base = r.history.loans[0];
  r.history.loans = [
    { ...clone(base), prospect_ref: "PR******01", effective_status: "ACTIVE", status_source: "LMS", lms_status: "FOUND", source_status: "DISBURSED" },
    { ...clone(base), prospect_ref: "PR******02", effective_status: "DISBURSED", status_source: "MIFIN", lms_status: "NOT_FOUND",
      source_status: "DISBURSED", lms: null, amount_problems: ["SANCTIONED_AMOUNT_NOT_A_NUMBER"] },
    { ...clone(base), prospect_ref: "PR******03", effective_status: "UNKNOWN", status_source: "MIFIN", lms_status: "UNAVAILABLE",
      source_status: "ON HOLD", lms: null },
  ];
  const rows = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT);
  const [live, disb, unk] = rows;
  assert.equal(live.status_why, "Confirmed by Leviosa.");
  assert.equal(disb.loan_status, "Disbursed");
  assert.match(disb.status_why, /^Disbursed per MIFIN\. Leviosa has no record of it/);
  assert.equal(disb.lms_record, "No record");
  assert.equal(disb.data_problems, "loan amount is not a number");
  assert.equal(unk.loan_status, "Unknown");
  assert.match(unk.status_why, /MIFIN's status "ON HOLD" is not mapped yet and Leviosa could not be reached/);
  const card = C.loanDetail(disb, UI_TEXT).map((d) => d.key);
  for (const k of ["loan_status", "status_why", "mifin_status", "lms_record", "data_problems"]) assert.ok(card.includes(k), k);
});

test("no bare 'Partial' anywhere: totals, history and sources say what is missing; the LMS is named", () => {
  const r = clone(REAL);
  r.history.history_status = "PARTIAL";
  r.history.loans.forEach((x, i) => { x.resolved = i !== 0; });
  r.history.aggregates.monthly_emi_burden = { status: "PARTIAL", value: null, loans_not_confirmed: ["PR******31", "PR******32"] };
  const v = C.viewResult(r, UI_TEXT);
  assert.equal(v.totals.emi.label, "Not calculated · 2 loans not confirmed");
  assert.deepEqual(C.sourceLine({ source: "LMS", status: "PARTIAL", provider: "LEVIOSA", loans_resolved: 2, loans_total: 3 }, UI_TEXT),
    { name: "Leviosa", state: "2 of 3 loans found", tone: "warn" });
  assert.equal(C.sourceLine({ source: "LMS", status: "UNAVAILABLE", provider: null }, UI_TEXT).name, "Leviosa");
  const shown = JSON.stringify([v.history.label, v.totals.emi.label, v.totals.exposure.label]);
  assert.ok(!/partial/i.test(shown), shown);
});

test("the officer reads 'Leviosa', never 'LMS' (owner 2026-10-23); the API's ids stay", () => {
  const visible = [];
  const walk = (o) => { if (typeof o === "string") visible.push(o); else if (o && typeof o === "object") Object.values(o).forEach(walk); };
  walk(UI_TEXT);
  assert.deepEqual(visible.filter((x) => /LMS/.test(x)), []);
  assert.equal(C.sourceLine({ source: "LMS", status: "AVAILABLE", provider: "LEVIOSA" }, UI_TEXT).name, "Leviosa");
  const r = clone(REAL);
  r.history.history_status = "MIFIN_ONLY";
  assert.equal(C.viewResult(r, UI_TEXT).history.label, "MIFIN only (Leviosa unavailable)");
});

test("the page never shows the word 'demo' (owner 2026-10-23)", () => {
  const visible = [];
  const walk = (o, key) => {
    if (typeof o === "string") { if (!/_note$/.test(key || "")) visible.push(o); } else if (o && typeof o === "object") {
      Object.entries(o).forEach(([k, v]) => walk(v, k));
    }
  };
  walk(UI_TEXT);
  assert.deepEqual(visible.filter((x) => /demo/i.test(x)), []);
  for (const provider of ["DEMO", "LEVIOSA", null]) {
    assert.equal(C.sourceLine({ source: "LMS", status: "AVAILABLE", provider }, UI_TEXT).name, "Leviosa", String(provider));
  }
});



test("a running loan's top-up result and EMI bounces in its details", () => {
  const r = clone(REAL);
  const live = r.history.loans.find((x) => x.effective_status === "ACTIVE");
  live.topup = { status: "NOT_ELIGIBLE", reasons: ["TOO_SOON_AFTER_DISBURSAL", "OUTSTANDING_ABOVE_LIMIT"] };
  live.lms = { ...(live.lms || {}), emi_bounces_12m: 1 };
  const row = C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT).find((x) => x.loan_status === "Active");
  assert.equal(row.topup, "✗ Not eligible (too soon after disbursal, too much still outstanding)");
  assert.equal(row.topup_tone, "bad");
  assert.equal(row.emi_bounces, "1");
  const d = Object.fromEntries(C.loanDetail(row, UI_TEXT).map((x) => [x.key, x]));
  assert.equal(d.topup.tone, "bad");
  live.topup = { status: "NOT_APPLICABLE", reasons: [] };
  assert.equal(C.existingLoanRows(C.viewResult(r, UI_TEXT), UI_TEXT).find((x) => x.loan_status === "Active").topup, null);
});

test("other LOS cases with the same identifier: one counted line, never a list of case IDs", () => {
  const r = clone(REAL);
  r.searched_parameter = "PAN";
  r.los_dedupe.search = { status: "MATCH", matches: [
    ...Array.from({ length: 5 }, () => ({ visible: true, case_id: "CASE-X", role: "CO_APPLICANT", product: "HOME_LOAN" })),
    ...Array.from({ length: 3 }, () => ({ visible: true, case_id: "CASE-Y", role: "CO_APPLICANT", product: "PERSONAL_LOAN" })),
    { visible: false, role: "PRIMARY_APPLICANT", product: "LOAN_AGAINST_PROPERTY" }] };
  const line = C.otherCasesLine(C.viewResult(r, UI_TEXT), UI_TEXT, null, null);
  assert.equal(line, "9 other LOS cases have this PAN · as co applicant 8, primary applicant 1 · Home loan 5, Personal loan 3, Loan against property 1");
  assert.ok(!line.includes("CASE-"));
});


test("What matched, searched by the customer ID: one row with the ID on both sides and the match (sir, 2026-10-23)", () => {
  const r = clone(REAL);
  r.searched_parameter = "AP_CODE";
  const v = C.viewResult(r, UI_TEXT);
  const rows = C.matchTable(v, { value: v.match.ref }, UI_TEXT);
  const ids = rows.filter((x) => /Customer ID/.test(x.field));
  assert.equal(ids.length, 1, "the customer ID once, not twice");
  assert.equal(ids[0].entered, v.match.ref);
  assert.equal(ids[0].mifin, v.match.ref);
  assert.equal(ids[0].result, v.match.label);
});


test("What matched: a loan-account search shows the account on both sides; no MIFIN customer says why", () => {
  const r = clone(REAL);
  r.searched_parameter = "PR_CODE";
  const v = C.viewResult(r, UI_TEXT);
  const pr = C.matchTable(v, { value: "PR02407753" }, UI_TEXT)[0];
  assert.deepEqual([pr.entered, pr.mifin, pr.result], ["PR02407753", "PR02407753", v.match.label]);
  for (const [code, said] of [["NO_MATCH", "Not in MIFIN"], ["AMBIGUOUS", "More than one customer in MIFIN"],
    ["SOURCE_ERROR", "MIFIN could not answer"]]) {
    const n = clone(REAL);
    n.searched_parameter = "PAN";
    n.history.customer.match = code;
    const row = C.matchTable(C.viewResult(n, UI_TEXT), { value: "ZZZPZ1111Z" }, UI_TEXT)[0];
    assert.equal(row.result, said, code);
    assert.equal(row.mifin, null);
  }
});



// ---- All parameters (sir, 2026-10-24) -------------------------------------------------------------------------
test("All parameters: each value optional, each in its own format; the request names the mode", () => {
  const ok = C.allValues({ pan: "bcdpm5678n", mobile: "+91 98765 02222", ap_code: "", pr_code: "PR02407753" });
  assert.deepEqual(ok.errors, {});
  assert.deepEqual(ok.body, { pan: "BCDPM5678N", mobile: "9876502222", pr_code: "PR02407753" });
  assert.ok(C.allValues({ ap_code: "AP1" }).errors.ap_code, "a malformed customer ID is refused");
  assert.equal(C.parameter("All parameters").all, true);
  assert.ok(C.PARAMETER_OPTIONS.some((p) => p.all), "offered in the drop-down");
});

test("All parameters view: a conflict is said, the combined rules name which parameter found them", () => {
  const a = clone(REAL); a.searched_parameter = "PAN";
  const b = clone(REAL); b.searched_parameter = "MOBILE";
  const data = {
    request_id: "dda_1", checked: ["PAN", "MOBILE"], missing: [{ parameter: "AP_CODE" }, { parameter: "PR_CODE" }],
    rows: [
      { parameter: "PAN", value_shown: "******678N", source: "ENTERED", match: "CONFIRMED_MATCH", customer_code: "AP00745218",
        recommendation: "PROCEED_TO_NEXT_STAGE", rule_findings: [], result: a },
      { parameter: "MOBILE", value_shown: "98******11", source: "CASE_RECORD", match: "CONFIRMED_MATCH", customer_code: "AP00731942",
        recommendation: "HOLD_FOR_REVIEW", rule_findings: ["NAME_DIFFERS_FROM_MIFIN_RECORD"], result: b }],
    consistency: { status: "CONFLICT", customers: ["AP00731942", "AP00745218"],
      by_parameter: [{ parameter: "PAN", customer: "AP00745218" }, { parameter: "MOBILE", customer: "AP00731942" }] },
    combined: { recommendation: "HOLD_FOR_REVIEW", reason_codes: ["PARAMETERS_POINT_TO_DIFFERENT_CUSTOMERS"] } };
  const v = C.allView(data, UI_TEXT);
  assert.equal(v.combined.label, "Hold for review");
  assert.equal(v.consistency.tone, "bad");
  assert.match(v.consistency.text, /different customers: AP00731942 and AP00745218/);
  assert.deepEqual(v.rows.map((r) => r.from), ["Entered", "Case record"]);
  assert.equal(v.rows[1].findings[0], "Name differs from MIFIN");
  assert.deepEqual(v.missing, ["Customer ID (AP code)", "Loan account (PR code)"]);
  assert.ok(v.rows.every((r) => r.view && r.view.requestId !== undefined), "each row opens its own detailed view");
  const ids = v.business.map((x) => x.id);
  assert.equal(new Set(ids).size, ids.length, "each rule once");
  for (const rule of v.business.filter((x) => x.tone === "fail")) assert.match(rule.reason, /Found via /);
  assert.ok(!JSON.stringify(v.rows.map(({ view, ...r }) => r)).match(/BCDPM5678N|9876501111/), "no full PAN / mobile");
});
