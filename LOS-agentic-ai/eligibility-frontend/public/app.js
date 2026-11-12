// Loan Eligibility -- frontend for the LOS eligibility agent (/api/v1/eligibility/*). No framework, no build.
"use strict";

const API = "/api/v1/eligibility";
const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { form: null, cases: [], caseId: null, caseData: null, income: "CASE", topup: "NONE" };

// ---------------------------------------------------------------- formatting
// rupees: Indian grouping, whole rupees, a minus BEFORE the sign (−₹5,000, never ₹-5,000)
const inr = (n) => {
  if (n == null || n === "" || isNaN(n)) return "—";
  const v = Math.round(Number(n));
  return (v < 0 ? "−₹" : "₹") + Math.abs(v).toLocaleString("en-IN");
};
// percentages: at most 2 decimals, no trailing zeros (49.99%, 50%, 12.5%)
const pc = (n) => (n == null || n === "" || isNaN(n)) ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 }) + "%";
// share of the request: never rounded UP to 100% when the offer is short of the full amount
const sharePct = (part, whole) => { const p = whole ? (part / whole) * 100 : 0; return p >= 100 ? 100 : Math.min(99, Math.floor(p)); };
const short = (n) => {
  if (n == null || isNaN(n)) return "—";
  const v = Number(n);
  if (v >= 1e7) return "₹" + (v / 1e7).toFixed(v % 1e7 ? 2 : 0).replace(/\.?0+$/, "") + " Cr";
  if (v >= 1e5) return "₹" + (v / 1e5).toFixed(v % 1e5 ? 1 : 0).replace(/\.0$/, "") + " L";
  return inr(v);
};
const pretty = (code) => String(code || "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase())
  .replace(/\bTopup\b/, "Top-up").replace(/\bEmi\b/, "EMI").replace(/\bItr\b/, "ITR").replace(/\bBt\b/, "BT");
const digits = (s) => { const t = String(s ?? "").replace(/[^\d.]/g, ""); return t === "" ? null : Number(t); };
const groupInput = (input) => input.addEventListener("input", () => {
  const n = digits(input.value); const pos = input.value.length - input.selectionStart;
  input.value = n == null ? "" : Math.round(n).toLocaleString("en-IN");
  input.setSelectionRange(input.value.length - pos, input.value.length - pos);
});

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const res = await fetch(API + path, { headers: { "content-type": "application/json" }, ...opts });
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error(messageOf(body, res.status));
    err.status = res.status; err.body = body; throw err;
  }
  return body;
}
function messageOf(body, status) {
  const d = body && (body.detail ?? body.error);
  if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).slice(1).join(" › ") || "input"}: ${e.msg}`).join("\n");
  if (d && typeof d === "object") return d.message || d.error || JSON.stringify(d);
  if (typeof d === "string") return d;
  if (status === 401 || status === 403) return "Not authorised. Start the backend with AUTH_ENABLED=false (dev) or sign in.";
  return `Request failed (${status}).`;
}

// ---------------------------------------------------------------- toast / status
let toastTimer;
function toast(text, bad = false) {
  const t = $("toast"); t.textContent = text; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}
function setStatus(ok, text) {
  const s = $("backend-status"); s.className = "status " + (ok ? "ok" : "bad"); s.querySelector(".status-text").textContent = text;
}
function showError(text) { const e = $("form-error"); e.textContent = text; e.hidden = !text; }

// ---------------------------------------------------------------- boot
async function boot() {
  ["requested_amount", "existing_exposure"].forEach((id) => groupInput($(id)));
  $("case-form").addEventListener("submit", (e) => { e.preventDefault(); fetchCase($("case-id").value); });
  $("product").addEventListener("change", syncProperty);
  $("btn-check").addEventListener("click", runCheck);
  $("btn-save").addEventListener("click", saveEntries);
  $("btn-print").addEventListener("click", () => window.print());
  $("btn-reco").addEventListener("click", toggleReco);
  // any edit: the summary for the new figures starts in the background (server-cached; identical figures cost nothing)
  document.querySelector(".inputs").addEventListener("input", () => state.caseId && prefetchExplanation());
  document.querySelector(".inputs").addEventListener("change", () => state.caseId && prefetchExplanation());
  // FILL SAMPLE: the config's demo figures for the open section (to try a method); CLEAR empties it
  $("income-sample").addEventListener("click", () => renderGroup("income-fields", state.form.income_methods[state.income], sampleOf(state.form.income_methods[state.income])));
  $("topup-sample").addEventListener("click", () => renderGroup("topup-fields", state.form.topup_types[state.topup], sampleOf(state.form.topup_types[state.topup])));
  $("income-clear").addEventListener("click", () => renderGroup("income-fields", state.form.income_methods[state.income]));
  $("topup-clear").addEventListener("click", () => renderGroup("topup-fields", state.form.topup_types[state.topup]));
  lockForm(true);
  try {
    const [form, cases] = await Promise.all([api("/form"), api("/cases").catch(() => ({ cases: [] }))]);
    state.form = form; state.cases = cases.cases || [];
    setStatus(true, "Backend connected");
    // the rules' status from the backend config: anything not APPROVED is labelled, never hidden
    const cs = String(form.config_status || ""); const chip = $("config-chip");
    chip.hidden = !cs || cs === "APPROVED";
    chip.textContent = /DEMO/.test(cs) ? "Demo rules" : pretty(cs);
    chip.title = `Eligibility rules ${form.config_version || ""} · ${cs}`;
    renderStatic();
    renderQuick();
    // DEMO LINKS: ?case=CASE-E11D00000010 opens that case; &check=1 also runs the check
    const params = new URLSearchParams(location.search);
    if (params.get("case")) {
      const ok = await fetchCase(params.get("case"));
      // &income=ASSESSED / &topup=BT_TOPUP open that section; &sample=1 fills it with the sample figures
      const demo = (boxId, segId, key, value, specs, setter) => {
        if (!ok || !specs?.[value]) return;
        pickSegment(segId, value, (v) => { setter(v); renderGroup(boxId, specs[v], params.get("sample") === "1" ? sampleOf(specs[v]) : {}); });
      };
      demo("income-fields", "income-methods", "income", params.get("income"), state.form.income_methods, (v) => { state.income = v; incomeHint(); });
      demo("topup-fields", "topup-types", "topup", params.get("topup"), state.form.topup_types, (v) => { state.topup = v; topupState(); $("topup-card").open = true; });
      if (ok && params.get("check") === "1") {
        await runCheck();
        if (params.get("reco") === "1" && !$("btn-reco").hidden) toggleReco();   // demo link: the panel open
      }
    } else {
      $("case-id").focus();
    }
  } catch (err) {
    setStatus(false, "Backend not reachable");
    showError(err.message);
  }
}

// ---------------------------------------------------------------- case id entry
const RECENT_KEY = "elig.recent";
function recent() { try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch { return []; } }
function remember(id) {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify([id, ...recent().filter((x) => x !== id)].slice(0, 4))); } catch { /* private mode */ }
}
function renderQuick() {
  const rec = recent();
  const chip = (id, text) => `<button type="button" class="pick" data-id="${esc(id)}">${esc(text)}</button>`;
  $("quick").innerHTML =
    (rec.length ? `<span class="quick-label">Recent</span>` + rec.map((id) => chip(id, id)).join("") : "");
  $("quick").querySelectorAll(".pick").forEach((b) => b.addEventListener("click", () => fetchCase(b.dataset.id)));
}
function normaliseId(raw) {
  const t = String(raw || "").trim().toUpperCase();
  if (/^\d{1,8}$/.test(t)) {                      // "10" -> the demo case ending in 10 (when one exists)
    const hit = state.cases.find((c) => c.case_id.endsWith(t.padStart(8, "0")) || c.case_id.endsWith("D" + t.padStart(8, "0")));
    if (hit) return hit.case_id;
  }
  return t;
}
async function fetchCase(raw) {
  const id = normaliseId(raw);
  if (!id) { showError("Enter a case ID."); $("case-id").focus(); return false; }
  $("case-id").value = id;
  const btn = $("btn-fetch"); btn.disabled = true; btn.textContent = "Fetching…";
  try {
    const ok = await selectCase(id);
    if (ok) { remember(id); renderQuick(); }
    return ok;
  } finally { btn.disabled = false; btn.textContent = "Fetch"; }
}
function lockForm(locked) { document.body.classList.toggle("no-case", locked); $("btn-check").disabled = locked; $("btn-save").disabled = locked; }

function renderStatic() {
  const f = state.form;
  $("product").innerHTML = (f.products || []).map((p) => `<option value="${esc(p)}">${esc(pretty(p))}</option>`).join("");
  $("collateral_type").innerHTML = `<option value="">Case default</option>` +
    Object.entries(f.collateral_types || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  $("occupancy_type").innerHTML = `<option value="">Case default</option>` +
    Object.entries(f.occupancy_types || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  if (f.existing_exposure_label) $("exposure-label").textContent = f.existing_exposure_label;
  // INCOME: optional -- nothing selected = the case's declared income (no "Case data" button, owner 2026-10-15)
  segmented("income-methods", Object.entries(f.income_methods || {}).map(([k, v]) => [k, shortLabel(v.label)]),
    () => state.income, (v) => { state.income = v; renderGroup("income-fields", f.income_methods[v]); incomeHint(); }, true);
  segmented("topup-types", [["NONE", "None"], ...Object.entries(f.topup_types || {}).map(([k, v]) => [k, shortLabel(v.label)])],
    () => state.topup, (v) => { state.topup = v; renderGroup("topup-fields", f.topup_types[v]); topupState(); });
}
const shortLabel = (label) => String(label || "").replace(/\s*\(.*\)\s*$/, "");
function topupState() {
  const chosen = state.form?.topup_types?.[state.topup];
  $("topup-state").textContent = chosen ? shortLabel(chosen.label) : "None";
  sectionTools();
}
function incomeHint() {
  const chosen = state.form?.income_methods?.[state.income];
  $("income-hint").textContent = chosen ? "Optional · click again to use the declared income" : "Optional · none selected uses the declared income";
  sectionTools();
}

// a row of options; `optional`: clicking the selected one again clears it (value "CASE" = the case's own data)
function segmented(id, options, get, set, optional = false) {
  const box = $(id); box.innerHTML = "";
  options.forEach(([value, label]) => {
    const b = el("button", null, esc(label)); b.type = "button"; b.setAttribute("role", "tab"); b.dataset.value = value;
    b.setAttribute("aria-selected", String(get() === value));
    b.addEventListener("click", () => {
      const next = optional && get() === value ? "CASE" : value;
      set(next);
      [...box.children].forEach((c) => c.setAttribute("aria-selected", String(c.dataset.value === next)));
    });
    box.appendChild(b);
  });
  set(get());
}

// one section's fields (config `form`): sub-headings by `group`; type amount (default) | int | percent | text | select
function renderGroup(boxId, spec, values = {}) {
  const box = $(boxId); box.innerHTML = "";
  let group = null;
  (spec?.fields || []).forEach((f) => {
    if (f.group && f.group !== group) { group = f.group; box.appendChild(el("div", "group-title", esc(group))); }
    const kind = f.type || "amount";
    const wrap = el("label", "field");
    wrap.innerHTML = `<span class="label">${esc(f.label)}${f.required ? '<span class="req">*</span>' : ""}</span>`;
    let input;
    if (kind === "select") {
      input = el("select");
      input.innerHTML = `<option value="">—</option>` +
        Object.entries(f.options || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
    } else {
      input = el("input"); input.autocomplete = "off";
      if (kind === "int") { input.type = "number"; input.min = "0"; input.step = "1"; }
      else if (kind === "percent") { input.type = "number"; input.min = "0"; input.max = "100"; input.step = "0.5"; }
      else if (kind === "text") { input.type = "text"; input.maxLength = 80; }
      else { input.type = "text"; input.inputMode = "numeric"; groupInput(input); }
    }
    input.dataset.key = f.key; input.dataset.kind = kind;
    const v = values[f.key];
    if (v != null && v !== "") input.value = kind === "amount" ? Math.round(v).toLocaleString("en-IN") : v;
    if (f.required) input.dataset.required = "1";
    input.addEventListener(kind === "select" ? "change" : "input", schedulePreview);
    wrap.appendChild(input); box.appendChild(wrap);
  });
  schedulePreview();
  if (state.caseId) prefetchExplanation();                       // Fill sample / switching method: figures changed
}
// a field's value as the API takes it (null = empty)
function fieldValue(i) {
  const raw = i.value.trim(); if (raw === "") return null;
  switch (i.dataset.kind) {
    case "text": case "select": return raw;
    case "int": return parseInt(raw, 10);
    case "percent": return parseFloat(raw);
    default: return digits(raw);
  }
}
const sampleOf = (spec) => Object.fromEntries((spec?.fields || []).filter((f) => f.sample != null).map((f) => [f.key, f.sample]));
function sectionTools() {
  const inc = state.form?.income_methods?.[state.income]; const top = state.form?.topup_types?.[state.topup];
  $("income-tools").hidden = !inc; $("topup-tools").hidden = !top;
  // samples come from config (DEMO); a production config without them shows no "Fill sample" button
  $("income-sample").hidden = !Object.keys(sampleOf(inc)).length; $("topup-sample").hidden = !Object.keys(sampleOf(top)).length;
  if (inc) $("income-period").textContent = (inc.label.match(/\(([^)]*)\)/) || [])[1] || "";
}

// ---------------------------------------------------------------- live working (POST /preview: no case, no rules)
let previewTimer = null, previewSeq = 0;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(runPreview, 250); }
async function runPreview() {
  const seq = ++previewSeq;
  const pick = (boxId, key, value) => {
    if (value === "CASE" || value === "NONE") return null;
    const obj = { [key]: value };
    $(boxId).querySelectorAll("input, select").forEach((i) => { const v = fieldValue(i); if (v != null && !Number.isNaN(v)) obj[i.dataset.key] = v; });
    return obj;
  };
  const body = { income: pick("income-fields", "method", state.income), topup: pick("topup-fields", "type", state.topup) };
  if (!body.income && !body.topup) { $("income-calc").hidden = $("topup-calc").hidden = true; return; }
  try {
    const r = await api("/preview", { method: "POST", body: JSON.stringify(body) });
    if (seq !== previewSeq) return;
    renderIncomeCalc(r.income); renderTopupCalc(r.topup);
  } catch { /* the preview is a convenience; the check still validates everything */ }
}
const fieldLabel = (spec, key) => ((spec?.fields || []).find((f) => f.key === key.split(".").pop())?.label || pretty(key.split(".").pop())).replace(/\s*\(₹.*$/, "");
function calcBox(id, html) { const b = $(id); b.hidden = !html; b.innerHTML = html || ""; }
function calcNotes(notes) {
  // the workbook-correction notes are for the full working; here only what the officer should act on
  const act = (notes || []).filter((n) => !/workbook/i.test(n));
  return act.map((n) => `<div class="calc-note">${esc(n)}</div>`).join("");
}
function renderIncomeCalc(inc) {
  if (!inc) return calcBox("income-calc", "");
  const spec = state.form.income_methods[state.income];
  if (inc.missing?.length) return calcBox("income-calc", `<div class="calc-wait">Enter ${inc.missing.map((m) => esc(fieldLabel(spec, m))).join(", ")} to see the income.</div>`);
  const lines = (inc.lines || []).map((l) => {
    const strong = /^\((?:D|F|H)\)|lower of/i.test(l.item);
    return `<div class="calc-row${strong ? " sub" : ""}"><span>${esc(l.item.charAt(0).toUpperCase() + l.item.slice(1))}</span><span>${l.amount < 0 ? "− " + inr(-l.amount) : inr(l.amount)}</span></div>`;
  }).join("");
  const total = inc.annual_income != null
    ? `<div class="calc-total"><span>Income considered</span><span>${inr(inc.annual_income)}<small>/year</small> · ${inr(inc.monthly_income)}<small>/month</small></span></div>`
    : `<div class="calc-total"><span>Income considered</span><span>${inr(inc.monthly_income)}<small>/month</small></span></div>`;
  calcBox("income-calc", `<div class="calc-lines">${lines}</div>${total}${calcNotes(inc.notes)}`);
}
function renderTopupCalc(t) {
  if (!t) return calcBox("topup-calc", "");
  const spec = state.form.topup_types[state.topup];
  if (t.missing?.length) return calcBox("topup-calc", `<div class="calc-wait">Enter ${t.missing.map((m) => esc(fieldLabel(spec, m))).join(", ")} to see the top-up.</div>`);
  const rows = [["Applicable % (MOB " + t.mob + " months)", pc(t.applicable_pct)]];
  if (t.applied_pct !== t.applicable_pct) rows.push(["Applied %", pc(t.applied_pct)]);
  if (t.balance_tenor_months != null) rows.push(["Balance tenure", t.balance_tenor_months + " months"]);
  rows.push(["Eligible top-up", inr(t.eligible_topup)]);
  if (t.exposure) rows.push(["Outstanding still on the property", inr(t.exposure)]);
  const lines = rows.map(([k, v]) => `<div class="calc-row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  calcBox("topup-calc", `<div class="calc-lines">${lines}</div><div class="calc-total"><span>${t.type === "BT_TOPUP" ? "Loan can be up to (POS + top-up)" : "New loan can be up to"}</span><span>${inr(t.cap)}</span></div>${calcNotes(t.notes)}`);
}

