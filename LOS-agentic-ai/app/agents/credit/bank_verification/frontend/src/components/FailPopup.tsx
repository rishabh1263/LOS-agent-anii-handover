import { useEffect } from "react";
import type { RuleResult } from "../types/api";
import { Icon } from "./Icon";

/**
 * Only the checks that FOUND A PROBLEM (the red ones), in a popup the moment the result is in, so they are not
 * missed: each with what differs, and "Show" to open it in the list. Closed once, it stays closed for that result.
 */
export function FailPopup({ rules, onShow, onClose }:
  { rules: RuleResult[]; onShow: (ruleId: string) => void; onClose: () => void }) {
  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);

  return (
    <div className="overlay" role="presentation" onClick={onClose}>
      <div className="popup" role="alertdialog" aria-modal="true" aria-labelledby="fail-title"
           onClick={(e) => e.stopPropagation()}>
        <header className="popup-head">
          <span className="popup-icon"><Icon name="x" size={18} /></span>
          <div>
            <h2 id="fail-title">{rules.length === 1 ? "1 check found a problem" : `${rules.length} checks found a problem`}</h2>
            <p>Look at these before the case goes further.</p>
          </div>
          <button className="btn btn-ghost" aria-label="Close" onClick={onClose}><Icon name="x" /></button>
        </header>
        <ul className="popup-list">
          {rules.map((r) => {
            const differs = r.comparisons.filter((c) => c.match === false).slice(0, 3);
            return (
              <li key={r.rule_id}>
                <div className="popup-row">
                  <div>
                    <strong>{r.rule_name}</strong>
                    <p>{r.reason}</p>
                  </div>
                  <button className="btn btn-ghost" onClick={() => onShow(r.rule_id)}>Show</button>
                </div>
                {differs.length > 0 && (
                  <table className="compare popup-compare">
                    <tbody>
                      {differs.map((c, i) => (
                        <tr key={i}>
                          <td>{c.left_label}</td>
                          <td>{c.left ?? "—"}</td>
                          <td>{c.right ?? "—"}{c.note && <div className="muted small">{c.note}</div>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </li>
            );
          })}
        </ul>
        <footer className="popup-foot"><button className="btn btn-primary" onClick={onClose}>Got it</button></footer>
      </div>
    </div>
  );
}
