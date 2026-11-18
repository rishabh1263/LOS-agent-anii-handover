import type { ReportCheck } from "../types/api";
import { Card, StatusBadge } from "./ui";

/** The agency's report read and checked against the case, full width: the basic checks, then report vs screen. */
export function ReportCheckView({ check }: { check: ReportCheck }) {
  const basic = check.checks;
  const fields = check.comparisons;
  const result = (match: boolean | null) => (
    <StatusBadge status={match === true ? "PASS" : match === false ? "FAIL" : "REVIEW_REQUIRED"}
                 label={match === true ? "Match" : match === false ? "Mismatch" : "Not found"} />
  );
  const rows = (list: typeof fields) => list.map((c, i) => (
    <tr key={i} className={c.match === false ? "is-bad" : undefined}>
      <th scope="row">{c.left_label}</th>
      <td>{c.left ?? "—"}</td>
      <td>{c.right ?? "—"}</td>
      <td className="result">{result(c.match)}{c.note && <div className="note">{c.note}</div>}</td>
    </tr>
  ));
  return (
    <Card icon="file" title="Agency report check" subtitle={check.reason}
          className={`report-check report-check-${check.status.toLowerCase()}`}
          actions={<StatusBadge status={check.status} large />}>
      <div className="grid-table-wrap">
        <table className="grid-table">
          <thead>
            <tr><th>Checked</th><th>In the report</th><th>On screen</th><th>Result</th></tr>
          </thead>
          <tbody>
            <tr className="group"><td colSpan={4}>Basic checks</td></tr>
            {rows(basic)}
            <tr className="group"><td colSpan={4}>Report vs case on screen</td></tr>
            {rows(fields)}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** One line for the agency card: where the full check is. */
export function ReportCheckLine({ check }: { check: ReportCheck }) {
  return (
    <p className="report-check-line">
      Report check: <StatusBadge status={check.status} />{" "}
      <a href="#sec-report-check" onClick={(e) => {
        e.preventDefault();
        document.getElementById("sec-report-check")?.scrollIntoView({ behavior: "smooth", block: "start" });
      }}>see the details</a>
    </p>
  );
}
