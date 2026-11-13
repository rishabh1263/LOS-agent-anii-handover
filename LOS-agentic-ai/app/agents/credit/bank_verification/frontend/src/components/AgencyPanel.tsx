import { useEffect, useState } from "react";
import { ReportCheckLine } from "./ReportCheckView";
import { ApiError, api } from "../services/api";
import type { AgencyInput, AppConfig, CaseDetail, ChecklistAnswer, FinalizeResult, ReportCheck } from "../types/api";
import { Icon } from "./Icon";
import { Card, Field, Notice, StatusBadge } from "./ui";

const ANSWERS: [ChecklistAnswer, string][] = [["VERIFIED", "Yes"], ["MISMATCH", "No"], ["NOT_VERIFIED", "Not checked"]];
const DECISIONS = [["POSITIVE", "Positive", "positive"], ["NEGATIVE", "Negative", "negative"],
                   ["REVIEW_REQUIRED", "Review", "review"]] as const;

function empty(config: AppConfig, agencyId: string | null): AgencyInput {
  return {
    agency_id: agencyId ?? config.agencies[0]?.id ?? "",
    checklist: Object.fromEntries(config.checklist.map((c) => [c.id, "NOT_VERIFIED"])) as Record<string, ChecklistAnswer>,
    branch_visit: "NOT_DONE", ownership: "PENDING", requested_decision: "REVIEW_REQUIRED", rationale: "", remarks: "",
  };
}

/**
 * The agency's confirmation: branch visit, ownership, the field checks, the report and the decision. The decision
 * rules apply: a Positive without the needed confirmations comes back as "needs review" with the reasons.
 */