// ---------------------------------------------------------------- case
async function selectCase(caseId) {
  showError("");
  try {
    const data = await api(`/cases/${encodeURIComponent(caseId)}`);
    state.caseId = caseId; state.caseData = data;
    const c = data.case || {}; const saved = (data.saved && data.saved.inputs) || {};
    const meta = state.cases.find((x) => x.case_id === caseId) || {};
    const facts = [
      ["Age", c.age != null ? `${c.age} yrs` : "—"], ["Declared income", inr(c.net_monthly_income) + "/m"],
      ["Existing EMIs", inr(c.existing_emi) + "/m"], ["Bureau score", c.bureau_score ?? "—"],
      ["Worst DPD (12m)", c.max_dpd_12m != null ? `${c.max_dpd_12m} days` : "—"],
      ["Property value", c.property_value ? short(c.property_value) : "—"],
    ];
    const box = $("case-facts"); box.hidden = false;
    box.innerHTML = facts.map(([k, v]) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("") +
      `<div class="case-label span-all"><b>${esc(caseId)}</b> · ${esc(pretty(c.product))}${meta.label ? " · " + esc(meta.label) : ""}${Object.keys(saved).length ? ` · <b>saved entries loaded</b>` : ""}</div>`;
    $("product").value = saved.product || c.product || "";
    $("requested_amount").value = (saved.requested_amount ?? c.requested_amount) != null ? Math.round(saved.requested_amount ?? c.requested_amount).toLocaleString("en-IN") : "";
    $("tenure_months").value = saved.tenure_months ?? c.tenure_months ?? "";
    $("rate_percent").value = saved.rate_percent ?? c.rate_percent ?? "";
    $("collateral_type").value = saved.collateral_type || "";
    $("occupancy_type").value = saved.occupancy_type || "";
    $("existing_exposure").value = saved.existing_exposure ? Math.round(saved.existing_exposure).toLocaleString("en-IN") : "";
    pickSegment("income-methods", saved.income?.method || "CASE", (v) => { state.income = v; renderGroup("income-fields", state.form.income_methods[v], saved.income || {}); incomeHint(); });
    pickSegment("topup-types", saved.topup?.type || "NONE", (v) => { state.topup = v; renderGroup("topup-fields", state.form.topup_types[v], saved.topup || {}); topupState(); });
    // the folded sections open by themselves only when the case has entries in them
    $("topup-card").open = !!saved.topup;
    $("property-card").open = !!(saved.collateral_type || saved.occupancy_type || saved.existing_exposure);
    syncProperty();
    clearResult();
    lockForm(false);
    prefetchExplanation();                                     // the summary for these inputs, ready early
    return true;
  } catch (err) {
    showError(err.status === 404 ? `No case ${caseId} -- check the ID.` : err.message);
    if (!state.caseId) lockForm(true);
    return false;
  }
}
function pickSegment(id, value, set) {
  [...$(id).children].forEach((c) => c.setAttribute("aria-selected", String(c.dataset.value === value)));
  set(value);
}
// products that take a property come from the backend config (form.secured_products), never a list here
function syncProperty() { $("property-card").hidden = !(state.form?.secured_products || []).includes($("product").value || ""); }

// ---------------------------------------------------------------- request body
function entries(silent = false) {
  const body = {};
  const amount = digits($("requested_amount").value); if (amount) body.requested_amount = amount;
  const tenure = parseInt($("tenure_months").value, 10); if (tenure) body.tenure_months = tenure;
  const rate = parseFloat($("rate_percent").value); if (rate) body.rate_percent = rate;
  const product = $("product").value; if (product && product !== state.caseData?.case?.product) body.product = product;
  let missing = [];
  const group = (boxId, key, value) => {
    if (value === "CASE" || value === "NONE") return;
    const obj = { [key]: value };
    $(boxId).querySelectorAll("input, select").forEach((i) => {
      if (!silent) i.classList.remove("invalid");
      const v = fieldValue(i);
      if (v != null && !Number.isNaN(v)) obj[i.dataset.key] = v;
      else if (i.dataset.required) { missing.push(i.closest(".field").querySelector(".label").textContent.replace("*", "")); if (!silent) i.classList.add("invalid"); }
    });
    return obj;
  };
  const income = group("income-fields", "method", state.income); if (income) body.income = income;
  const topup = group("topup-fields", "type", state.topup); if (topup) body.topup = topup;
  if (!$("property-card").hidden) {
    if ($("collateral_type").value) body.collateral_type = $("collateral_type").value;
    if ($("occupancy_type").value) body.occupancy_type = $("occupancy_type").value;
    const exp = digits($("existing_exposure").value); if (exp) body.existing_exposure = exp;
  }
  return { body, missing };
}

// ---------------------------------------------------------------- actions
async function runCheck() {
  if (!state.caseId) return;
  const { body, missing } = entries();
  if (missing.length) { showError("Fill the required field(s): " + missing.join(", ")); return; }
  showError("");
  const btn = $("btn-check"); btn.classList.add("loading"); btn.disabled = true;
  try {
    const t0 = performance.now();
    const request = { case_id: state.caseId, detail: true, use_saved: false, ...body };
    const result = await api("/check", { method: "POST", body: JSON.stringify(request) });
    renderResult(result, Math.round(performance.now() - t0));
    loadExplanation(request);
  } catch (err) { showError(err.message); toast("Check failed", true); }
  finally { btn.classList.remove("loading"); btn.disabled = false; }
}

async function saveEntries() {
  if (!state.caseId) return;
  const { body, missing } = entries();
  if (missing.length) { showError("Fill the required field(s): " + missing.join(", ")); return; }
  showError("");
  try {
    await api(`/cases/${encodeURIComponent(state.caseId)}/inputs`, { method: "PUT", body: JSON.stringify(body) });
    toast("Entries saved for " + state.caseId);
  } catch (err) { showError(err.message); toast("Not saved", true); }
}

// ---------------------------------------------------------------- result
function clearResult() { explainSeq++; clearInterval(explainTick); $("r-ai").hidden = true; $("result").hidden = true; $("empty").hidden = false; }

// widths through the CSSOM: the CSP refuses style="" attributes in injected HTML
const applyWidths = (root) => root.querySelectorAll("[data-w]").forEach((n) => { n.style.width = n.dataset.w + "%"; });
const _observer = new MutationObserver((list) => list.forEach((m) => m.addedNodes.forEach((n) => n.nodeType === 1 && applyWidths(n.parentNode || n))));

function renderResult(r, ms) {
  state.lastResult = r;                                        // the AI card's key facts read it
  $("empty").hidden = true; $("result").hidden = false;
  const loan = r.loan || {}; const final = r.final || {};
  const decision = final.decision || r.decision || "—";
  const amount = final.amount ?? loan.eligible ?? 0;
  const requested = loan.requested ?? 0;

  const badge = $("r-decision"); badge.textContent = pretty(decision).toUpperCase(); badge.className = "badge d-" + decision;
  document.querySelector(".hero").className = "card hero hero-" + decision;
  $("r-case").textContent = `${r.case_id} · ${pretty(loan.product)}`;
  countUp($("r-amount"), Number(amount) || 0);
  $("r-of").textContent = requested ? `of ${inr(requested)} requested` : "";
  $("r-emi").textContent = final.emi != null || loan.emi != null ? inr(final.emi ?? loan.emi) : "—";
  $("r-tenure").textContent = (final.tenure_months ?? loan.tenure_months) ? `${final.tenure_months ?? loan.tenure_months} months` : "—";
  $("r-rate").textContent = loan.rate_percent != null ? pc(loan.rate_percent) : "—";
  const pct = requested ? Math.max(0, Math.min(100, (amount / requested) * 100)) : 0;
  $("r-meter").style.width = "0"; requestAnimationFrame(() => { $("r-meter").style.width = pct.toFixed(1) + "%"; });
  $("r-meter-left").textContent = requested ? `${sharePct(amount, requested)}% of the request` : "";
  $("r-meter-right").textContent = requested ? short(requested) : "";
  // THE RING: eligible as a share of the request, in the decision's colour
  const R = 30, C = 2 * Math.PI * R;
  const ringColor = { APPROVED: "var(--ok)", CONDITIONAL: "var(--warn)", REFER: "var(--info)", REJECTED: "var(--bad)" }[decision] || "var(--muted)";
  $("r-ring").innerHTML = `<svg viewBox="0 0 76 76" width="76" height="76">
      <circle cx="38" cy="38" r="${R}" fill="none" stroke="var(--line)" stroke-width="8"/>
      <circle cx="38" cy="38" r="${R}" fill="none" stroke="${ringColor}" stroke-width="8" stroke-linecap="round"
        stroke-dasharray="${(C * pct / 100).toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 38 38)" class="ring-arc"/>
    </svg><span class="ring-v">${sharePct(amount, requested)}%</span>`;
  $("r-summary").textContent = r.summary || "";

  // what decided the amount
  $("r-limiting").textContent = r.limiting_factor ? "Limited by " + pretty(r.limiting_factor).replace("Foir", "FOIR").replace("Ltv", "LTV") : "";
  // WHICH INCOME THIS CHECK USED (owner 2026-10-15): the method's figure replaces the declared one for THIS check only;
  // the case's stored income is never changed -- both are shown so nobody reads one for the other
  const used = r.income && r.income.monthly_income != null ? r.income : null;
  const chipIncome = $("r-income-used");
  chipIncome.hidden = !used;
  if (used) {
    const label = state.form?.income_methods?.[used.method]?.label;
    chipIncome.textContent = `Income used: ${inr(used.monthly_income)}/m (${shortLabel(label || pretty(used.method))})`;
    chipIncome.title = "This check used the income worked out from the method; the case's declared income is unchanged.";
  }
  const g = $("r-gauges"); g.innerHTML = "";
  const ratios = r.ratios || {};
  g.appendChild(gauge("FOIR", ratios.foir));
  g.appendChild(gauge("LTV", ratios.ltv));

  // recommendation (the Recommend button's popover)
  renderReco(r.recommendation, final, amount);

  // rules
  const rules = r.rules || r.checks || [];
  const passed = rules.filter((x) => x.result === "pass").length;
  $("r-rules-count").textContent = `${passed} of ${rules.length} passed`;
  // TWO GROUPS: the knock-outs (a fail rejects) and the sizing rules (they cap the amount)
  const card = (x) => {
    const icon = { pass: "✓", fail: "✕", missing: "?", not_applicable: "–" }[x.result] || "•";
    return `<div class="rule r-${esc(x.result)}"><div class="rule-icon">${icon}</div><div class="rule-body">
      <div class="rule-name">${esc(x.label || x.rule)}</div>
      <div class="rule-meta">Value <b>${esc(fmtRule(x, x.value))}</b> · limit ${esc(fmtRule(x, x.limit))}</div>
      ${ruleBar(x)}</div></div>`;
  };
  const sizing = rules.filter((x) => /FOIR|LTV/i.test(String(x.rule || x.label)));
  const knock = rules.filter((x) => !sizing.includes(x));
  const group = (title, sub, list) => list.length ? `<div class="rule-group"><div class="rule-group-head"><b>${title}</b><span>${sub}</span></div>
    <div class="rules-grid">${list.map(card).join("")}</div></div>` : "";
  $("r-rules").innerHTML = group("Must pass", "a fail here rejects the case", knock)
    + group("Loan sizing", "these cap the amount", sizing);

  // lists
  fillList("r-takes", r.what_it_takes, "r-takes-card");
  // the recommendation lives ONLY in the Recommend panel: its lines are not repeated under Next steps
  fillList("r-next", (r.next_steps || []).filter((t) => !/^(?:Recommend|Or at)/i.test(String(t))), "r-next-card");
  fillList("r-reasons", r.reasons);
  const ex = r.exposure;
  $("r-exposure").innerHTML = ex ? [["Existing on property", inr(ex.existing)], ["Total with request", inr(ex.total_with_request)],
    ["Exposure limit", `${pc(ex.limit_pct)} (${ex.limit_basis || "config"})`], ["Room left", inr(ex.room)]]
    .map(([k, v]) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("") : "";
  $("r-run").textContent = r.run_id ? `Run ${r.run_id}, saved for audit` : "";
}

// the amount counts up to its value (a reduced-motion setting shows it at once)
function countUp(node, target) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || target <= 0) { node.textContent = inr(target); return; }
  const start = performance.now(), dur = 650;
  const step = (t) => {
    const k = Math.min(1, (t - start) / dur), eased = 1 - Math.pow(1 - k, 3);
    node.textContent = inr(target * eased);
    if (k < 1) requestAnimationFrame(step); else node.textContent = inr(target);
  };
  requestAnimationFrame(step);
}

