// Credit Document Desk. Every status comes from the backend; this page decides nothing.
//   POST /api/v1/credit/document-readiness/{case_id}/rules
//     -> case, answer (summary.route, summary.people), rules + sections, documents (rows x people)
"use strict";

const API = "/api/v1/credit/document-readiness";
const $ = (id) => document.getElementById(id);
const state = { board: null, rule: null, loading: false, filter: null, checkedAt: null };
const RECENT_KEY = "docs-credit-recent";
const FILTER_WORD = { bad: "Critical", approval: "Approval", warn: "To collect", ok: "Verified" };
const FILTER_EMPTY = { bad: "Nothing critical", approval: "Nothing awaiting approval", warn: "Nothing to collect", ok: "Nothing verified yet" };
// which banner count a document belongs to: an exception awaiting approval is its own group, not "to collect"
const group = (cell, look) => (cell.state === "AWAITING_APPROVAL" ? "approval" : look[0]);

const ROLES = { PRIMARY_APPLICANT: "Applicant", CO_APPLICANT: "Co-applicant" };
const ROUTE_TONE = { MOVE_AHEAD: ["ok", "✓"], MOVE_AHEAD_WITH_NOTES: ["ok", "✓"], REFER_TO_AUTHORITY: ["bad", "↑"], AWAIT_APPROVAL: ["warn", "…"] };
const RULE_TONE = { PASS: "ok", WARN: "warn", FAIL: "bad", "N/A": "na", OFF: "na" };
const RULE_WORD = { PASS: "Pass", WARN: "To collect", FAIL: "Critical", "N/A": "Not applicable", OFF: "Switched off" };
const DONE = new Set(["SATISFIED", "WAIVED"]);
const OPEN_WORD = {
  PENDING_VERIFICATION: "Being verified", UNDER_REVIEW: "With reviewer", EXCEPTION_PENDING: "Awaiting approval",
  MISMATCH: "Mismatch", UNRESOLVED: "Can't check", ACTION_REQUIRED: "Action needed",
};

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null || v === "" || isNaN(Number(v)) ? "—" : "₹" + Number(v).toLocaleString("en-IN"));
const words = (v) => String(v || "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
const initials = (name) => String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
const tenth = (n, of) => (of ? Math.round((n / of) * 10) : 0);              // width class w0..w10 (CSP: no inline styles)
// a whole sentence: cut at a full stop only ("…gave the ITR; it needs its ITR computation." stays whole)
const firstSentence = (t) => { const s = String(t || ""); const i = s.search(/\.\s/); return i > 0 ? s.slice(0, i + 1) : s; };
const rest = (t) => { const s = String(t || ""); const i = s.search(/[.;:]\s+(?=[A-Z(])/); return i > 0 ? s.slice(i + 1).trim() : ""; };

function personName(role) {
  const c = state.board.case;
  return role === "PRIMARY_APPLICANT" ? c.applicant_name : role === "CO_APPLICANT" ? c.co_applicant_name : null;
}

// ---------------------------------------------------------------------------------------------------- api
async function api(path, opts = {}) {
  const res = await fetch(API + path, { headers: { "content-type": "application/json" }, cache: "no-store", ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail || {};
    const known = {
      403: "No case with this ID, or you are not allowed to see it.", 404: "No case with this ID.",
      422: d.message || "This case cannot be checked: " + (d.code || "invalid request"),
      502: "The LOS backend is not reachable.", 504: "The LOS backend took too long to answer.",
    };
    throw new Error(known[res.status] || d.message || d.error || `Request failed (${res.status})`);
  }
  return body;
}

function toast(message) {
  const t = $("toast");
  t.textContent = message;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 4200);
}

// ---------------------------------------------------------------------------------------------------- one document of one person
/** tone (ok / warn / bad), icon, short status line for a checklist cell -- from the backend's state + severity */
function docLook(c) {
  if (c.state === "OK") return ["ok", "✓", c.file ? `Verified · ${c.file}` : "Verified"];
  if (c.state === "WAIVED") return ["ok", "✓", "Waived by an approved exception"];
  if (c.state === "AWAITING_APPROVAL") return ["warn", "…", "Exception awaiting approval"];
  const tone = c.blocking ? "bad" : "warn";
  const issue = c.reasons.length > 1 ? firstSentence(c.reasons[1]) : firstSentence(c.reasons[0]);
  const line = {
    MISSING: "Missing · ask the customer", REPLACE: c.blocking ? "Rejected · a new one is needed" : "Needs a fresh copy",
    ISSUE: issue, MINOR: issue, IN_REVIEW: "Being verified", CHECK: "Could not be checked · re-check",
  }[c.state] || words(c.state);
  const icon = { MISSING: "✕", REPLACE: "↻", IN_REVIEW: "…" }[c.state] || "!";
  return [tone, icon, line];
}

function docsFor(role) {
  const order = { bad: 0, warn: 1, ok: 2 };
  return state.board.documents.rows
    .filter((r) => r.cells[role])
    .map((r) => ({ row: r, cell: r.cells[role], look: docLook(r.cells[role]) }))
    .sort((a, b) => order[a.look[0]] - order[b.look[0]]);
}

// ---------------------------------------------------------------------------------------------------- header + banner
function renderHead() {
  const { case: c, answer: a } = state.board;
  $("case-head").innerHTML = `
    <div class="grow">
      <div class="names"><h1>${esc(c.applicant_name || c.case_id)}</h1>${c.co_applicant_name ? `<span class="co">&amp; ${esc(c.co_applicant_name)}</span>` : ""}</div>
      <div class="meta">
        <button class="id copy" type="button" id="copy-id" title="Copy case ID">${esc(c.case_id)}<span aria-hidden="true">⧉</span></button>
        <span>Loan <b>${money(c.loan_amount)}</b></span>
        <span>Stage <b>${esc(a.stage || "—")}</b></span>
        <span>${esc(words(a.product))}</span>
      </div>
    </div>
    <div class="head-right">
      <div class="policy">Policy ${esc(a.policy_version)}${a.audit.policy_status !== "APPROVED" ? `<span class="sample">${esc(a.audit.policy_status)}</span>` : ""}
        <div class="checked">Checked ${esc(clock(state.checkedAt))}</div></div>
      <button class="ghost-btn" type="button" id="recheck" title="Check this case again">↻ Re-check</button>
    </div>`;
  $("copy-id").addEventListener("click", () => copyText(c.case_id, "Case ID copied"));
  $("recheck").addEventListener("click", () => openCase(c.case_id, { keepFilter: true }));
}

function renderBanner() {
  const { answer: a, documents: d } = state.board;
  const route = a.summary.route;
  const [tone, icon] = ROUTE_TONE[route.decision] || ["na", "•"];
  const verified = d.rows.reduce((n, r) => n + Object.values(r.cells).filter((c) => docLook(c)[0] === "ok").length, 0);
  const approval = a.requirements.filter((q) => q.status === "EXCEPTION_PENDING").length;
  const counts = [["bad", route.critical], ...(approval ? [["approval", approval]] : []), ["warn", route.non_critical], ["ok", verified]];
  const allowed = a.credit_underwriting_allowed;
  $("banner").className = `banner fade-in ${tone}`;
  $("banner").innerHTML = `
    <div class="ico">${icon}</div>
    <div class="grow"><div class="label">${esc(route.label)}</div><div class="text">${esc(route.text)}</div></div>
    <div class="banner-right">
      <div class="counts" role="group" aria-label="Show only">
        ${counts.map(([t, n]) => `
          <button type="button" class="count ${t} ${state.filter === t ? "on" : ""}" data-filter="${t}" aria-pressed="${state.filter === t}"
            title="${state.filter === t ? "Show every document" : `Show only ${FILTER_WORD[t].toLowerCase()} documents`}"><b>${n}</b><span>${FILTER_WORD[t]}</span></button>`).join("")}
      </div>
      <div class="gate ${allowed ? "yes" : "no"}">${allowed ? "🔓 Can start" : "🔒 Not yet"}<small>Underwriting</small></div>
    </div>`;
  $("banner").querySelectorAll(".count").forEach((b) => b.addEventListener("click", () => {
    state.filter = state.filter === b.dataset.filter ? null : b.dataset.filter;
    renderBanner();
    renderPeople();
  }));
}

// ---------------------------------------------------------------------------------------------------- people
function renderPeople() {
  const { documents: d, answer: a } = state.board;
  const people = d.people.length ? d.people : ["PRIMARY_APPLICANT"];
  const host = $("people");
  host.className = "people" + (people.length === 1 ? " solo" : "");
  host.innerHTML = people.map((role) => {
    const docs = docsFor(role);
    const n = (t) => docs.filter((x) => x.look[0] === t).length;
    docs.forEach((x) => { x.group = group(x.cell, x.look); });
    const total = docs.length || 1;
    const p = (a.summary.people || {})[role] || {};
    const job = role === "CO_APPLICANT" ? state.board.case.co_applicant_employment_type : state.board.case.employment_type;
    const bar = ["ok", "warn", "bad"].map((t) => `<i class="${t} w${tenth(n(t), total)}"></i>`).join("");
    const shown = state.filter ? docs.filter((x) => x.group === state.filter) : docs;
    return `
      <article class="person fade-in ${role === "CO_APPLICANT" ? "co" : ""}">
        <div class="p-head">
          <div class="avatar" aria-hidden="true">${esc(initials(p.name || personName(role)))}</div>
          <div class="grow"><div class="p-role">${esc(ROLES[role] || words(role))}</div><div class="p-name">${esc(p.name || personName(role) || "—")}</div>
            <div class="job ${job ? "" : "none"}">${esc(words(job) || "Employment not captured")}</div></div>
          <div class="p-prog"><span class="n">${n("ok")} of ${docs.length} verified</span><span class="bar">${bar}</span></div>
        </div>
        ${state.filter ? `<div class="filter-note"><span>${shown.length} ${FILTER_WORD[state.filter].toLowerCase()} of ${docs.length}</span>
          <button type="button" class="link clear-filter">Show all</button></div>` : ""}
        <div class="docs">${shown.map(({ row, cell, look }) => `
          <button class="doc ${look[0]}" data-slot="${esc(row.slot)}" data-role="${role}">
            <span class="st">${look[1]}</span>
            <span class="grow"><div class="t">${esc(row.label)}</div><div class="s">${esc(look[2])}</div></span>
            ${look[0] === "bad" ? `<span class="tag bad">Critical</span>`
              : cell.state === "AWAITING_APPROVAL" ? `<span class="tag approval">Approval</span>`
              : look[0] === "warn" ? `<span class="tag warn">To collect</span>` : ""}
          </button>`).join("") || `<div class="empty-note">${docs.length ? `${FILTER_EMPTY[state.filter]} for this person.` : "No document requirements for this person."}</div>`}
        </div>
      </article>`;
  }).join("");
  host.querySelectorAll(".doc").forEach((b) => b.addEventListener("click", () => openDoc(b.dataset.slot, b.dataset.role)));
  host.querySelectorAll(".clear-filter").forEach((b) => b.addEventListener("click", () => { state.filter = null; renderBanner(); renderPeople(); }));
}

// ---------------------------------------------------------------------------------------------------- brief
function renderBrief() {
  const a = state.board.answer;
  const s = a.summary;
  const who = (q) => (q.party_role ? `${personName(q.party_role) || ROLES[q.party_role]}: ` : "");
  const critical = a.requirements.filter((q) => q.severity === "BLOCKING" && q.status !== "EXCEPTION_PENDING");
  const approval = a.requirements.filter((q) => q.status === "EXCEPTION_PENDING");
  const collect = a.requirements.filter((q) => q.severity !== "BLOCKING");
  const list = (items, tone) => items.slice(0, 6).map((q) => {
    const at = slotOf(q);
    const text = `<i class="dot ${tone}"></i><span>${esc(who(q) + firstSentence(q.reason))}</span>`;
    return at ? `<button type="button" class="mini go" data-slot="${esc(at.slot)}" data-role="${at.role}" title="Open this document">${text}<span class="arrow" aria-hidden="true">›</span></button>`
      : `<div class="mini">${text}</div>`;
  }).join("")
    + (items.length > 6 ? `<div class="mini"><span>+ ${items.length - 6} more</span></div>` : "");
  const ai = s.ai_summary.source === "ai";
  $("brief").innerHTML = `
    <div class="card memo fade-in">
      <div class="memo-head"><h3>Underwriter brief</h3>
        <span class="memo-tools"><button type="button" class="link" id="copy-brief" title="Copy the brief as text">Copy</button>
        <span class="src ${ai ? "" : "rules"}" title="${ai ? "Written by the local model, checked against the facts" : "Built from the rule results"}">${ai ? "AI" : "RULES"}</span></span></div>
      <div class="memo-scroll">
        <p class="memo-text">${esc(s.ai_summary.text || s.headline)}</p>
        ${critical.length ? `<div class="list-title">Critical</div>${list(critical, "bad")}` : ""}
        ${approval.length ? `<div class="list-title">Awaiting approval</div>${list(approval, "approval")}` : ""}
        ${collect.length ? `<div class="list-title">To collect</div>${list(collect, "warn")}` : ""}
      </div>
    </div>
    <div class="next fade-in"><span class="k">Next step</span><span>${esc(s.next_step)}</span>
      ${s.route.decision === "REFER_TO_AUTHORITY" || s.route.decision === "AWAIT_APPROVAL" ? `<span class="a">Higher authority: ${esc(s.route.authority)}</span>` : ""}</div>`;
  $("brief").querySelectorAll(".mini.go").forEach((b) => b.addEventListener("click", () => openDoc(b.dataset.slot, b.dataset.role)));
  $("copy-brief").addEventListener("click", () => copyText(briefText({ critical, approval, collect, who }), "Brief copied"));
}

/** the document a requirement is about: its slot when the check names one, else the row whose reasons name that document */
function slotOf(q) {
  const role = q.party_role;
  const rows = state.board.documents.rows.filter((r) => role && r.cells[role]);
  const kind = String(q.check_id || "").split(":").pop();
  const hit = rows.find((r) => r.slot === kind)
    || rows.find((r) => r.cells[role].reasons.some((t) => t.toLowerCase().includes(words(kind).toLowerCase())));
  return hit ? { slot: hit.slot, role } : null;
}

function briefText({ critical, approval, collect, who }) {
  const { case: c, answer: a } = state.board;
  const s = a.summary;
  const block = (title, items) => (items.length ? [``, `${title}:`, ...items.map((q) => `- ${who(q)}${firstSentence(q.reason)}`)] : []);
  return [
    `${c.case_id} · ${c.applicant_name || ""}${c.co_applicant_name ? ` & ${c.co_applicant_name}` : ""} · ${words(a.product)} · ${money(c.loan_amount)}`,
    `${s.route.label}: ${s.route.text}`,
    ``, s.ai_summary.text || s.headline,
    ...block("Critical", critical), ...block("Awaiting approval", approval), ...block("To collect", collect),
    ``, `Next step: ${s.next_step}`,
  ].join("\n");
}

async function copyText(text, done) {
  try { await navigator.clipboard.writeText(text); toast(done); } catch { toast("Copy is not allowed in this browser."); }
}

const clock = (d) => (d ? d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "—");

// ---------------------------------------------------------------------------------------------------- rail: the rules
const ruleById = (id) => state.board.rules.find((r) => r.rule_id === id);

function renderRules() {
  $("rules").innerHTML = state.board.sections.map((s) => {
    const open = s.counts.FAIL + s.counts.WARN;
    return `
      <div class="rsec">
        <div class="rsec-head"><h3>${esc(s.name)}</h3><span class="t ${open ? "" : "clear"}">${open ? `${open} open` : "✓ clear"}</span></div>
        ${s.rules.map((id) => {
          const r = ruleById(id);
          const tone = RULE_TONE[r.status] || "na";
          const n = r.status === "OFF" ? "off" : r.total ? `${r.passed}/${r.total}` : "—";
          return `<button class="rule ${tone} ${id === state.rule ? "active" : ""}" data-id="${id}" title="${esc(r.help)}">
            <i class="dot ${tone}"></i><span class="nm">${esc(r.name)}</span><span class="n">${n}</span></button>`;
        }).join("")}
      </div>`;
  }).join("");
  $("rules").querySelectorAll(".rule").forEach((b) => b.addEventListener("click", () => openRule(b.dataset.id)));
}

// ---------------------------------------------------------------------------------------------------- drawer
function line(text, tone, tag) {
  return `<div class="line fade-in"><span class="edge ${tone}"></span>
    <div class="grow"><div class="t">${esc(firstSentence(text))}</div>${rest(text) ? `<div class="d">${esc(rest(text))}</div>` : ""}</div>
    ${tag ? `<span class="tag ${tone}">${esc(tag)}</span>` : ""}</div>`;
}

function showDrawer({ kicker, title, help, stat, body }) {
  $("drawer-kicker").textContent = kicker;
  $("drawer-title").textContent = title;
  $("drawer-help").textContent = help || "";
  $("drawer-stat").innerHTML = stat;
  $("drawer-body").innerHTML = body;
  $("scrim").hidden = false;
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  $("drawer-close").focus();
}

function openDoc(slot, role) {
  const row = state.board.documents.rows.find((r) => r.slot === slot);
  const c = row && row.cells[role];
  if (!c) return;
  const [tone, , text] = docLook(c);
  const accepts = (c.accepts || []).map(words).join(", ");
  state.rule = null;
  showDrawer({
    kicker: `${ROLES[role] || words(role)} · ${personName(role) || ""}`, title: row.label,
    help: c.state === "MISSING" ? `Not on file yet.${accepts ? ` Any one of these is accepted: ${accepts}.` : ""}` : "",
    stat: (c.state === "AWAITING_APPROVAL" ? `<span class="tag approval">Approval</span>`
      : `<span class="tag ${tone}">${tone === "bad" ? "Critical" : tone === "warn" ? "To collect" : "OK"}</span>`) + ` ${esc(text)}`
      + (c.file && c.state !== "OK" ? ` <span class="tag na">${esc(c.file)}</span>` : ""),
    body: c.reasons.map((r, i) => line(r, i === 0 && ["ISSUE", "MINOR"].includes(c.state) ? "ok" : tone, "")).join(""),
  });
  renderRules();
}

function openRule(id) {
  state.rule = id;
  const r = ruleById(id);
  const tone = RULE_TONE[r.status] || "na";
  const who = (role) => (role ? ` (${personName(role) || (ROLES[role] || words(role)).toLowerCase()})` : "");
  showDrawer({
    kicker: `${r.rule_id} · ${RULE_WORD[r.status] || r.status}`, title: r.name, help: r.explain || r.help,
    stat: r.status === "OFF" ? `<span class="tag na">Off</span> This rule is switched off in the policy.`
      : `<span class="tag ${tone}">${RULE_WORD[r.status] || r.status}</span> <b>${r.passed}</b>&nbsp;of&nbsp;<b>${r.total}</b>&nbsp;checks passed`,
    body: (r.checks.length ? r.checks.map((c) => {
      const done = DONE.has(c.status);
      const t = done ? "ok" : c.severity === "BLOCKING" ? "bad" : "warn";
      return line(c.reason + who(c.party_role), t, done ? "OK" : OPEN_WORD[c.status] || "Open");
    }).join("") : `<div class="empty-note">Nothing to check on this case.${id === "RULE-09" ? " No exception has been raised." : ""}</div>`)
      + (id === "RULE-11" ? notUploaded() : ""),
  });
  renderRules();
}

/** The file rule checks UPLOADED files only: name the documents that have no file yet, so a pass is not misread. */
function notUploaded() {
  const rows = [];
  for (const r of state.board.documents.rows) {
    for (const [role, c] of Object.entries(r.cells)) {
      if (c.state === "MISSING") rows.push(`${r.label} (${personName(role) || ROLES[role]})`);
    }
  }
  // supporting documents that are not on file (e.g. an ITR's computation), with whose they are
  for (const q of state.board.answer.requirements.filter((x) => x.rule_id === "RULE-07" && x.status === "ACTION_REQUIRED")) {
    const m = /needs its (.+?)\.?$/i.exec(q.reason || "");
    const what = m ? m[1].charAt(0).toUpperCase() + m[1].slice(1) : q.reason;
    rows.push(`${what} (${personName(q.party_role) || ROLES[q.party_role] || "case"})`);
  }
  if (!rows.length) return "";
  return `<div class="list-title">Not uploaded · no file to check</div>` + rows.map((t) =>
    `<div class="line"><span class="edge"></span><div class="grow"><div class="t">${esc(t)}</div>
      <div class="d">Counted under Missing documents, not here</div></div><span class="tag na">No file</span></div>`).join("");
}

function closeDrawer() {
  if (!$("drawer").classList.contains("open")) return;
  state.rule = null;
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  $("scrim").hidden = true;
  if (state.board) renderRules();
}

// ---------------------------------------------------------------------------------------------------- recent cases (this browser only)
function recent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]").filter((x) => x && x.id); } catch { return []; }
}