export function AgencyPanel({ detail, config, onFinalized }:
  { detail: CaseDetail; config: AppConfig; onFinalized: (r: FinalizeResult) => void }) {
  const closed = ["POSITIVE", "NEGATIVE", "WAIVED"].includes(detail.case.status);
  const [input, setInput] = useState<AgencyInput>(() => empty(config, detail.case.assigned_agency));
  const [report, setReport] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; missing?: string[] } | null>(null);
  const [result, setResult] = useState<FinalizeResult | null>(null);
  // the report is evaluated when the agency SUBMITS (owner 2026-10-27: never while the form is still being filled):
  // the check comes back with the decision, and the case keeps the last one
  const shownCheck = result?.report_check
    ?? (detail.decisions.at(-1)?.outcome as { report_check?: ReportCheck } | undefined)?.report_check ?? null;

  useEffect(() => {
    setInput(empty(config, detail.case.assigned_agency));
    setResult(null);
    setError(null);
  }, [detail.case.case_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const disabled = closed || !detail.latest_run;
  const assigned = detail.case.assigned_agency;          // set at the run: only that agency decides
  useEffect(() => {
    if (assigned) setInput((i) => ({ ...i, agency_id: assigned }));
  }, [assigned]);
  const sample = detail.demo?.suggested_agency_input;
  const last = detail.decisions.at(-1);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const out = await api.finalize(detail.case.case_id, input, report);
      setResult(out);
      onFinalized(out);
    } catch (e) {
      if (e instanceof ApiError) setError({ message: e.message, missing: (e.details as { missing?: string[] } | null)?.missing });
      else setError({ message: String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card icon="shield" title="Agency confirmation"
          actions={sample && !disabled ? (
            <button className="btn btn-ghost" onClick={() => {
              const { with_report: _w, ...rest } = sample; void _w;
              // the sample never changes the agency the case is assigned to
              setInput({ ...rest, agency_id: assigned ?? rest.agency_id, remarks: "" }); }}>
              <Icon name="sparkles" /> Fill sample</button>) : null}
          foot={!disabled && (
            <button className="btn btn-primary" disabled={busy} onClick={submit}>
              {busy ? <><span className="spinner" aria-hidden /> Checking the report…</> : "Submit decision"}</button>
          )}>
      <div className="stack">
        {closed && last && (
          <Notice tone={last.final_status === "POSITIVE" ? "ok" : "bad"}>
            Final decision: <b>{last.final_status === "POSITIVE" ? "Positive" : "Negative"}</b>
          </Notice>
        )}
        {!closed && !detail.latest_run && <Notice tone="warn">Run verification first.</Notice>}
        <Field label="Agency">
          <select value={input.agency_id} disabled={disabled || Boolean(assigned)}
                  title={assigned ? "The case is assigned to this agency" : undefined}
                  onChange={(e) => setInput({ ...input, agency_id: e.target.value })}>
            {config.agencies.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </Field>
        <Field label="Branch visit">
          <select value={input.branch_visit} disabled={disabled}
                  onChange={(e) => setInput({ ...input, branch_visit: e.target.value as AgencyInput["branch_visit"] })}>
            <option value="NOT_DONE">Not done</option><option value="SCHEDULED">Scheduled</option><option value="COMPLETED">Completed</option>
          </select>
        </Field>
        <Field label="Account belongs to the applicant?">
          <select value={input.ownership} disabled={disabled}
                  onChange={(e) => setInput({ ...input, ownership: e.target.value as AgencyInput["ownership"] })}>
            <option value="PENDING">Pending</option><option value="CONFIRMED">Confirmed by branch</option>
            <option value="NOT_CONFIRMED">Not confirmed by branch</option>
          </select>
        </Field>

        <table className="checklist">
          <thead><tr><th>Confirmed with branch</th>{ANSWERS.map(([, label]) => <th key={label}>{label}</th>)}</tr></thead>
          <tbody>
            {config.checklist.map((item) => (
              <tr key={item.id}>
                <td>{item.label}</td>
                {ANSWERS.map(([a, label]) => (
                  <td key={a}>
                    <input type="radio" name={`chk-${item.id}`} aria-label={`${item.label}: ${label}`} disabled={disabled}
                           checked={input.checklist[item.id] === a}
                           onChange={() => setInput({ ...input, checklist: { ...input.checklist, [item.id]: a } })} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>

        <Field label="Decision reason">
          <textarea rows={2} value={input.rationale} disabled={disabled} placeholder="What the branch confirmed"
                    onChange={(e) => setInput({ ...input, rationale: e.target.value })} />
        </Field>
        <Field label="Remarks">
          <textarea rows={2} value={input.remarks ?? ""} disabled={disabled} placeholder="Any discrepancy noticed"
                    onChange={(e) => setInput({ ...input, remarks: e.target.value })} />
        </Field>
        <Field label="Verification report">
          <label className="file">
            <Icon name="upload" />
            <span>{report ? report.name : "Upload PDF or image"}</span>
            <input type="file" accept=".pdf,.png,.jpg,.jpeg" disabled={disabled} onChange={(e) => setReport(e.target.files?.[0] ?? null)} />
          </label>
        </Field>
        <Field label="Decision">
          <div className="decision" role="radiogroup">
            {DECISIONS.map(([value, label, cls]) => (
              <button key={value} type="button" disabled={disabled}
                      className={`${cls} ${input.requested_decision === value ? "is-on" : ""}`}
                      onClick={() => setInput({ ...input, requested_decision: value })}>{label}</button>
            ))}
          </div>
        </Field>
        {shownCheck && <ReportCheckLine check={shownCheck} />}
        {error && <Notice tone="bad">{error.message}{error.missing && <ul>{error.missing.map((m, i) => <li key={i}>{m}</li>)}</ul>}</Notice>}
        {result && (
          <Notice tone={result.final_status === "POSITIVE" ? "ok" : result.final_status === "NEGATIVE" ? "bad" : "warn"}>
            Result: <StatusBadge status={result.final_status} />
            {result.blocked_reasons.length > 0 && <ul>{result.blocked_reasons.map((b, i) => <li key={i}>{b}</li>)}</ul>}
          </Notice>
        )}
      </div>
    </Card>
  );
}
