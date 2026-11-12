import type { CaseDetail } from "../types/api";
import { Icon } from "./Icon";

interface Step { id: string; title: string; note: string; state: "done" | "current" | "todo" }

/** Where the case stands: Bank details -> Verification -> Agency -> Report. A step scrolls to its section. */
export function Flow({ detail }: { detail: CaseDetail }) {
  const run = detail.latest_run;
  const final = ["POSITIVE", "NEGATIVE", "WAIVED"].includes(detail.case.status);
  const form = run?.form ?? detail.prefill;
  const filled = Boolean(form?.bank_name && form?.account_number);
  const steps: Step[] = [
    { id: "sec-details", title: "Bank details", note: filled ? "Filled" : "To fill",
      state: filled ? "done" : "current" },
    { id: "sec-checks", title: "Verification",
      note: detail.case.status === "READING" ? "Reading statement…"
        : run ? `${run.checks.checks_passed} of ${run.checks.checks_applicable} passed` : "Not run",
      state: run ? "done" : filled ? "current" : "todo" },
    { id: "sec-agency", title: "Agency", note: final ? (detail.case.status === "WAIVED" ? "Waived" : detail.case.status === "POSITIVE" ? "Positive" : "Negative") : run ? "Awaiting decision" : "After verification",
      state: final ? "done" : run ? "current" : "todo" },
    { id: "sec-report", title: "Report", note: final ? "Ready to download" : "After the decision",
      state: final ? "current" : "todo" },
  ];
  const go = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  return (
    <ol className="flow" aria-label="Verification flow">
      {steps.map((s, i) => (
        <li key={s.id}>
          <button className={s.state} onClick={() => go(s.id)} aria-current={s.state === "current" ? "step" : undefined}>
            <span className="num">{s.state === "done" ? <Icon name="check" size={13} /> : i + 1}</span>
            <span><b>{s.title}</b><small>{s.note}</small></span>
          </button>
        </li>
      ))}
    </ol>
  );
}
