import { useEffect, useRef, useState } from "react";
import { api } from "../services/api";
import type { CaseSummary } from "../types/api";
import { tone } from "../utils/format";
import { t } from "../utils/text";
import { Icon } from "./Icon";

const POLL_MS = 2500;
const POLL_LIMIT = 60;              // ~2.5 minutes; the rules-written summary stays if the model never answers
const TYPE_MS = 14;                 // per character of the typewriter

/** The summary as points: one per sentence (the same split as the PDF report). */
export const points = (text: string) =>
  // the server sends numbered points one per line (2026-11-02); an older text: one point per sentence
  (text.includes("\n") ? text.split("\n") : text.split(/(?<=[.!?])\s+/)).map((s) => s.trim()).filter(Boolean);

/** A leading "Applicant:" / "Co-Applicant 3:" (the rules-written summary) in bold. */
function lead(point: string) {
  const at = point.indexOf(":");
  if (at < 1 || at > 30) return point;
  return <><b>{point.slice(0, at + 1)}</b>{point.slice(at + 1)}</>;
}

export interface GlancePerson { party: string; label: string; name: string; level: string; levelLabel: string }

function useTypewriter(text: string, run: boolean) {
  const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const [shown, setShown] = useState(run && !reduced ? "" : text);
  useEffect(() => {
    if (!run || reduced) { setShown(text); return; }
    let i = 0;
    setShown("");
    const timer = window.setInterval(() => {
      i += 2;
      setShown(text.slice(0, i));
      if (i >= text.length) window.clearInterval(timer);
    }, TYPE_MS);
    return () => window.clearInterval(timer);
  }, [text, run, reduced]);
  return shown;
}

/**
 * The case summary at the top of the results page: an "at a glance" row (each person, their risk by colour AND word),
 * then the text -- the AI's when it passed the server's check (it types itself in when it arrives), else the
 * rules-written one while the AI writes (spinning border, "Writing…"). The ribbon says Clean result / Needs review.
 */
export function SummaryCard({ caseId, version, people, celebrate, labels }: {
  caseId: string; version: string; people: GlancePerson[]; celebrate: string[]; labels: Record<string, string>;
}) {
  const [summary, setSummary] = useState<CaseSummary | null>(null);
  const [typed, setTyped] = useState(false);
  const firstSource = useRef<string | null>(null);

  useEffect(() => {
    let alive = true;
    let tries = 0;
    let timer: number | undefined;
    firstSource.current = null;
    const load = async () => {
      try {
        const s = await api.summary(caseId);
        if (!alive) return;
        if (firstSource.current === null) firstSource.current = s.pending ? "pending" : s.source;
        // the AI text types itself in when it arrives while the screen is open (not when it was ready already)
        setTyped(s.source === "ai" && firstSource.current === "pending");
        setSummary(s);
        if (s.pending && ++tries < POLL_LIMIT) timer = window.setTimeout(load, POLL_MS);
      } catch { /* the summary is a convenience: the results stand on their own */ }
    };
    load();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [caseId, version]);

  const clean = people.length > 0 && people.every((p) => celebrate.includes(p.level));
  const text = useTypewriter(summary?.text ?? "", typed);
  if (!summary?.available || !summary.text) return null;
  const ai = summary.source === "ai";
  const writing = summary.pending;
  return (
    <section className={`ai-card ai-top${ai ? " is-ai" : ""}${writing ? " is-writing" : ""}${clean ? " is-clean" : " is-review"}`}
             aria-live="polite">
      <div className="ai-card-inner">
        <header className="ai-head">
          <span className="ai-orb" aria-hidden="true"><Icon name={ai ? "sparkles" : "file"} size={22} /></span>
          <div className="ai-titles">
            <h2>{ai ? labels.ai : writing ? labels.pending : labels.rules}</h2>
            <p>{ai ? labels.ai_note : labels.rules_note}</p>
          </div>
          {writing && <span className="writing"><span className="spinner spinner-xs" aria-hidden="true" /> {t("summary.writing")}</span>}
          <span className={`ai-ribbon ${clean ? "ok" : "warn"}`}>
            <Icon name={clean ? "check" : "alert"} size={13} /> {clean ? labels.clean : labels.review}
          </span>
        </header>
        {people.length > 0 && (
          <div className="glance" aria-label={t("results.at_a_glance")}>
            {people.map((p) => (
              <span key={p.party} className={`glance-chip tone-${tone(p.level)}`}>
                <span className="glance-dot" aria-hidden="true" />
                <span className="glance-who">{p.label}</span>
                <span className="glance-name">{p.name}</span>
                <b>{p.levelLabel}</b>
              </span>
            ))}
          </div>
        )}
        {/* in points: one per sentence -- every sentence names its person (owner 2026-10-29: "summary in points") */}
        <ul className="ai-text ai-points" style={{ listStyle: "none", paddingLeft: 0 }}>
          {points(text).map((p, i, all) => (
            <li key={i}>{lead(p)}
              {i === all.length - 1 && (writing || (typed && text.length < (summary.text ?? "").length)) &&
                <span className="caret" aria-hidden="true" />}</li>
          ))}
        </ul>
        {writing && <div className="ai-shimmer" aria-hidden="true"><span /><span /><span /></div>}
      </div>
    </section>
  );
}
