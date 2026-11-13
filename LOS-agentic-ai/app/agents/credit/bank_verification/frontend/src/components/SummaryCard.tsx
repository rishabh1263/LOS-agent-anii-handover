import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { PlainSummary, Run } from "../types/api";
import { Icon } from "./Icon";
import { ErrorNote, Loading } from "./ui";

const POLL_MS = 3000;
const POLL_FOR_MS = 150_000;

/** The summary as points: one per sentence. */
export const points = (text: string) =>
  // the server sends numbered points one per line (2026-11-02); an older text: one point per sentence
  (text.includes("\n") ? text.split("\n") : text.split(/(?<=[.!?])\s+/)).map((s) => s.trim()).filter(Boolean);

/**
 * The result in a few plain points, in the top-right corner of the result banner (owner 2026-10-29: "summary top
 * corner me, points me, cleanly"). While a better-worded paragraph is being prepared the card shows the current
 * one and quietly refreshes.
 */
export function SummaryCard({ caseId, run }: { caseId: string; run: Run }) {
  const [summary, setSummary] = useState<PlainSummary | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const started = Date.now();
    setSummary(null);
    setError(null);
    const load = () =>
      api.plainSummary(caseId)
        .then((s) => {
          if (stopped) return;
          setSummary(s);
          if (s.pending && Date.now() - started < POLL_FOR_MS) timer = setTimeout(load, POLL_MS);
        })
        .catch((e: Error) => !stopped && setError(e));
    load();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [caseId, run.run_id]);

  return (
    <aside className="banner-summary" aria-label="Summary">
      <div className="banner-summary-head">
        <Icon name="sparkles" size={14} /> <span>Summary</span>
        {summary?.pending && <span className="updating"><span className="spinner" aria-hidden /> Updating</span>}
      </div>
      {error && <ErrorNote error={error} />}
      {!error && !summary && <Loading what="the summary" />}
      {summary && (
        <ul className="summary-points" data-testid="summary-text" style={{ listStyle: "none", paddingLeft: 0 }}>
          {points(summary.text).map((p, i) => <li key={i}>{p}</li>)}
        </ul>
      )}
    </aside>
  );
}
