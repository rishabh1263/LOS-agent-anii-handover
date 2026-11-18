import { useEffect, useState } from "react";
import { CountUp } from "../components/CountUp";
import { Icon } from "../components/Icon";
import { Badge, Card, Empty, Notice, Skeleton } from "../components/ui";
import { ApiError, api } from "../services/api";
import type { CaseRow, Dashboard, UiConfig } from "../types/api";
import { when } from "../utils/format";
import { t } from "../utils/text";

// the tiles: which number, which status filter it toggles, how it looks (labels: config screen.dashboard.tiles)
const TILES: { key: keyof Dashboard & string; status: string; icon: string; tone: string }[] = [
  { key: "total", status: "", icon: "folder", tone: "na" },
  { key: "in_progress", status: "PROCESSING", icon: "clock", tone: "info" },
  { key: "completed", status: "COMPLETED", icon: "check", tone: "ok" },
  { key: "review_required", status: "REVIEW_REQUIRED", icon: "alert", tone: "warn" },
  { key: "failed", status: "FAILED", icon: "x", tone: "bad" },
  { key: "waived", status: "WAIVED", icon: "lock", tone: "na" },
];

/** Numbers and the case table, straight from the database (GET /dashboard, /cases). */
export function DashboardPage({ config, onOpen }: { config: UiConfig; onOpen: (caseId: string) => void }) {
  const [stats, setStats] = useState<Dashboard | null>(null);
  const [rows, setRows] = useState<CaseRow[] | null>(null);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.dashboard().then(setStats).catch((e) => setError(e instanceof ApiError ? e.message : t("common.loading_failed")));
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.cases(search, status).then((r) => setRows(r.cases))
        .catch((e) => setError(e instanceof ApiError ? e.message : t("common.loading_failed")));
    }, 200);
    return () => window.clearTimeout(timer);
  }, [search, status]);

  const withResults = stats ? stats.completed + stats.review_required : 0;
  return (
    <div className="page">
      <header className="hero hero-dash">
        <div className="hero-glow" aria-hidden="true" />
        <div>
          <span className="hero-kicker">{t("dashboard.kicker")}</span>
          <h1>{t("dashboard.title")}</h1>
          <p>{t("dashboard.subtitle")}</p>
        </div>
        {stats && (
          <div className="hero-ring">
            <svg viewBox="0 0 120 120" aria-hidden="true">
              <circle cx="60" cy="60" r="50" className="ring-track" />
              <circle cx="60" cy="60" r="50" className="ring-fill" pathLength={100}
                      style={{ strokeDasharray: `${stats.total ? (withResults / stats.total) * 100 : 0} 100` }} />
            </svg>
            <div><strong><CountUp value={withResults} /></strong><span>{t("dashboard.ring")}</span></div>
          </div>
        )}
      </header>
      {error && <Notice kind="error">{error}</Notice>}
      <div className="kpis">
        {TILES.map((tile) => (
          <button key={tile.key} type="button" className={`kpi tone-${tile.tone}${status === tile.status && tile.status ? " is-on" : ""}`}
                  onClick={() => setStatus(status === tile.status ? "" : tile.status)}
                  aria-pressed={status === tile.status && !!tile.status}>
            <span className="kpi-icon"><Icon name={tile.icon} /></span>
            <span className="kpi-value">{stats ? <CountUp value={Number(stats[tile.key])} /> : "–"}</span>
            <span className="kpi-label">{t(`dashboard.tiles.${tile.key}`)}</span>
          </button>
        ))}
      </div>
      <Card title={t("dashboard.table_title")} subtitle={stats ? t("dashboard.table_subtitle", { n: stats.applications }) : undefined}
            actions={
              <div className="filters">
                <label className="search">
                  <Icon name="search" />
                  <input className="input" placeholder={t("dashboard.search")} value={search}
                         onChange={(e) => setSearch(e.target.value)} aria-label={t("dashboard.search")} />
                </label>
                <select className="input" value={status} onChange={(e) => setStatus(e.target.value)} aria-label={t("dashboard.columns.status")}>
                  <option value="">{t("dashboard.all_statuses")}</option>
                  {Object.entries(config.statuses).filter(([k]) => k !== "NOT_INITIATED")
                    .map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </select>
              </div>
            }>
        {!rows ? <Skeleton lines={5} /> : rows.length === 0 ? (
          <Empty icon="search" title={t("dashboard.none_title")}>{t("dashboard.none_text")}</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  {(["case", "application", "applicant"] as const).map((c) => <th key={c}>{t(`dashboard.columns.${c}`)}</th>)}
                  <th className="num">{t("dashboard.columns.subjects")}</th>
                  {(["vendor", "status", "result", "initiated"] as const).map((c) => <th key={c}>{t(`dashboard.columns.${c}`)}</th>)}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.case_id} className="clickable" tabIndex={0} onClick={() => onOpen(r.case_id)}
                      onKeyDown={(e) => { if (e.key === "Enter") onOpen(r.case_id); }}>
                    <td className="mono">{r.case_id}</td>
                    <td className="mono">{r.application_id}</td>
                    <td>{r.applicant ?? "—"}</td>
                    <td className="num">{r.subject_count}</td>
                    <td>{r.vendor_name}</td>
                    <td><Badge value={r.status} label={r.status_label} /></td>
                    <td>
                      <div className="result-cell">
                        {r.results.map((x) => (
                          <span key={x.party} className="muted small">{x.role_label}: {x.risk
                            ? <Badge value={x.risk} label={x.risk_label} /> : <Badge value={x.status} label={x.status_label} />}</span>
                        ))}
                      </div>
                    </td>
                    <td className="muted small">{when(r.initiated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
