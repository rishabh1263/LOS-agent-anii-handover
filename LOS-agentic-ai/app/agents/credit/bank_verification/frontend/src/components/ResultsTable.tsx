import { useEffect, useState } from "react";
import type { RuleResult } from "../types/api";
import { Icon } from "./Icon";
import { StatusBadge } from "./ui";

const ICON: Record<string, string> = { PASS: "check", FAIL: "x", REVIEW_REQUIRED: "alert", NOT_APPLICABLE: "minus" };
const ORDER: Record<string, number> = { FAIL: 0, REVIEW_REQUIRED: 1, PASS: 2, NOT_APPLICABLE: 3 };

/** The one source every row of a check is compared with ("Statement"), or null when the rows differ. */
const oneSource = (r: RuleResult) => {
  const labels = new Set(r.comparisons.map((c) => c.right_label).filter(Boolean));
  return labels.size === 1 ? [...labels][0]! : null;
};
/** The column title names the source once ("Compared with statement") instead of a tag on every row. */
const comparedWith = (r: RuleResult) => {
  const source = oneSource(r);
  return source ? `Compared with ${source.charAt(0).toLowerCase()}${source.slice(1)}` : "Compared with";
};

/** Every verification check: status, the reason in words; open one to see what was compared with what. */
export function ResultsTable({ rules, actions, focus }:
  { rules: RuleResult[]; actions: Record<string, string>; focus?: { id: string; at: number } | null }) {
  const [open, setOpen] = useState<string | null>(null);
  // "Show" in the problems popup: open that check and bring it into view
  useEffect(() => {
    if (!focus) return;
    setOpen(focus.id);
    window.setTimeout(() => document.getElementById(`check-${focus.id}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
  }, [focus]);
  // the overall result is the banner, not a row; problems first, then reviews, then passed, then not applicable
  const shown = rules.filter((r) => r.rule_id !== "R8").sort((a, b) => ORDER[a.status] - ORDER[b.status]);
  return (
    <ul className="checks">
      {shown.map((r) => {
        const expanded = open === r.rule_id;
        return (
          <li key={r.rule_id} id={`check-${r.rule_id}`} className={`check check-${r.status.toLowerCase()} ${expanded ? "is-open" : ""}`}>
            <button className="check-row" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : r.rule_id)}>
              <span className="check-icon"><Icon name={ICON[r.status]} size={16} /></span>
              <span><span className="name">{r.rule_name}</span><span className="why">{r.reason}</span></span>
              <StatusBadge status={r.status} />
              <Icon name="chevron" className="chev" />
            </button>
            {expanded && (
              <div className="check-detail">
                {r.comparisons.length > 0 && (
                  <table className="compare">
                    <thead><tr><th>Checked</th><th>Value</th><th>{comparedWith(r)}</th><th>Result</th></tr></thead>
                    <tbody>
                      {r.comparisons.map((c, i) => (
                        <tr key={i}>
                          <td>{c.left_label}</td>
                          <td>{c.left ?? "—"}</td>
                          <td>{!oneSource(r) && c.right_label && <span className="muted">{c.right_label}: </span>}
                            {c.right ?? "—"}{c.note && <div className="muted small">{c.note}</div>}</td>
                          <td><StatusBadge status={c.match === true ? "PASS" : c.match === false ? "FAIL" : "REVIEW_REQUIRED"}
                                           label={c.match === true ? "Match" : c.match === false ? "Mismatch" : "Not confirmed"} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <div className="detail-cols">
                  {r.missing_evidence.length > 0 && <div><h3>Missing</h3><ul>{r.missing_evidence.map((e, i) => <li key={i}>{e}</li>)}</ul></div>}
                  {r.risk_flags.length > 0 && <div><h3>Watch out</h3><ul>{r.risk_flags.map((f, i) => <li key={i}>{f.message}</li>)}</ul></div>}
                  {r.recommended_action && <div><h3>What to do</h3><ul><li>{actions[r.recommended_action] ?? ""}</li></ul></div>}
                </div>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