function remember(board) {
  const tone = (ROUTE_TONE[board.answer.summary.route.decision] || ["na"])[0];
  const item = { id: board.case.case_id, name: board.case.applicant_name || "", tone };
  const list = [item, ...recent().filter((x) => x.id !== item.id)].slice(0, 5);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch { /* private window: no list, nothing breaks */ }
  renderRecent();
}

function renderRecent() {
  const list = recent();
  const host = $("recent");
  host.hidden = !list.length;
  const now = state.board && state.board.case.case_id;
  host.innerHTML = `<div class="search-label">Recent</div>` + list.map((x) => `
    <button type="button" class="recent-item ${x.id === now ? "active" : ""}" data-id="${esc(x.id)}">
      <i class="dot ${esc(x.tone)}"></i><span class="nm">${esc(x.name || x.id)}</span><span class="rid">${esc(x.id.slice(-4))}</span></button>`).join("");
  host.querySelectorAll(".recent-item").forEach((b) => b.addEventListener("click", () => openCase(b.dataset.id)));
}

function render() {
  renderHead();
  renderBanner();
  renderPeople();
  renderBrief();
  renderRules();
}

// ---------------------------------------------------------------------------------------------------- search
const normalise = (raw) => String(raw || "").trim().toUpperCase().replace(/\s+/g, "");

