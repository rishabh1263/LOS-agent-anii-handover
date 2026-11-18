import { useCallback, useEffect, useState } from "react";
import { Alarm } from "../components/Alarm";
import { AuditTimeline } from "../components/AuditTimeline";
import { Celebration } from "../components/Celebration";
import { FinalizePanel } from "../components/FinalizePanel";
import { Icon } from "../components/Icon";
import { ResultPanel } from "../components/ResultPanel";
import { Stepper, stepOf, steps } from "../components/Stepper";
import { SummaryCard } from "../components/SummaryCard";
import { Badge, Card, Empty, Notice, Skeleton } from "../components/ui";
import { ApiError, api } from "../services/api";
import type { ApplicationView, AuditEvent, Case, Subject, UiConfig } from "../types/api";
import { setSound, soundOn } from "../utils/chime";
import { rupees, tone, when } from "../utils/format";
import { t } from "../utils/text";

// a case is celebrated / flagged once (per browser tab): reopening it does not replay the moment
function shownBefore(kind: string, caseId: string): boolean {
  try { return sessionStorage.getItem(`crv.${kind}.${caseId}`) === "1"; } catch { return false; }
}
function markShown(kind: string, caseId: string) {
  try { sessionStorage.setItem(`crv.${kind}.${caseId}`, "1"); } catch { /* storage blocked: may show again */ }
}

const initials = (name: string) => (name || "?").split(" ").map((w) => w[0]).slice(0, 2).join("").toUpperCase();

/** The results of one verification case -- the page the screen opens after Confirm & Initiate. The summary sits at
 *  the top; then one card per person (the applicant and any number of co-applicants), the decision and the history. */