// a bar for a numeric rule: the limit sits at the middle mark, the value on the same scale (capped at 2x the limit)
function ruleBar(rule) {
  const v = Number(rule.value), lim = Number(rule.limit);
  if (rule.value == null || rule.limit == null || isNaN(v) || isNaN(lim) || lim <= 0) return "";
  const pos = Math.max(2, Math.min(100, (v / (lim * 2)) * 100));
  return `<div class="rbar" title="value against the limit"><span class="rbar-fill" data-w="${pos.toFixed(1)}"></span><span class="rbar-mark"></span></div>`;
}

function fmtRule(rule, v) {
  if (v == null || v === "") return "—";
  if (typeof v !== "number") return String(v);
  const code = String(rule.rule || rule.label || "").toUpperCase();
  if (/FOIR|LTV/.test(code)) return pc(v);
  if (/INCOME/.test(code)) return inr(v);
  if (/DPD/.test(code)) return `${v} days`;
  if (/AGE/.test(code)) return `${v} yrs`;
  return String(v);
}

function fillList(id, items, cardId) {
  const list = items || []; $(id).innerHTML = list.map((t) => `<li>${esc(t)}</li>`).join("");
  if (cardId) $(cardId).hidden = !list.length;
}

// THE GAUGE (owner 2026-10-15): a true 0-100% scale -- FOIR's 50% limit sits at the top centre, LTV's 75% exactly at
// three quarters; green up to the limit, red after it; the needle points at the value on the eligible amount, the dot
// at the value on the request (past 100% both pin at the end). Under it, the formula with the case's own figures.
function gauge(code, ratio) {
  const box = el("div", "gauge");
  const name = code === "FOIR" ? "FOIR" : "LTV";
  if (!ratio || ratio.limit == null) {
    box.classList.add("na");
    box.innerHTML = `<div class="gauge-title">${name}</div><div class="gauge-sub">Not applicable for this product</div>`;
    return box;
  }
  const limit = Number(ratio.limit); const onReq = Number(ratio.on_requested ?? 0);
  const hasElig = ratio.on_eligible != null;                    // a rejected case has no eligible amount to measure
  const onElig = hasElig ? Number(ratio.on_eligible) : onReq;
  const frac = (v) => Math.max(0, Math.min(1, v / 100));
  const R = 70, C = Math.PI * R, cx = 90, cy = 96;
  const at = (f, r) => [cx - r * Math.cos(Math.PI * f), cy - r * Math.sin(Math.PI * f)];
  const pt = (f, r) => at(f, r).map((n) => n.toFixed(1)).join(" ");
  const over = onElig > limit, reqOver = onReq > limit;
  const fl = frac(limit);
  const [nx, ny] = at(frac(onElig), R - 16);
  const [rx, ry] = at(frac(onReq), R + 10);
  const [lx, ly] = at(fl, R + 17);                                   // the limit label, just outside its tick
  const anchor = fl < 0.4 ? "start" : fl > 0.6 ? "end" : "middle";
  const sub = code === "FOIR" ? "EMIs ÷ monthly income" : "Loan ÷ property value";
  box.innerHTML = `
    <div class="gauge-title">${name} <span class="gauge-sub">${sub}</span></div>
    <svg viewBox="0 0 180 116" role="img" aria-label="${name} ${pc(onElig)} on the eligible amount, limit ${pc(limit)}">
      <path d="M${pt(0, R)} A${R} ${R} 0 0 1 ${pt(fl, R)}" fill="none" stroke="var(--ok-soft)" stroke-width="12"/>
      <path d="M${pt(fl, R)} A${R} ${R} 0 0 1 ${pt(1, R)}" fill="none" stroke="var(--bad-soft)" stroke-width="12"/>
      <path d="M${pt(0, R)} A${R} ${R} 0 0 1 ${pt(1, R)}" fill="none" stroke="${over ? "var(--bad)" : "var(--ok)"}" stroke-width="12"
        stroke-dasharray="${(C * frac(onElig)).toFixed(1)} ${C.toFixed(1)}"/>
      <line x1="${at(fl, R - 12)[0].toFixed(1)}" y1="${at(fl, R - 12)[1].toFixed(1)}" x2="${at(fl, R + 11)[0].toFixed(1)}" y2="${at(fl, R + 11)[1].toFixed(1)}"
            stroke="var(--text)" stroke-width="3" stroke-linecap="round"/>
      <circle cx="${rx.toFixed(1)}" cy="${ry.toFixed(1)}" r="3.5" fill="${reqOver ? "var(--bad)" : "var(--muted)"}"><title>on the request: ${pc(onReq)}</title></circle>
      <line x1="${cx}" y1="${cy}" x2="${nx.toFixed(1)}" y2="${ny.toFixed(1)}" stroke="var(--text)" stroke-width="3" stroke-linecap="round" class="needle"/>
      <circle cx="${cx}" cy="${cy}" r="5.5" fill="var(--text)"/>
      <text x="${lx.toFixed(1)}" y="${(ly + 3).toFixed(1)}" text-anchor="${anchor}" class="gauge-tick gauge-limit">limit ${pc(limit)}</text>
      <text x="${cx - R}" y="112" text-anchor="middle" class="gauge-tick">0%</text>
      <text x="${cx + R}" y="112" text-anchor="middle" class="gauge-tick">100%</text>
    </svg>
    <div class="gauge-v ${over ? "tone-bad" : "tone-ok"}">${pc(onElig)}</div>
    <div class="gauge-sub">${hasElig ? `on the eligible amount · <span class="${reqOver ? "tone-bad" : "tone-muted"}">${pc(onReq)} on the request</span>` : "on the amount requested"}</div>
    ${formula(code, ratio)}`;
  return box;
}
// the formula and the arithmetic with this case's figures, one line for the offer and one for the request
function formula(code, ratio) {
  const w = ratio.working; if (!w) return "";
  const line = (label, parts, result, over) =>
    `<div class="f-row"><span class="f-label">${label}</span><span class="f-calc">${parts} = <b class="${over ? "tone-bad" : ""}">${pc(result)}</b></span></div>`;
  const limit = Number(ratio.limit);
  // the FULL amount is eligible: one row, not the same arithmetic twice
  const same = w.eligible != null && w.requested != null && Math.round(w.eligible) === Math.round(w.requested);
  if (same && ratio.on_eligible != null) {
    const row = code === "FOIR"
      ? line(`Asked = eligible ${short(w.requested)}`, `(${inr(w.existing_emi)} + ${inr(w.emi_on_eligible)}) ÷ ${inr(w.income)}`, ratio.on_eligible, ratio.on_eligible > limit)
      : line(`Asked = eligible ${short(w.requested)}`, `(${inr(w.requested)} + ${inr(w.existing_on_property)}) ÷ ${inr(w.property_value)}`, ratio.on_eligible, ratio.on_eligible > limit);
    const head = code === "FOIR" ? "FOIR = (existing EMIs + new EMI) ÷ net monthly income" : "LTV = (loan + loans already on the property) ÷ property value";
    return `<div class="formula"><div class="f-head">${head}</div>${row}</div>`;
  }
  if (code === "FOIR") {
    const rows = [];
    if (w.emi_on_eligible != null && ratio.on_eligible != null) rows.push(line(`Eligible ${short(w.eligible)}`, `(${inr(w.existing_emi)} + ${inr(w.emi_on_eligible)}) ÷ ${inr(w.income)}`, ratio.on_eligible, ratio.on_eligible > limit));
    if (w.emi_on_requested != null) rows.push(line(`Asked ${short(w.requested)}`, `(${inr(w.existing_emi)} + ${inr(w.emi_on_requested)}) ÷ ${inr(w.income)}`, ratio.on_requested, ratio.on_requested > limit));
    return `<div class="formula"><div class="f-head">FOIR = (existing EMIs + new EMI) ÷ net monthly income</div>${rows.join("")}</div>`;
  }
  const rows = [];
  if (w.eligible != null && ratio.on_eligible != null) rows.push(line(`Eligible ${short(w.eligible)}`, `(${inr(w.eligible)} + ${inr(w.existing_on_property)}) ÷ ${inr(w.property_value)}`, ratio.on_eligible, ratio.on_eligible > limit));
  if (w.requested != null) rows.push(line(`Asked ${short(w.requested)}`, `(${inr(w.requested)} + ${inr(w.existing_on_property)}) ÷ ${inr(w.property_value)}`, ratio.on_requested, ratio.on_requested > limit));
  return `<div class="formula"><div class="f-head">LTV = (loan + loans already on the property) ÷ property value</div>${rows.join("")}</div>`;
}

