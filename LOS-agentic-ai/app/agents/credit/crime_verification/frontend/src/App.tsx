import { useEffect, useState } from "react";
import { Icon } from "./components/Icon";
import { Notice, Skeleton } from "./components/ui";
import { DashboardPage } from "./pages/DashboardPage";
import { ResultsPage } from "./pages/ResultsPage";
import { VerifyPage } from "./pages/VerifyPage";
import { ApiError, api } from "./services/api";
import type { UiConfig } from "./types/api";
import { setScreen, t } from "./utils/text";

type Route = { page: "dashboard" } | { page: "verify"; id: string | null } | { page: "case"; id: string };

function parse(hash: string): Route {
  const [first, second] = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const id = second ? decodeURIComponent(second) : null;
  if (first === "cases" && id) return { page: "case", id };
  if (first === "verify" || first === "applications") return { page: "verify", id };     // old links keep working
  return { page: "dashboard" };
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  const [config, setConfig] = useState<UiConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onHash = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    api.config().then((c) => { setScreen(c.screen as Parameters<typeof setScreen>[0]); setConfig(c); })
      .catch((e) => setError(e instanceof ApiError ? e.message : t("common.unreachable")));
  }, []);
  useEffect(() => { window.scrollTo(0, 0); }, [route.page, "id" in route ? route.id : null]);

  const go = (hash: string) => { window.location.hash = hash; };
  const openApplication = (id: string) => go(`#/verify/${encodeURIComponent(id)}`);
  const openCase = (id: string) => go(`#/cases/${encodeURIComponent(id)}`);
  const resetDemo = async () => {
    if (!window.confirm(t("nav.reset_confirm"))) return;
    await api.resetDemo();
    window.location.reload();
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark"><Icon name="shieldCheck" size={18} /></span>
          <div><strong>{config ? t("brand.title") : ""}</strong><span>{config ? t("brand.subtitle") : ""}</span></div>
        </div>
        <nav aria-label={t("common.main_nav")}>
          <a href="#/" className={route.page === "dashboard" ? "is-active" : undefined}
             aria-current={route.page === "dashboard" ? "page" : undefined}><Icon name="dashboard" /> {config ? t("nav.dashboard") : ""}</a>
          <a href="#/verify" className={route.page !== "dashboard" ? "is-active" : undefined}
             aria-current={route.page !== "dashboard" ? "page" : undefined}><Icon name="shieldCheck" /> {config ? t("nav.verify") : ""}</a>
        </nav>
        <div className="sidebar-foot">
          {config && (
            <div className="user-chip">
              <span className="avatar sm" aria-hidden="true"><Icon name="user" /></span>
              <div><strong>{config.user.id || "—"}</strong><span>{config.user.permissions.includes("initiate") ? t("user.officer") : t("user.viewer")}</span></div>
            </div>
          )}
          {config?.show_reset && config.user.permissions.includes("reset_demo") && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={resetDemo}><Icon name="refresh" /> {t("nav.reset")}</button>
          )}
        </div>
      </aside>
      <div className="main">
        {config?.banner && <div className="notice-strip" role="note"><Icon name="alert" /> {config.banner}</div>}
        {error && <div className="page"><Notice kind="error" title={t("common.cannot_start")}>{error}</Notice></div>}
        {!config && !error && <div className="page"><Skeleton lines={8} /></div>}
        {config && route.page === "dashboard" && <DashboardPage config={config} onOpen={openCase} />}
        {config && route.page === "verify" &&
          <VerifyPage id={route.id} config={config} onSelect={openApplication} onOpenCase={openCase} />}
        {config && route.page === "case" &&
          <ResultsPage key={route.id} caseId={route.id} config={config}
                       onBack={(appId) => go(appId ? `#/verify/${encodeURIComponent(appId)}` : "#/verify")} />}
      </div>
    </div>
  );
}
