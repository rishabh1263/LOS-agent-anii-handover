import { useEffect, useMemo, useState } from "react";
import { playChime, soundOn } from "../utils/chime";
import { Icon } from "./Icon";
import { Petals } from "./Petals";

const BURST = ["#5b3df5", "#9b4dff", "#e0458f", "#ffb547", "#20c997", "#ff7aa2", "#7ee0c3"];

/**
 * The clean-result celebration: a burst of colour from the centre, an "All clear" badge that pops in, petals and
 * little flowers drifting down, and a short chime (config ui.celebration_sound, the viewer's mute switch). Gone after
 * ~5 s; with reduced motion only the badge shows, briefly, and nothing moves.
 */
export function Celebration({ title, subtitle, sound, onDone }: {
  title: string; subtitle: string; sound: boolean; onDone: () => void;
}) {
  const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const [phase, setPhase] = useState<"in" | "out">("in");
  const sparks = useMemo(() => Array.from({ length: 42 }, (_, i) => {
    const angle = (i / 42) * Math.PI * 2 + Math.random() * 0.3;
    const distance = 140 + Math.random() * 220;
    return { dx: Math.cos(angle) * distance, dy: Math.sin(angle) * distance, colour: BURST[i % BURST.length],
             size: 6 + Math.random() * 8, delay: Math.random() * 0.15, round: i % 3 === 0 };
  }), []);

  useEffect(() => {
    if (sound && soundOn()) playChime();
    const fade = window.setTimeout(() => setPhase("out"), reduced ? 1600 : 3400);
    const end = window.setTimeout(onDone, reduced ? 2000 : 5200);
    return () => { window.clearTimeout(fade); window.clearTimeout(end); };
  }, [sound, reduced, onDone]);

  return (
    <div className={`celebration is-${phase}`} aria-live="polite">
      {!reduced && <Petals count={44} ms={5200} />}
      {!reduced && (
        <div className="burst" aria-hidden="true">
          {sparks.map((s, i) => (
            <span key={i} className={s.round ? "spark round" : "spark"} style={{
              background: s.colour, width: s.size, height: s.round ? s.size : s.size * 0.45, animationDelay: `${s.delay}s`,
              ["--dx" as string]: `${s.dx}px`, ["--dy" as string]: `${s.dy}px`,
            }} />
          ))}
        </div>
      )}
      <div className="celebrate-badge" role="status">
        <span className="celebrate-ring" aria-hidden="true" />
        <span className="celebrate-icon"><Icon name="shieldCheck" size={34} /></span>
        <strong>{title}</strong>
        <span>{subtitle}</span>
      </div>
    </div>
  );
}