function renderReco(reco, final, amount) {
  const btn = $("btn-reco"), pop = $("reco-panel");
  closeReco();
  // nothing to recommend on a rejection / a zero amount: no button at all
  if (!(Number(amount) > 0) || ["REJECTED", "DATA_MISSING"].includes(final.decision)) { btn.hidden = true; pop.innerHTML = ""; return; }
  btn.hidden = false;
  if (!reco || !reco.range) {
    // FULL AMOUNT (approved) or REFER: the engine gives no reduced-amount range -- the offer is the amount itself
    const refer = final.decision === "REFER";
    pop.innerHTML = `
      <div class="pop-head"><span>${refer ? "Offer after credit review" : "Recommended offer"}</span>
        <span class="pop-chips">${final.bucket ? `<span class="chip chip-accent">${esc(final.bucket)}</span>` : ""}
        <span class="chip ${refer ? "" : "chip-ok"}">${refer ? "Refer" : "Full amount"}</span></span></div>
      <div class="pop-amount">${inr(amount)}</div>
      <div class="pop-sub">EMI ${inr(final.emi)} · ${esc(String(final.tenure_months ?? ""))} months</div>
      <p class="pop-text">${refer
        ? "Eligible on the figures, but the case needs a credit review before this amount is offered."
        : "The full amount requested can be offered. Every limit is met."}${final.bucket ? ` Ticket bucket <b>${esc(final.bucket)}</b>.` : ""}</p>`;
    return;
  }
  const longer = reco.longer_tenure;
  pop.innerHTML = `
    <div class="pop-head"><span>Recommended offer</span><span class="chip chip-accent">${esc(reco.bucket || final.bucket || "")}</span></div>
    <div class="pop-amount">${inr(reco.amount ?? amount)}</div>
    <div class="pop-sub">EMI ${inr(reco.emi ?? final.emi)} · ${esc(String(final.tenure_months ?? ""))} months</div>
    <p class="pop-text">Offer within <b>${esc(reco.bucket || "")}</b>${reco.band ? `, inside the product band <b>${esc(reco.band)}</b>` : ""}.
      ${longer ? `Stretching to <b>${longer.tenure_months} months</b> allows up to <b>${inr(longer.eligible)}</b> at EMI ${inr(longer.emi)}.` : ""}</p>`;
}
function toggleReco() {
  const pop = $("reco-panel"), open = pop.hidden;
  pop.hidden = !open; $("btn-reco").setAttribute("aria-expanded", String(open));
}
function closeReco() { $("reco-panel").hidden = true; $("btn-reco").setAttribute("aria-expanded", "false"); }

