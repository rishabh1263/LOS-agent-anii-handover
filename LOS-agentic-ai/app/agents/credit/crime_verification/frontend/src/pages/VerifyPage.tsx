import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ConfirmModal, type ConfirmSubject } from "../components/ConfirmModal";
import { Icon } from "../components/Icon";
import { addressOf, type FormValues, InputDetails, validateForm } from "../components/InputDetails";
import { Badge, Empty, Notice, Skeleton } from "../components/ui";
import { ApiError, api } from "../services/api";
import type { ApplicationRow, ApplicationSubject, ApplicationView, Case, UiConfig } from "../types/api";
import { parseAddress } from "../utils/address";
import { newKey, rupees } from "../utils/format";
import { sampleDetails } from "../utils/sample";
import { t } from "../utils/text";
import { maskPan } from "../utils/validate";

const FILTERS: { id: string; match: (s: string | null) => boolean }[] = [
  { id: "all", match: () => true },
  { id: "new", match: (s) => !s || s === "WAIVED" },
  { id: "progress", match: (s) => s === "INITIATED" || s === "PROCESSING" },
  { id: "done", match: (s) => s === "COMPLETED" || s === "REVIEW_REQUIRED" || s === "FAILED" },
];

function formOf(s: ApplicationSubject, config: UiConfig): FormValues {
  const address = parseAddress(s.permanent_address, config);
  return { full_name: s.full_name, father_name: s.father_name, date_of_birth: s.date_of_birth, address,
           structured: !!address.state || !address.line };
}

const initials = (name: string) => (name || "?").split(" ").map((w) => w[0]).slice(0, 2).join("").toUpperCase();

/** The verification desk: find the case on the left, fill each person's details on the right (the applicant and up
 *  to N co-applicants), initiate -> the results page. */
