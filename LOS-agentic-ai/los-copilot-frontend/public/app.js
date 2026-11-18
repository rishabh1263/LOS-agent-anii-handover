// LOS Copilot -- the page. Everything shown is from the backend (Core.viewResult); every backend value is written with
// textContent, never innerHTML, so nothing the backend returns can run as code. Nothing sensitive is logged.
"use strict";

(function () {
  const C = window.Core;
  const $ = (id) => document.getElementById(id);
  const STAGES = ["FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT"];   // app/config/stage_lifecycle.yaml
  const STAGE_LABEL = { FOS: "Sourcing", CPA: "CPA", CREDIT: "Credit", RCU: "RCU", BOPS: "BOPS", HOPS: "HOPS", DISBURSEMENT: "Disbursement" };
  const TOKEN_KEY = "los-copilot-token";
  const ICONS = {
    ok: '<svg viewBox="0 0 24 24"><path d="M5 12.5 10 17l9-10"/></svg>',
    warn: '<svg viewBox="0 0 24 24"><path d="M12 4 2.8 20h18.4z M12 10v4.5 M12 17.5h.01"/></svg>',
    bad: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v6 M12 16.5h.01"/></svg>',
    info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5 M12 7.5h.01"/></svg>',
    na: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>',
    spark: '<svg viewBox="0 0 24 24"><path d="M12 3v4 M12 17v4 M3 12h4 M17 12h4 M6 6l2.5 2.5 M15.5 15.5 18 18 M6 18l2.5-2.5 M15.5 8.5 18 6"/></svg>',
    user: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 5-6 8-6s6.5 2 8 6"/></svg>',
    db: '<svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6 M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/></svg>',
    rules: '<svg viewBox="0 0 24 24"><path d="M9 6h11 M9 12h11 M9 18h11 M4 6h.01 M4 12h.01 M4 18h.01"/></svg>',
    list: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18 M8 14h8"/></svg>',
    shield: '<svg viewBox="0 0 24 24"><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/></svg>',
  };

  const app = {
    schema: null, form: C.initialState(), submission: null, caseId: null, lastDedupe: null, view: null,
    sort: { key: "status", dir: "asc" }, tab: "all", controller: null, text: {}, openLoans: new Set(),
  };

  // ---------------------------------------------------------------- DOM helpers
  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;                     // ONLY for the static ICONS above
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  const icon = (name, cls) => h("span", { class: cls || "", html: ICONS[name], "aria-hidden": "true" });
  const pill = (tone, text) => h("span", { class: `pill ${tone || "na"}` }, text);
  const label = (code) => String(code || "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());

  function toast(text, tone) {
    const t = h("div", { class: `toast ${tone || ""}`, role: tone === "bad" ? "alert" : "status" }, text);
    $("toasts").append(t);
    setTimeout(() => t.remove(), 5200);
  }

  function setBusy(button, busy) {
    button.classList.toggle("busy", busy);
    button.disabled = busy;
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }

  // ---------------------------------------------------------------- auth + API
  function token() {
    try { return sessionStorage.getItem(TOKEN_KEY) || null; } catch { return null; }
  }

  async function api(path, options = {}) {
    try {
      return await C.request(path, { token: token(), ...options });
    } catch (err) {
      if (err.status === 401) {
        const ok = await signIn();
        if (ok) return C.request(path, { token: token(), ...options });
      }
      throw err;
    }
  }

  function signIn() {
    const dlg = $("signin");
    $("si-error").textContent = "";
    dlg.showModal();
    return new Promise((resolve) => {
      const form = $("signin-form");
      const done = (value) => { form.onsubmit = null; $("si-cancel").onclick = null; dlg.close(); resolve(value); };
      $("si-cancel").onclick = () => done(false);
      form.onsubmit = async (e) => {
        e.preventDefault();
        try {
          const { data } = await C.request("/api/v1/auth/login", { method: "POST", timeoutMs: 15000,
            body: { username: $("si-user").value.trim(), password: $("si-pass").value } });
          try { sessionStorage.setItem(TOKEN_KEY, data.access_token); } catch { /* memory only */ }
          $("si-pass").value = "";
          showWho($("si-user").value.trim());
          done(true);
        } catch (err) {
          $("si-error").textContent = err.status === 404 ? "Sign-in is not available on this backend." : err.message;
        }
      };
    });
  }

  function showWho(name) {
    if (!name) return;
    $("who").hidden = false;
    $("who-text").textContent = name;
  }

  function connection(state, text) {
    $("conn").className = `conn ${state}`;
    $("conn-text").textContent = text;
  }

  // ---------------------------------------------------------------- navigation, theme, stepper
  function go(page) {
    document.querySelectorAll(".page").forEach((p) => p.classList.toggle("is-active", p.dataset.page === page));
    document.querySelectorAll(".nav-item").forEach((b) => {
      const on = b.dataset.page === page;
      b.classList.toggle("is-active", on);
      if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    $("crumb-page").textContent = { apply: "New application", lookup: "Check an existing case", result: "Dedupe & loan history" }[page];
    $("main").focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function stepper(stage) {
    const current = STAGES.indexOf(stage || "FOS");
    const list = $("stepper");
    list.replaceChildren(...STAGES.map((s, i) => h("li", {
      class: i < current ? "done" : i === current ? "current" : "", "aria-current": i === current ? "step" : null,
    }, h("span", { class: "n" }, i < current ? "✓" : String(i + 1)), STAGE_LABEL[s])));
  }

  function topChips(view) {
    const c = $("chip-case"), s = $("chip-stage");
    c.hidden = !app.caseId; c.textContent = app.caseId ? `Case ${app.caseId}` : "";
    const stage = (view && view.stage) || (app.caseId ? "FOS" : null);
    s.hidden = !stage; s.textContent = stage ? `Stage · ${STAGE_LABEL[stage] || stage}` : "";
    stepper(stage || "FOS");
  }

  function initTheme() {
    $("theme").addEventListener("click", () => {
      const root = document.documentElement;
      const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("los-copilot-theme", root.dataset.theme); } catch { /* not saved */ }
    });
  }

  // ---------------------------------------------------------------- the form
  function fieldHint(f) {
    const r = f.rules || {};
    if (r.date && "min_age" in r) return `Applicant aged ${r.min_age}–${r.max_age}.`;
    if ("min" in r && "max" in r) return `Between ${r.min} and ${r.max}.`;
    if (f.name === "mobile") return "10-digit mobile (+91 allowed).";
    if (r.number) return "In rupees. Product limits are checked by the backend.";
    return null;
  }

  /** The Co-applicants card: fields from schema.co_applicants (the backend's own list and rules), up to its max. */
  /** A PAN / mobile box shows the value MASKED while it is not being edited (ui_text.json display_mask), the full
   * value only while the officer is typing in it. The real value lives in the form state, which is what is sent. */
  function maskWhenIdle(input, kind, real) {
    const show = () => {
      const k = typeof kind === "function" ? kind() : kind;
      const v = real() || "";
      input.value = document.activeElement === input || !k ? v : (C.maskShown(k, v, app.text) || "");
    };
    input.addEventListener("focus", () => {
      const v = real() || "";
      if (input.value !== v) { input.value = v; input.select(); }     // typing then replaces it, never appends
    });
    input.addEventListener("blur", show);
    show();
    return show;
  }

  function renderCoApplicants() {
    const sch = C.coApplicantSchema(app.schema);
    const card = $("coapp-card");
    card.hidden = !sch.max;
    if (!sch.max) return;
    const list = $("coapp-list");
    list.replaceChildren(...(app.form.coApplicants || []).map((co, i) => {
      const fields = sch.fields.map((f) => {
        const key = `co_applicants[${i}].${f.name}`;
        const id = `co-${i}-${f.name}`;
        const r = f.rules || {};
        const input = h("input", { id, type: r.date ? "date" : "text", "aria-describedby": `${id}-err`, autocomplete: "off",
          required: f.required || null, maxlength: f.name === "pan" ? 10 : null, spellcheck: "false" });
        input.value = co[f.name] || "";
        input.addEventListener("input", () => {
          if (r.upper) input.value = input.value.toUpperCase();
          app.form = C.setCoApplicantValue(app.form, i, f.name, input.value);
          app.submission = null;
          showFieldError(key, null);
        });
        if (f.name === "pan") maskWhenIdle(input, "PAN", () => ((app.form.coApplicants || [])[i] || {}).pan);
        return h("div", { class: "field", "data-field": key },
          h("label", { for: id }, f.label, f.required ? h("span", { class: "req", "aria-hidden": "true" }, " *") : null),
          input, h("p", { class: "field-error", id: `${id}-err`, role: "alert" }));
      });
      return h("fieldset", { class: "coapp" },
        h("legend", {}, `Co-applicant ${i + 1}`),
        h("div", { class: "fields four" }, ...fields),
        h("button", { type: "button", class: "btn ghost small coapp-remove", "aria-label": `Remove co-applicant ${i + 1}`,
          onclick: () => { app.form = C.removeCoApplicant(app.form, i); app.submission = null; renderCoApplicants(); } }, "Remove"));
    }));
    const add = $("coapp-add");
    add.disabled = (app.form.coApplicants || []).length >= sch.max;
    add.textContent = add.disabled ? `Up to ${sch.max} co-applicant(s)` : "+ Add co-applicant";
  }

  /** The form in the LOS Sourcing screen's order (ui_text.json form_layout): Application Details, Primary Applicant,
   * (Co-applicants), Loan Details. A backend field the layout does not name goes to Loan Details -- never dropped. */
  function renderFields() {
    const layout = (app.text && app.text.form_layout) || {};
    const boxes = { application_details: $("fields-top"), applicant: $("fields-applicant"), loan: $("fields-application") };
    const statics = $("static-controls");
    Object.values(boxes).forEach((b) => {
      b.querySelectorAll("[data-slot]").forEach((node) => statics.append(node));   // keep the page's own controls
      b.replaceChildren();
    });
    const byName = Object.fromEntries(app.schema.fields.map((f) => [f.name, f]));
    const placed = new Set(["tenure_months"]);
    for (const [section, names] of Object.entries(layout)) {
      for (const name of names || []) {
        if (name.startsWith("@")) {
          const node = name === "@tenure_years" ? tenureYearsNode(byName.tenure_months)
            : statics.querySelector(`[data-slot="${name.slice(1)}"]`);
          if (node) boxes[section].append(node);
        } else if (byName[name]) {
          boxes[section].append(fieldNode(byName[name]));
          placed.add(name);
        }
      }
    }
    const rest = layout.loan ? boxes.loan : boxes.application_details;      // never dropped
    for (const f of app.schema.fields) {
      if (!placed.has(f.name)) (f.group === "applicant" && !layout.applicant ? boxes.applicant : rest).append(fieldNode(f));
    }
    $("loan-card").hidden = !boxes.loan.children.length;
    if (!byName.tenure_months) placed.delete("tenure_months");
    renderCoApplicants();
  }

  /** Loan Tenure (Years): a whole-year choice inside the backend's own tenure_months limits; sent as months. */
  function tenureYearsNode(field) {
    if (!field) return null;
    const r = field.rules || {};
    const lo = Math.max(1, Math.ceil((r.min || 12) / 12));
    const hi = Math.max(lo, Math.floor((r.max || 480) / 12));
    const id = "f-tenure_years";
    const select = h("select", { id, "aria-describedby": `${id}-hint f-tenure_months-err` },
      h("option", { value: "" }, "Choose…"), ...Array.from({ length: hi - lo + 1 }, (_, i) => h("option", { value: String(lo + i) }, String(lo + i))));
    const months = Number(app.form.values.tenure_months);
    select.value = months && months % 12 === 0 ? String(months / 12) : "";
    select.addEventListener("change", () => {
      app.form = C.setValue(app.form, "tenure_months", select.value ? String(Number(select.value) * 12) : "");
      app.submission = null;
      showFieldError("tenure_months", null);
    });
    const label = ((app.text && app.text.form_labels) || {}).tenure_years || "Loan Tenure (Years)";
    return h("div", { class: "field", "data-field": "tenure_months" },
      h("label", { for: id }, label), select,
      h("p", { class: "hint", id: `${id}-hint` }, `${lo}–${hi} years.`),
      h("p", { class: "field-error", id: "f-tenure_months-err", role: "alert" }));
  }

  /** A product change re-draws the fields that depend on it (rules.by_product, e.g. property type): shown only where
   * the product uses them, with that product's choices; a value the new product does not offer is cleared. */
  function refreshDependentFields() {
    for (const raw of app.schema.fields) {
      if (!(raw.rules && raw.rules.by_product)) continue;
      const f = C.effectiveField(raw, app.form.values);
      const value = app.form.values[raw.name];
      if (value && (f.hidden || !(f.choices || []).includes(value))) app.form = C.setValue(app.form, raw.name, "");
      const old = document.querySelector(`[data-field="${CSS.escape(raw.name)}"]`);
      if (old) old.replaceWith(fieldNode(raw));
    }
  }

  function fieldNode(raw) {
    {
      const f = C.effectiveField(raw, app.form.values);
      const id = `f-${f.name}`;
      const r = f.rules || {};
      let control;
      if (r.boolean && r.control === "yes_no") {          // Yes / No radios (e.g. Co-origination): none chosen = not sent
        const text = ((app.text && app.text.form_labels) || {})[f.name] || f.label;
        const now = String(app.form.values[f.name] || "").toLowerCase();
        const radio = (val, word) => {
          const input = h("input", { type: "radio", name: id, value: val, id: `${id}-${val}` });
          input.checked = now === val;
          input.addEventListener("change", () => { app.form = C.setValue(app.form, f.name, val); app.submission = null; showFieldError(f.name, null); });
          return h("label", { class: "yn-opt", for: `${id}-${val}` }, input, h("span", {}, word));
        };
        return h("div", { class: "field", "data-field": f.name, role: "radiogroup", "aria-labelledby": `${id}-label` },
          h("span", { class: "label", id: `${id}-label` }, text),
          h("div", { class: "yn" }, radio("true", "Yes"), radio("false", "No")),
          h("p", { class: "field-error", id: `${id}-err`, role: "alert" }));
      }
      if (r.boolean) {                                    // a yes / no checkbox, e.g. "Is it Aadhaar Linked Mobile No"
        const box = h("input", { id, name: f.name, type: "checkbox" });
        box.checked = String(app.form.values[f.name] || "").toLowerCase() === "true";
        if (app.form.values[f.name] === undefined) app.form = C.setValue(app.form, f.name, "false");
        box.addEventListener("change", () => { app.form = C.setValue(app.form, f.name, box.checked ? "true" : "false"); app.submission = null; });
        const text = ((app.text && app.text.form_labels) || {})[f.name] || f.label;
        return h("div", { class: "field check-field", "data-field": f.name }, h("label", { class: "check-label", for: id }, box, h("span", {}, text)),
          h("p", { class: "field-error", id: `${id}-err`, role: "alert" }));
      }
      const listed = ((app.text && app.text.form_choices) || {})[f.name];
      if (Array.isArray(f.choices) && f.choices.length) {
        control = h("select", { id, name: f.name, "aria-describedby": `${id}-hint`, required: f.required || null },
          h("option", { value: "" }, "Choose…"), ...f.choices.map((c) => h("option", { value: c }, label(c))));
      } else if (Array.isArray(listed) && listed.length) {          // a free-text field with a configured list
        control = h("select", { id, name: f.name, "aria-describedby": `${id}-hint`, required: f.required || null },
          h("option", { value: "" }, "Choose…"), ...listed.map((c) => h("option", { value: c }, c)));
      } else {
        control = h("input", {
          id, name: f.name, type: r.date ? "date" : f.name === "email" ? "email" : "text",
          inputmode: r.number || r.integer ? "decimal" : f.name === "mobile" ? "tel" : null,
          maxlength: f.max_length || null, autocomplete: { full_name: "name", mobile: "tel", email: "email", address: "street-address" }[f.name] || "off",
          "aria-describedby": `${id}-hint ${id}-err`, required: f.required || null,
        });
      }
      control.value = app.form.values[f.name] || "";
      control.addEventListener(control.tagName === "SELECT" ? "change" : "input", () => {
        app.form = C.setValue(app.form, f.name, control.value);
        showFieldError(f.name, null);
        app.submission = null;                                         // edited: a new logical request, a new key
        if (f.name === "product") refreshDependentFields();
      });
      control.addEventListener("blur", () => showFieldError(f.name, C.validateField(f, control.value)));
      const hint = f.defaultValue ? `Leave blank for ${label(f.defaultValue)}.` : fieldHint(f);
      const wrap = h("div", { class: `field${f.name === "address" ? " wide" : ""}`, "data-field": f.name, hidden: f.hidden || null },
        h("label", { for: id }, ((app.text && app.text.form_labels) || {})[f.name] || f.label,
          f.required ? h("span", { class: "req", "aria-hidden": "true" }, " *") : null),
        control,
        h("p", { class: "hint", id: `${id}-hint` }, hint || ""),
        h("p", { class: "field-error", id: `${id}-err`, role: "alert" }));
      return wrap;
    }
  }

  function showFieldError(name, message) {
    const wrap = document.querySelector(`[data-field="${CSS.escape(name)}"]`);
    if (wrap) {
      wrap.classList.toggle("invalid", Boolean(message));
      const slot = wrap.querySelector(".field-error");          // a field without an error slot never breaks Submit
      if (slot) slot.textContent = message || "";
      const control = wrap.querySelector("input, select");
      if (control) control.setAttribute("aria-invalid", message ? "true" : "false");
      return;
    }
    const el = $(`err-${name}`);
    if (el) el.textContent = message || "";
  }

  function initCustomerType() {
    const select = $("dedupe-parameter");
    select.replaceChildren(h("option", { value: "" }, "Choose…"),
      ...C.PARAMETER_OPTIONS.map((p) => h("option", { value: p.value }, p.label)));
    Object.keys(C.ALL_FIELDS).forEach((field) => {
      const input = $(`dedupe-all-${field}`);
      if (input) input.addEventListener("input", () => {
        app.form = { ...app.form, allValues: { ...(app.form.allValues || {}), [field]: input.value } };
        app.submission = null;
        showFieldError("dedupeAll", null);
      });
    });
    $("customer-type").addEventListener("change", (e) => setCustomerType(e.target.value));
    select.addEventListener("change", () => {
      app.form = C.setDedupeParameter(app.form, select.value);
      app.submission = null;
      syncDedupe();
    });
    $("dedupe-value").addEventListener("input", (e) => {
      app.form = { ...app.form, dedupeValue: e.target.value };
      app.submission = null;
      showFieldError("dedupeValue", null);
    });
    app.showDedupeValue = maskWhenIdle($("dedupe-value"), dedupeMaskKind, () => app.form.dedupeValue);
  }

  /** Which mask the Dedupe Parameter Value takes: PAN / mobile are masked; a customer ID or loan account is not. */
  function dedupeMaskKind() {
    return { PAN: "PAN", "Mobile No": "MOBILE" }[app.form.dedupeParameter] || null;
  }

  function setCustomerType(type) {
    app.form = C.setCustomerType(app.form, type);
    app.submission = null;
    syncDedupe();
    showFieldError("customerType", null);
  }

  function syncDedupe() {
    const f = app.form;
    $("customer-type").value = f.customerType || "";
    $("w-dedupe-parameter").hidden = f.customerType !== "Existing";
    const p = C.parameter(f.dedupeParameter);
    $("w-dedupe-value").hidden = f.customerType !== "Existing" || Boolean(p && p.all);
    $("w-dedupe-all").hidden = f.customerType !== "Existing" || !(p && p.all);
    Object.keys(C.ALL_FIELDS).forEach((field) => {
      const input = $(`dedupe-all-${field}`);
      if (input) input.value = (f.allValues || {})[field] || "";
    });
    $("dedupe-parameter").value = f.dedupeParameter;
    const value = $("dedupe-value");
    if (app.showDedupeValue) app.showDedupeValue(); else value.value = f.dedupeValue;
    value.disabled = !p;
    value.placeholder = p ? p.hint : "";
    $("dedupe-parameter-hint").textContent = p ? p.hint : "";
    $("dedupe-value-hint").textContent = p ? p.hint : "Choose a parameter first.";
    if (f.customerType !== "Existing") { showFieldError("dedupeParameter", null); showFieldError("dedupeValue", null); }
  }

  function resetForm() {
    app.form = C.initialState();
    app.submission = null;
    renderFields();
    syncDedupe();
    $("form-summary").textContent = "";
  }

  /** One "Fill sample" button: every click fills the whole form with a NEW random, realistic, synthetic applicant
   * drawn from /sample_config.json. If the config is missing the button stays hidden. */
  async function initSamples() {
    let cfg = null;
    try {
      const res = await fetch("/sample_config.json", { cache: "no-store" });
      if (res.ok) cfg = await res.json();
    } catch { /* no config: no button */ }
    if (!cfg || !cfg.names || !cfg.products) return;
    const button = $("sample-fill");
    button.addEventListener("click", () => {
      let s;
      try { s = C.generateSample(cfg, app.schema); } catch { toast("The sample config could not be read.", "bad"); return; }
      app.form = C.applySample(s, app.schema);
      app.submission = null;
      renderFields();
      syncDedupe();
      ["customerType", "dedupeParameter", "dedupeValue"].forEach((k) => showFieldError(k, null));
      $("form-summary").textContent = "";
      toast(`Sample filled: ${s.label}. Click again for another.`);
    });
    button.hidden = false;
    // the same config fills Check an existing case
    const lk = $("lk-sample");
    lk.addEventListener("click", () => {
      const s = C.lookupSample(cfg, app.caseId || lastCase());
      $("lk-case").value = s.caseId;
      $("lk-party").value = s.party;
      $("lk-type").value = s.customerType;
      $("lk-parameter").value = s.parameter;
      $("lk-parameter").dispatchEvent(new Event("change"));
      $("lk-value").value = s.value;
      ["err-lk-case", "err-lk-value"].forEach((id) => { $(id).textContent = ""; });
      toast(s.caseId ? `Sample filled for case ${s.caseId}. Click again for another.`
        : "Sample filled. Open a case on New application first: its case ID is used here.");
    });
    lk.hidden = false;
  }

  /** The last case opened in this browser (a per-viewer convenience; may be unavailable). */
  function rememberCase(caseId) {
    try { localStorage.setItem("los-copilot-last-case", caseId); } catch { /* storage unavailable */ }
  }
  function lastCase() {
    try { return localStorage.getItem("los-copilot-last-case") || ""; } catch { return ""; }
  }

  // ---------------------------------------------------------------- submit: confirm -> open case -> dedupe
  function confirmCreate(body) {
    const list = $("confirm-list");
    const rows = [["Customer", app.form.customerType], ["Applicant", body.applicant.full_name],
      ["Product", label(body.application.product)], ["Amount", C.money(body.application.loan_amount)],
      app.form.customerType === "Existing" ? ["Dedupe by", app.form.dedupeParameter] : null,
      (body.co_applicants || []).length ? ["Co-applicants", body.co_applicants.map((c) => c.full_name).join(", ")] : null].filter(Boolean);
    list.replaceChildren(...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v || "—")]));
    const dlg = $("confirm");
    dlg.showModal();
    return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
  }

  async function onSubmit(e) {
    e.preventDefault();
    const errors = C.validate(app.form, app.schema);
    for (const f of app.schema.fields) showFieldError(f.name, errors[f.name] || null);
    ["customerType", "dedupeParameter", "dedupeValue", "dedupeAll"].forEach((k) => showFieldError(k, errors[k] || null));
    document.querySelectorAll("[data-field^='co_applicants[']").forEach((w) => showFieldError(w.dataset.field, errors[w.dataset.field] || null));
    const count = Object.keys(errors).length;
    $("form-summary").textContent = count ? `${count} field${count > 1 ? "s need" : " needs"} attention.` : "";
    if (count) {
      const first = document.querySelector(".field.invalid input, .field.invalid select") ||
        (errors.customerType ? $("customer-type").querySelector("button") : errors.dedupeParameter ? $("dedupe-parameter") : $("dedupe-value"));
      if (first) first.focus();
      return;
    }
    const body = C.createBody(app.form, app.schema);
    if (!(await confirmCreate(body))) return;
    const button = $("submit");
    setBusy(button, true);
    try {
      app.submission = C.caseSubmission(app.submission, body);
      const { data, headers } = await api("/api/v1/fos/applicants", { method: "POST", body: app.submission.body,
        headers: { "Idempotency-Key": app.submission.key }, retries: 2, timeoutMs: 30000 });
      app.caseId = data.case_id;
      rememberCase(data.case_id);
      toast(headers.get("Idempotent-Replayed") === "true" ? `Case ${data.case_id} was already open.` : `Case ${data.case_id} opened.`);
      topChips(null);
      app.lastDedupe = C.dedupeBody(app.caseId, app.form, app.schema);
      await runDedupe();
    } catch (err) {
      if (err.code === "INVALID_FIELDS" || (err.fields && typeof err.fields === "object")) {
        for (const [name, message] of Object.entries(err.fields || {})) showFieldError(name, String(message));
      }
      // a field the form does not show (e.g. one the form schema does not expose) is still named, never lost
      const onForm = new Set(app.schema.fields.map((f) => f.name));
      const offForm = Object.entries(err.fields || {}).filter(([name]) => !onForm.has(name))
        .map(([name, message]) => `${C.humanise(name)}: ${message}`);
      $("form-summary").textContent = [err.message, ...offForm].join(" ");
      toast(err.code === "IDEMPOTENCY_KEY_REUSED" ? "This form changed after an earlier attempt; submit again." : err.message, "bad");
      if (err.code === "IDEMPOTENCY_KEY_REUSED") app.submission = null;
    } finally {
      setBusy(button, false);
    }
  }

  async function runDedupe() {
    if (!app.lastDedupe) {
      showResultError({ title: "No identifier to check", message: "The applicant has no mobile to check against existing records." });
      return;
    }
    go("result");
    $("nav-result").disabled = false;
    $("result").hidden = true;
    $("result-error").hidden = true;
    $("result-loading").hidden = false;
    if (app.controller) app.controller.abort();
    app.controller = new AbortController();
    try {
      if (app.lastDedupe.mode === "ALL") {
        // EVERY PARAMETER AT ONCE: one call, one combined screen (each row opens its own detailed view)
        const { mode, ...body } = app.lastDedupe;
        const { data } = await api("/api/v1/credit/dedupe/all", { method: "POST", body, timeoutMs: 120000,
          signal: app.controller.signal });
        app.allView = C.allView(data, app.text);
        app.view = null;
        renderAll();
        return;
      }
      app.allView = null;
      const { data } = await api("/api/v1/credit/dedupe", { method: "POST", body: app.lastDedupe, timeoutMs: 45000,
        signal: app.controller.signal });
      app.view = C.viewResult(data, app.text);
      app.sort = { key: "status", dir: "asc" };
      app.tab = "all";
      renderResult();
      topChips(app.view);
      loadReason(app.view);
    } catch (err) {
      if (err.code === "CANCELLED") return;
      showResultError({ title: err.code === "MALFORMED_RESPONSE" ? "The answer could not be read" : "The check could not run",
        message: err.message, ref: err.requestId, retry: err.status !== 403 && err.status !== 422 });
    } finally {
      $("result-loading").hidden = true;
    }
  }

  function showResultError({ title, message, ref, retry }) {
    const box = $("result-error");
    box.replaceChildren(icon("bad", "ico ico-bad"), h("div", {},
      h("h3", {}, title), h("p", {}, message || ""),
      ref ? h("p", { class: "ref" }, `Reference: ${ref}`) : null,
      h("div", { class: "actions" },
        retry ? h("button", { class: "btn primary small", type: "button", onclick: runDedupe }, "Retry check") : null,
        h("button", { class: "btn ghost small", type: "button", onclick: () => go("apply") }, "Back to the form"))));
    box.hidden = false;
    $("result").hidden = true;
  }

  // ---------------------------------------------------------------- existing case
  function initLookup() {
    const select = $("lk-parameter");
    select.replaceChildren(h("option", { value: "" }, "Choose…"),
      ...C.PARAMETER_OPTIONS.map((p) => h("option", { value: p.value }, p.label)));
    select.addEventListener("change", () => {
      const p = C.parameter(select.value);
      $("w-lk-value").hidden = Boolean(p && p.all);
      $("w-lk-all").hidden = !(p && p.all);
      $("lk-value").value = "";
      $("lk-parameter-hint").textContent = p ? p.hint : "";
      $("lk-value").placeholder = p ? p.hint : "";
    });
    $("lookup-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const caseId = $("lk-case").value.trim();
      const p = C.parameter(select.value);
      $("err-lk-case").textContent = caseId ? "" : "Enter the case ID.";
      if (p && p.all) {
        // ALL PARAMETERS: every value optional; PAN / mobile left empty come from the case record
        const raw = Object.fromEntries(Object.keys(C.ALL_FIELDS).map((f) => [f, $(`lk-all-${f}`).value]));
        const { errors, body } = C.allValues(raw);
        $("err-lk-all").textContent = Object.values(errors).join(" ");
        if (!caseId || Object.keys(errors).length) return;
        app.caseId = caseId;
        app.lastDedupe = { case_id: caseId, party_role: $("lk-party").value, mode: "ALL", ...body,
          ...($("lk-party").value === "Applicant" ? { customer_type: $("lk-type").value } : {}) };
        const button = $("lk-submit");
        setBusy(button, true);
        try { await runDedupe(); } finally { setBusy(button, false); }
        return;
      }
      const value = p ? p.normalise($("lk-value").value) : "";
      $("err-lk-value").textContent = !p ? "Choose a parameter and enter its value." : (!p.pattern.test(value) ? `${p.label}: ${p.hint}.` : "");
      if (!caseId || !p || !p.pattern.test(value)) return;
      app.caseId = caseId;
      app.lastDedupe = { case_id: caseId, party_role: $("lk-party").value, dedupe_parameter: p.value, dedupe_parameter_value: value,
        ...($("lk-party").value === "Applicant" ? { customer_type: $("lk-type").value } : {}) };
      const button = $("lk-submit");
      setBusy(button, true);
      try { await runDedupe(); } finally { setBusy(button, false); }
    });
  }

  // ---------------------------------------------------------------- the result
  function renderResult() {
    const v = app.view;
    if (app.openLoansFor !== v.requestId) { closeLoanPanel(false); app.openLoan = null; app.openLoansFor = v.requestId; }
    const root = $("result");
    root.replaceChildren(
      app.allView ? h("button", { class: "btn ghost small back-all", type: "button", onclick: () => renderAll() },
        "← Back to all parameters") : null,
      hero(v), stats(v), h("div", { class: "wide-row" }, copilot(v)), h("div", { class: "eld-row" }, loansPanel(v)),
      // "What matched" across the full width: the field-by-field table needs the room (sir, 2026-10-23)
      h("div", { class: "wide-row match-row" }, customerPanel(v)),
      h("div", { class: "cols" },
        h("div", { class: "stack" }, businessPanel(v), historyChecks(v)),
        h("div", { class: "stack" }, losPanel(v), sourcesPanel(v))),
      h("div", { class: "meta-line" },
        h("span", {}, `Request ${v.requestId}`), h("span", {}, `Policy ${v.policy.policy_id || ""} ${v.policy.policy_version || ""}`),
        v.policy.signed_off ? null : h("span", {}, "Policy not signed off"),
        v.timings.total_ms !== undefined ? h("span", {}, `${Math.round(v.timings.total_ms)} ms`) : null));
    root.hidden = false;
    $("result-error").hidden = true;
  }

  function hero(v) {
    // the headline is the dedupe WORKFLOW recommendation (one decision on the page); the loan-history policy outcome
    // stays visible as a pill. Older answers without a dedupe block keep the history outcome.
    const head = v.headline || v.outcomeView;
    const tone = head.tone;
    return h("section", { class: `hero tone-${tone}`, "aria-labelledby": "h-result" },
      h("div", {},
        h("div", { class: "verdict-kicker" }, `Existing-customer check · ${roleWord(v.checkedParty, "parties")}`),
        v.dedupe ? h("div", { class: `dd-status dd-${v.dedupe.tone}` },
          h("span", { class: "dd-dot", "aria-hidden": "true" }), h("b", {}, v.dedupe.label),
          h("span", { class: "dd-sep" }, "·"), h("span", {}, `Recommended: ${v.dedupe.action}`),
          !v.dedupe.complete && v.dedupe.missing.length ? h("span", { class: "dd-miss" }, `(${v.dedupe.missing.join(", ")})`) : null) : null,
        h("div", { class: "verdict" }, icon(tone === "ok" ? "ok" : tone === "bad" ? "bad" : "warn", "verdict-ico"),
          h("h2", { id: "h-result" }, head.title)),
        h("p", { class: "verdict-note" }, head.note),
        h("div", { class: "pills" },
          pill(v.customer.tone, v.customer.label),
          pill(v.match.code === "CONFIRMED_MATCH" ? "brand" : ["SOURCE_ERROR", "UNAVAILABLE", "MALFORMED"].includes(v.match.code) ? "bad" : "warn", `MIFIN: ${v.match.label}`),
          pill(v.history.tone, `History: ${v.history.label}`),
          v.headline ? pill(v.outcomeView.tone, `History policy: ${v.outcomeView.title}`) : null,
          pill(v.complete ? "ok" : "warn", v.complete ? "All checks evaluated" : "Some checks not evaluated"),
          v.typeCheck ? pill(v.typeCheck.result === "CONSISTENT" ? "ok" : "warn", `Customer type: ${label(v.typeCheck.result)}`) : null),
        v.confirmation.required ? h("div", { class: "hero-confirm" }, confirmBox(v)) : null));
  }

  /** A POSSIBLE match: the officer decides, in two steps (choose, then record). The decision is saved on the case
   * with the officer's name and every later check on this case uses it. */
  function confirmBox(v) {
    const box = h("div", { class: "decide" });
    const ic = v.identityCheck || {};
    const tick = (label, state) => h("span", { class: `idc idc-${String(state || "NOT_COMPARED").toLowerCase()}` },
      state === "MATCH" ? "✓" : state === "MISMATCH" ? "✗" : "–", ` ${label}`);
    const what = C.searchedLabel(v.searched);
    const ask = () => box.replaceChildren(
      h("p", { class: "d-q" }, "Is this the same customer?"),
      h("div", { class: "idcs" }, tick(what, ic.searched_identifier), tick("Name", ic.name), tick("Date of birth", ic.dob)),
      h("p", { class: "hint" }, "Compared with MIFIN's record (✓ same, ✗ different, – not compared). Check the KYC documents too."),
      h("div", { class: "d-actions" },
        h("button", { type: "button", class: "btn primary small", onclick: () => sure("SAME_CUSTOMER") }, "Yes, same customer"),
        h("button", { type: "button", class: "btn ghost small", onclick: () => sure("NOT_SAME_CUSTOMER") }, "No, different person")));
    const sure = (decision) => box.replaceChildren(
      h("p", { class: "d-q" }, decision === "SAME_CUSTOMER"
        ? "Record: this IS the same customer. Their loans will be used for this case."
        : "Record: this is a DIFFERENT person. The case continues as a new customer."),
      h("p", { class: "hint" }, "Saved on the case with your name."),
      h("div", { class: "d-actions" },
        h("button", { type: "button", class: "btn primary small", onclick: (e) => decide(decision, v.confirmation.candidate, e.currentTarget) }, "Record decision"),
        h("button", { type: "button", class: "btn ghost small", onclick: ask }, "Back")));
    ask();
    return box;
  }

  async function decide(decision, candidate, button) {
    if (!app.lastDedupe || !candidate) return;
    setBusy(button, true);
    const plain = { ...app.lastDedupe };
    app.lastDedupe = { ...plain, confirm_match: decision, candidate };
    try {
      await runDedupe();
      toast(decision === "SAME_CUSTOMER" ? "Recorded: same customer." : "Recorded: different person.");
    } finally {
      app.lastDedupe = plain;                                          // a retry never records the decision twice
    }
  }

  /** A role code in the officer's words (ui_text.json roles / parties); unknown codes are humanised, never hidden. */
  function roleWord(code, group = "roles") {
    if (!code) return "";
    return ((app.text && app.text[group]) || {})[code] || ((app.text && app.text.roles) || {})[code] || label(code);
  }

  function stat(k, valueView, sub, iconName) {
    const tone = valueView.tone === "ok" ? "" : valueView.tone;
    return h("div", { class: `stat ${tone}` }, h("div", { class: "k" }, icon(iconName), k),
      h("div", { class: "v" }, valueView.label), sub ? h("div", { class: "s" }, sub) : null);
  }

  function stats(v) {
    if (!v.totals) {
      const why = { NOT_APPLICABLE: "No customer record: nothing to total.", NOT_ATTRIBUTED: "Identity not confirmed: loans are not counted.",
        UNAVAILABLE: "Source unavailable: unknown, not zero.", UNKNOWN: "Loan section missing: unknown, not zero.",
        NO_PREVIOUS_LOANS: "No previous loans." }[v.history.status] || "Unavailable.";
      const na = { label: v.history.status === "NO_PREVIOUS_LOANS" ? "None" : "Unavailable", tone: v.history.status === "NO_PREVIOUS_LOANS" ? "ok" : "na" };
      return h("div", { class: "stats" }, stat("Previous loans", { label: v.history.status === "NO_PREVIOUS_LOANS" ? "0" : "—", tone: na.tone }, why, "list"),
        stat("Live exposure", na, null, "db"), stat("Monthly EMI", na, null, "db"), stat("Worst DPD (12 m)", na, null, "warn"));
    }
    const t = v.totals;
    const missing = (x) => (x.missing && x.missing.length ? `Missing: ${x.missing.join(", ")}` : x.reason ? label(x.reason) : null);
    return h("div", { class: "stats" },
      stat("Previous loans", { label: String(v.history.count), tone: "ok" },
        Object.entries(t.byStatus).map(([s, n]) => `${n} ${label(s).toLowerCase()}`).join(" · "), "list"),
      stat("Live exposure", t.exposure, missing(t.exposure), "db"),
      stat("Monthly EMI", t.emi, missing(t.emi), "db"),
      stat("Worst DPD (12 m)", { label: t.dpd, tone: t.dpd === "Unavailable" ? "na" : "ok" },
        t.conflicts ? `${t.conflicts} conflicting record(s) not counted` : null, "warn"));
  }

  function loansPanel(v) {
    const head = h("div", { class: "panel-head" }, h("div", { class: "t" }, icon("list", "ico ico-brand"),
      h("div", {}, h("h2", {}, "Existing Loan Details"), h("p", {}, `${v.history.count} loan(s) with SBFC · ${v.history.label}`))));
    const who = v.match.ref && v.match.ref !== "—" ? h("div", { class: "cust-strip" },
      h("div", {}, h("span", { class: "k" }, "Customer ID"), h("b", { class: "mono" }, v.match.ref)),
      h("div", {}, h("span", { class: "k" }, "Role in MIFIN"), h("b", {}, v.match.role ? roleWord(v.match.role) : "Not given by MIFIN")),
      h("div", {}, h("span", { class: "k" }, "Checked for"), h("b", {}, roleWord(v.checkedParty, "parties"))),
      h("div", {}, h("span", { class: "k" }, "Match"), h("b", {}, v.match.label))) : null;
    if (!v.loans.length) {
      const text = {
        NO_PREVIOUS_LOANS: ["No previous loans", "MIFIN lists no earlier loan for this customer."],
        NOT_APPLICABLE: ["No earlier customer record", "No SBFC customer was found, so there is no loan history."],
        NOT_ATTRIBUTED: ["Hidden until the customer is confirmed",
          `${v.history.withheld || 0} loan(s) belong to a possible match. Confirm the customer to see them.`],
        UNAVAILABLE: ["Loan history unavailable", "The source could not answer. This is unknown, not zero loans."],
        UNKNOWN: ["Loan history unknown", "MIFIN did not send its loan section. This is unknown, not zero loans."],
      }[v.history.status] || ["No records", ""];
      return h("section", { class: "panel" }, head, summaryBox(v), who, h("div", { class: "empty" }, h("div", { class: "big" }, text[0]), h("div", {}, text[1])));
    }
    // Existing Loan Details: the Loan Existing screen's columns (ui_text.json existing_loan_columns), sources verified
    const all = (app.text && app.text.existing_loan_columns) || [];
    const rows = C.existingLoanRows({ ...v, loans: C.sortLoans(v.loans, "status", "asc") }, app.text);
    const { shown: cols, empty } = C.loanColumns(all, rows, app.text);
    const cell = (c, row) => {
      const val = row[c.key];
      if (val === null || val === undefined || val === "") return h("td", { class: c.num ? "num" : "" }, h("span", { class: "dash", title: `Not available (source: ${c.source})` }, "—"));
      if (c.key === "loan_status") return h("td", {}, pill(row.status_tone, val));
      if (c.key === "pan_match") return h("td", {}, h("span", { class: `mt-res mt-${row.pan_tone}` }, val));
      if (c.key === "cancel_reason") return h("td", { class: "reason" }, h("span", { title: val }, val));
      if (c.key === "match_found") {
        return h("td", {}, h("span", { class: `mf ${val.includes("This loan") ? "mf-this" : "mf-same"}`, title: row.match_found_why || "" },
          val.replace(/^✓ /, "")));
      }
      if (c.key === "product_name") {
        return h("td", { class: "prod" }, h("span", { title: row.product_code && row.product_code !== val ? `MIFIN scheme: ${row.product_code}` : "MIFIN scheme name, as sent" }, val));
      }
      return h("td", { class: `${c.num ? "num" : ""}${c.key === "prospect_code" ? " mono" : ""}` }, String(val));
    };
    // each PR row opens its details (▸ / ▾); which rows are open survives a re-render of the same answer
    // a loan's details open in the side panel (openLoanPanel): the table stays one clean line per loan
    const body = h("tbody", {}, ...rows.map((row, i) => {
      const id = row.prospect_code || `loan-${i}`;
      const open = () => openLoanPanel(row, id);
      const toggle = h("button", { type: "button", class: "pr-toggle", "aria-haspopup": "dialog",
        "aria-label": `Show details of ${row.prospect_code || "this loan"}`,
        onclick: (e) => { e.stopPropagation(); open(); } }, "▸");
      return h("tr", { class: `loan-row${app.openLoan === id ? " pr-selected" : ""}`, "data-loan": id, onclick: open },
        h("td", { class: "pr-arrow" }, toggle), ...cols.map((c) => cell(c, row)));
    }));
    return h("section", { class: "panel" }, head, summaryBox(v), who, commonLine(v),
      h("div", { class: "table-wrap eld" }, h("table", {},
        h("caption", { class: "skip" }, "Existing loan details"),
        h("thead", {}, h("tr", {}, h("th", { scope: "col", class: "pr-arrow" }, h("span", { class: "skip" }, "Details")),
          ...cols.map((c) => h("th", { scope: "col", class: c.num ? "num" : "", title: `Source: ${c.source}` }, c.label)))),
        body)),
      h("p", { class: "eld-note" },
        empty.length ? h("span", {}, h("b", {}, "Not available from the source for these loans: "), `${empty.join(", ")}. `) : null,
        "Click a loan to see its details. — = not available for that loan. AP Code and Applicant Type are in the strip above. Hover a column for its source."));
  }

  /** One loan's details in a side panel from the right (the same 4 cards). Esc, the close button or a click outside
   * closes it; focus returns to the loan's row. */
  function openLoanPanel(row, id) {
    closeLoanPanel(false);
    const t = (app.text && app.text.pr_panel) || {};
    const opener = document.activeElement;
    app.openLoan = id;
    document.querySelectorAll(".eld tr.loan-row").forEach((tr) => tr.classList.toggle("pr-selected", tr.dataset.loan === id));
    const close = h("button", { type: "button", class: "drawer-close", "aria-label": t.close || "Close",
      onclick: () => closeLoanPanel(true) }, "✕");
    const panel = h("aside", { class: "drawer", id: "pr-drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": "pr-drawer-title", tabindex: "-1" },
      h("header", { class: "drawer-head" },
        h("div", {},
          h("div", { class: "drawer-kicker" }, t.title || "Loan details"),
          h("h3", { id: "pr-drawer-title", class: "mono" }, row.prospect_code || "Loan"),
          h("div", { class: "drawer-sub" },
            row.product_name ? h("span", {}, row.product_name) : null,
            row.loan_amount ? h("span", {}, row.loan_amount) : null,
            row.loan_status ? pill(row.status_tone, row.loan_status) : null)),
        close),
      h("div", { class: "drawer-body" }, loanDetailBox(row)));
    const shade = h("div", { class: "drawer-shade", id: "pr-shade", onclick: () => closeLoanPanel(true) });
    panel.addEventListener("keydown", (e) => { if (e.key === "Escape") closeLoanPanel(true); });
    app.panelOpener = opener;
    document.body.append(shade, panel);
    requestAnimationFrame(() => { shade.classList.add("on"); panel.classList.add("on"); });
    close.focus();
  }

  function closeLoanPanel(restoreFocus) {
    const panel = $("pr-drawer");
    const shade = $("pr-shade");
    if (panel) panel.remove();
    if (shade) shade.remove();
    if (restoreFocus) {
      document.querySelectorAll(".eld tr.pr-selected").forEach((tr) => tr.classList.remove("pr-selected"));
      app.openLoan = null;
      if (app.panelOpener && document.contains(app.panelOpener)) app.panelOpener.focus();
    }
  }

  /** One line over the table: is the applicant common to every case, does the PAN match, cancelled with a reason. */
  function commonLine(v) {
    const c = v.commonApplicant;
    if (!c || !c.cases) return null;
    const pan = (((app.text.match_table || {}).checks) || {})[c.pan] || c.pan;
    const tone = { MATCH: "ok", MISMATCH: "bad" }[c.pan] || "na";
    return h("div", { class: "common-line" },
      h("span", {}, h("b", {}, "Common applicant: "), c.same_customer_on_all
        ? `✓ all ${c.cases} case${c.cases > 1 ? "s" : ""} are under one customer${v.match.ref !== "—" ? ` (${v.match.ref})` : ""}` : "not the same customer on every case"),
      h("span", {}, h("b", {}, "PAN: "), h("span", { class: `mt-res mt-${tone}` }, pan), c.pan === "NOT_COMPARED" ? " (no PAN on this case yet)" : ` on all ${c.cases}`),
      c.cancelled_cases ? h("span", {}, h("b", {}, "Cancelled: "), `${c.cancelled_cases} `
        + (c.cancelled_with_reason === c.cancelled_cases ? "(reason given)" : c.cancelled_with_reason
          ? `(reason given for ${c.cancelled_with_reason})` : "(no reason given)")) : null);
  }

  /** One PR's details (Core.loanDetail): label / value pairs; a value the source does not send says so. */
  function loanDetailBox(row) {
    // grouped cards (ui_text.json loan_detail_groups), side by side; a field in no group is still shown, last
    const all = C.loanDetail(row, app.text);
    const groups = (app.text && app.text.loan_detail_groups) || [];
    const used = new Set(groups.flatMap((g) => g.keys));
    const rest = all.filter((d) => !used.has(d.key));
    const value = (d) => (d.value === null ? h("span", { class: "dash" }, d.note || "Not in source")
      : /_match$/.test(d.key) || d.key === "topup" ? h("span", { class: `mt-res mt-${d.tone}` }, d.value)
        : d.tone ? pill(d.tone, d.value) : d.value);
    const card = (title, tone, items) => (items.length ? h("section", { class: `pr-card pr-${tone}` }, h("h5", {}, title),
      h("dl", {}, ...items.map((d) => h("div", { class: "pr-row", title: `Source: ${d.source}` }, h("dt", {}, d.label),
        h("dd", { class: `${d.num ? "num " : ""}${d.key === "ap_code" || d.key === "product_code" ? "mono" : ""}` }, value(d)))))) : null);
    return h("div", { class: "pr-cards" },
      ...groups.map((g) => card(g.title, g.tone || "na", g.keys.map((k) => all.find((d) => d.key === k)).filter(Boolean))),
      card("More", "na", rest));
  }

  /** What the officer entered for this check: the searched identifier, name and DOB (this page's own input). */
  function enteredForCheck() {
    const d = app.lastDedupe || {};
    const vals = app.form.values || {};
    return { value: d.dedupe_parameter_value || app.form.dedupeValue || null,
      name: vals.full_name || null, dob: vals.date_of_birth ? vals.date_of_birth.split("-").reverse().join("-") : null };
  }

  function matchTableBox(v) {
    const t = (app.text && app.text.match_table) || {};
    const rows = C.matchTable(v, enteredForCheck(), app.text);
    if (!rows.length) return null;
    const dash = () => h("span", { class: "dash" }, "—");
    return h("div", { class: "mt" }, h("h4", {}, t.title || "Field by field"),
      h("div", { class: "table-wrap" }, h("table", { class: "mt-table" },
        h("thead", {}, h("tr", {}, ...["Field", "You entered", "MIFIN record", "Result"].map((x) => h("th", { scope: "col" }, x)))),
        h("tbody", {}, ...rows.map((r) => h("tr", { class: `mt-row-${r.tone}` }, h("th", { scope: "row" }, r.field),
          h("td", { class: "mono" }, r.entered || dash()),
          h("td", { class: "mono" }, r.mifin || dash()),
          h("td", {}, h("span", { class: `mt-res mt-${r.tone}` }, r.result))))))),
      t.note ? h("p", { class: "mt-note" }, t.note) : null);
  }

  /** The Dedupe Assessment Summary (core.assessmentSummary): inside the Existing Loan Details section. */
  function summaryBox(v) {
    const sm = v.summary;
    if (!sm) return null;
    // compact: the decision, the facts as chips, one line per party, why / next; the long lines behind "Details"
    const get = (k) => sm.rows.find((x) => x.key === k);
    const rec = get("recommendation");
    const chip = (row, prefix) => (row ? h("span", { class: `das-chip t-${row.tone}` }, prefix ? h("i", {}, `${prefix} `) : null, row.value) : null);
    const conflicts = get("conflicts");
    const parties = sm.rows.filter((x) => x.key === "applicant" || /^co\d*$/.test(x.key));
    const line = (k, label) => (get(k) ? h("div", { class: "das-line" }, h("b", {}, label), h("span", {}, get(k).value)) : null);
    const easy = v.simple || [];
    return h("div", { class: `das das-${rec ? rec.tone : "na"}` },
      h("div", { class: "das-head" }, h("h3", {}, sm.title),
        rec ? h("span", { class: `das-rec t-${rec.tone}` }, h("span", { class: "dd-dot", "aria-hidden": "true" }), rec.value) : null),
      easy.length ? h("ol", { class: "easy" }, ...easy.map((x) => h("li", { class: `easy-${x.key} t-${x.tone}` },
        h("span", { class: "easy-q" }, x.q), h("span", { class: "easy-a" }, x.a)))) : null,
      h("div", { class: "das-chips" },
        chip(get("overall")), chip(get("existing"), "Existing:"), chip(get("duplicate"), "Duplicate:"),
        chip(get("complete"), "Search:"), conflicts && conflicts.tone === "bad" ? chip({ ...conflicts, value: "Conflict found" }) : null),
      parties.length ? h("ul", { class: "das-parties" }, ...parties.map((p) => h("li", {}, h("span", { class: `dd-dot t-${p.tone}`, "aria-hidden": "true" }),
        h("b", {}, p.label.replace(" result", "")), h("span", {}, p.value)))) : null,
      line("reason", "Why"), line("question", "Open question"),      // the next step: point 3 of "Why this recommendation"
      h("details", { class: "das-more" }, h("summary", {}, "Details"),
        h("dl", { class: "das-grid" }, ...sm.rows.filter((x) => ["lookup", "agent", "evidence", "conflicts"].includes(x.key))
          .flatMap((x) => [h("dt", {}, x.label), h("dd", { class: `t-${x.tone}` }, x.value)]))),
      sm.note ? h("p", { class: "das-note" }, icon("info"), sm.note) : null);
  }

  /** ALL PARAMETERS (sir, 2026-10-24): one decision, whether the identifiers agree, a row per parameter (value, where
   * it came from, MIFIN, customer, findings, decision, Details), what was not checked, and R01-R11 combined. */
  function renderAll() {
    const a = app.allView;
    const t = (app.text && app.text.all_parameters) || {};
    const dash = () => h("span", { class: "dash" }, "—");
    const banner = h("section", { class: `hero tone-${a.combined.tone}`, "aria-labelledby": "h-all" },
      h("div", {},
        h("div", { class: "verdict-kicker" }, `${t.title || "All parameters"} · ${a.rows.length} checked`),
        h("div", { class: "verdict" }, icon(a.combined.tone === "ok" ? "ok" : a.combined.tone === "bad" ? "bad" : "warn", "verdict-ico"),
          h("h2", { id: "h-all" }, a.combined.label)),
        h("p", { class: `verdict-note all-consistency c-${a.consistency.tone}` }, a.consistency.text),
        a.combined.reasons.length ? h("div", { class: "pills" }, ...a.combined.reasons.map((r) => pill("warn", r))) : null));
    const table = h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "t" }, icon("user", "ico ico-teal"),
        h("div", {}, h("h2", {}, "Checked parameters"), h("p", {}, "Each identifier, what MIFIN found, and what it means")))),
      h("div", { class: "panel-body" }, h("div", { class: "table-wrap" }, h("table", { class: "mt-table all-table" },
        h("thead", {}, h("tr", {}, ...["Parameter", "Value", "From", "MIFIN", "Customer ID", "Found", "Decision", ""]
          .map((x) => h("th", { scope: "col" }, x)))),
        h("tbody", {},
          ...a.rows.map((r) => h("tr", { class: `mt-row-${r.tone === "ok" ? "ok" : r.tone === "bad" ? "bad" : "na"}` },
            h("th", { scope: "row" }, r.label),
            h("td", { class: "mono" }, r.value || dash()),
            h("td", {}, r.from),
            h("td", {}, r.match),
            h("td", { class: "mono" }, r.customer || dash()),
            h("td", {}, r.found.length || r.findings.length ? [...r.found, ...r.findings].join(", ") : "Nothing"),
            h("td", {}, h("span", { class: `mt-res mt-${r.tone === "ok" ? "ok" : r.tone === "bad" ? "bad" : "warn"}` }, r.decision)),
            h("td", {}, h("button", { class: "btn ghost small", type: "button",
              onclick: () => { app.view = r.view; app.sort = { key: "status", dir: "asc" }; app.tab = "all"; renderResult(); } },
              "Details")))),
          ...a.missing.map((m) => h("tr", { class: "mt-row-na" }, h("th", { scope: "row" }, m),
            h("td", { colspan: "7" }, h("span", { class: "mt-res mt-na" }, t.missing || "Not checked")))))))),
      a.consistency.byParameter.length > 1 ? h("p", { class: "mt-note" }, a.consistency.byParameter.join(" · ")) : null);
    const root = $("result");
    root.replaceChildren(banner, h("div", { class: "wide-row match-row" }, table),
      h("div", { class: "cols" }, h("div", { class: "stack" }, businessPanel({ business: a.business })), h("div", {})),
      h("div", { class: "meta-line" }, h("span", {}, `Request ${a.requestId}`)));
    root.hidden = false;
    $("result-loading").hidden = true;
    root.scrollIntoView({ block: "start" });
  }

  /** The Dedupe rules' display order by colour: found (red), partly / not checked (yellow), clear (green). */
  const RULE_ORDER = { fail: 0, partial: 1, skip: 1, pass: 2 };

  /** R01-R11: the business's own rule list, traffic-light, one line each (core.businessRules). */
  function businessPanel(v) {
    const count = (t) => v.business.filter((b) => b.tone === t).length;
    const mark = { pass: "ok", fail: "bad", skip: "warn", partial: "warn", na: "info" };
    const unavailable = v.business.filter((b) => b.tone === "na");
    const rule = (b) => h("li", { class: `check c-${b.tone}` },
      h("span", { class: "light", "aria-hidden": "true" }, icon(mark[b.tone] || "info")),
      h("div", { class: "c-body" },
        h("div", { class: "c-title" }, h("span", { class: "rid" }, b.id), b.name),
        h("div", { class: "c-what" }, b.what),
        h("div", { class: "c-reason" }, b.reason),
        ...b.next.map((n) => h("div", { class: "c-act" }, `Next: ${n}`))),
      h("span", { class: `c-status s-${b.tone}` }, b.status));
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "t" }, icon("shield", "ico ico-violet"),
        h("div", {}, h("h2", {}, "Dedupe rules"), h("p", {},
          `${count("fail")} found · ${count("pass")} clear · ${count("partial") + count("skip")} not fully checked · ${count("na")} not available`)))),
      // red first, then yellow, then green (sir, 2026-10-23); the R-order is kept inside each colour
      h("ul", { class: "checks brules" }, ...v.business.filter((b) => b.tone !== "na")
        .map((b, i) => [b, i]).sort(([a, i], [b, j]) => ((RULE_ORDER[a.tone] ?? 9) - (RULE_ORDER[b.tone] ?? 9)) || i - j)
        .map(([b]) => rule(b))),
      // rules that cannot run yet (their system is not connected): never hidden, folded into one line
      unavailable.length ? h("details", { class: "brules-na" },
        h("summary", {}, icon("info"), h("b", {}, `${unavailable.length} rule${unavailable.length > 1 ? "s" : ""} can't run yet`),
          h("span", {}, ` — ${unavailable.map((b) => b.id).join(", ")}: their system is not connected. Click to see why.`)),
        h("ul", { class: "checks brules" }, ...unavailable.map(rule))) : null);
  }

  function rulesPanel(v) {
    const by = (tone) => v.rules.filter((r) => r.view.tone === tone);
    const groups = { all: v.rules, fail: by("fail"), skip: by("skip"), pass: by("pass") };
    const names = { all: "All", fail: "Failed", skip: "Not checked", pass: "Passed" };
    const tabs = h("div", { class: "tabs light", role: "tablist", "aria-label": "Filter checks" },
      ...Object.keys(groups).map((k) => h("button", { role: "tab", type: "button", class: `t-${k}`, "aria-selected": String(app.tab === k),
        onclick: () => { app.tab = k; renderResult(); } }, h("span", { class: "dot", "aria-hidden": "true" }), `${names[k]} ${groups[k].length}`)));
    const mark = { pass: "ok", fail: "bad", skip: "warn" };
    // the All tab: one row per kind of "not checked" (limits not set / waiting for data), not one row per check
    let list = groups[app.tab] || groups.all;
    const grouped = [];
    if (app.tab === "all") {
      const waiting = (reason) => groups.skip.filter((r) => (reason === "THRESHOLD_UNCONFIGURED") === (r.reason === "THRESHOLD_UNCONFIGURED"));
      for (const reason of ["DATA", "THRESHOLD_UNCONFIGURED"]) {
        const rows = waiting(reason);
        if (!rows.length) continue;
        grouped.push(h("li", { class: "check c-skip group" },
          h("span", { class: "light", "aria-hidden": "true" }, icon("warn")),
          h("div", { class: "c-body" },
            h("div", { class: "c-title" }, reason === "THRESHOLD_UNCONFIGURED"
              ? `${rows.length} limit check(s) not run yet` : v.confirmation.required
                ? `${rows.length} check(s) will run once you confirm the customer` : `${rows.length} check(s) could not run`),
            h("div", { class: "c-what" }, reason === "THRESHOLD_UNCONFIGURED"
              ? "The business has not set these limits yet." : v.confirmation.required
                ? "They need the customer's loan history, which is shown only after the match is confirmed."
                : "The data they need is not available or not verified."),
            h("div", { class: "chips" }, ...rows.map((r) => h("span", { class: "chip-s" }, r.view.title)))),
          h("span", { class: "c-status s-skip" }, reason === "THRESHOLD_UNCONFIGURED"
            ? ((((app.text || {}).status || {}).LIMIT_NOT_SET || {}).label || "Limit not set") : "Not checked")));
      }
      list = list.filter((r) => r.view.tone !== "skip");
    }
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "t" }, icon("rules", "ico ico-violet"),
        h("div", {}, h("h2", {}, "Loan history checks"), h("p", {}, checkCounts(v)))), tabs),
      list.length || grouped.length ? h("ul", { class: "checks" }, ...list.map((r) => h("li", { class: `check c-${r.view.tone}` },
        h("span", { class: "light", "aria-hidden": "true" }, icon(mark[r.view.tone] || "info")),
        h("div", { class: "c-body" },
          h("div", { class: "c-title" }, r.view.title),
          r.view.what ? h("div", { class: "c-what" }, r.view.what) : null,
          h("div", { class: "c-reason" }, r.view.reason),
          r.view.tone === "fail" && r.nextAction ? h("div", { class: "c-act" }, `Next: ${r.nextAction}`) : null),
        h("span", { class: `c-status s-${r.view.tone}` }, r.view.status))), ...grouped)
        : h("div", { class: "empty" }, "No checks in this group."));
  }

  /** Why this recommendation: 3 points at once (from the verified result); the model's wording replaces them when it
   * arrives (loadReason) -- the page never waits for the model. The full summary stays one click away. */
  function copilot(v) {
    const e = v.explanation;
    const t = (app.text && app.text.reason) || {};
    const fromLlm = e.pointsSource === "LLM" || (!e.points.length && e.source === "LLM");
    const points = e.points.length ? e.points : sentences(e.text).slice(0, 3);
    return h("section", { class: "copilot", id: "copilot", "aria-labelledby": "h-copilot" }, h("div", { class: "copilot-in" },
      h("div", { class: "copilot-head" }, h("div", { class: "t" }, icon("spark", "spark"), h("h2", { id: "h-copilot" }, t.title || "Why this recommendation")),
        e.pending ? h("span", { class: "reason-wait" }, h("span", { class: "spinner", "aria-hidden": "true" }), t.loading || "Getting the AI's wording…")
          : pill(fromLlm ? "brand" : "na", fromLlm ? (t.llm || "AI-worded, checked") : (t.standard || "From the verified result"))),
      points.length ? h("ol", { class: "reason-points" }, ...points.map((x) => h("li", {}, x)))
        : h("p", { class: "text" }, "No summary available."),
      e.text ? h("details", { class: "reason-more" }, h("summary", {}, t.more || "Full summary"),
        h("ul", { class: "copilot-points" }, ...sentences(e.text).map((x) => h("li", {}, x)))) : null));
  }

  /** The model's 3 points for this check, after the page is shown; a newer check or an error leaves the page as is. */
  async function loadReason(view) {
    if (!view.explanation.pending || !/^ddp_[0-9a-f]{32}$/.test(view.requestId || "")) return;
    let answer = null;
    try {
      ({ data: answer } = await api(`/api/v1/credit/dedupe/${view.requestId}/reason`, { timeoutMs: 60000 }));
    } catch (err) {
      answer = null;                                        // the deterministic points stay
    }
    if (!app.view || app.view.requestId !== view.requestId) return;
    app.view = C.applyReason(app.view, answer);
    const old = $("copilot");
    if (old) old.replaceWith(copilot(app.view));
  }

  /** The summary as points: split on sentence ends (". " followed by a capital), keeping masked refs like AP******18. */
  function sentences(text) {
    return String(text).split(/(?<=\.)\s+(?=[A-Z0-9(])/).map((t) => t.trim()).filter(Boolean);
  }

  /** The 10 loan-history checks: collapsed to one line (R01-R11 already carry them); open for the detail / audit. */
  /** "6 passed · 0 failed · 4 limit not set": a check waiting for a business limit is named as such. */
  function checkCounts(v) {
    const n = (t) => v.rules.filter((r) => r.view.tone === t).length;
    const limits = v.rules.filter((r) => r.view.tone === "skip" && r.reason === "THRESHOLD_UNCONFIGURED").length;
    const other = n("skip") - limits;
    return [`${n("pass")} passed`, `${n("fail")} failed`, limits ? `${limits} limit not set` : null,
      other ? `${other} not checked` : null].filter(Boolean).join(" · ");
  }

  function historyChecks(v) {
    const n = (t) => v.rules.filter((r) => r.view.tone === t).length;
    const box = h("details", { class: "panel hist-checks" },
      h("summary", {}, h("span", { class: "t" }, icon("rules", "ico ico-violet"),
        h("span", {}, h("b", {}, "Loan history checks"),
          h("small", {}, `${checkCounts(v)} — click to see each check`)))));
    box.append(rulesPanel(v));
    if (n("fail")) box.open = true;                        // a failed check is never hidden behind a click
    return box;
  }

  function customerPanel(v) {
    const iconFor = { ok: "ok", brand: "user", warn: "warn", bad: "bad", na: "info" };
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "t" }, icon("user", "ico ico-teal"),
        h("div", {}, h("h2", {}, "What matched"), h("p", {}, `Searched by ${C.searchedLabel(v.searched)}, in MIFIN and the LOS`)))),
      h("div", { class: "panel-body" }, h("ul", { class: "matched" },
        ...v.matched.map((m) => h("li", { class: `m-${m.tone}` }, icon(iconFor[m.tone] || "info"), h("span", {}, m.text)))),
        matchTableBox(v),
        // a match on other LOS cases: one line with the counts (by role, by product) -- not a list of case IDs
        v.otherCases.length ? h("p", { class: "match-line" }, icon("warn"), C.otherCasesLine(v, app.text, roleWord, label)) : null));
  }

  function losPanel(v) {
    const items = [];
    const recs = (v.los.recommendations || []).filter((r) => !["CONTINUE_STANDARD_ONBOARDING", "LINK_EXISTING_CUSTOMER", "PARTIAL_ANALYSIS", "DATA_UNAVAILABLE"].includes(r));
    recs.forEach((r) => items.push(h("li", {}, icon("warn"), label(r))));
    if (v.los.documentsReused) items.push(h("li", {}, icon("warn"), `${v.los.documentsReused} file(s) also on another customer's case`));
    if (!items.length) items.push(h("li", { class: "ok" }, icon("ok"), v.los.status === "INSUFFICIENT_DATA"
      ? "No PAN or mobile on this case's records yet: the LOS check has little to compare." : "No issue in the LOS's own records."));
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "t" }, icon("shield", "ico ico-warn"), h("div", {}, h("h2", {}, "LOS records"), h("p", {}, "Role swaps, file reuse, parallel applications")))),
      h("div", { class: "panel-body" }, h("ul", { class: "findings" }, ...items)));
  }

  function sourcesPanel(v) {
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "t" }, icon("db", "ico ico-brand"), h("div", {}, h("h2", {}, "Sources"), h("p", {}, "What answered, and how completely")))),
      h("div", { class: "panel-body" }, h("ul", { class: "sources" }, ...v.sources.map((s) => {
        const line = C.sourceLine(s, app.text);
        return h("li", {}, h("span", {}, line.name), pill(line.tone, line.state));
      }))));
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    try {                                                           // officer-facing words: optional, never blocking
      const res = await fetch("/ui_text.json", { cache: "no-store" });
      if (res.ok) app.text = await res.json();
    } catch { /* the backend's own words are used */ }
    initTheme();
    stepper("FOS");
    document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => !b.disabled && go(b.dataset.page)));
    initCustomerType();
    initLookup();
    $("apply-form").addEventListener("submit", onSubmit);
    $("coapp-add").addEventListener("click", () => {
      app.form = C.addCoApplicant(app.form, app.schema);
      app.submission = null;
      renderCoApplicants();
      const inputs = $("coapp-list").querySelectorAll("fieldset:last-child input");
      if (inputs[0]) inputs[0].focus();
    });
    $("reset").addEventListener("click", resetForm);
    try {
      const { data } = await api("/api/v1/fos/form-schema", { retries: 2, timeoutMs: 15000 });
      if (!data || !Array.isArray(data.fields)) throw new C.ApiError(0, "MALFORMED_RESPONSE", "The form definition could not be read.");
      app.schema = C.formSchema(data, app.text);          // only the fields the LOS Sourcing screen shows
      renderFields();
      syncDedupe();
      $("form-loading").hidden = true;
      $("apply-form").hidden = false;
      initSamples();
      connection("ok", "Backend connected");
    } catch (err) {
      $("form-loading").hidden = true;
      connection("bad", "Backend unreachable");
      const box = $("form-error");
      box.replaceChildren(icon("bad", "ico ico-bad"), h("div", {}, h("h3", {}, "The form could not be loaded"),
        h("p", {}, err.message), h("div", { class: "actions" }, h("button", { class: "btn primary small", type: "button", onclick: () => location.reload() }, "Try again"))));
      box.hidden = false;
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