// ---------------------------------------------------------------- AI summary
// THE SUMMARY IS SLOW ON A CPU MODEL (5-10 s), so it is asked EARLY and REMEMBERED (2026-10-15):
//   - one request per distinct set of inputs, shared: the check, a re-check and the prefetch all use the same one
//   - started together with the check (not after it), and already after a Fetch with the case's own inputs
const explainCache = new Map();
function explanationFor(request) {
  // POST /explain: the same body as /check, NO run saved (the summary never creates a second record)
  const body = explainBody(request);
  if (!explainCache.has(body)) {
    explainCache.set(body, api("/explain", { method: "POST", body })
      .then((r) => r.explanation || null)
      .catch(() => { explainCache.delete(body); return null; }));        // a failure is retried on the next check
    if (explainCache.size > 40) explainCache.delete(explainCache.keys().next().value);
  }
  return explainCache.get(body);
}
let explainSeq = 0;
let explainTick = null;
const explainBody = (request) => { const { detail, explain, ...rest } = request; return JSON.stringify({ ...rest, language: "en" }); };
// THE SUMMARY AS IT IS WRITTEN (POST /explain/stream, NDJSON): words appear within ~1-2 s on CPU, shown muted with a
// cursor while unchecked; the last line is the CHECKED sentence, which replaces them (null -> withdrawn)
async function streamExplanation(body, onDelta) {
  const res = await fetch(API + "/explain/stream", { method: "POST", headers: { "content-type": "application/json" }, body });
  if (!res.ok || !res.body) return null;
  const reader = res.body.getReader(); const decoder = new TextDecoder();
  let buffer = "", final;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim(); buffer = buffer.slice(nl + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if ("delta" in msg) onDelta(msg.delta);
      if ("final" in msg) final = msg.final;
    }
  }
  return final ?? null;
}
async function loadExplanation(request) {
  const seq = ++explainSeq;
  const card = $("r-ai"), text = $("r-ai-text"), skel = $("r-ai-skel"), status = $("r-ai-status");
  card.hidden = false; text.textContent = ""; skel.hidden = false; $("r-ai-facts").hidden = true;
  card.classList.add("loading"); card.classList.remove("done", "failed", "streaming");
  // WHILE IT WRITES: the icon spins and the seconds count, so the wait is visibly alive
  const t0 = performance.now();
  clearInterval(explainTick);
  status.textContent = "Writing summary…";
  explainTick = null;                                          // no seconds shown (owner 2026-10-15: "timing hatao")
  const body = explainBody(request);
  let explanation;
  try {
    if (explainCache.has(body)) {
      explanation = await explainCache.get(body);                      // the prefetch already has it (or is close)
    } else {
      explanation = await streamExplanation(body, (delta) => {
        if (seq !== explainSeq) return;
        skel.hidden = true; card.classList.add("streaming");
        text.textContent += delta;
      });
      if (explanation) explainCache.set(body, Promise.resolve(explanation));
    }
  } catch { explanation = null; }
  if (seq !== explainSeq) return;                                      // a newer check replaced this one
  clearInterval(explainTick);
  card.classList.remove("loading", "streaming"); skel.hidden = true;
  if (explanation) {
    text.innerHTML = formatSummary(explanation); card.classList.add("done");
    renderAiFacts();
    status.textContent = "";
  } else {
    card.classList.add("failed");                                      // model unavailable / its text failed the check
    status.textContent = "";
    text.textContent = "The AI summary is not available right now. The result above is complete.";
    $("r-ai-facts").hidden = true;
  }
}
// THE SUMMARY, EASY TO READ (owner 2026-10-15: "padhne me aacha lage"): the outcome in its colour, every amount and
// percentage in bold -- the checked sentence itself is not changed, only presented
const LEADS = [[/^(Approved for the full)/, "lead-ok"], [/^(Eligible for)/, "lead-warn"], [/^(Not eligible)/, "lead-bad"],
  [/^(Referred for review)/, "lead-warn"], [/^(Cannot be assessed yet)/, "lead-muted"]];