export function VerifyPage({ id, config, onSelect, onOpenCase }: {
  id: string | null; config: UiConfig; onSelect: (applicationId: string) => void; onOpenCase: (caseId: string) => void;
}) {
  const [query, setQuery] = useState(id ?? "");
  const [filter, setFilter] = useState("all");
  const [rows, setRows] = useState<ApplicationRow[] | null>(null);
  const [lookupError, setLookupError] = useState<string | null>(null);
  const [view, setView] = useState<ApplicationView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState("PRIMARY");                 // a party key
  const [include, setInclude] = useState<Record<string, boolean>>({});
  const [forms, setForms] = useState<Record<string, FormValues>>({});
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [vendor, setVendor] = useState(config.default_vendor);
  const [scenario, setScenario] = useState("");
  const [modal, setModal] = useState<{ retrigger: boolean; key: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [filled, setFilled] = useState<string | null>(null);

  // the list on the left (search as you type)
  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.applications(/^CRV-/i.test(query.trim()) ? "" : query.trim())
        .then((r) => setRows(r.applications)).catch(() => setRows([]));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [query]);

  const load = useCallback(async (appId: string) => {
    setView(null);
    setLoadError(null);
    setFlash(null);
    try {
      const v = await api.application(appId);
      setView(v);
      setForms(Object.fromEntries(v.subjects.map((s) => [s.party, formOf(s, config)])));
      setInclude(Object.fromEntries(v.subjects.map((s) => [s.party, !config.blocked_while.includes(s.latest?.status ?? "")])));
      setTouched({});
      setSelected(v.subjects[0]?.party ?? "PRIMARY");
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.message : t("verify.open_failed"));
    }
  }, [config]);

  useEffect(() => { if (id) load(id); else setView(null); }, [id, load]);

  const find = async (e: FormEvent) => {
    e.preventDefault();
    setLookupError(null);
    const value = query.trim().toUpperCase();
    if (!value) return;
    if (value.startsWith("CRV-")) {
      try { await api.caseDetail(value); onOpenCase(value); }
      catch (err) { setLookupError(err instanceof ApiError ? err.message : t("verify.case_not_found")); }
      return;
    }
    const exact = rows?.find((r) => r.application_id.toUpperCase() === value) ?? (rows?.length === 1 ? rows[0] : null);
    if (exact) onSelect(exact.application_id);
    else setLookupError(t("verify.lookup_not_found", { id: value }));
  };

  const subjects = view?.subjects ?? [];
  const byParty = useMemo(() => Object.fromEntries(subjects.map((s) => [s.party, s])), [subjects]);
  const current = byParty[selected] ?? subjects[0];
  const inFlight = (s: ApplicationSubject) => config.blocked_while.includes(s.latest?.status ?? "");
  const needsRetrigger = (s: ApplicationSubject) => config.retrigger_needed_after.includes(s.latest?.status ?? "");
  const chosen = subjects.filter((s) => include[s.party] && !inFlight(s));
  const errorsOf = (party: string) => (forms[party] ? validateForm(forms[party], config) : {});
  const retrigger = chosen.some(needsRetrigger);
  const latestCase: Case | null = view?.latest_case ?? null;
  const vendorName = config.vendors.find((v) => v.id === vendor)?.name ?? vendor;
  const visible = (rows ?? []).filter((r) => FILTERS.find((f) => f.id === filter)!.match(r.latest_status));

  const fillSample = () => {
    if (!config.samples || !current) return;
    const s = sampleDetails(config.samples, config);
    setForms({ ...forms, [current.party]: { ...forms[current.party], full_name: s.full_name, father_name: s.father_name,
                                            date_of_birth: s.date_of_birth, address: s.address, structured: true } });
    setTouched({ ...touched, [current.party]: true });
    setFilled(current.party);
    window.setTimeout(() => setFilled(null), 900);
  };

  const startInitiate = (asRetrigger: boolean, parties?: string[]) => {
    setFlash(null);
    const list = parties ? subjects.filter((s) => parties.includes(s.party) && !inFlight(s)) : chosen;
    if (parties) setInclude((prev) => ({ ...prev, ...Object.fromEntries(parties.map((p) => [p, true])) }));
    if (!list.length) return setFlash(t("verify.select_someone"));
    setTouched((prev) => ({ ...prev, ...Object.fromEntries(list.map((s) => [s.party, true])) }));
    const invalid = list.find((s) => Object.keys(errorsOf(s.party)).length);
    if (invalid) {
      setSelected(invalid.party);
      return setFlash(t("verify.complete_first", { who: invalid.role_label }));
    }
    setModalError(null);
    setModal({ retrigger: asRetrigger || list.some(needsRetrigger), key: newKey() });
  };

  const modalSubjects: ConfirmSubject[] = (modal ? subjects.filter((s) => include[s.party] && !inFlight(s)) : []).map((s) => {
    const f = forms[s.party];
    return { party: s.party, role: s.role, label: s.role_label, full_name: f.full_name, father_name: f.father_name,
             date_of_birth: f.date_of_birth, permanent_address: addressOf(f),
             pan_masked: f.pan !== undefined ? maskPan(f.pan.trim().toUpperCase()) : s.pan_masked };
  });

  const confirm = async () => {
    if (!modal || submitting || !view) return;
    setSubmitting(true);
    setModalError(null);
    try {
      const kase = await api.initiate({
        application_id: view.application.application_id, vendor, demo_scenario: scenario || null, retrigger: modal.retrigger,
        subjects: modalSubjects.map((s) => {
          const f = forms[s.party];
          const person = byParty[s.party];
          return { role: person.role, seq: person.seq, full_name: f.full_name.trim(), father_name: f.father_name.trim(),
                   date_of_birth: f.date_of_birth, permanent_address: addressOf(f),
                   ...(f.pan !== undefined ? { pan: f.pan.trim().toUpperCase() } : {}) };
        }),
      }, modal.key);
      setModal(null);
      onOpenCase(kase.case_id);                      // the next page: the results
    } catch (e) {
      if (e instanceof ApiError) {
        const fields = e.fields.map((f) => f.message).filter(Boolean);
        setModalError(fields.length ? `${e.message} ${fields.join(" ")}` : e.message);
      } else {
        setModalError(t("verify.initiate_failed"));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const app = view?.application;
  // many people (config ui.compact_people_from): the tabs wrap into a grid, the action bar names only who is left out
  const many = subjects.length >= (config.ui.compact_people_from ?? Number.MAX_SAFE_INTEGER);
  const leftOut = subjects.filter((s) => !include[s.party] || inFlight(s));
  const readOnly = !view?.can.initiate || (current ? inFlight(current) : true);
  const blockedByRetrigger = retrigger && !view?.can.retrigger;

  return (
    <div className="page verify-page">
      <div className="page-head">
        <div>
          <span className="eyebrow">{t("verify.eyebrow")}</span>
          <h1>{t("verify.title")}</h1>
          <p className="muted">{t("verify.subtitle")}</p>
        </div>
      </div>

      <div className="verify-grid">
        {/* ---------------------------------------------------------------- left: find the case */}
        <aside className="lookup card">
          <form className="lookup-search" onSubmit={find} role="search">
            <label htmlFor="lookup" className="lookup-label">{t("verify.lookup_label")}</label>
            <div className="lookup-box">
              <Icon name="search" />
              <input id="lookup" className="input" placeholder={t("verify.lookup_placeholder")} value={query} autoComplete="off"
                     onChange={(e) => { setQuery(e.target.value); setLookupError(null); }} />
              <button type="submit" className="btn btn-primary btn-sm" aria-label={t("verify.lookup_open")}><Icon name="arrowRight" /></button>
            </div>
            {lookupError && <p className="field-error"><Icon name="alert" size={13} /> {lookupError}</p>}
          </form>
          <div className="chips-row" role="tablist">
            {FILTERS.map((f) => (
              <button key={f.id} type="button" role="tab" aria-selected={filter === f.id}
                      className={`chip-btn${filter === f.id ? " is-on" : ""}`} onClick={() => setFilter(f.id)}>
                {t(`verify.filters.${f.id}`)}</button>
            ))}
          </div>
          <ul className="lookup-list">
            {!rows && <li><Skeleton lines={5} /></li>}
            {rows && visible.length === 0 && <li className="muted small lookup-none">{t("verify.none_match")}</li>}
            {visible.map((r) => (
              <li key={r.application_id}>
                <button type="button" className={`lookup-item${app?.application_id === r.application_id ? " is-on" : ""}`}
                        onClick={() => onSelect(r.application_id)} aria-current={app?.application_id === r.application_id}>
                  <span className="avatar sm">{initials(r.applicant ?? "")}</span>
                  <span className="lookup-text">
                    <strong>{r.applicant ?? "—"}</strong>
                    <span className="mono muted">{r.application_id}</span>
                    <span className="muted small">{r.product} · {rupees(r.loan_amount)}</span>
                  </span>
                  {r.latest_status ? <Badge value={r.latest_status} label={r.latest_status_label} />
                                   : <span className="dot-new" title={config.statuses.NOT_INITIATED} />}
                </button>
              </li>
            ))}
          </ul>
        </aside>

        {/* ---------------------------------------------------------------- right: the form */}
        <section className="verify-main" aria-live="polite">
          {!id && (
            <div className="card verify-empty">
              <Empty icon="shieldCheck" title={t("verify.empty_title")}>{t("verify.empty_text")}</Empty>
            </div>
          )}
          {id && loadError && (
            <div className="card"><Empty icon="search" title={t("verify.open_failed_title")}>{loadError}</Empty></div>
          )}
          {id && !loadError && (!view || !current) && <div className="card card-pad"><Skeleton lines={8} /></div>}

          {view && current && app && (
            <>
              <header className="case-head card">
                <span className="avatar avatar-lg">{initials(app.applicant_name)}</span>
                <div className="case-head-text">
                  <h2>{app.applicant_name}</h2>
                  <p className="muted"><span className="mono">{app.application_id}</span> · {app.product} ·{" "}
                    {t("verify.branch", { branch: app.branch })}</p>
                </div>
                <dl className="mini-stats">
                  <div><dt>{t("verify.loan")}</dt><dd>{rupees(app.loan_amount)}</dd></div>
                  <div><dt>{t("verify.stage")}</dt><dd>{app.stage}</dd></div>
                </dl>
                {latestCase && latestCase.status !== "WAIVED" && (
                  <button type="button" className="btn btn-secondary" onClick={() => onOpenCase(latestCase.case_id)}>
                    <Badge value={latestCase.status} label={latestCase.status_label} /> {t("verify.view_results")} <Icon name="arrowRight" />
                  </button>
                )}
              </header>

              {latestCase?.status === "WAIVED" && (
                <div className="waived card">
                  <Icon name="info" size={18} />
                  <div>
                    <strong>{t("verify.waived_title")}</strong>
                    <p className="muted">{latestCase.waived_reason}</p>
                  </div>
                  {view.can.retrigger && (
                    <button type="button" className="btn btn-secondary" onClick={() => startInitiate(true, subjects.map((s) => s.party))}>
                      <Icon name="refresh" /> {t("verify.retrigger")}
                    </button>
                  )}
                </div>
              )}

              <div className="card form-card">
                <div className="form-card-head">
                  <div className={`person-tabs${many ? " is-many" : ""}`} role="tablist" aria-label={t("verify.people_tabs")}>
                    {subjects.map((s) => {
                      const errs = Object.keys(errorsOf(s.party)).length;
                      return (
                        <div key={s.party} className={`person-tab${s.party === current.party ? " is-on" : ""}`}>
                          <button type="button" role="tab" aria-selected={s.party === current.party} className="person-tab-btn"
                                  onClick={() => setSelected(s.party)}>
                            <span className={`ring sm ring-${(s.latest?.result?.risk_level ?? s.latest?.status ?? "none").toLowerCase().replace(/_/g, "-")}`}>
                              {initials(forms[s.party]?.full_name || s.full_name)}</span>
                            <span className="person-tab-text">
                              <span className="subject-role">{s.role_label}</span>
                              <span className="subject-name">{forms[s.party]?.full_name || s.full_name || "—"}</span>
                            </span>
                            {errs > 0 && touched[s.party] && <span className="count-bad" title={t("verify.to_fix", { n: errs })}>{errs}</span>}
                          </button>
                          <label className="switch" title={inFlight(s) ? t("verify.in_progress") : t("verify.include", { who: s.role_label })}>
                            <input type="checkbox" checked={!!include[s.party] && !inFlight(s)}
                                   aria-label={t("verify.include", { who: s.role_label })}
                                   disabled={inFlight(s) || !view.can.initiate}
                                   onChange={(e) => setInclude({ ...include, [s.party]: e.target.checked })} />
                            <span className="switch-track"><span className="switch-thumb" /></span>
                          </label>
                        </div>
                      );
                    })}
                  </div>
                  <div className="form-tools">
                    <label className="inline-field">
                      <span>{t("verify.vendor")}</span>
                      <select className="input" value={vendor} onChange={(e) => setVendor(e.target.value)}>
                        {config.vendors.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
                      </select>
                    </label>
                    {config.scenarios.length > 0 && (
                      <label className="inline-field">
                        <span>{t("verify.scenario")}</span>
                        <select className="input" value={scenario} onChange={(e) => setScenario(e.target.value)}>
                          <option value="">{t("verify.scenario_default")}</option>
                          {config.scenarios.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                        </select>
                      </label>
                    )}
                    {config.samples && view.can.initiate && !readOnly && (
                      <button type="button" className="btn btn-ghost" onClick={fillSample}><Icon name="wand" /> {t("verify.fill_sample")}</button>
                    )}
                  </div>
                </div>

                {current.latest && (
                  <div className="person-status">
                    <Badge value={current.latest.status} label={current.latest.status_label} />
                    <span className="muted small">{t("verify.latest_check", { case: current.latest.case_id })}
                      {current.latest.result ? ` · ${current.latest.result.risk_label}` : ""}</span>
                  </div>
                )}

                <div className={`form-body${filled === current.party ? " just-filled" : ""}`}>
                  <InputDetails values={forms[current.party]} original={formOf(current, config)} panMasked={current.pan_masked}
                                errors={errorsOf(current.party)} touched={!!touched[current.party]} readOnly={readOnly} config={config}
                                onChange={(next) => setForms({ ...forms, [current.party]: next })}
                                onTouch={() => setTouched({ ...touched, [current.party]: true })} />
                </div>

                {flash && <div className="form-flash"><Notice kind="error">{flash}</Notice></div>}
                {!view.can.initiate && <div className="form-flash"><Notice kind="info">{t("verify.no_permission")}</Notice></div>}

                <div className="action-bar">
                  <div className="action-people">
                    <span className="muted small">{t("verify.people_count", { n: chosen.length, total: subjects.length })}</span>
                    {many && leftOut.length > 0 && <span className="muted small">{t("verify.left_out")}</span>}
                    {(many ? leftOut : subjects).map((s) => (
                      <span key={s.party} className={`pill${include[s.party] && !inFlight(s) ? " is-on" : ""}`}>
                        <Icon name={include[s.party] && !inFlight(s) ? "check" : "x"} size={12} /> {s.role_label}
                      </span>
                    ))}
                  </div>
                  {view.can.initiate && (
                    <button type="button" className="btn btn-primary btn-lg" disabled={!chosen.length || blockedByRetrigger}
                            onClick={() => startInitiate(false)}>
                      <Icon name={retrigger ? "refresh" : "shieldCheck"} /> {retrigger ? t("verify.retrigger_button") : t("verify.initiate")}
                    </button>
                  )}
                </div>
              </div>
            </>
          )}
        </section>
      </div>

      {modal && (
        <ConfirmModal subjects={modalSubjects} config={config} vendorName={vendorName} retrigger={modal.retrigger}
                      submitting={submitting} error={modalError}
                      onCancel={() => { if (!submitting) { setModal(null); setModalError(null); } }} onConfirm={confirm} />
      )}
    </div>
  );
}