async function openCase(raw, { keepFilter = false } = {}) {
  const caseId = normalise(raw);
  if (!caseId || state.loading) return;
  if (!/^[A-Z0-9._:-]+$/.test(caseId)) return fail("A case ID has only letters, numbers, dots, dashes and colons.");
  closeDrawer();
  state.loading = true;
  document.body.classList.add("is-loading");
  const btn = document.querySelector(".btn-brand");
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const board = await api(`/${encodeURIComponent(caseId)}/rules`, { method: "POST" });
    state.board = board;
    state.checkedAt = new Date();
    if (!keepFilter) state.filter = null;
    document.body.classList.remove("is-empty");
    $("search-error").hidden = true;
    $("case-input").value = caseId;
    history.replaceState(null, "", `?case=${encodeURIComponent(caseId)}`);
    document.title = `${board.case.applicant_name || caseId} · Credit Document Desk`;
    render();
    remember(board);
  } catch (err) {
    fail(err.message);
  } finally {
    state.loading = false;
    document.body.classList.remove("is-loading");
    btn.disabled = false;
    btn.textContent = "Check documents";
  }
}

function fail(message) {
  const e = $("search-error");
  e.textContent = message;
  e.hidden = false;
  if (!document.body.classList.contains("is-empty")) toast(message);
}

// ---------------------------------------------------------------------------------------------------- light / dark
const THEME_KEY = "docs-credit-theme";
const SUN = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
const MOON = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>';
const isDark = () => document.documentElement.dataset.theme === "dark"
  || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);

function paintThemeButton() {
  const dark = isDark();
  const b = $("theme-btn");
  b.innerHTML = dark ? SUN : MOON;
  b.title = dark ? "Switch to light mode" : "Switch to dark mode";
  b.setAttribute("aria-label", b.title);
}

function toggleTheme() {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem(THEME_KEY, next); } catch { /* not saved: still switches for this visit */ }
  paintThemeButton();
}

function start() {
  paintThemeButton();
  $("theme-btn").addEventListener("click", toggleTheme);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", paintThemeButton);
  $("search").addEventListener("submit", (e) => { e.preventDefault(); openCase($("case-input").value); });
  $("drawer-close").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); $("case-input").focus(); $("case-input").select(); }
    if (e.key === "Escape") closeDrawer();
  });
  renderRecent();
  const wanted = new URLSearchParams(location.search).get("case");
  if (wanted) openCase(wanted);
  else $("case-input").focus();
}

start();
