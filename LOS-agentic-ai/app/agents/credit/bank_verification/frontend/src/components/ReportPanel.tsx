import { api, download, openDocument } from "../services/api";
import { useAsync } from "../hooks/useAsync";
import type { CaseDetail } from "../types/api";
import { when } from "../utils/format";
import { Icon } from "./Icon";
import { Card, ErrorNote, Loading } from "./ui";

/** Case history in plain words; housekeeping events are not shown. */
const EVENTS: Record<string, string> = {
  CASE_SEEDED: "Case opened", CASE_OPENED: "Case opened", STATEMENT_RECEIVED: "Statement uploaded", STATEMENT_READ: "Statement read",
  STATEMENT_REJECTED: "Statement not accepted", VERIFICATION_RUN: "Verification completed",
  AGENCY_ASSIGNED: "Agency assigned", REPORT_UPLOADED: "Agency report uploaded",
  RULES_RERUN_WITH_AGENCY_INPUT: "Checks updated with the branch result", AGENCY_UPDATE: "Agency update saved",
  FINAL_DECISION: "Final decision recorded", FINALIZE_REFUSED: "Decision not accepted, items missing",
  STATEMENT_QUEUED: "Statement reading started", STATEMENT_READ_FAILED: "Statement could not be read",
};

/** The report (download / print) and the case history. */
export function ReportPanel({ detail, refreshKey }: { detail: CaseDetail; refreshKey: string }) {
  const id = detail.case.case_id;
  const history = useAsync(() => api.history(id), [id, refreshKey]);
  const events = (history.data?.events ?? []).filter((e) => EVENTS[e.event]).reverse();
  const reports = detail.documents.filter((d) => d.role === "REPORT");
  return (
    <Card icon="file" title="Report and history">
      <div className="stack">
        {detail.latest_run ? (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="btn btn-primary" onClick={() => download(api.reportUrl(id, "pdf"), `bank-verification-${id}.pdf`)}>
              <Icon name="download" /> Download PDF</button>
            <button className="btn btn-ghost" onClick={() => openDocument(api.reportUrl(id, "html"))}>Print view</button>
            {reports.map((r) => (
              <button key={r.document_id} className="btn btn-ghost" onClick={() => openDocument(api.documentUrl(id, r.document_id))}>
                <Icon name="file" /> Agency report</button>
            ))}
          </div>
        ) : <p className="muted" style={{ margin: 0 }}>The report is ready after verification.</p>}
        {history.loading && !history.data && <Loading what="history" />}
        {history.error && <ErrorNote error={history.error} onRetry={history.reload} />}
        {history.data && (
          <ol className="timeline">
            {events.map((e, i) => <li key={i}><div><b>{EVENTS[e.event]}</b><span>{when(e.at)}</span></div></li>)}
          </ol>
        )}
      </div>
    </Card>
  );
}
