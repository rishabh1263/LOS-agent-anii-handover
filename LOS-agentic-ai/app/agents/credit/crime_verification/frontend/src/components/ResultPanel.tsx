import { useState } from "react";
import type { Subject } from "../types/api";
import { when } from "../utils/format";
import { t } from "../utils/text";
import { CountUp } from "./CountUp";
import { Icon } from "./Icon";
import { RiskGauge } from "./RiskGauge";
import { Empty, KeyValue, Notice } from "./ui";

/** One person's result -- each person's result stands on its own. Every word from config (screen.result). */
export function ResultPanel({ subject, riskLabels, arcLabels, waivedReason, canDownload, onDownload, downloading }: {
  subject: Subject | null;
  riskLabels: Record<string, string>;
  arcLabels?: Record<string, string>;
  waivedReason?: string | null;
  canDownload: boolean;
  onDownload: (s: Subject) => void;
  downloading: boolean;
}) {
  const [showCoverage, setShowCoverage] = useState(false);
  if (!subject) return <Empty icon="shield" title={t("result.none_title")}>{t("result.none_text")}</Empty>;
  if (subject.status === "WAIVED") {
    return <div className="result"><Notice kind="info" title={t("result.waived")}>{waivedReason}</Notice></div>;
  }
  if (subject.status === "PROCESSING" || subject.status === "INITIATED") {
    return (
      <div className="result">
        <div className="pending">
          <span className="radar" aria-hidden="true"><span className="radar-sweep" /><span className="radar-dot" /></span>
          <div>
            <strong>{subject.outcome_label}</strong>
            <p className="muted small">{t("result.pending_text", { ref: subject.provider_reference ?? "—" })}</p>
          </div>
        </div>
      </div>
    );
  }
  if (subject.status === "FAILED") {
    return <div className="result"><Notice kind="error" title={subject.outcome_label}>{subject.error?.message}</Notice></div>;
  }
  const r = subject.result;
  if (!r) return <div className="result" />;
  const found = r.groups.flatMap((g) => g.items).filter((i) => i.cases_found > 0);
  return (
    <div className="result">
      <div className={`risk-card risk-bg-${r.risk_level.toLowerCase().replace(/_/g, "-")}`}>
        <RiskGauge level={r.risk_level} label={riskLabels[r.risk_level] ?? r.risk_label} labels={arcLabels ?? riskLabels}
                   ariaLabel={t("result.overall_risk", { risk: riskLabels[r.risk_level] ?? r.risk_label })} />
        <p className="risk-meaning">{r.risk_meaning}</p>
      </div>
      <div className="stat-row">
        <div className="stat"><span className="stat-value"><CountUp value={r.total_cases} /></span>
          <span className="stat-label">{t("result.cases_found")}</span></div>
        <div className="stat"><span className="stat-value">{found.length}</span>
          <span className="stat-label">{t("result.sources_hit")}</span></div>
        <div className="stat"><span className="stat-value">{r.groups.reduce((n, g) => n + g.items.length, 0)}</span>
          <span className="stat-label">{t("result.sources_covered")}</span></div>
      </div>
      <p className="summary"><Icon name="info" /> {r.summary}</p>
      {r.clusters.length > 0 && (
        <div className="chips" aria-label={t("result.matter_types")}>
          {r.clusters.map((c) => <span key={c.cluster} className="chip">{c.label}<b>{c.count}</b></span>)}
        </div>
      )}
      {found.length > 0 && (
        <table className="table compact">
          <thead><tr><th>{t("result.source_with_findings")}</th><th className="num">{t("result.cases")}</th></tr></thead>
          <tbody>{found.map((i) => <tr key={i.code}><td>{i.name}</td><td className="num"><b>{i.cases_found}</b></td></tr>)}</tbody>
        </table>
      )}
      <button type="button" className="btn btn-link" aria-expanded={showCoverage} onClick={() => setShowCoverage(!showCoverage)}>
        <Icon name="chevron" className={showCoverage ? "rot-90" : ""} /> {showCoverage ? t("result.hide_coverage") : t("result.show_coverage")}
      </button>
      {showCoverage && (
        <table className="table compact">
          <thead><tr><th>{t("result.source")}</th><th className="num">{t("result.since")}</th><th className="num">{t("result.cases")}</th></tr></thead>
          <tbody>
            {r.groups.map((g) => [
              <tr key={g.group} className="group-row"><td colSpan={3}>{g.label}</td></tr>,
              ...g.items.map((i) => (
                <tr key={i.code} className={i.cases_found ? "is-hit" : undefined}>
                  <td>{i.name}</td><td className="num">{i.coverage_since}</td><td className="num">{i.cases_found}</td>
                </tr>
              )),
            ])}
          </tbody>
        </table>
      )}
      <KeyValue items={[[t("result.provider_ref"), <span className="mono">{r.provider_reference}</span>],
                        [t("result.received"), when(r.result_at)]]} />
      {canDownload && (
        <button type="button" className="btn btn-secondary" onClick={() => onDownload(subject)} disabled={downloading}>
          <Icon name="download" /> {downloading ? t("result.preparing") : t("result.download")}
        </button>
      )}
    </div>
  );
}
