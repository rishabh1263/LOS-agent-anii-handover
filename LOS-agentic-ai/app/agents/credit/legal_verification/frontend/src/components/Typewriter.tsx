import { useEffect, useState } from "react";
import { ui } from "../utils/text";

/** Text that types itself, pauses, erases and types again (owner 2026-10-30: "auto type again and again"). The whole
 *  text is always the accessible name; with reduced motion it simply shows. */
export function Typewriter({ text, className }: { text: string; className?: string }) {
  const [n, setN] = useState(0);
  const [erasing, setErasing] = useState(false);
  const still = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  useEffect(() => {
    if (still || !text) return;
    const full = n >= text.length;
    const speed = ui().typewriter ?? {};                 // config ui.typewriter
    const delay = erasing ? speed.erase_ms ?? 45 : full ? speed.hold_ms ?? 2200 : n === 0 ? speed.rest_ms ?? 500 : speed.type_ms ?? 95;
    const timer = window.setTimeout(() => {
      if (!erasing && full) setErasing(true);
      else if (erasing && n === 0) setErasing(false);
      else setN((x) => x + (erasing ? -1 : 1));
    }, delay);
    return () => window.clearTimeout(timer);
  }, [n, erasing, text, still]);
  return (
    <span className={`typewriter ${className ?? ""}`} aria-label={text}>
      <span aria-hidden="true">{still ? text : text.slice(0, n)}</span>
      <span className="typewriter-caret" aria-hidden="true" />
    </span>
  );
}