function formatSummary(sentence) {
  let html = esc(sentence).replace(/(₹[\d,]+(?:\.\d+)?|\d+(?:\.\d+)?%)/g, "<b>$1</b>");
  for (const [re, cls] of LEADS) {
    if (re.test(sentence)) { html = html.replace(re, `<span class="${cls}">$1</span>`); break; }
  }
  return html;
}
// THE AI CARD'S KEY FACTS AND HIGHLIGHTS (2026-10-15): chips (offer, EMI, tenure, limit) and up to three short,
// decision-ready points -- affordability, the way to more, watch-outs -- all read from the result itself: no model,
// no wait, no figure the engine did not compute
function renderAiFacts() {
  const r = state.lastResult || {}; const final = r.final || {};
  const chips = [];
  if (final.amount != null) chips.push(["Offer", inr(final.amount)]);
  if (final.emi != null) chips.push(["EMI", inr(final.emi) + "/month"]);
  if (final.tenure_months) chips.push(["Tenure", final.tenure_months + " months"]);
  if (r.limiting_factor) chips.push(["Limited by", pretty(r.limiting_factor).replace("Foir", "FOIR").replace("Ltv", "LTV")]);
  const points = highlights(r);
  const box = $("r-ai-facts");
  box.innerHTML = chips.map(([k, v]) => `<span class="ai-chip"><span>${esc(k)}</span><b>${esc(v)}</b></span>`).join("") +
    (points.length ? `<ul class="ai-points">${points.map(([kind, label, text]) =>
      `<li class="pt-${kind}"><span class="pt-tag">${esc(label)}</span><span class="pt-text">${text}</span></li>`).join("")}</ul>` : "");
  box.hidden = !chips.length && !points.length;
}

