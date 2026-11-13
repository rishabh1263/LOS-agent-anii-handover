import { FormEvent, useEffect, useState } from "react";
import { ApiError, api } from "../services/api";
import { Icon } from "../components/Icon";

/**
 * The search in the top bar: the officer types the case ID or application number and that ONE case loads below as a
 * form. The screen never lists cases (owner 2026-10-26) -- an exact match only.
 */
export function SearchBox({ onFound, current }: { onFound: (caseId: string) => void; current: string | null }) {
  const [ref, setRef] = useState(current ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { if (current) setRef(current); }, [current]);

  async function search(e: FormEvent) {
    e.preventDefault();
    const wanted = ref.trim();
    if (wanted.length < 3) {
      setError("Enter the full case ID or applicant ID.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onFound((await api.lookup(wanted)).case_id);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 404
        ? `No case found for “${wanted}”.`
        : "Search is not available right now. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="search">
      <form onSubmit={search} role="search">
        <div className="search-input">
          <Icon name="search" size={16} />
          <input value={ref} onChange={(e) => { setRef(e.target.value); setError(null); }}
                 placeholder="Case ID or applicant ID" aria-label="Case ID or applicant ID" />
        </div>
        <button className="btn btn-primary" disabled={busy}>{busy ? "Searching…" : "Search"}</button>
      </form>
      {error && <div className="search-error" role="alert"><Icon name="alert" size={14} /> {error}</div>}
    </div>
  );
}
