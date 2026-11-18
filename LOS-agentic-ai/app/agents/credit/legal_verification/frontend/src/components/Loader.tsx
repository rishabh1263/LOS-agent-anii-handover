import { useEffect, useId, useState } from "react";
import { t, ui } from "../utils/text";


/** The loader -- small, exactly centred, no box, an icon in the middle (owner: "loader chota aur beech me ek icon ke
 *  saath", "koi box me nahi", "aur accha loader"): a gradient arc that draws itself round while it turns, a thin amber
 *  arc the other way, sparks chasing round, and in the middle a glowing tile whose icon is "scanned" by a light line and
 *  flips to the next one with each line about what is being done (config screen.loader.<topic>, one per line, in turn).
 *  `overlay` covers the window, centred in it. */
export function Loader({ topic, overlay = false, compact = false }: { topic: string; overlay?: boolean; compact?: boolean }) {
  // before the config arrives there are no words yet: the rings turn alone
  const lines = t(`loader.${topic}`).split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("⟨"));
  const [i, setI] = useState(0);
  const gid = `lg${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  useEffect(() => {
    if (lines.length < 2) return;
    const timer = window.setInterval(() => setI((x) => (x + 1) % lines.length), ui().loader_line_ms ?? 1300);
    return () => window.clearInterval(timer);
  }, [lines.length]);
  const brand = t("loader.brand").startsWith("⟨") ? "" : t("loader.brand");      // config screen.loader.brand
  const body = (
    <div className="lv-loader" role="status" aria-live="polite">
      <span className="lv-spin" aria-hidden="true">
        <svg viewBox="0 0 80 80" className="lv-svg">
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="var(--acc-1)" /><stop offset="100%" stopColor="var(--acc-2)" />
            </linearGradient>
          </defs>
          <circle cx="40" cy="40" r="34" className="lv-track" />
          <g className="lv-turn"><circle cx="40" cy="40" r="34" className="lv-arc" stroke={`url(#${gid})`} /></g>
          <g className="lv-turn-back"><circle cx="40" cy="40" r="27" className="lv-arc2" /></g>
        </svg>
        <span className="lv-sparks"><i /><i /><i /></span>
        {/* the emblem in the middle (owner 2026-10-30: "beech ka logo change"): a sapphire seal with the scales of
            justice, the beam weighing to and fro */}
        <span className="lv-emblem">
          <svg viewBox="0 0 40 40">
            <defs>
              <linearGradient id={`${gid}h`} x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stopColor="var(--acc-1)" /><stop offset="100%" stopColor="var(--acc-2)" />
              </linearGradient>
            </defs>
            <path className="em-hex" d="M20 2 L35.6 11 L35.6 29 L20 38 L4.4 29 L4.4 11 Z" fill={`url(#${gid}h)`} />
            <path className="em-ring" d="M20 6.5 L31.7 13.25 L31.7 26.75 L20 33.5 L8.3 26.75 L8.3 13.25 Z" />
            <g className="em-scales">
              <circle cx="20" cy="9.6" r="1.4" />
              <path d="M20 11 V29 M15.5 29.5 H24.5" />
              <g className="em-beam">
                <path d="M11.5 13.5 H28.5" />
                <path d="M11.5 13.5 L8.6 20.2 M11.5 13.5 L14.4 20.2 M28.5 13.5 L25.6 20.2 M28.5 13.5 L31.4 20.2" />
                <path d="M7.8 20.2 Q11.5 24.6 15.2 20.2 Z M24.8 20.2 Q28.5 24.6 32.2 20.2 Z" className="em-pans" />
              </g>
            </g>
          </svg>
          <span className="lv-scan" />
        </span>
      </span>
      {brand && (
        // the wordmark is written, not shown: its outline draws itself in the palette, then it fills, a swoosh under it
        <svg className="lv-brand" viewBox="0 0 132 34" aria-hidden="true">
          <defs>
            <linearGradient id={`${gid}b`} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="var(--acc-1)" /><stop offset="55%" stopColor="var(--acc-2)" />
              <stop offset="100%" stopColor="var(--warm)" />
            </linearGradient>
          </defs>
          <text x="66" y="23" textAnchor="middle" className="lv-brand-word" stroke={`url(#${gid}b)`} fill={`url(#${gid}b)`}>{brand}</text>
          <path d="M22 29 Q66 35 110 28" className="lv-brand-swoosh" />
        </svg>
      )}
      {lines[i] && <span key={`t${i}`} className="lv-text">{lines[i]}</span>}
      {lines.length > 1 && (
        <span className="mini-steps" aria-hidden="true">{lines.map((_, j) => <i key={j} className={j === i ? "on" : j < i ? "done" : ""} />)}</span>
      )}
    </div>
  );
  return <div className={overlay ? "loader-overlay" : compact ? "loader-wrap is-compact" : "loader-wrap"}>{body}</div>;
}