// [kind, label, html] -- each built only when the result holds what it needs
function highlights(r) {
  const out = []; const final = r.final || {};
  const foir = (r.ratios || {}).foir || {}; const w = foir.working || {};
  const b = (v) => `<b>${esc(v)}</b>`;
  // 1. AFFORDABILITY: the new EMI against the income, and all EMIs together against the FOIR limit
  if (final.emi != null && w.income > 0) {
    const own = (final.emi / w.income) * 100;
    const all = ((Number(w.existing_emi) || 0) + final.emi) / w.income * 100;
    const existing = Number(w.existing_emi) > 0
      ? `; with existing EMIs of ${b(inr(w.existing_emi))}, ${b(pc(all))} of income` : "";
    out.push(["ok", "Affordability", `EMI ${b(inr(final.emi))} is ${b(pc(own))} of the monthly income ${b(inr(w.income))}` +
      `${existing}${foir.limit != null ? ` (limit ${esc(pc(foir.limit))})` : ""}.`]);
  }
  // 2. THE WAY TO MORE: a longer tenure first (no new documents), else the single lever that lifts the limit
  const longer = (r.recommendation || {}).longer_tenure;
  const take = (r.what_it_takes || [])[0];
  if (longer && longer.eligible > (final.amount || 0)) {
    out.push(["up", longer.covers_request ? "Full amount" : "More possible",
      `Stretching to ${b(longer.tenure_months + " months")} allows up to ${b(inr(longer.eligible))} at EMI ${b(inr(longer.emi))}` +
      `${longer.covers_request ? ", the full request" : ""}.`]);
  } else if (take) {
    out.push(["up", "Full amount", esc(String(take).replace(/\s*→\s*/, ": ").replace(/\.$/, "")) + "."]);
  }
  // 3. WATCH-OUTS: a referral, or what the income / top-up working flagged (never the workbook-correction notes)
  const flags = [];
  if (r.decision === "REFER") flags.push(...(r.reasons || []).filter((x) => /review|borderline|delay|refer/i.test(x)));
  for (const n of [...((r.income || {}).notes || []), ...((r.topup || {}).notes || [])]) {
    if (!/workbook|divided by 12/i.test(n)) flags.push(n);
  }
  if (flags.length) {
    // a RISK (verify, above / below a limit, review, borderline, delay) is a watch-out; an explanation is a note
    const risky = (t) => /verify|above the|below|review|borderline|delay|refer|no top-up/i.test(t);
    const first = flags.find(risky) || flags[0];
    const text = esc(first.charAt(0).toUpperCase() + first.slice(1).replace(/\s*--\s*/g, ", "))
      + (flags.length > 1 ? ` <span class="pt-more">+${flags.length - 1} more</span>` : "");
    out.push(risky(first) ? ["warn", "Watch-out", text] : ["info", "Note", text]);
  }
  return out.slice(0, 3);
}
let prefetchTimer;
function prefetchExplanation() {
  // the case just fetched, with its inputs as shown: the summary is usually ready by the time Check is pressed
  clearTimeout(prefetchTimer);
  prefetchTimer = setTimeout(() => {
    const { body, missing } = entries(true);
    if (state.caseId && !missing.length) explanationFor({ case_id: state.caseId, detail: true, use_saved: false, ...body });
  }, 1200);                                                    // after a pause in typing, not on every key
}

_observer.observe(document.getElementById("result"), { childList: true, subtree: true });
boot();