export function ResultsPage({ caseId, config, onBack }: { caseId: string; config: UiConfig; onBack: (applicationId?: string) => void }) {
  const [kase, setKase] = useState<Case | null>(null);
  const [view, setView] = useState<ApplicationView | null>(null);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [party, setParty] = useState(false);
  const [alarm, setAlarm] = useState(false);
  const [sound, setSoundState] = useState(soundOn());
  const [picked, setPicked] = useState<string | null>(null);     // the person shown (compact view)
  const [onlyReview, setOnlyReview] = useState(false);

  const load = useCallback(async () => {
    try {
      const c = await api.caseDetail(caseId);
      const [v, a] = await Promise.all([api.application(c.application_id), api.caseAudit(caseId)]);
      setKase(c);
      setView(v);
      setEvents(a.events);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t("results.open_failed"));
    }
  }, [caseId]);

  useEffect(() => { load(); }, [load]);

  // a clean result (every person in config ui.celebrate_risks) is celebrated as soon as the results are on screen
  const levels = kase?.subjects.map((s) => s.result?.risk_level ?? s.status) ?? [];
  const clean = !!kase && levels.length > 0 && levels.every((r) => config.ui.celebrate_risks.includes(r));
  useEffect(() => {
    if (clean && kase && !shownBefore("celebrated", kase.case_id)) { markShown("celebrated", kase.case_id); setParty(true); }
  }, [clean, kase]);

  // findings that need review (config ui.alarm_risks) are flagged as soon as the results are on screen
  const flagged = kase?.subjects.filter((s) => (config.ui.alarm_risks ?? []).includes(s.result?.risk_level ?? "")) ?? [];
  useEffect(() => {
    if (flagged.length && kase && !shownBefore("alarmed", kase.case_id)) { markShown("alarmed", kase.case_id); setAlarm(true); }
  }, [flagged.length, kase]);

  // a processing case can refresh itself (config ui.refresh_seconds; 0 = the button only)
  useEffect(() => {
    const seconds = config.ui.refresh_seconds;
    if (!seconds || !kase?.can.refresh) return;
    const timer = window.setInterval(() => { refresh(true); }, seconds * 1000);
    return () => window.clearInterval(timer);
  });

  const refresh = async (quiet = false) => {
    if (!kase) return;
    setBusy("refresh");
    try {
      const next = await api.refresh(kase.case_id);
      if (!quiet) setFlash({ kind: "info", text: t("results.refreshed", { case: next.case_id, status: next.status_label }) });
      await load();
    } catch (e) {
      if (!quiet) setFlash({ kind: "error", text: e instanceof ApiError ? e.message : t("results.refresh_failed") });
    } finally {
      setBusy(null);
    }
  };

  const save = async (key: string, fetchBlob: () => Promise<Blob>, name: string) => {
    setBusy(key);
    try {
      const url = URL.createObjectURL(await fetchBlob());
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      load();
    } catch (e) {
      setFlash({ kind: "error", text: e instanceof ApiError ? e.message : t("results.download_failed") });
    } finally {
      setBusy(null);
    }
  };
  // EVERY download on this screen is the final report of the whole case -- the applicant and every co-applicant in
  // one PDF (owner 2026-10-29: "jab pdf download final report me, tb sab combine report aana chahiye")
  const downloadAll = () => kase && save("all", () => api.caseReportBlob(kase.case_id),
    `crime-verification-${kase.application_id}-all.pdf`);
  const download = (_s: Subject) => downloadAll();

  if (error) {
    return (
      <div className="page">
        <Card>
          <Empty icon="search" title={t("results.open_failed_title")}>
            {error}<br />
            <button type="button" className="btn btn-primary" style={{ marginTop: 14 }} onClick={() => onBack()}>
              <Icon name="folder" /> {t("results.go_verify")}
            </button>
          </Empty>
        </Card>
      </div>
    );
  }
  if (!kase || !view) return <div className="page"><div className="card card-pad"><Skeleton lines={10} /></div></div>;
  const app = view.application;
  const step = stepOf(kase);
  const findings = kase.subjects.reduce((n, s) => n + (s.result?.total_cases ?? 0), 0);
  const people = kase.subjects.map((s) => ({
    party: s.party, label: s.role_label, name: s.full_name, level: s.result?.risk_level ?? s.status,
    levelLabel: s.result?.risk_label ?? s.status_label,
  }));
  // up to 1 + max_co_applicants people: from config ui.compact_people_from on, a list + one result instead of a card each
  const many = kase.subjects.length >= (config.ui.compact_people_from ?? Number.MAX_SAFE_INTEGER);
  const listed = onlyReview ? flagged : kase.subjects;
  // the person shown: the one picked, else the first that needs review, else the applicant
  const shown = kase.subjects.find((s) => s.subject_id === picked) ?? flagged[0] ?? kase.subjects[0];
  const everyone = new Intl.ListFormat(undefined, { type: "conjunction" }).format(kase.subjects.map((s) => s.role_label));

  return (
    <div className={`page results-page${many ? " is-many" : ""}`}>
      {party && (
        <Celebration title={config.celebration.title} sound={config.ui.celebration_sound !== false}
                     subtitle={config.celebration.subtitle.replace("{people}", everyone)} onDone={() => setParty(false)} />
      )}
      {alarm && (
        <Alarm title={config.alarm.title} sound={config.ui.alarm_sound !== false} onDone={() => setAlarm(false)}
               subtitle={config.alarm.subtitle.replace("{people}", new Intl.ListFormat(undefined, { type: "conjunction" })
                 .format(flagged.map((s) => `${s.role_label} (${s.result?.risk_label ?? ""})`)))} />
      )}
      <div className="results-top">
        <button type="button" className="btn btn-link back" onClick={() => onBack(app.application_id)}>
          <Icon name="arrowLeft" /> {t("results.back", { id: app.application_id })}
        </button>
        <span className="results-tools">
        {view.can.download_report && kase.subjects.some((s) => s.result) && (
          <button type="button" className="btn btn-primary btn-sm" onClick={downloadAll} disabled={busy === "all"}>
            <Icon name="download" /> {busy === "all" ? t("results.preparing_all") : t("results.download_all")}
          </button>
        )}
        {config.ui.celebration_sound !== false && (
          <button type="button" className="btn btn-ghost btn-sm" aria-pressed={sound}
                  onClick={() => { setSound(!sound); setSoundState(!sound); }}>
            <Icon name={sound ? "volume" : "mute"} /> {sound ? config.celebration.sound_on : config.celebration.sound_off}
          </button>
        )}
        </span>
      </div>

      <header className={`hero ${clean ? "hero-clean" : kase.status === "REVIEW_REQUIRED" ? "hero-review" : ""}${alarm ? " is-alarmed" : ""}`}>
        <div className="hero-glow" aria-hidden="true" />
        <div className="who">
          <span className="avatar avatar-hero" aria-hidden="true">
            <Icon name={clean ? "shieldCheck" : kase.status === "PROCESSING" ? "clock" : "gavel"} size={26} />
          </span>
          <div>
            <span className="hero-kicker">{t("results.kicker")}</span>
            <h1>{clean ? t("results.all_clear") : kase.status === "REVIEW_REQUIRED" ? t("results.review") : kase.status_label}</h1>
            <p>{app.applicant_name} · <span className="mono">{kase.case_id}</span> · {kase.vendor_name} · {when(kase.initiated_at)}</p>
          </div>
        </div>
        <dl className="header-stats">
          <div><dt>{t("results.people")}</dt><dd>{kase.subjects.length}</dd></div>
          <div><dt>{t("results.found")}</dt><dd>{findings}</dd></div>
          <div><dt>{t("results.loan")}</dt><dd>{rupees(app.loan_amount)}</dd></div>
        </dl>
        <Stepper steps={steps()} current={step.current} failed={step.failed} />
      </header>

      {flash && <Notice kind={flash.kind}>{flash.text}</Notice>}
      {kase.can.refresh && (
        <div className="refresh-bar card">
          <span className="radar sm" aria-hidden="true"><span className="radar-sweep" /></span>
          <div><strong>{t("results.working_title")}</strong>
            <p className="muted small">{t("results.working_text")}</p></div>
          <button type="button" className="btn btn-secondary" onClick={() => refresh()} disabled={busy === "refresh"}>
            <Icon name="refresh" className={busy === "refresh" ? "spin" : ""} /> {t("results.refresh")}
          </button>
        </div>
      )}

      {kase.status !== "WAIVED" && (
        <SummaryCard caseId={kase.case_id} version={kase.updated_at} celebrate={config.ui.celebrate_risks}
                     labels={config.summary_labels} people={people} />
      )}

      {many ? (
        // MANY PEOPLE (config ui.compact_people_from): one compact list of everyone + the chosen person's result
        <div className="people-board">
          <Card className="people-list" title={t("results.people_list", { n: kase.subjects.length })}
                subtitle={t("results.people_list_note")}>
            {flagged.length > 0 && (
              <div className="chips-row people-filter" role="tablist">
                <button type="button" role="tab" aria-selected={!onlyReview} className={`chip-btn${!onlyReview ? " is-on" : ""}`}
                        onClick={() => setOnlyReview(false)}>{t("results.show_all", { n: kase.subjects.length })}</button>
                <button type="button" role="tab" aria-selected={onlyReview} className={`chip-btn${onlyReview ? " is-on" : ""}`}
                        onClick={() => setOnlyReview(true)}>{t("results.show_review", { n: flagged.length })}</button>
              </div>
            )}
            <ul className="people-rows" role="tablist" aria-orientation="vertical">
              {listed.map((s) => {
                const level = s.result?.risk_level ?? s.status;
                return (
                  <li key={s.subject_id}>
                    <button type="button" role="tab" aria-selected={s.subject_id === shown?.subject_id}
                            className={`people-row${s.subject_id === shown?.subject_id ? " is-on" : ""}`}
                            onClick={() => setPicked(s.subject_id)}>
                      <span className={`ring xs ring-${level.toLowerCase().replace(/_/g, "-")}`}>{initials(s.full_name)}</span>
                      <span className="people-row-text">
                        <span className="subject-role">{s.role_label}</span>
                        <span className="subject-name">{s.full_name}</span>
                      </span>
                      <span className={`people-risk tone-${tone(level)}`}>
                        {s.result?.risk_label ?? s.status_label}
                        {s.result ? <small>{t("results.cases_n", { n: s.result.total_cases })}</small> : null}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </Card>
          {shown && (
            <Card key={shown.subject_id} className="result-card people-detail" title={<span className="result-title">
              <span className={`ring sm ring-${(shown.result?.risk_level ?? shown.status).toLowerCase().replace(/_/g, "-")}`}>
                {initials(shown.full_name)}</span>
              <span><span className="subject-role">{shown.role_label}</span><br /><span className="subject-name">{shown.full_name}</span></span>
            </span>} actions={<Badge value={shown.status} label={shown.status_label} />}>
              <div className="stagger">
                <ResultPanel subject={shown} riskLabels={config.risk.labels} arcLabels={config.risk.short} waivedReason={kase.waived_reason}
                             canDownload={view.can.download_report} downloading={busy === "all"} onDownload={download} />
              </div>
            </Card>
          )}
        </div>
      ) : (
      <div className={`result-grid${kase.subjects.length > 1 ? " cols-2" : ""}`}>
        {kase.subjects.map((s, i) => (
          <Card key={s.subject_id} className="result-card" title={<span className="result-title">
            <span className={`ring sm ring-${(s.result?.risk_level ?? s.status).toLowerCase().replace(/_/g, "-")}`}>
              {initials(s.full_name)}</span>
            <span><span className="subject-role">{s.role_label}</span><br /><span className="subject-name">{s.full_name}</span></span>
          </span>} actions={<Badge value={s.status} label={s.status_label} />}>
            <div style={{ animationDelay: `${Math.min(i, 6) * 0.1}s` }} className="stagger">
              <ResultPanel subject={s} riskLabels={config.risk.labels} arcLabels={config.risk.short} waivedReason={kase.waived_reason}
                           canDownload={view.can.download_report} downloading={busy === "all"} onDownload={download} />
            </div>
          </Card>
        ))}
      </div>
      )}

      {kase.status !== "WAIVED" && <FinalizePanel kase={kase} config={config} onDone={() => load()} />}

      <Card title={t("results.activity")} subtitle={t("results.activity_subtitle")} className="activity">
        <AuditTimeline events={events} roles={config.roles} risk={config.risk.labels} decisions={config.decision_labels}
                       visible={config.ui.activity_visible ?? 0} />
      </Card>
    </div>
  );
}
