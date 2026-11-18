import { useEffect, useState } from "react";
import { api } from "../services/api";
import { useAsync } from "../hooks/useAsync";
import type { AppConfig, ReportCheck } from "../types/api";
import { AgencyPanel } from "../components/AgencyPanel";
import { BankAccountPanel } from "../components/BankAccountPanel";
import { FailPopup } from "../components/FailPopup";
import { Flow } from "../components/Flow";
import { ReportPanel } from "../components/ReportPanel";
import { ResultsTable } from "../components/ResultsTable";
import { StatementViewer, type Viewing } from "../components/StatementViewer";
import { ReportCheckView } from "../components/ReportCheckView";
import { SummaryCard } from "../components/SummaryCard";
import { Card, ErrorNote, Loading, StatusBadge, Value } from "../components/ui";
import { rupees } from "../utils/format";

const PRODUCTS: Record<string, string> = { PERSONAL_LOAN: "Personal Loan", LAP: "LAP", HOME_LOAN: "Home Loan" };
const RING: Record<string, string> = { PASS: "#34d399", FAIL: "#f87171", REVIEW_REQUIRED: "#fbbf24", NOT_APPLICABLE: "#94a3b8" };

/** One case, loaded by the search: the result banner, the checks, and the case as a form. */
export function CaseDetailPage({ caseId, config }: { caseId: string; config: AppConfig }) {
  const [tick, setTick] = useState(0);
  const detail = useAsync(() => api.caseDetail(caseId), [caseId, tick]);
  const refresh = () => setTick((t) => t + 1);
  const reading = detail.data?.case.status === "READING";
  const [viewing, setViewing] = useState<Viewing | null>(null);
  const [closedFor, setClosedFor] = useState<string | null>(null);      // the result whose problems popup was closed
  const [focus, setFocus] = useState<{ id: string; at: number } | null>(null);
  // a chosen file is a local blob URL: let it go when it is replaced, closed, or the case changes
  const view = (next: Viewing | null) => setViewing((old) => {
    if (old?.url.startsWith("blob:") && old.url !== next?.url) URL.revokeObjectURL(old.url);
    return next;
  });
  useEffect(() => () => view(null), [caseId]); // eslint-disable-line react-hooks/exhaustive-deps

  // A LONG / SCANNED STATEMENT IS READ IN THE BACKGROUND: ask again every few seconds until its result is in
  useEffect(() => {
    if (!reading) return;
    const timer = setTimeout(refresh, 3000);
    return () => clearTimeout(timer);
  }, [reading, tick]);

  if (detail.loading && !detail.data) return <Loading what="the case" />;
  if (detail.error) return <ErrorNote error={detail.error} onRetry={detail.reload} />;
  if (!detail.data) return null;
  const d = detail.data;
  const c = d.case;
  const run = d.latest_run;
  const closed = ["POSITIVE", "NEGATIVE", "WAIVED"].includes(c.status);
  const passed = run?.checks.checks_passed ?? 0;
  const total = run?.checks.checks_applicable ?? 0;
  const next = run && !closed ? config.actions[run.recommended_action] : null;
  const agencyName = config.agencies.find((a) => a.id === c.assigned_agency)?.name;
  // the agency's report, checked when the agency submitted: full width, under the checks
  const reportCheck = (d.decisions.at(-1)?.outcome as { report_check?: ReportCheck | null } | undefined)?.report_check ?? null;
  // only the checks that found a problem (red) open the popup; never on a closed case
  const failed = run && !closed ? run.rules.filter((r) => r.rule_id !== "R8" && r.status === "FAIL") : [];

  // with a statement open, the viewer takes the right side and these follow the form on the left
  const side = (
    <>
      <div id="sec-agency"><AgencyPanel detail={d} config={config} onFinalized={refresh} /></div>
      <div id="sec-report"><ReportPanel detail={d} refreshKey={run?.run_id ?? "none"} /></div>
    </>
  );

  return (
    <>
      <section className={`card banner${run ? " has-summary" : ""}`}>
        <div className="ring" style={{ ["--p" as string]: total ? Math.round((passed / total) * 100) : 0,
                                       ["--c" as string]: RING[run?.overall_status ?? "NOT_APPLICABLE"] }}>
          <span>{run ? <>{passed}/{total}<small>checks</small></> : <small>Not run</small>}</span>
        </div>
        <div className="banner-who">
          <h1>{c.applicant_name}</h1>
          <div className="meta">
            <span>{PRODUCTS[c.product] ?? c.product}</span>
            <span>Loan <b>{rupees(c.loan_amount)}</b></span>
            <span>Stage <b>{c.stage}</b></span>
            <span>AA <b>{c.mode === "AA_SUCCESS" ? "received" : "not received"}</b></span>
          </div>
          <div className="verdict">
            <StatusBadge status={closed || reading ? c.status : run?.overall_status ?? "PENDING_VERIFICATION"} large />
            {next && !reading && <span className="next">{next}</span>}
            {reading && <span className="next"><span className="spinner" aria-hidden /> Reading the statement. The checks will appear here as soon as it is read.</span>}
          </div>
        </div>
        {run && <SummaryCard caseId={caseId} run={run} />}
      </section>

      <Flow detail={d} />

      <div id="sec-checks">{run && <ResultsTable rules={run.rules} actions={config.actions} focus={focus} />}</div>
      {run && failed.length > 0 && closedFor !== run.run_id && (
        <FailPopup rules={failed} onClose={() => setClosedFor(run.run_id)}
                   onShow={(id) => { setClosedFor(run.run_id); setFocus({ id, at: Date.now() }); }} />
      )}

      {reportCheck && <div id="sec-report-check"><ReportCheckView check={reportCheck} /></div>}

      <div className={viewing ? "split viewing" : "split"}>
        <div className="col">
          <Card icon="file" title="Application details">
            <div className="form">
              <Value label="Applicant Name" value={c.applicant_name} />
              <Value label="Applicant ID" value={c.application_ref} />
              <Value label="Case ID" value={c.case_id} />
              <Value label="Product" value={PRODUCTS[c.product] ?? c.product} />
              <Value label="Loan Amount (₹)" value={rupees(c.loan_amount)} />
              <Value label="Stage" value={c.stage} />
              <Value label="Agency" value={agencyName ?? "Not assigned"} />
              <Value label="Status" value={<StatusBadge status={c.status} />} />
            </div>
          </Card>
          <BankAccountPanel detail={d} config={config} onProcessed={refresh} onView={view} />
          {viewing && side}
        </div>
        <div className="col">
          {viewing ? <StatementViewer view={viewing} onClose={() => view(null)} /> : side}
        </div>
      </div>
    </>
  );
}
