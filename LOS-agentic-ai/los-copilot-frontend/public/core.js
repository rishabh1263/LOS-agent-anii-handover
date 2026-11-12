// LOS Copilot -- the page's logic, with no DOM: form state, validation from the backend's own form schema, request
// bodies, the API client, and the view of a dedupe answer. Loaded by the page (window.Core) and by the Node tests.
//
// THE PAGE DECIDES NOTHING. Every status, outcome, amount and reason shown comes from the backend response; a value the
// response does not carry is shown as unavailable -- never zero, never guessed. Validation rules come from
// GET /api/v1/fos/form-schema; the dedupe value formats mirror the backend's (app/agents/dedupe/identity.py,
// app/config/mifin.yaml) and the backend validates again.
"use strict";

(function (root, factory) {
  const core = factory();
  if (typeof module === "object" && module.exports) module.exports = core;
  else root.Core = core;
})(typeof self !== "undefined" ? self : this, function () {
  // ---------------------------------------------------------------- dedupe parameters (the backend's dropdown labels)
  // `value` is what the API takes; `label` is what the officer reads. Where each one is checked (LOS records / MIFIN)
  // is shown on the result page's Sources panel, not in the dropdown.
  const DEDUPE_PARAMETERS = [
    { value: "PAN", label: "PAN", hint: "10 characters, e.g. ABCDE1234F",
      pattern: /^[A-Z]{5}[0-9]{4}[A-Z]$/, normalise: (v) => String(v || "").toUpperCase().replace(/[^A-Z0-9]/g, "") },
    { value: "Mobile No", label: "Mobile No", hint: "10-digit Indian mobile",
      pattern: /^[6-9]\d{9}$/, normalise: (v) => {
        let d = String(v || "").replace(/\D/g, "");
        if (d.length === 12 && d.startsWith("91")) d = d.slice(2);
        else if (d.length === 11 && d.startsWith("0")) d = d.slice(1);
        return d;
      } },
    { value: "AP Code", label: "Customer ID", hint: "AP followed by 8 digits, e.g. AP00012345", pattern: /^AP\d{8}$/,
      normalise: (v) => String(v || "").toUpperCase().replace(/\s+/g, "") },
    { value: "PR Code", label: "Loan account no.", hint: "PR followed by 8 digits, e.g. PR00012345", pattern: /^PR\d{8}$/,
      normalise: (v) => String(v || "").toUpperCase().replace(/\s+/g, "") },
  ];

  /** "All parameters" (sir, 2026-10-24): PAN, mobile, customer ID and loan account checked together
   * (POST /api/v1/credit/dedupe/all). Every value is optional; PAN / mobile left empty come from the case record. */
  const ALL_PARAMETERS = { value: "All parameters", label: "All parameters", all: true,
    hint: "Checks PAN, mobile, customer ID and loan account together. Empty PAN / mobile use the case record." };
  /** The parameter drop-down: each single parameter, then All parameters. */
  const PARAMETER_OPTIONS = [...DEDUPE_PARAMETERS, ALL_PARAMETERS];
  /** The optional values of an All-parameters check: request field -> the single parameter that validates it. */
  const ALL_FIELDS = { pan: "PAN", mobile: "Mobile No", ap_code: "AP Code", pr_code: "PR Code" };

  function parameter(value) {
    return DEDUPE_PARAMETERS.find((p) => p.value === value) || (value === ALL_PARAMETERS.value ? ALL_PARAMETERS : null);
  }

  /** {errors, body}: the All-parameters values normalised and checked (each optional, each in its own format). */
  function allValues(raw) {
    const errors = {};
    const body = {};
    for (const [field, single] of Object.entries(ALL_FIELDS)) {
      const p = parameter(single);
      const v = p.normalise((raw || {})[field] || "");
      if (!v) continue;
      if (!p.pattern.test(v)) errors[field] = `${p.label}: ${p.hint}.`;
      else body[field] = v;
    }
    return { errors, body };
  }

  // ---------------------------------------------------------------- form state
  function initialState() {
    return { customerType: "", dedupeParameter: "", dedupeValue: "", values: {}, touched: {}, coApplicants: [] };
  }

  /** The form's own schema: the backend's, minus the OPTIONAL fields ui_text.json form_hidden_fields removes. A
   * required field is never hidden (the case could not be created); a field with per-product rules (property type)
   * stays, shown only where its product needs it. A hidden field is then never filled, validated or sent. */
  function formSchema(schema, text) {
    const hidden = new Set((text && text.form_hidden_fields) || []);
    if (!schema || !hidden.size) return schema;
    return { ...schema, fields: (schema.fields || []).filter((f) => !hidden.has(f.name) || f.required || (f.rules && f.rules.by_product)) };
  }

  // ---------------------------------------------------------------- co-applicants (schema.co_applicants: max + fields)
  function coApplicantSchema(schema) {
    const block = (schema && schema.co_applicants) || {};
    return { max: Number(block.max) || 0, fields: (block.fields || []).map((f) => ({ ...f, label: f.label || humanise(f.name) })) };
  }

  function addCoApplicant(state, schema) {
    const { max } = coApplicantSchema(schema);
    if (!max || (state.coApplicants || []).length >= max) return state;
    return { ...state, coApplicants: [...(state.coApplicants || []), {}] };
  }

  function removeCoApplicant(state, index) {
    return { ...state, coApplicants: (state.coApplicants || []).filter((_, i) => i !== index) };
  }

  function setCoApplicantValue(state, index, name, value) {
    return { ...state, coApplicants: (state.coApplicants || []).map((c, i) => (i === index ? { ...c, [name]: value } : c)) };
  }

  /** errors keyed exactly as the backend names them: co_applicants[i].field */
  function validateCoApplicants(state, schema, today) {
    const errors = {};
    const { fields } = coApplicantSchema(schema);
    (state.coApplicants || []).forEach((co, i) => {
      for (const f of fields) {
        const raw = f.rules && f.rules.upper ? String(co[f.name] || "").toUpperCase() : co[f.name];
        const message = validateField(f, raw, today);
        if (message) errors[`co_applicants[${i}].${f.name}`] = message;
      }
    });
    return errors;
  }

  function coApplicantsBody(state, schema) {
    const { fields } = coApplicantSchema(schema);
    return (state.coApplicants || []).map((co) => {
      const out = {};
      for (const f of fields) {
        const raw = co[f.name];
        if (raw === undefined || raw === null || String(raw).trim() === "") continue;
        out[f.name] = f.rules && f.rules.upper ? String(raw).trim().toUpperCase() : String(raw).trim();
      }
      return out;
    });
  }

  /** Customer Type changes: New hides AND clears the dedupe fields, so a hidden stale value is never sent. */
  function setCustomerType(state, type) {
    const next = { ...state, customerType: type };
    if (type !== "Existing") {
      next.dedupeParameter = "";
      next.dedupeValue = "";
    }
    return next;
  }

  /** A new parameter clears the value typed for the old one (a PAN is never sent as a mobile). */
  function setDedupeParameter(state, value) {
    return { ...state, dedupeParameter: value, dedupeValue: value === state.dedupeParameter ? state.dedupeValue : "" };
  }

  function setValue(state, name, value) {
    return { ...state, values: { ...state.values, [name]: value }, touched: { ...state.touched, [name]: true } };
  }

  // ---------------------------------------------------------------- random sample (pools and ranges: sample_config.json)
  const pick = (rng, list) => list[Math.floor(rng() * list.length)];
  const between = (rng, [lo, hi], step = 1) => lo + step * Math.floor(rng() * (Math.floor((hi - lo) / step) + 1));
  const roundTo = (n, step) => Math.round(n / (step || 1)) * (step || 1);
  const digits = (rng, n) => Array.from({ length: n }, () => String(Math.floor(rng() * 10))).join("");
  const letters = (rng, n) => Array.from({ length: n }, () => String.fromCharCode(65 + Math.floor(rng() * 26))).join("");

  function weighted(rng, weights, allowed) {
    const entries = Object.entries(weights || {}).filter(([k, w]) => w > 0 && (!allowed || allowed.includes(k)));
    const total = entries.reduce((s, [, w]) => s + w, 0);
    let roll = rng() * total;
    for (const [k, w] of entries) { roll -= w; if (roll < 0) return k; }
    return entries.length ? entries[entries.length - 1][0] : null;
  }

  /** Clamp a configured [lo, hi] to the schema's own min / max for that field. */
  function withinRules(range, rules = {}) {
    return [Math.max(range[0], rules.min ?? -Infinity), Math.min(range[1], rules.max ?? Infinity)];
  }

  function monthlyEmi(amount, ratePct, months) {
    const r = ratePct / 1200;
    return r ? (amount * r) / (1 - (1 + r) ** -months) : amount / months;
  }

  function isoDate(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  /** A random, realistic, SYNTHETIC applicant + loan. Pure: (config, backend schema, rng, today) -> a sample for
   * applySample. Every number stays inside the schema's rules; affordability is kept under config.max_foir. */
  function generateSample(cfg, schema, rng = Math.random, today = new Date()) {
    const fields = Object.fromEntries(((schema && schema.fields) || []).map((f) => [f.name, f]));
    const rules = (name) => (fields[name] && fields[name].rules) || {};
    const choices = (name) => (fields[name] && Array.isArray(fields[name].choices) && fields[name].choices.length ? fields[name].choices : null);
    const v = {};

    const pool = cfg.names[weighted(rng, cfg.names.gender_weights)];  // first + middle from ONE gender pool
    const first = pick(rng, pool.first);
    const last = pick(rng, cfg.names.last);
    const middle = pool.middle && pool.middle.length && rng() < (cfg.names.middle_probability || 0) ? ` ${pick(rng, pool.middle)}` : "";
    v.full_name = `${first}${middle} ${last}`;
    v.mobile = pick(rng, cfg.mobile.first_digit) + digits(rng, 9);

    const dob = rules("date_of_birth");
    const [ageLo, ageHi] = [Math.max(cfg.age[0], dob.min_age ?? 0), Math.min(cfg.age[1], dob.max_age ?? 200)];
    const age = between(rng, [ageLo, ageHi]);
    const newest = new Date(today.getFullYear() - age, today.getMonth(), today.getDate());
    v.date_of_birth = isoDate(new Date(newest.getTime() - Math.floor(rng() * 364) * 86400000));

    const a = cfg.address;
    const place = pick(rng, a.cities);
    const unit = pick(rng, a.unit_formats).replace("{n}", between(rng, a.unit_number)).replace("{m}", between(rng, [1, 20]));
    v.address = `${unit}, ${pick(rng, a.buildings)}, ${pick(rng, place.localities)}, ${place.city}, ${place.state} ${between(rng, place.pincode)}`;
    if (rng() < (cfg.email.probability || 0)) {
      v.email = `${first}.${last}${rng() < 0.5 ? between(rng, [1, 99]) : ""}@${pick(rng, cfg.email.domains)}`.toLowerCase();
    }

    const employment = weighted(rng, cfg.employment_weights, choices("employment_type"));
    if (employment) v.employment_type = employment;
    const incomeRange = cfg.monthly_income[employment] || cfg.monthly_income.SALARIED;
    let income = roundTo(between(rng, incomeRange), cfg.monthly_income.round_to);
    const share = cfg.obligations_share_of_income[0] + rng() * (cfg.obligations_share_of_income[1] - cfg.obligations_share_of_income[0]);
    const obligations = roundTo(income * share, cfg.monthly_income.round_to);

    const product = weighted(rng, cfg.product_weights, choices("product"));
    const p = cfg.products[product] || {};
    v.product = product;
    const [tLo, tHi] = withinRules(p.tenure_months, rules("tenure_months"));
    const tenure = between(rng, [tLo, tHi], p.tenure_step || 1);
    const [rLo, rHi] = withinRules(p.interest_rate_pct, rules("interest_rate_pct"));
    const rate = Number(between(rng, [Math.round(rLo * 100), Math.round(rHi * 100)], Math.round((cfg.interest_rate_step || 0.05) * 100)) / 100);
    let amount = roundTo(between(rng, p.amount), p.round_to);
    // affordability: EMI + obligations within max_foir of income -- the amount shrinks, and if even the product's
    // smallest amount does not fit, the income is the applicant's that would make it fit
    const room = cfg.max_foir * income - obligations;
    const affordable = Math.floor((room > 0 ? room / monthlyEmi(1, rate, tenure) : 0) / p.round_to) * p.round_to;
    amount = Math.max(p.amount[0], Math.min(amount, affordable));
    const needed = (monthlyEmi(amount, rate, tenure) + obligations) / cfg.max_foir;
    if (needed > income) income = Math.ceil(needed / cfg.monthly_income.round_to) * cfg.monthly_income.round_to;
    v.loan_amount = String(amount);
    v.tenure_months = String(tenure);
    v.interest_rate_pct = String(rate);
    v.declared_monthly_income = String(income);
    v.declared_monthly_obligations = String(obligations);
    if (p.loan_to_value) {
      const ltv = p.loan_to_value[0] + rng() * (p.loan_to_value[1] - p.loan_to_value[0]);
      v.property_value = String(Math.ceil(amount / ltv / cfg.property_value_round_to) * cfg.property_value_round_to);
    }
    // property type: only where the schema says the product uses one, from that product's choices
    const typeSpec = ((rules("property_type").by_product) || {})[product];
    if (typeSpec && (typeSpec.required || rng() < (cfg.property_type_optional_probability || 0))) {
      v.property_type = weighted(rng, cfg.property_type_weights, typeSpec.choices) || pick(rng, typeSpec.choices);
    }

    const customerType = weighted(rng, cfg.customer_type_weights, ["New", "Existing"]);
    const sample = { customerType, dedupeParameter: "", dedupeValue: "", values: v };
    if (customerType === "Existing") {
      const ex = cfg.existing;
      const param = weighted(rng, ex.parameter_weights, DEDUPE_PARAMETERS.map((d) => d.value));
      const known = (ex.known_customers || []).filter((c) => c[param]);
      let value;
      if (known.length && rng() < (ex.use_known_customer_probability || 0)) value = pick(rng, known)[param];
      else if (param === "PAN") value = letters(rng, 3) + (ex.pan_holder_type || "P") + last[0].toUpperCase() + digits(rng, 4) + letters(rng, 1);
      else if (param === "Mobile No") value = v.mobile;
      else value = ex.random_formats[param].prefix + digits(rng, ex.random_formats[param].digits);
      if (param === "Mobile No") v.mobile = value;                    // the applicant's own number is the one searched
      Object.assign(sample, { dedupeParameter: param, dedupeValue: value });
    }
    // the LOS Sourcing screen's own fields (sample_config.json sourcing): only where the schema has them
    const src = cfg.sourcing || {};
    if (fields.loan_purpose && (src.loan_purposes || []).length) v.loan_purpose = pick(rng, src.loan_purposes);
    if (fields.applicant_type && (src.applicant_types || []).length) v.applicant_type = pick(rng, src.applicant_types);
    if (fields.mobile_aadhaar_linked) v.mobile_aadhaar_linked = rng() < (src.aadhaar_linked_probability ?? 0.5) ? "true" : "false";
    if (fields.document_received_date && Array.isArray(src.document_received_days_ago)) {
      const [lo, hi] = src.document_received_days_ago;
      const d = new Date(today || Date.now());
      d.setDate(d.getDate() - (lo + Math.floor(rng() * (hi - lo + 1))));
      v.document_received_date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    }
    if (fields.co_origination && src.co_origination_probability !== undefined) {
      v.co_origination = rng() < src.co_origination_probability ? "true" : "false";
    }
    // co-applicants (sample_config.json co_applicants): same last name, own first name / age / PAN, within schema max
    const coCfg = cfg.co_applicants || {};
    const coMax = coApplicantSchema(schema).max;
    const count = Math.min(coMax, Number(weighted(rng, coCfg.count_weights || { 0: 1 })) || 0);
    const limits = coCfg.age_limits || [18, 80];
    sample.coApplicants = [];
    for (let k = 0, tries = 0; sample.coApplicants.length < count && tries < count * 10; tries += 1, k += 1) {
      const gender = weighted(rng, cfg.names.gender_weights);
      const g = cfg.names[gender];
      const relationship = pick(rng, ((coCfg.relationships || {})[gender]) || ["Spouse"]);
      const offset = (coCfg.age_offset_by_relationship || {})[relationship] || [-5, 5];
      const coAge = age + between(rng, offset);                 // parents older, children younger than the applicant
      if (coAge < limits[0] || coAge > limits[1]) continue;     // not a realistic co-applicant for this applicant
      const born = new Date(today.getFullYear() - coAge, today.getMonth(), today.getDate());
      const co = { full_name: `${pick(rng, g.first)} ${last}`,
        date_of_birth: isoDate(new Date(born.getTime() - Math.floor(rng() * 364) * 86400000)), relationship };
      if (rng() < (coCfg.pan_probability ?? 1)) {
        co.pan = (coCfg.known_pans || []).length && rng() < (coCfg.known_pan_probability || 0) ? pick(rng, coCfg.known_pans)
          : letters(rng, 3) + "P" + last[0].toUpperCase() + digits(rng, 4) + letters(rng, 1);
      }
      sample.coApplicants.push(co);
    }
    const coCount = sample.coApplicants.length;
    sample.label = `${customerType} · ${humanise(product)} · ${place.city}${coCount ? ` · ${coCount} co-applicant(s)` : ""}`;
    return sample;
  }

  /** "Fill sample" on Check an existing case: the last case opened here (the check needs a real case) plus a random
   * party and identifier from sample_config.json (usually a demo MIFIN customer). Pure. */
  function lookupSample(cfg, lastCaseId, rng = Math.random) {
    const ex = cfg.existing || {};
    const lk = cfg.lookup || {};
    const param = weighted(rng, ex.parameter_weights, DEDUPE_PARAMETERS.map((d) => d.value)) || "PAN";
    const known = (ex.known_customers || []).filter((c) => c[param]);
    let value;
    if (known.length && rng() < (ex.use_known_customer_probability || 0)) value = pick(rng, known)[param];
    else if (param === "PAN") value = letters(rng, 3) + (ex.pan_holder_type || "P") + letters(rng, 1) + digits(rng, 4) + letters(rng, 1);
    else if (param === "Mobile No") value = pick(rng, (cfg.mobile || {}).first_digit || ["9"]) + digits(rng, 9);
    else value = ex.random_formats[param].prefix + digits(rng, ex.random_formats[param].digits);
    return { caseId: lastCaseId || "", party: weighted(rng, lk.party_weights || { Applicant: 1 }) || "Applicant",
      customerType: weighted(rng, lk.customer_type_weights || { Existing: 1 }) || "Existing", parameter: param, value };
  }

  /** "Fill sample": a fresh form from a synthetic sample. Only fields the schema has (and choices it offers) are
   * filled, so a sample can never put a field or value on the page that the backend does not know. */
  function applySample(sample, schema) {
    let next = setCustomerType(initialState(), sample.customerType || "");
    if (next.customerType === "Existing" && parameter(sample.dedupeParameter)) {
      next = { ...setDedupeParameter(next, sample.dedupeParameter), dedupeValue: sample.dedupeValue || "" };
    }
    for (const f of (schema && schema.fields) || []) {
      const value = (sample.values || {})[f.name];
      if (value == null || value === "") continue;
      if (Array.isArray(f.choices) && f.choices.length && !f.choices.includes(value)) continue;
      next = setValue(next, f.name, String(value));
    }
    const { max } = coApplicantSchema(schema);
    next = { ...next, coApplicants: (sample.coApplicants || []).slice(0, max).map((c) => ({ ...c })) };
    return next;
  }

  // ---------------------------------------------------------------- validation (rules from the backend's form schema)
  /** The schema's strip rule applied until the value stops changing: "+91 98765 43210" -> "9876543210". (The backend
   *  applies it once, which leaves "919876543210" for that input -- so the page sends the cleaned value.) */
  function applyStrip(value, pattern) {
    let out = value;
    for (let i = 0; i < 5; i += 1) {
      const next = out.replace(new RegExp(pattern, "g"), "");
      if (next === out) break;
      out = next;
    }
    return out;
  }

  function ageOn(dateText, today) {
    const d = new Date(String(dateText) + "T00:00:00");
    if (Number.isNaN(d.getTime())) return null;
    let age = today.getFullYear() - d.getFullYear();
    const m = today.getMonth() - d.getMonth();
    if (m < 0 || (m === 0 && today.getDate() < d.getDate())) age -= 1;
    return age;
  }

  /** The message for one schema field's value, or null. `today` is injectable for tests. */
  function validateField(field, raw, today = new Date()) {
    const rules = field.rules || {};
    let value = raw === undefined || raw === null ? "" : String(raw).trim();
    if (rules.boolean) return null;                           // a checkbox: ticked or not, never invalid
    if (rules.strip) value = applyStrip(value, rules.strip);
    if (!value) return field.required ? `${field.label} is required.` : null;
    if (field.max_length && value.length > field.max_length) return `${field.label}: at most ${field.max_length} characters.`;
    if (Array.isArray(field.choices) && field.choices.length && !field.choices.includes(value)) {
      return `${field.label}: choose one of the listed options.`;
    }
    if (rules.pattern && !new RegExp(rules.pattern, "u").test(value)) return `${field.label} is not in a valid format.`;
    if (rules.number || rules.integer) {
      const n = Number(value.replace(/,/g, ""));
      if (!Number.isFinite(n)) return `${field.label} must be a number.`;
      if (rules.integer && !Number.isInteger(n)) return `${field.label} must be a whole number.`;
      if (n <= 0 && rules.number && !("min" in rules)) return `${field.label} must be more than zero.`;
      if ("min" in rules && n < rules.min) return `${field.label}: at least ${rules.min}.`;
      if ("max" in rules && n > rules.max) return `${field.label}: at most ${rules.max}.`;
    }
    if (rules.date) {
      const age = ageOn(value, today);
      if (age === null) return `${field.label} is not a valid date.`;
      if (rules.not_future) return age < 0 ? `${field.label} cannot be in the future.` : null;
      if ("min_age" in rules && age < rules.min_age) return `The applicant must be at least ${rules.min_age}.`;
      if ("max_age" in rules && age > rules.max_age) return `The applicant must be at most ${rules.max_age}.`;
    }
    return null;
  }

  function validateDedupe(state) {
    if (!state.customerType) return { customerType: "Choose New or Existing." };
    if (state.customerType !== "Existing") return {};
    const errors = {};
    const p = parameter(state.dedupeParameter);
    if (!p) errors.dedupeParameter = "Choose what to search by.";
    else if (p.all) {
      const bad = allValues(state.allValues).errors;
      if (Object.keys(bad).length) errors.dedupeAll = Object.values(bad).join(" ");
    } else if (!state.dedupeValue.trim()) errors.dedupeValue = `Enter the ${p.label}.`;
    else if (!p.pattern.test(p.normalise(state.dedupeValue))) errors.dedupeValue = `${p.label}: ${p.hint}.`;
    return errors;
  }

  /** Every error in the form: { fieldName: message }. */
  /** A field as it applies to the chosen product: rules.by_product (e.g. property_type) gives its choices, whether it is
   * required and its default per product; a product it does not apply to hides it (and it is never sent). */
  function effectiveField(field, values) {
    const by = field.rules && field.rules.by_product;
    if (!by) return field;
    const spec = by[(values || {}).product];
    if (!spec) return { ...field, hidden: true, required: false };
    return { ...field, hidden: false, choices: spec.choices, required: Boolean(spec.required), defaultValue: spec.default || null };
  }

  function validate(state, schema, today) {
    const errors = { ...validateDedupe(state) };
    for (const raw of (schema && schema.fields) || []) {
      const field = effectiveField(raw, state.values);
      if (field.hidden) continue;
      const message = validateField(field, state.values[field.name], today);
      if (message) errors[field.name] = message;
    }
    return { ...errors, ...validateCoApplicants(state, schema, today) };
  }

  // ---------------------------------------------------------------- request bodies
  function _clean(field, raw) {
    const rules = field.rules || {};
    if (rules.boolean) return raw === true || String(raw).toLowerCase() === "true";
    let value = String(raw).trim();
    if (rules.strip) value = applyStrip(value, rules.strip);
    if (rules.number || rules.integer) return Number(value.replace(/,/g, ""));
    return value;
  }

  /** POST /api/v1/fos/applicants body -- only schema fields with a value; nothing hidden or stale. */
  function createBody(state, schema) {
    const body = { applicant: {}, application: {} };
    for (const field of (schema && schema.fields) || []) {
      const raw = state.values[field.name];
      if (raw === undefined || raw === null || String(raw).trim() === "") continue;
      if (effectiveField(field, state.values).hidden) continue;       // e.g. a property type for a personal loan
      const group = field.group === "application" ? "application" : "applicant";
      body[group][field.name] = _clean(field, raw);
    }
    const cos = coApplicantsBody(state, schema);
    if (cos.length) body.co_applicants = cos;           // only when declared: an older backend sees the same body
    return body;
  }

  /** POST /api/v1/credit/dedupe body. Existing: the officer's parameter / value. New: the applicant's own mobile is
   *  checked (never a hidden dedupe field) -- so a "new" customer the records already know is flagged. */
  function dedupeBody(caseId, state, schema) {
    const create = createBody(state, schema);
    const common = { case_id: caseId, customer_type: state.customerType };
    if (create.application.product) common.requested_product = create.application.product;
    if (create.application.loan_amount) common.requested_amount = create.application.loan_amount;
    if (state.customerType === "Existing") {
      const p = parameter(state.dedupeParameter);
      // ALL PARAMETERS: the form's own PAN / mobile (the case record has them too) and any codes typed
      if (p.all) return { ...common, mode: "ALL", ...allValues(state.allValues).body };
      return { ...common, dedupe_parameter: p.value, dedupe_parameter_value: p.normalise(state.dedupeValue) };
    }
    if (!create.applicant.mobile) return null;
    return { ...common, dedupe_parameter: "Mobile No", dedupe_parameter_value: String(create.applicant.mobile) };
  }

  // ---------------------------------------------------------------- idempotency (one key per logical submission)
  function canonical(value) {
    if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
    if (value && typeof value === "object") {
      return `{${Object.keys(value).filter((k) => value[k] !== undefined && value[k] !== null).sort()
        .map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
    }
    return JSON.stringify(value);
  }

  function newKey() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return "k-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
  }

  /** The same key while the body is unchanged (a retry); a new key for an edited form or a new case. */
  function caseSubmission(previous, body) {
    if (previous && canonical(previous.body) === canonical(body)) return previous;
    return { key: newKey(), body: JSON.parse(JSON.stringify(body)) };
  }

  // ---------------------------------------------------------------- the API client
  class ApiError extends Error {
    constructor(status, code, message, requestId, fields) {
      super(message || code || `HTTP ${status}`);
      this.status = status;
      this.code = code || (status === 0 ? "NETWORK" : `HTTP_${status}`);
      this.requestId = requestId || null;
      this.fields = fields || null;
    }
    get retryable() {
      return this.status === 0 || this.status === 502 || this.status === 503 || this.status === 504 ||
        this.code === "CREATE_BUSY" || this.code === "TIMEOUT";
    }
  }

  const FRIENDLY = {
    401: "Your session has ended. Sign in again.",
    403: "You are not allowed to do this for this case.",
    404: "Not found.",
    409: "This request conflicts with an earlier one.",
    422: "Some details are not valid.",
    429: "Too many requests. Wait a moment and try again.",
    502: "The LOS backend is not reachable.",
    503: "A backend service is unavailable. Try again shortly.",
    504: "The backend took too long to answer.",
  };

  function errorFrom(status, body) {
    const d = (body && (body.detail || body.error)) || {};
    if (Array.isArray(d)) return new ApiError(status, "VALIDATION_ERROR", FRIENDLY[422], null, null);
    const code = d.code || d.error || null;
    const message = d.message || FRIENDLY[status] || "Something went wrong.";
    return new ApiError(status, code, message, d.request_id, d.fields || null);
  }

  /**
   * fetch with a timeout, cancellation (signal), a bearer token when one is held, and retries only where safe:
   * GET, or a POST that carries an Idempotency-Key (the server returns the same case). Errors are ApiError.
   */
  async function request(path, { method = "GET", body, headers = {}, timeoutMs = 30000, retries = 0, signal,
    token, fetchImpl, sleep } = {}) {
    const doFetch = fetchImpl || fetch;
    const wait = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
    const safe = method === "GET" || Boolean(headers["Idempotency-Key"]);
    for (let attempt = 0; ; attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(new Error("TIMEOUT")), timeoutMs);
      const onAbort = () => controller.abort(signal.reason);
      if (signal) signal.addEventListener("abort", onAbort, { once: true });
      try {
        const res = await doFetch(path, {
          method, cache: "no-store", signal: controller.signal,
          headers: { Accept: "application/json", ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
            ...(token ? { Authorization: `Bearer ${token}` } : {}), ...headers },
          body: body !== undefined ? JSON.stringify(body) : undefined,
        });
        let data = null;
        const text = await res.text();
        if (text) {
          try { data = JSON.parse(text); } catch { throw new ApiError(res.status || 0, "MALFORMED_RESPONSE", "The backend's answer could not be read."); }
        }
        if (!res.ok) throw errorFrom(res.status, data);
        return { data, headers: res.headers, status: res.status };
      } catch (err) {
        let error = err;
        if (!(err instanceof ApiError)) {
          if (signal && signal.aborted) throw new ApiError(0, "CANCELLED", "Cancelled.");
          error = controller.signal.aborted ? new ApiError(0, "TIMEOUT", "The request timed out.")
            : new ApiError(0, "NETWORK", "The network request failed.");
        }
        if (safe && error.retryable && attempt < retries) {
          await wait(400 * (attempt + 1));
          continue;
        }
        throw error;
      } finally {
        clearTimeout(timer);
        if (signal) signal.removeEventListener("abort", onAbort);
      }
    }
  }

  // ---------------------------------------------------------------- the view of a dedupe answer
  const OUTCOMES = {
    READY_FOR_DECISION: { tone: "ok", title: "Ready for a credit decision",
      note: "The verified history is complete. This is not an approval: the credit decision is still to be made." },
    REVIEW_REQUIRED: { tone: "warn", title: "Review required",
      note: "Something needs an officer's review before a credit decision." },
    DATA_INSUFFICIENT: { tone: "bad", title: "Not enough verified information",
      note: "A source or a record is missing; the decision cannot be prepared yet." },
  };
  const CUSTOMER = {
    EXISTING: { tone: "brand", label: "Existing customer" },
    POSSIBLE_EXISTING: { tone: "warn", label: "Possible existing customer" },
    AMBIGUOUS: { tone: "warn", label: "Ambiguous identity" },
    NEW: { tone: "ok", label: "New customer" },
    UNKNOWN: { tone: "bad", label: "Unknown -- source unavailable" },
  };
  const MATCH = {
    CONFIRMED_MATCH: "Confirmed match", POTENTIAL_MATCH: "Potential match", AMBIGUOUS: "Ambiguous match",
    NO_MATCH: "No match", SOURCE_ERROR: "Source error", UNAVAILABLE: "Source unavailable", MALFORMED: "Unreadable answer",
  };
  const HISTORY = {
    COMPLETE: { tone: "ok", label: "Complete" },
    PARTIAL: { tone: "warn", label: "Partial" },
    MIFIN_ONLY: { tone: "warn", label: "MIFIN only (Leviosa unavailable)" },
    NO_PREVIOUS_LOANS: { tone: "ok", label: "No previous loans" },
    NOT_APPLICABLE: { tone: "na", label: "Not applicable (new customer)" },
    NOT_ATTRIBUTED: { tone: "warn", label: "Not attributed (identity not confirmed)" },
    UNKNOWN: { tone: "bad", label: "Unknown (loan section missing)" },
    UNAVAILABLE: { tone: "bad", label: "Unavailable" },
  };
  const LOAN_TONE = { ACTIVE: "brand", DISBURSED: "brand", CLOSED: "ok", SETTLED: "warn", WRITTEN_OFF: "bad", CANCELED: "na", UNKNOWN: "warn" };

  // AP numbers are shown in full (owner 2026-10-23); a PR code, PAN or mobile in free text is still hidden
  const UNMASKED = [/\bPR\d{8}\b/gi, /\b[A-Z]{5}\d{4}[A-Z]\b/g, /(?<!\d)[6-9]\d{9}(?!\d)/g];

  /** Defence in depth: the backend masks; if a full code / PAN / mobile ever reached the page it is not shown. */
  function safeText(value) {
    if (value === null || value === undefined) return "";
    let text = String(value);
    for (const re of UNMASKED) text = text.replace(re, "••••");
    return text;
  }

  /** A customer_code / prospect_code field, in full (owner 2026-10-23; the backend sends it only with
   * mifin.yaml privacy.show_full_codes on). Only a value that IS one AP / PR code passes; anything else -- or no code --
   * falls back to the masked ref through safeText, so free text still never shows a full code. */
  const CODE = /^(AP|PR)\d{8}$/i;
  function code(full, masked) {
    return full && CODE.test(String(full).trim()) ? String(full).trim() : safeText(masked);
  }

  function money(value) {
    if (value === null || value === undefined || value === "") return null;
    const n = Number(value);
    if (!Number.isFinite(n)) return null;
    return "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }

  /** "{value} · {n} loans not included" style words (ui_text.json completeness_words). */
  function fillWords(tpl, vals) {
    return String(tpl || "").replace(/\{(\w+)\}/g, (_, k) => (vals[k] === undefined ? "" : String(vals[k])));
  }

  function total(t, text) {
    if (!t) return { label: "Unavailable", tone: "na", value: null };
    const value = money(t.value);
    const W = (text && text.completeness_words) || {};
    if (t.status === "COMPLETE") return { label: value || "Unavailable", tone: "ok", value };
    if (t.status === "PARTIAL") {
      const missing = [...(t.loans_missing || []), ...(t.loans_status_unknown || []), ...(t.loans_conflicting || []),
        ...(t.loans_not_confirmed || [])];
      const n = missing.length;
      const label = value ? fillWords(W.total_partial || "{value} · {n} loan{s} not included", { value, n, s: n === 1 ? "" : "s" })
        : fillWords(W.total_partial_none || "Not calculated · {n} loan{s} not confirmed", { n, s: n === 1 ? "" : "s" });
      return { label, tone: "warn", value, missing };
    }
    if (t.status === "NOT_APPLICABLE") return { label: "No live loan", tone: "na", value: null };
    return { label: "Not calculable", tone: "warn", value: null, reason: t.reason || null };
  }

  /** The page's view of a POST /api/v1/credit/dedupe answer. Throws on a body that is not that contract. */
  // ---------------------------------------------------------------- "what matched", in plain sentences
  const SEARCHED = { PAN: "PAN", MOBILE: "mobile number", AP_CODE: "Customer ID", PR_CODE: "loan account number" };
  const IDENTITY_WORDS = { FNAME: "name", LNAME: "name", NAME: "name", DOB: "date of birth", MOBILE: "mobile number",
    PAN: "PAN", EMAIL: "email" };

  /** [{tone, text}] -- each line one plain fact from the answer; nothing inferred, nothing shown as a flag or score. */
  function whatMatched(r) {
    const c = (r.history && r.history.customer) || {};
    const what = SEARCHED[r.searched_parameter] || "identifier";
    const cid = code(c.customer_code, c.customer_ref);
    const ref = cid ? ` (Customer ID ${cid})` : "";
    const lines = [];
    const add = (tone, text) => lines.push({ tone, text });
    ({
      CONFIRMED_MATCH: () => add("brand", `This ${what} belongs to an existing SBFC customer in MIFIN${ref}.`),
      POTENTIAL_MATCH: () => add("warn", `This ${what} may belong to an existing customer in MIFIN${ref}. Confirm before linking.`),
      AMBIGUOUS: () => add("warn", `This ${what} matches more than one customer in MIFIN. It needs a manual check.`),
      NO_MATCH: () => add("ok", `No customer in MIFIN has this ${what}.`),
    }[c.match] || (() => add("bad", "MIFIN could not be checked, so the customer match is unknown.")))();
    const decided = (c.confirmation || {}).decision;
    if (decided === "SAME_CUSTOMER") add("ok", "An officer confirmed this is the same customer.");
    if (decided === "NOT_SAME_CUSTOMER") add("na", "An officer decided MIFIN's possible match is a different person.");
    if (c.searched_identifier_matches === true) add("ok", `The ${what} on the MIFIN record is the same one you searched.`);
    if (c.searched_identifier_matches === false) add("warn", `The ${what} on the MIFIN record is different from the one you searched.`);
    const held = [...new Set((c.identity_fields_present || []).map((f) => IDENTITY_WORDS[f]).filter(Boolean))];
    if (held.length && ["CONFIRMED_MATCH", "POTENTIAL_MATCH"].includes(c.match)) add("na", `MIFIN's record holds the customer's ${held.join(", ")}.`);
    if (r.history && r.history.loan_count) add("brand", `${r.history.loan_count} earlier loan(s) found for this customer.`);
    (c.warnings || []).forEach((w) => add("warn", humanise(w) + "."));
    const s = (r.los_dedupe || {}).search || {};
    if (s.status === "MATCH" || (Array.isArray(s.matches) && s.matches.length)) {
      // said once, with the counts, in the panel's match line (otherCasesLine)
    } else if (s.status === "NO_MATCH") {
      add("ok", `No other case in the LOS has this ${what}.`);
    } else if (s.status === "INSUFFICIENT_DATA") {
      add("na", `The LOS does not store the ${what}, so only MIFIN was checked for it.`);
    } else if (s.status === "FAILED") {
      add("bad", "The LOS records could not be searched.");
    }
    return lines;
  }

  function searchedLabel(code) {
    return SEARCHED[code] || humanise(code);
  }

  /** One policy check as the officer reads it: a short title, what it checks, a traffic-light status and a plain reason.
   * Words come from ui_text.json (keyed by rule id); the id is never shown. */
  function ruleView(rule, text) {
    const t = (text && text.rules && text.rules[rule.rule_id || rule.id]) || {};
    const limitNotSet = rule.outcome === "NOT_EVALUATED" && rule.reason === "THRESHOLD_UNCONFIGURED";
    const st = (text && text.status && text.status[limitNotSet ? "LIMIT_NOT_SET" : rule.outcome])
      || (text && text.status && text.status[rule.outcome]) || { label: humanise(rule.outcome), tone: "skip" };
    const reasons = (text && text.reasons) || {};
    let reason;
    if (rule.outcome === "NOT_TRIGGERED") reason = reasons.PASSED || "";
    else if (rule.outcome === "TRIGGERED") reason = rule.reason_code || rule.reasonCode ? `${reasons.FAILED || ""} ${humanise(rule.reason_code || rule.reasonCode)}.`.trim() : (reasons.FAILED || "");
    else if (rule.outcome === "EVALUATION_FAILED") reason = reasons.EVALUATION_FAILED || "";
    else reason = reasons[rule.reason] || reasons.DATA_UNAVAILABLE || "";
    return { title: t.title || rule.description || humanise(rule.rule_id || rule.id), what: t.what || "", status: st.label,
      tone: st.tone, reason };
  }

  /** The business's rules R01-R11 (ui_text.json business_rules), each built ONLY from what the answer carries.
   * A part is pass / fail / skip(+why); a rule is Found if any part found something, Clear if every part was clear,
   * Partly checked if some were clear and some could not run, Not checked if none ran, Not available if no source. */
  function businessRules(r, text) {
    const spec = (text && text.business_rules) || [];
    const words = (text && text.business_status) || {};
    const rules = Object.fromEntries((r.evaluation.rules || []).map((x) => [x.rule_id, x]));
    const losCase = (r.los_dedupe || {}).case || {};
    const caseData = losCase.status !== "INSUFFICIENT_DATA" && losCase.status !== "FAILED";
    const scenarios = Object.fromEntries((losCase.scenarios || []).map((s) => [s.scenario, s.status]));
    const search = (r.los_dedupe || {}).search || {};
    const ident = ((r.history || {}).customer || {}).searched_identifier_matches;
    const part = (p) => {
      if (p.kind === "history") {
        const x = rules[p.rule];
        if (!x) return { tone: "skip", why: "not in this answer" };
        if (x.outcome === "TRIGGERED") return { tone: "fail", next: x.next_action || null };
        if (x.outcome === "NOT_TRIGGERED") return { tone: "pass" };
        return { tone: "skip", why: x.reason === "THRESHOLD_UNCONFIGURED" ? "limit not set by the business" : "data not available" };
      }
      if (p.kind === "scenario") {
        if (p.needs_identity && !caseData) return { tone: "skip", why: "this case has no PAN or mobile on record yet" };
        const s = scenarios[p.scenario];
        return s === "DETECTED" ? { tone: "fail" } : s === "NOT_DETECTED" ? { tone: "pass" } : { tone: "skip", why: "data not available" };
      }
      if (p.kind === "los_search") {
        const n = Array.isArray(search.matches) ? search.matches.length : 0;
        if (n || ["MATCH", "POSSIBLE_MATCH"].includes(search.status)) return { tone: "fail", count: n };
        if (search.status === "NO_MATCH") return { tone: "pass" };
        return { tone: "skip", why: search.status === "INSUFFICIENT_DATA" ? "the LOS does not store this identifier" : "LOS search did not run" };
      }
      if (p.kind === "unproven") {           // a record sharing only a weak identifier: never "clear", never "found"
        if (p.needs_identity && !caseData) return { tone: "skip", why: "this case has no PAN or mobile on record yet" };
        const s = scenarios[p.scenario];
        return s === "DETECTED" ? { tone: "skip", why: p.why || "not proven" } : s === "NOT_DETECTED" ? { tone: "pass" }
          : { tone: "skip", why: "data not available" };
      }
      if (p.kind === "identity") {           // the applicant's PAN / name / DOB against the MIFIN record they matched
        const c = (r.history || {}).customer || {};
        if (c.match === "NO_MATCH") return { tone: "na" };            // no SBFC customer: nothing to disagree with
        if (c.match === "AMBIGUOUS") return { tone: "skip", why: "MIFIN matched more than one customer" };
        if (!["CONFIRMED_MATCH", "POTENTIAL_MATCH"].includes(c.match)) return { tone: "skip", why: "MIFIN could not answer" };
        const st = (c.identity_check || {})[p.field];
        return st === "MISMATCH" ? { tone: "fail" } : st === "MATCH" ? { tone: "pass" }
          : { tone: "skip", why: p.field === "pan" ? "no PAN on this case yet" : "not entered for this check" };
      }
      if (p.kind === "identifier") {
        if (ident === false) return { tone: "fail" };
        if (ident === true) return { tone: "pass" };
        const c = (r.history || {}).customer || {};
        // nothing to compare: no customer, or the search was by a customer ID / loan account (that IS the match)
        if (c.match === "NO_MATCH" || ["AP_CODE", "PR_CODE"].includes(r.searched_parameter)) return { tone: "na" };
        return { tone: "skip", why: ["UNAVAILABLE", "SOURCE_ERROR", "MALFORMED"].includes(c.match) ? "MIFIN could not answer"
          : c.match === "AMBIGUOUS" ? "MIFIN matched more than one customer" : "not compared" };
      }
      return { tone: "skip", why: "unknown check" };
    };
    return spec.map((b) => {
      const all = (b.parts || []).map((p) => ({ label: p.label, short: p.short || p.label, ...part(p) }));
      const parts = all.filter((p) => p.tone !== "na");          // a comparison that does not apply is left out
      let tone;
      if (!all.length) tone = "na";
      else if (!parts.length) tone = "pass";                       // nothing applied, so nothing was found
      else if (parts.some((p) => p.tone === "fail")) tone = "fail";
      else if (parts.every((p) => p.tone === "pass")) tone = "pass";
      else if (parts.some((p) => p.tone === "pass")) tone = "partial";
      else tone = "skip";
      const found = parts.filter((p) => p.tone === "fail").map((p) => p.label);
      // the not-checked parts in ONE short line, grouped by why: "Not checked (MIFIN could not answer): PAN, name"
      const byWhy = {};
      parts.filter((p) => p.tone === "skip").forEach((p) => { (byWhy[p.why] = byWhy[p.why] || []).push(p.short); });
      const skipped = Object.entries(byWhy).map(([why, labels]) => `${labels.join(", ")} (${why})`);
      const reason = tone === "na" ? (b.needs || "No source connected yet.")
        : tone === "fail" ? `Found: ${found.join("; ")}.`
          : tone === "pass" ? "Nothing found."
            : tone === "partial" ? `Clear so far. Not checked: ${skipped.join("; ")}.`
              : `Not checked: ${skipped.join("; ")}.`;
      return { id: b.id, name: b.name, what: b.what, tone, status: words[tone] || tone, reason,
        next: parts.filter((p) => p.tone === "fail" && p.next).map((p) => p.next) };
    });
  }

  /** Existing Loan Details rows: one per loan, keyed by ui_text.json existing_loan_columns. A value with no source is
   * null (shown "Not available"), never guessed. `v` is the result view (viewResult). */
  function existingLoanRows(v, text) {
    const roles = (text && text.roles) || {};
    const matched = v.match && v.match.code === "CONFIRMED_MATCH";
    const day = (d) => (d ? d.split("-").reverse().join("-") : null);
    return (v.loans || []).map((l) => ({
      prospect_code: l.ref, ap_code: v.match && v.match.ref !== "—" ? v.match.ref : null,
      applicant_type: v.match && v.match.role ? (roles[v.match.role] || humanise(v.match.role)) : null,
      product_name: l.scheme !== "—" ? (((text && text.scheme_labels) || {})[l.scheme] || l.scheme) : null,
      product_code: l.scheme !== "—" ? l.scheme : null,
      loan_amount: l.sanctioned, principal_outstanding: l.outstanding,
      emi: l.emi, overdue: l.overdue, disbursement_date: day(l.disbursedOn),
      tenor: tenor(l.tenure), roi: null, foreclosure_interest: null,
      // every case: the applicant common to it (one confirmed MIFIN customer) and the PAN against that record
      common_applicant: l.check && l.check.common_applicant && matched ? "✓ Yes" : null,   // same customer; AP code in the strip
      pan_match: l.check ? (((text && text.match_table) || {}).checks || {})[l.check.pan] || humanise(l.check.pan) : null,
      pan_tone: l.check ? ({ MATCH: "ok", MISMATCH: "bad" }[l.check.pan] || "na") : null,
      // a cancelled loan always says its reason -- or that the source gave none (never a silent blank)
      ...statusExplained(l, text),
      ...topupView(l, text),
      cancel_reason: l.cancelReason || (l.status === "CANCELED" ? ((text && text.cancel_reason_missing) || "Not given by source") : null),
      is_cancelled: l.status === "CANCELED",
      name_match: l.check ? (((text && text.match_table) || {}).checks || {})[l.check.name] || humanise(l.check.name) : null,
      name_tone: l.check ? ({ MATCH: "ok", MISMATCH: "bad" }[l.check.name] || "na") : null,
      dob_match: l.check ? (((text && text.match_table) || {}).checks || {})[l.check.dob] || humanise(l.check.dob) : null,
      dob_tone: l.check ? ({ MATCH: "ok", MISMATCH: "bad" }[l.check.dob] || "na") : null,
      loan_status: humanise(l.status), status_tone: l.tone,
      match_found: !matched ? null : l.searchedAccount ? "✓ This loan account" : "✓ Same customer",
      match_found_why: !matched ? null : l.searchedAccount ? "The searched loan account no. is this loan"
        : `This loan belongs to the customer the searched ${searchedLabel(v.searched)} matched`,
    }));
  }

  /** A running loan's top-up result in words (ui_text.json topup_words); null for a loan that is not running. */
  function topupView(l, text) {
    const W = (text && text.topup_words) || {};
    const t = l.topup;
    const bounces = l.bounces === null || l.bounces === undefined ? null : String(l.bounces);
    if (!t || t.status === "NOT_APPLICABLE") return { topup: null, topup_tone: null, emi_bounces: bounces };
    const why = (t.reasons || []).map((r) => ((W.reasons || {})[r] || humanise(r).toLowerCase()));
    const label = (W[t.status] || humanise(t.status)) + (why.length ? ` (${why.join(", ")})` : "");
    return { topup: label, topup_tone: { ELIGIBLE: "ok", NOT_ELIGIBLE: "bad" }[t.status] || "na", emi_bounces: bounces };
  }

  /** Where a loan's status came from and, for Unknown, why (ui_text.json status_explain). Facts from the answer only. */
  function statusExplained(l, text) {
    const t = (text && text.status_explain) || {};
    const mifin = l.sourceStatus && l.sourceStatus !== "—" ? l.sourceStatus : null;
    const lms = l.lms && l.lms !== "—" ? l.lms : null;
    const lmsSays = (t.lms_says || {})[lms] || humanise(lms || "UNAVAILABLE").toLowerCase();
    let why;
    if (l.status === "DISBURSED") why = String(t.why_disbursed || "").replace("{lms}", lmsSays);
    else if (l.status !== "UNKNOWN") why = l.statusSource === "LMS" ? t.why_lms : String(t.why_mifin || "").replace("{status}", mifin || "—");
    else why = String((mifin ? t.why_unknown : t.why_unknown_no_status) || "").replace("{status}", mifin || "").replace("{lms}", lmsSays);
    const problems = (l.problems || []).map((x) => (t.problems || {})[x] || humanise(x).toLowerCase());
    return { mifin_status: mifin, lms_record: lms ? ((t.lms_record || {})[lms] || humanise(lms)) : null,
      status_why: why || null, data_problems: problems.length ? problems.join("; ") : null };
  }

  /** Tenor as the LOS screen writes it: "120 M 0 D" (the LMS sends whole months). null when not sent. */
  function tenor(months) {
    return months === null || months === undefined || months === "" || !Number.isFinite(Number(months))
      ? null : `${Number(months)} M 0 D`;
  }

  /** One previous loan's details, opened by its ▸ (ui_text.json loan_detail_fields): [{label, value, source, num}].
   * A value the source does not send is null ("Not in source"), never guessed. `row` is an existingLoanRows row. */
  function loanDetail(row, text) {
    return ((text && text.loan_detail_fields) || []).filter((f) => !f.only_when || (f.only_when === "CANCELED" && row.is_cancelled))
      .map((f) => {
      const val = row[f.key];
      return { key: f.key, label: f.label, source: f.source, num: Boolean(f.num),
        value: val === null || val === undefined || val === "" ? null : String(val),
        // a cancelled loan was never disbursed: its LMS fields do not apply (not "missing")
        note: f.key === "data_problems" ? "None"                    // nothing unreadable is "none", not "missing"
          : row.is_cancelled && /^(LMS|Leviosa) /.test(f.source) ? "N/A – cancelled" : "Not in source",
        tone: f.key === "loan_status" ? row.status_tone : f.key === "topup" ? row.topup_tone
          : /_match$/.test(f.key) ? row[f.key.replace(/_match$/, "_tone")] : null };
    });
  }

  /** An identifier as the page SHOWS it (ui_text.json display_mask): "BCDPM5678N" -> "******678N". */
  function maskShown(kind, value, text) {
    const spec = ((text && text.display_mask) || {})[kind];
    const v = String(value || "");
    if (!spec || !v) return v || null;
    const start = Math.min(spec.keep_start || 0, v.length);
    const end = Math.min(spec.keep_end || 0, Math.max(v.length - start, 0));
    return v.slice(0, start) + String(spec.char || "*").repeat(Math.max(v.length - start - end, 0)) + (end ? v.slice(-end) : "");
  }

  /** "What matched", field by field: [{field, entered, mifin, result, tone}] -- BOTH SIDES (sir, 2026-10-23): what
   * was entered (typed on this page, else the case's own record as the backend compared it) and what MIFIN holds
   * (identity_values: name / DOB as written, PAN / mobile masked). A side with no value stays null (shown "—"). */
  function matchTable(v, entered, text) {
    const e = entered || {};
    const words = ((text && text.match_table) || {}).results || {};
    const say = (s) => words[s] || humanise(s);
    const tone = (s) => ({ MATCH: "ok", MISMATCH: "bad" }[s] || "na");
    const ic = v.identityCheck || {};
    const iv = v.identityValues || {};
    const ours = iv.entered || {};
    const theirs = iv.mifin || {};
    const day = (d) => (d && /^\d{4}-\d{2}-\d{2}$/.test(d) ? d.split("-").reverse().join("-") : d || null);
    const rows = [];
    if (v.searched) {
      const same = v.match.identifierMatches;
      const s = same === true ? "MATCH" : same === false ? "MISMATCH" : "NOT_COMPARED";
      const kind = String(v.searched).toLowerCase();
      const shown = ["PAN", "MOBILE"].includes(v.searched) ? maskShown(v.searched, e.value, text) : (e.value || null);
      const named = ["CONFIRMED_MATCH", "POTENTIAL_MATCH"].includes(v.match.code);
      const nobody = ((text && text.match_table) || {}).no_customer || {};
      if (named && ["AP_CODE", "PR_CODE"].includes(v.searched)) {
        // searched BY the customer ID / loan account: MIFIN found exactly that record -- it is the match itself
        rows.push({ field: searchedLabel(v.searched), entered: shown,
          mifin: v.searched === "AP_CODE" && v.match.ref !== "—" ? v.match.ref : shown, result: v.match.label,
          tone: v.match.code === "CONFIRMED_MATCH" ? "ok" : "warn" });
      } else if (!named && nobody[v.match.code]) {
        // MIFIN named no one: say why (not in MIFIN / more than one customer / MIFIN could not answer)
        rows.push({ field: searchedLabel(v.searched), entered: shown || ours[kind] || null, mifin: null,
          result: nobody[v.match.code], tone: v.match.code === "NO_MATCH" ? "na" : "warn" });
      } else {
        const theirsShown = theirs[kind] || (same === true ? shown : null);
        rows.push({ field: searchedLabel(v.searched), entered: shown || ours[kind] || null, mifin: theirsShown,
          result: say(s), tone: tone(s) });
      }
    }
    if (v.searched !== "PAN") {
      const s = ic.pan || "NOT_COMPARED";
      rows.push({ field: "PAN", entered: e.pan ? maskShown("PAN", e.pan, text) : (ours.pan || null),
        mifin: theirs.pan || null, result: say(s), tone: tone(s) });
    }
    [["Name", "name", e.name], ["Date of birth", "dob", e.dob]].forEach(([field, k, mine]) => {
      const s = ic[k] || "NOT_COMPARED";
      const fix = k === "dob" ? day : (x) => x || null;
      rows.push({ field, entered: fix(mine || ours[k]), mifin: fix(theirs[k]), result: say(s), tone: tone(s) });
    });
    if (v.match.ref && v.match.ref !== "—" && v.searched !== "AP_CODE") {     // (searched by it: already the first row)
      rows.push({ field: "Customer ID (AP Code)", entered: null, mifin: v.match.ref, result: v.match.label,
        tone: v.match.code === "CONFIRMED_MATCH" ? "ok" : "warn" });
    }
    return rows;
  }

  /** The model's reason (GET /api/v1/credit/dedupe/{id}/reason) into the view: only points that are text, only as
   * many as the deterministic ones (never more); anything else leaves the view as it was. Pure. */
  function applyReason(view, answer) {
    const pts = answer && Array.isArray(answer.points) ? answer.points.map(safeText).filter(Boolean) : [];
    const max = Math.max(view.explanation.points.length, 3);
    if (!answer || !pts.length || pts.length > max) return { ...view, explanation: { ...view.explanation, pending: false } };
    return { ...view, explanation: { ...view.explanation, pending: false, points: pts,
      pointsSource: answer.source === "LLM" ? "LLM" : "DETERMINISTIC" } };
  }

  /** The summary in very simple words (ui_text.json simple_summary): [{key, q, a, tone}] -- each answer from the
   * answer's own facts, nothing guessed. */
  function simpleSummary(r, v, text) {
    const t = (text && text.simple_summary) || {};
    const Q = t.questions || {};
    const fill = (s, vals) => String(s || "").replace(/\{(\w+)\}/g, (_, k) => (vals[k] === undefined ? "" : String(vals[k])));
    const out = [];
    const add = (key, a, tone) => (a ? out.push({ key, q: Q[key] || key, a, tone }) : null);
    const m = v.match.code;
    const cust = t.customer || {};
    add("customer", fill(cust[m] || cust.UNKNOWN, { ref: v.match.ref !== "—" ? v.match.ref : "" }).replace(" .", "."),
      { CONFIRMED_MATCH: "brand", NO_MATCH: "ok", POTENTIAL_MATCH: "warn", AMBIGUOUS: "warn" }[m] || "bad");
    const L = t.loans || {};
    if (v.loans.length) {
      const by = {};
      v.loans.forEach((l) => { const w = humanise(l.status).toLowerCase(); by[w] = (by[w] || 0) + 1; });
      add("loans", fill(L.HAS, { n: v.loans.length, list: Object.entries(by).map(([w, n]) => `${n} ${w}`).join(", ") }), "brand");
    } else {
      add("loans", L[v.history.status] || L.UNKNOWN, v.history.status === "NOT_ATTRIBUTED" ? "warn" : v.history.status === "UNAVAILABLE" || v.history.status === "UNKNOWN" ? "bad" : "ok");
    }
    const pan = (v.commonApplicant && v.commonApplicant.pan) || (v.identityCheck && v.identityCheck.pan);
    if (pan && ["CONFIRMED_MATCH", "POTENTIAL_MATCH"].includes(m)) {
      const P = t.pan || {};
      const key = pan === "MATCH" && !v.loans.length ? "MATCH_NO_LOANS" : pan;
      add("pan", fill(P[key], { n: v.loans.length }), { MATCH: "ok", MISMATCH: "bad" }[pan] || "na");
    }
    const d = (r && r.dedupe) || {};
    const dup = d.invoked === false ? "NOT_EVALUATED" : (d.assessment || {}).duplicate_application;
    if (dup) add("duplicate", (t.duplicate || {})[dup], { CONFIRMED: "bad", POSSIBLE: "warn", UNKNOWN: "warn", NOT_FOUND: "ok", NOT_EVALUATED: "ok" }[dup] || "na");
    const w = d.workflow_recommendation || {};
    if (w.recommendation) add("decision", (t.decision || {})[w.recommendation],
      { PROCEED_TO_NEXT_STAGE: "ok", HOLD_FOR_REVIEW: "warn", BLOCK_APPLICATION: "bad" }[w.recommendation] || "na");
    return out;                                            // the next step is point 3 of "Why this recommendation"
  }

  /** One source as the Sources panel shows it: "LMS (Leviosa)" and "2 of 3 loans found" -- never a bare "Partial". */
  function sourceLine(s, text) {
    const W = (text && text.completeness_words) || {};
    const provider = s.provider ? ((W.providers || {})[s.provider] || humanise(s.provider)) : null;
    // the loan system is named by its provider ("Leviosa"); the API's own id ("LMS") is not shown
    // the loan system is Leviosa (owner 2026-10-23): named so, never "LMS" or "(demo)"
    const name = s.source === "LMS" ? ((W.providers || {}).LEVIOSA || "Leviosa")
      : (W.source_names || {})[s.source] || (provider ? `${s.source} (${provider})` : s.source);
    const state = s.status === "PARTIAL" && s.loans_total !== undefined
      ? fillWords(W.source_partial || "{done} of {total} loans found", { done: s.loans_resolved || 0, total: s.loans_total })
      : humanise(s.status);
    return { name, state, tone: s.status === "AVAILABLE" ? "ok" : s.status === "PARTIAL" ? "warn" : "bad" };
  }

  /** "9 other LOS cases have this PAN · as co-applicant 9 · Home loan 5, Personal loan 3": counts only. */
  function otherCasesLine(v, text, roleWord, productWord) {
    const n = v.otherCases.length;
    const count = (key, word) => {
      const by = {};
      v.otherCases.forEach((c) => { if (c[key]) { const w = word(c[key]); by[w] = (by[w] || 0) + 1; } });
      return Object.entries(by).sort((a, b) => b[1] - a[1]).map(([w, k]) => `${w} ${k}`).join(", ");
    };
    const roles = count("role", (r) => (roleWord ? roleWord(r) : humanise(r)).toLowerCase());
    const products = count("product", (p) => (productWord ? productWord(p) : humanise(p)));
    return [`${n} other LOS case${n === 1 ? " has" : "s have"} this ${searchedLabel(v.searched)}`,
      roles ? `as ${roles}` : null, products || null].filter(Boolean).join(" · ");
  }

  /** Which Existing Loan Details columns to draw (ui_text.json existing_loan_layout): a column the same for every row
   * is shown once in the strip above; a column empty for every loan is hidden and NAMED below the table -- never
   * silently dropped. Returns {shown, empty: [labels], constant: [labels]}. */
  function loanColumns(cols, rows, text) {
    const layout = (text && text.existing_loan_layout) || {};
    const constant = new Set(layout.constant_columns || []);
    const blank = (val) => val === null || val === undefined || val === "";
    const empty = layout.hide_empty_columns
      ? new Set(cols.filter((c) => rows.length && rows.every((r) => blank(r[c.key]))).map((c) => c.key)) : new Set();
    return { shown: cols.filter((c) => !constant.has(c.key) && !empty.has(c.key)),
      empty: cols.filter((c) => empty.has(c.key) && !constant.has(c.key)).map((c) => c.label),
      constant: cols.filter((c) => constant.has(c.key)).map((c) => c.label) };
  }

  /** humanise, with the acronyms an officer reads in capitals */
  function words(code) {
    return humanise(code).replace(/\b(pan|los|mifin|lms|emi|dpd|kyc|ap|pr)\b/gi, (m) => m.toUpperCase());
  }

  /** The Dedupe Assessment Summary: rows of {key, label, value, tone}, built ONLY from the answer's dedupe block
   * (per-party results, assessment, workflow recommendation) and ui_text.json assessment_summary. null when absent. */
  function assessmentSummary(r, text) {
    const d = r && r.dedupe;
    if (!d || (!d.status && d.invoked !== false)) return null;
    const t = (text && text.assessment_summary) || {};
    const L = t.labels || {};
    const statusWord = (s) => (((text && text.dedupe_status) || {})[s] || { label: humanise(s), tone: "na" });
    const roles = (text && text.roles) || {};
    const party = (p) => {
      const cid = p.mifin ? code(p.mifin.customer_code, p.mifin.customer_ref) : "";
      const who = p.existing_customer ? `existing customer${cid ? ` (${cid})` : ""}` : "no SBFC customer record";
      const via = p.identifier_used ? ` · checked by ${SEARCHED[p.identifier_used] || humanise(p.identifier_used)}` : "";
      const why = (p.match_reasons || []).filter((x) => !/MATCHES_EXISTING_CUSTOMER$/.test(x)).map((x) => words(x).replace(/^\w/, (c) => c.toLowerCase()));
      return { value: `${statusWord(p.status).label} – ${who}${via}${why.length ? ` · ${why.join(", ")}` : ""}`, tone: statusWord(p.status).tone };
    };
    const parties = d.parties || [];
    const applicant = parties.find((p) => p.role === "PRIMARY_APPLICANT");
    const coapps = parties.filter((p) => p.role === "CO_APPLICANT");
    const a = d.assessment || {};
    const w = d.workflow_recommendation || {};
    const rec = ((t.recommendation || {})[w.recommendation]) || { label: humanise(w.recommendation || "UNABLE_TO_DECIDE"), tone: "na" };
    const evidence = (d.match_reasons || []).map(words);
    const rows = [
      d.trigger ? { key: "lookup", label: L.lookup, value: d.trigger.reason
        || ((text && text.customer_lookup) || {})[d.trigger.customer_lookup] || humanise(d.trigger.customer_lookup),
      tone: { NEW: "ok", EXISTING: "na", POSSIBLE_EXISTING: "warn", CONFLICTING: "bad", LOOKUP_FAILED: "bad" }[d.trigger.customer_lookup] || "na" } : null,
      { key: "agent", label: L.agent, value: (t.agent || {})[d.invoked === false ? "not_invoked" : "invoked"] || "", tone: "na" },
      { key: "overall", label: L.overall, value: d.invoked === false ? statusWord("NOT_INVOKED").label : statusWord(d.status).label,
        tone: d.invoked === false ? statusWord("NOT_INVOKED").tone : statusWord(d.status).tone },
      { key: "existing", label: L.existing, value: (t.existing || {})[a.existing_customer_or_loan] || humanise(a.existing_customer_or_loan), tone: "na" },
      { key: "duplicate", label: L.duplicate, value: (t.duplicate || {})[a.duplicate_application] || humanise(a.duplicate_application),
        tone: { CONFIRMED: "bad", POSSIBLE: "warn", UNKNOWN: "bad", NOT_FOUND: "ok" }[a.duplicate_application] || "na" },
      applicant ? { key: "applicant", label: L.applicant, ...party(applicant) } : null,
      ...(coapps.length ? coapps.map((p, i) => ({ key: `co${i}`, label: `${L.co_applicant}${coapps.length > 1 ? ` ${i + 1}` : ""}`, ...party(p) }))
        : [{ key: "co", label: L.co_applicant, value: t.no_co_applicant, tone: "na" }]),
      { key: "evidence", label: L.evidence, value: evidence.length ? evidence.slice(0, 5).join("; ") : (t.none || "None"), tone: "na" },
      { key: "conflicts", label: L.conflicts, value: (a.conflicts || []).length ? a.conflicts.map(words).join("; ") : (t.none || "None"),
        tone: (a.conflicts || []).length ? "bad" : "ok" },
      { key: "complete", label: L.complete, value: d.search_complete ? "Complete"
        : `Incomplete – ${[...(d.searches_missing || []).map(words), ...(d.parties_incomplete || []).map((x) => {
          const [role, reason] = String(x).split(":");
          return `${roles[role] || humanise(role)}: ${words(reason || "")}`;
        })].join("; ")}`,
        tone: d.search_complete ? "ok" : "bad" },
      { key: "recommendation", label: L.recommendation, value: rec.label, tone: rec.tone, strong: true },
      w.reason ? { key: "reason", label: L.reason, value: w.reason, tone: "na" } : null,
      w.next_action ? { key: "next", label: L.next, value: w.next_action, tone: "na" } : null,
      w.policy_question ? { key: "question", label: L.question, value: w.policy_question, tone: "warn" } : null,
    ].filter(Boolean);
    return { title: t.title || "Dedupe Assessment Summary", note: t.note || "", rows, recommendation: w.recommendation || null };
  }

  /** The answer's one dedupe status (backend dedupe_status.py) in the officer's words; null when absent. */
  function dedupeView(d, text) {
    if (!d || (!d.status && d.invoked !== false)) return null;
    const key = d.invoked === false ? "NOT_INVOKED" : d.status;
    const s = ((text && text.dedupe_status) || {})[key] || { label: humanise(key), tone: "na" };
    const action = ((text && text.dedupe_actions) || {})[d.recommended_action] || humanise(d.recommended_action);
    const w = d.workflow_recommendation || {};
    const rec = (((text && text.assessment_summary) || {}).recommendation || {})[w.recommendation];
    return { status: key, label: s.label, tone: s.tone, action: rec ? rec.label : action, complete: Boolean(d.search_complete),
      reasons: (d.match_reasons || []).map((x) => humanise(x)), missing: (d.searches_missing || []).map((x) => humanise(x)) };
  }

  /** What to do next with this customer, in one plain message (ui_text.json "next"). Picked from the answer only. */
  function guidance(r, text) {
    const n = (text && text.next) || {};
    const c = (r.history && r.history.customer) || {};
    const what = SEARCHED[r.searched_parameter] || "identifier";
    // a failed check named by what was found ("conflicting loan records"), not by the check's goal
    const failed = (r.evaluation.rules || []).filter((x) => x.outcome === "TRIGGERED")
      .map((x) => (x.reason_code ? humanise(x.reason_code).toLowerCase() : ruleView(x, text).title));
    const s = (r.los_dedupe || {}).search || {};
    const otherCases = Array.isArray(s.matches) ? s.matches.length : 0;
    const declared = (r.customer_type_check || {}).declared;
    let key;
    if (r.customer_status === "UNKNOWN") key = "UNKNOWN";
    else if (r.customer_status === "AMBIGUOUS") key = "AMBIGUOUS";
    else if (r.customer_status === "POSSIBLE_EXISTING") key = "POSSIBLE_EXISTING";
    else if (r.customer_status === "EXISTING") key = failed.length ? "EXISTING_FAILED" : "EXISTING_CLEAN";
    else if (declared === "EXISTING") key = "DECLARED_EXISTING_NOT_FOUND";
    else key = otherCases ? "NEW_OTHER_CASES" : "NEW";
    // the one dedupe status wins over a "clean" reading: an incomplete check or review evidence is never "go ahead"
    const dd = r.dedupe && r.dedupe.invoked !== false ? r.dedupe : null;   // a new customer: the agent did not run
    const plain = (x) => !/MATCHES_EXISTING_CUSTOMER$|^EXISTING_LOANS_ON_RECORD$/.test(x);
    // review evidence from the searched identifier AND from every party (a co-applicant's possible match counts too)
    const roleWords = (text && text.roles) || {};
    const review = dd ? [...(dd.match_reasons || []).filter(plain).map((x) => humanise(x).toLowerCase()),
      ...(dd.parties || []).flatMap((p, i, all) => (p.match_reasons || []).filter(plain).map((x) => {
        const sameRole = all.filter((q) => q.role === p.role);
        const who = `${roleWords[p.role] || humanise(p.role)}${sameRole.length > 1 ? ` ${sameRole.indexOf(p) + 1}` : ""}`;
        return `${who}: ${words(x).replace(/^\w/, (ch) => ch.toLowerCase())}`;
      }))] : [];
    if (dd && dd.status === "CHECK_INCOMPLETE" && n.CHECK_INCOMPLETE) key = "CHECK_INCOMPLETE";
    else if (dd && dd.status !== "NO_DUPLICATE_FOUND" && review.length && ["EXISTING_CLEAN", "NEW"].includes(key)) {
      key = key === "NEW" ? "NEW_REVIEW" : "EXISTING_OTHER_RECORDS";
    }
    const spec = n[key] || { tone: "na", title: humanise(key), text: "" };
    const custCode = code(c.customer_code, c.customer_ref);
    const fill = (t) => String(t || "").replace("{ref}", custCode ? `Customer ID ${custCode}` : "Customer ID not given")
      .replace(/\{what\}/g, what).replace("{failed}", failed.join(", ") || "none")
      .replace("{review}", review.join("; ") || "none")
      .replace("{missing}", ((dd && dd.searches_missing) || []).map((x) => humanise(x).toLowerCase()).join(", ") || "unknown");
    const lines = [fill(spec.text)];
    if (declared === "NEW" && ["EXISTING", "POSSIBLE_EXISTING"].includes(r.customer_status) && n.DECLARED_NEW_BUT_FOUND) lines.unshift(n.DECLARED_NEW_BUT_FOUND);
    return { key, tone: spec.tone, title: spec.title, lines };
  }

  /** Next steps split in two: what the officer can do now, and checks waiting for a business limit (said once). */
  function splitActions(r) {
    const waiting = new Set((r.evaluation.rules || [])
      .filter((x) => x.outcome === "NOT_EVALUATED" && x.reason === "THRESHOLD_UNCONFIGURED" && x.next_action)
      .map((x) => x.next_action));
    const all = (r.next_actions || []).map(safeText);
    return { officer: all.filter((a) => !waiting.has(a)), limitsNotSet: waiting.size };
  }

  /** The All-parameters answer (POST /api/v1/credit/dedupe/all) as the page shows it: one row per parameter (with its
   * own full view for "Details"), the missing ones, whether they all name the same MIFIN customer, one combined
   * decision, and R01-R11 combined (the worst colour of any parameter wins; "found via" names which). Pure. */
  function allView(data, text) {
    const W = (text && text.all_parameters) || {};
    const L = W.labels || {};
    const fill = (s, vals) => String(s || "").replace(/\{(\w+)\}/g, (_, k) => (vals[k] === undefined ? "" : String(vals[k])));
    const REC = ((text && text.assessment_summary) || {}).recommendation || {};
    const rec = (code) => REC[code] || { label: code ? humanise(code) : "—", tone: "na" };
    const rows = (data.rows || []).map((r) => {
      const view = viewResult(r.result, text);
      const found = view.business.filter((b) => b.tone === "fail").map((b) => b.id);
      return { parameter: r.parameter, label: L[r.parameter] || humanise(r.parameter), value: safeText(r.value_shown) || null,
        from: (W.sources || {})[r.source] || humanise(r.source), match: view.match.label, matchCode: r.match,
        customer: safeText(r.customer_code || r.customer_ref) || null,
        decision: rec(r.recommendation).label, tone: rec(r.recommendation).tone, found,
        findings: (r.rule_findings || []).map((x) => (W.findings || {})[x] || humanise(x)), view };
    });
    const c = data.consistency || {};
    const consistency = { status: c.status, tone: { CONFLICT: "bad", SAME_CUSTOMER: "ok" }[c.status] || "na",
      text: fill((W.consistency || {})[c.status] || humanise(c.status || ""), { customers: (c.customers || []).join(" and ") }),
      byParameter: (c.by_parameter || []).map((x) => `${L[x.parameter] || x.parameter}: ${x.customer}`) };
    const combined = { code: (data.combined || {}).recommendation, ...rec((data.combined || {}).recommendation),
      reasons: ((data.combined || {}).reason_codes || []).map((x) => (W.findings || {})[x] || humanise(x)) };
    // R01-R11 across every parameter: the worst colour wins; a rule found names the parameters that found it
    const RANK = { fail: 0, partial: 1, skip: 2, pass: 3, na: 4 };
    const business = [];
    rows.forEach((row) => row.view.business.forEach((b) => {
      const have = business.find((x) => x.id === b.id);
      if (!have) business.push({ ...b, via: b.tone === "fail" ? [row.label] : [] });
      else {
        if (b.tone === "fail") have.via.push(row.label);
        if ((RANK[b.tone] ?? 9) < (RANK[have.tone] ?? 9)) Object.assign(have, { ...b, via: have.via });
      }
    }));
    business.forEach((b) => {
      if (b.tone === "fail" && b.via.length) b.reason = `${b.reason} ${fill(W.found_via || "Found via {via}.", { via: b.via.join(", ") })}`;
    });
    return { requestId: safeText(data.request_id), checked: data.checked || [], rows,
      missing: (data.missing || []).map((m) => L[m.parameter] || humanise(m.parameter)), consistency, combined, business };
  }

  function viewResult(r, text) {
    const view = buildView(r, text);
    view.simple = simpleSummary(r, view, text);
    return view;
  }

  function buildView(r, text) {
    if (!r || typeof r !== "object" || !r.outcome || !r.history || !r.evaluation) {
      throw new ApiError(0, "MALFORMED_RESPONSE", "The dedupe answer is not in the expected shape.");
    }
    const h = r.history;
    const a = h.aggregates || null;
    const loans = (h.loans || []).map((x) => ({
      ref: code(x.prospect_code, x.prospect_ref), scheme: safeText(x.scheme || "—"),
      sourceStatus: safeText(x.source_status || "—"), status: x.effective_status || "UNKNOWN",
      statusSource: x.status_source || "—", tone: LOAN_TONE[x.effective_status] || "warn",
      sanctioned: money(x.amounts && x.amounts.sanctioned_amount),
      outstanding: x.lms && x.lms.principal_outstanding !== undefined ? money(x.lms.principal_outstanding) : null,
      emi: x.lms && x.lms.emi !== undefined ? money(x.lms.emi) : null,
      dpd: x.lms && x.lms.dpd_max_12m !== undefined ? x.lms.dpd_max_12m : null,
      dpdNow: x.lms && x.lms.dpd_current !== undefined ? x.lms.dpd_current : null,
      overdue: x.lms && x.lms.overdue_amount !== undefined ? money(x.lms.overdue_amount) : null,
      disbursedOn: (x.lms && x.lms.disbursed_on) || null, closedOn: (x.lms && x.lms.closed_on) || null,
      tenure: x.lms && x.lms.tenure_months !== undefined ? x.lms.tenure_months : null,
      lms: x.lms_status || "—", resolved: Boolean(x.resolved), searchedAccount: Boolean(x.searched_loan_account),
      conflict: Boolean(x.conflicting_duplicate), duplicates: x.duplicates_collapsed || 0,
      problems: [...(x.amount_problems || []), ...(x.lms_problems || [])],
      cancelReason: x.cancel_reason ? safeText(x.cancel_reason) : null, check: x.applicant_check || null,
      topup: x.topup || null, bounces: x.lms && x.lms.emi_bounces_12m !== undefined ? x.lms.emi_bounces_12m : null,
    }));
    const rules = (r.evaluation.rules || []).map((x) => ({
      id: x.rule_id, outcome: x.outcome, reason: x.reason || null, reasonCode: x.reason_code || null,
      description: x.description || x.rule_id, severity: x.severity || null, nextAction: x.next_action || null,
      version: x.policy_version, signal: x.signal, view: ruleView(x, text),
      value: x.value === null || x.value === undefined ? null : safeText(x.value),
    }));
    const los = r.los_dedupe || {};
    const losCase = los.case || {};
    return {
      requestId: r.request_id, caseId: r.case_id, stage: r.stage || null, isDemo: Boolean(r.is_demo),
      outcome: r.outcome, outcomeView: OUTCOMES[r.outcome] || { tone: "na", title: r.outcome, note: "" },
      customer: CUSTOMER[r.customer_status] || { tone: "na", label: r.customer_status },
      customerStatus: r.customer_status, typeCheck: r.customer_type_check || null,
      searched: r.searched_parameter,
      match: { code: h.customer.match, label: MATCH[h.customer.match] || h.customer.match,
        ref: code(h.customer.customer_code, h.customer.customer_ref || "—"), basis: h.customer.match_basis || {},
        identifierMatches: h.customer.searched_identifier_matches, warnings: h.customer.warnings || [],
        role: h.customer.role_in_source || null },
      confirmation: h.customer.confirmation || { required: false, candidate: null, decision: null },
      identityCheck: h.customer.identity_check || null,
      // both sides of the matching table (MIFIN's name / DOB as written, PAN / mobile masked by the backend)
      identityValues: h.customer.identity_values || null,
      checkedParty: r.checked_party || "APPLICANT",
      otherCases: (((r.los_dedupe || {}).search || {}).matches || []).map((m) => ({
        caseId: m.visible ? safeText(m.case_id) : null, role: m.role || null, product: m.product || null,
        status: m.status || null })),
      history: { status: h.history_status, ...(HISTORY[h.history_status] || { tone: "na", label: h.history_status }),
        // never a bare "Partial": how many loans the LMS confirmed
        ...(h.history_status === "PARTIAL" ? { label: fillWords(((text && text.completeness_words) || {}).history_partial
          || "{done} of {total} loans confirmed by Leviosa", { done: (h.loans || []).filter((x) => x.resolved).length, total: (h.loans || []).length }) } : {}),
        count: h.loan_count || 0, withheld: h.loans_withheld || 0, sources: h.sources || [] },
      loans,
      commonApplicant: h.common_applicant || null,
      totals: a ? {
        exposure: total(a.active_exposure, text), emi: total(a.monthly_emi_burden, text),
        dpd: a.worst_dpd_12m && a.worst_dpd_12m.value !== null && a.worst_dpd_12m.value !== undefined
          ? `${a.worst_dpd_12m.value} days` : (a.worst_dpd_12m && a.worst_dpd_12m.status === "NOT_APPLICABLE"
            ? "No disbursed loan" : "Unavailable"),
        active: a.active_loans, byStatus: a.by_status || {}, conflicts: a.conflicting_duplicates || 0,
        unknown: a.unknown_status_loans || 0,
      } : null,
      rules,
      ruleCounts: r.evaluation.rule_counts || {},
      reasonCodes: (r.reason_codes || []).map(safeText),
      nextActions: (r.next_actions || []).map(safeText),
      actions: splitActions(r),
      guidance: guidance(r, text),
      dedupe: dedupeView(r.dedupe, text),
      summary: assessmentSummary(r, text),
      simple: null,                                       // filled below, once the view's own facts exist
      headline: (() => {
        const w = r.dedupe && r.dedupe.workflow_recommendation;
        const rec = w && (((text && text.assessment_summary) || {}).recommendation || {})[w.recommendation];
        return rec ? { title: rec.label, tone: rec.tone === "na" ? "warn" : rec.tone, note: w.reason || "" } : null;
      })(),
      business: businessRules(r, text),
      matched: whatMatched(r),
      complete: Boolean(r.completeness && r.completeness.complete),
      sources: (r.completeness && r.completeness.sources) || h.sources || [],
      policy: r.policy || {},
      explanation: { text: safeText(r.explanation && r.explanation.text),
        source: (r.explanation && r.explanation.source) || "DETERMINISTIC",
        fallback: (r.explanation && r.explanation.fallback_reason) || null,
        // the reason in (at most) 3 points; the model's wording follows from GET .../reason when pending
        points: ((r.explanation && r.explanation.points) || []).map(safeText).filter(Boolean),
        pending: Boolean(r.explanation && r.explanation.llm_reason_pending) },
      los: { status: losCase.status || null, recommendation: losCase.recommendation || null,
        recommendations: losCase.recommendations || [],
        documentsReused: (losCase.documents && losCase.documents.reused_on_other_customers_cases) || 0,
        sameProduct: losCase.applications ? losCase.applications.same_product_other_cases : null,
        searchStatus: los.search ? los.search.status : null,
        searchMatches: los.search && Array.isArray(los.search.matches) ? los.search.matches.length : 0,
        searchTypeCheck: los.search ? los.search.customer_type_check || null : null },
      timings: r.timings_ms || {},
    };
  }

  function sortLoans(rows, key, direction = "asc") {
    const sign = direction === "desc" ? -1 : 1;
    const num = (v) => (v === null ? null : Number(String(v).replace(/[^\d.-]/g, "")));
    const numeric = ["sanctioned", "outstanding", "emi", "dpd"].includes(key);
    return [...rows].sort((a, b) => {
      const x = numeric ? num(a[key]) : String(a[key] || "");
      const y = numeric ? num(b[key]) : String(b[key] || "");
      if (x === null && y === null) return 0;
      if (x === null) return 1;                                     // unknowns last, whichever direction
      if (y === null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * sign;
    });
  }

  function humanise(code) {
    return String(code || "").replace(/^POLICY_NOT_CONFIGURED:/, "Limit not set: ").replace(/^DATA_UNAVAILABLE:/, "Data unavailable: ")
      .replace(/\bH\d+_/g, "")                                         // never a rule id (H06_...) in officer text
      .replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
  }

  return {
    DEDUPE_PARAMETERS, ALL_PARAMETERS, PARAMETER_OPTIONS, ALL_FIELDS, allValues, allView, parameter, initialState, setCustomerType, setDedupeParameter, setValue, applySample, generateSample, effectiveField, whatMatched, searchedLabel, ruleView, guidance, lookupSample, businessRules, dedupeView, existingLoanRows, loanDetail, matchTable, maskShown, otherCasesLine, tenor, applyReason, simpleSummary, assessmentSummary, loanColumns, formSchema, coApplicantSchema, addCoApplicant, removeCoApplicant, setCoApplicantValue, validateCoApplicants, validateField,
    validateDedupe, validate, createBody, dedupeBody, caseSubmission, canonical, request, ApiError, errorFrom,
    viewResult, sortLoans, safeText, code, money, sourceLine, humanise, OUTCOMES, HISTORY, MATCH,
  };
});
