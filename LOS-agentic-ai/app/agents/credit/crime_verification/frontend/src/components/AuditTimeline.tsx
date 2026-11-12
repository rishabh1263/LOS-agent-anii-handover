import { useState } from "react";
import type { AuditEvent } from "../types/api";
import { when } from "../utils/format";
import { t } from "../utils/text";
import { Empty } from "./ui";

function detail(e: AuditEvent, roles: Record<string, string>, risk: Record<string, string>,
                decisions: Record<string, string>): string {
  const m = e.metadata ?? {};
  const parts: string[] = [];
  // the server writes each person's label next to their key ("CO_APPLICANT-2" -> "Co-Applicant 2")
  if (typeof m.label === "string") parts.push(m.label);
  else if (typeof m.role === "string") parts.push(roles[m.role] ?? m.role);
  if (Array.isArray(m.labels)) parts.push(m.labels.map(String).join(" + "));
  else if (Array.isArray(m.roles)) parts.push(m.roles.map((r) => roles[String(r)] ?? String(r)).join(" + "));
  if (typeof m.risk_level === "string") parts.push(risk[m.risk_level] ?? m.risk_level);
  if (typeof m.decision === "string") parts.push(decisions[m.decision] ?? m.decision);
  return parts.join(" · ");
}

/** The newest `visible` events (config ui.activity_visible; 0 = all), the rest behind "Show all" -- a case with ten
 *  co-applicants writes dozens of events, and the page should not grow by a screen for them. */
export function AuditTimeline({ events, roles, risk, decisions, visible = 0 }: {
  events: AuditEvent[]; roles: Record<string, string>; risk: Record<string, string>; decisions: Record<string, string>;
  visible?: number;
}) {
  const [all, setAll] = useState(false);
  if (!events.length) return <Empty icon="history" title={t("results.no_activity")} />;
  const newest = [...events].reverse();
  const cut = visible > 0 && !all && newest.length > visible;
  return (
    <>
    <ol className="timeline">
      {(cut ? newest.slice(0, visible) : newest).map((e) => (
        <li key={e.event_id} className={`tl-${e.event_type.toLowerCase()}`}>
          <span className="tl-dot" aria-hidden="true" />
          <div>
            <strong>{e.label}</strong>
            {detail(e, roles, risk, decisions) && <span className="muted"> · {detail(e, roles, risk, decisions)}</span>}
            <div className="muted small">{e.actor} · {when(e.at)}{e.case_id ? <> · <span className="mono">{e.case_id}</span></> : null}</div>
          </div>
        </li>
      ))}
    </ol>
    {visible > 0 && newest.length > visible && (
      <button type="button" className="btn btn-link" aria-expanded={all} onClick={() => setAll(!all)}>
        {all ? t("results.activity_less") : t("results.activity_all", { n: newest.length })}
      </button>
    )}
    </>
  );
}
