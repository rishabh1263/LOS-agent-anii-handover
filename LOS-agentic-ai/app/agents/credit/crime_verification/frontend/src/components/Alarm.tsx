import { useEffect, useState } from "react";
import { playAlert, soundOn } from "../utils/chime";
import { Icon } from "./Icon";

/**
 * A result with findings that need review: red pulse rings, a warning shield that shakes, a badge naming who has
 * findings, and a short two-tone alert (config ui.alarm_sound, the viewer's mute switch). Serious, not loud: gone
 * after ~4 s; with reduced motion only the badge shows, briefly.
 */
export function Alarm({ title, subtitle, sound, onDone }: { title: string; subtitle: string; sound: boolean; onDone: () => void }) {
  const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const [phase, setPhase] = useState<"in" | "out">("in");
  useEffect(() => {
    if (sound && soundOn()) playAlert();
    const fade = window.setTimeout(() => setPhase("out"), reduced ? 1800 : 3200);
    const end = window.setTimeout(onDone, reduced ? 2200 : 4200);
    return () => { window.clearTimeout(fade); window.clearTimeout(end); };
  }, [sound, reduced, onDone]);

  return (
    <div className={`alarm is-${phase}`} aria-live="assertive">
      {!reduced && (
        <div className="alarm-rings" aria-hidden="true"><span /><span /><span /></div>
      )}
      <div className="alarm-badge" role="alert">
        <span className="alarm-icon"><Icon name="alert" size={34} /></span>
        <strong>{title}</strong>
        <span>{subtitle}</span>
      </div>
    </div>
  );
}
