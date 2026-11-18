import { useEffect, useMemo, useState } from "react";

const COLOURS = ["#ff7aa2", "#ffb3c7", "#ffd166", "#b69cff", "#7ee0c3", "#ff9f7a"];

/** A short shower of flower petals over the page -- the celebration of a clean result. Purely decorative: hidden from
 *  screen readers, skipped entirely for people who prefer reduced motion, gone after `ms`. */
export function Petals({ count = 36, ms = 4800, onDone }: { count?: number; ms?: number; onDone?: () => void }) {
  const [alive, setAlive] = useState(true);
  const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const petals = useMemo(() => Array.from({ length: count }, (_, i) => ({
    left: Math.random() * 100,
    delay: Math.random() * 1.6,
    duration: 2.8 + Math.random() * 2.2,
    size: 10 + Math.random() * 12,
    drift: (Math.random() - 0.5) * 220,
    spin: (Math.random() - 0.5) * 720,
    colour: COLOURS[i % COLOURS.length],
    flower: i % 7 === 0,
  })), [count]);

  useEffect(() => {
    const t = window.setTimeout(() => { setAlive(false); onDone?.(); }, ms);
    return () => window.clearTimeout(t);
  }, [ms, onDone]);

  if (!alive || reduced) return null;
  return (
    <div className="petals" aria-hidden="true">
      {petals.map((p, i) => (
        <span key={i} className={p.flower ? "petal flower" : "petal"} style={{
          left: `${p.left}%`, width: p.size, height: p.flower ? p.size : p.size * 0.72, background: p.flower ? undefined : p.colour,
          color: p.colour, animationDelay: `${p.delay}s`, animationDuration: `${p.duration}s`,
          ["--drift" as string]: `${p.drift}px`, ["--spin" as string]: `${p.spin}deg`,
        }}>{p.flower ? "✿" : null}</span>
      ))}
    </div>
  );
}
