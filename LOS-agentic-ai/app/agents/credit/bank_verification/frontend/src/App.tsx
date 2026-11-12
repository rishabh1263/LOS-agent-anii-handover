import { useEffect, useState } from "react";
import { api } from "./services/api";
import type { AppConfig } from "./types/api";
import { SearchBox } from "./pages/SearchPage";
import { CaseDetailPage } from "./pages/CaseDetailPage";
import { Icon } from "./components/Icon";
import { ErrorNote, Loading } from "./components/ui";

function caseFromHash(): string | null {
  const match = window.location.hash.match(/^#\/case\/([A-Za-z0-9-]+)/);
  return match ? match[1] : null;
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [caseId, setCaseId] = useState<string | null>(caseFromHash());

  useEffect(() => { api.config().then(setConfig).catch(setError); }, []);
  useEffect(() => {
    const onHash = () => setCaseId(caseFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const open = (id: string) => { window.location.hash = `#/case/${id}`; };

  return (
    <>
      <header className="topbar">
        <div className="topbar-in">
          <button className="brand" onClick={() => { window.location.hash = ""; }}>
            <span className="mark"><Icon name="bank" size={18} /></span>Bank Verification Agent
          </button>
          <SearchBox onFound={open} current={caseId} />
        </div>
      </header>
      <main className="page">
        {error && <ErrorNote error={error} onRetry={() => { setError(null); api.config().then(setConfig).catch(setError); }} />}
        {!error && !config && <Loading what="" />}
        {config && !caseId && (
          <div className="empty">
            <span className="mark"><Icon name="search" size={24} /></span>
            <h1>Search a case</h1>
            <p>Enter the case ID or applicant ID in the search bar above to load its details.</p>
          </div>
        )}
        {config && caseId && <CaseDetailPage key={caseId} caseId={caseId} config={config} />}
      </main>
    </>
  );
}
