// The risk meter of the CrimeCheck report: five labelled arcs from Very High Risk (left) to No Risk (right), each
// with its name written along it, and a needle that swings to the person's level. Colour, position AND the word carry
// the level; the caption below names it again.
const LEVELS = ["NO_RISK", "LOW_RISK", "AVERAGE_RISK", "HIGH_RISK", "VERY_HIGH_RISK"];
const R = 100;          // arc centre line
const W = 34;           // arc thickness
const CX = 130;
const CY = 128;
const GAP = 0.018;      // radians between arcs

const point = (r: number, a: number) => `${(CX + r * Math.cos(a)).toFixed(2)} ${(CY - r * Math.sin(a)).toFixed(2)}`;

/** Arc i of five, drawn left -> right (i = 0 is the leftmost). */
function band(i: number, r: number) {
  const start = Math.PI - (i * Math.PI) / 5 - GAP;
  const end = Math.PI - ((i + 1) * Math.PI) / 5 + GAP;
  return `M ${point(r, start)} A ${r} ${r} 0 0 1 ${point(r, end)}`;
}

export function RiskGauge({ level, label, labels, ariaLabel }: {
  level: string; label: string; labels: Record<string, string>; ariaLabel: string;
}) {
  const index = Math.max(0, LEVELS.indexOf(level));
  const order = [...LEVELS].reverse();                       // left -> right: Very High ... No Risk
  const angle = 72 - 36 * index;                             // clockwise from straight up: the middle of its arc
  const id = `g${Math.random().toString(36).slice(2, 8)}`;
  return (
    <figure className="gauge" role="img" aria-label={ariaLabel}>
      <svg viewBox="0 0 260 150" width="100%">
        <defs>
          {order.map((l, i) => <path key={l} id={`${id}-${i}`} d={band(i, R - 4)} />)}
          <filter id={`${id}-glow`} x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="3" stdDeviation="4" floodOpacity=".28" />
          </filter>
        </defs>
        {order.map((l, i) => {
          const on = l === level;
          return (
            <g key={l} className={`gauge-band arc-${l.toLowerCase().replace(/_/g, "-")}${on ? " is-on" : ""}`}
               filter={on ? `url(#${id}-glow)` : undefined}>
              <path d={band(i, R)} className="gauge-arc" style={{ strokeWidth: W }} />
              <text className="gauge-word" dy="4">
                <textPath href={`#${id}-${i}`} startOffset="50%" textAnchor="middle">{labels[l] ?? l}</textPath>
              </text>
            </g>
          );
        })}
        <g className="gauge-needle" style={{ ["--angle" as string]: `${angle}deg` }}>
          <path d={`M ${CX - 5} ${CY} L ${CX} ${CY - R + 26} L ${CX + 5} ${CY} Z`} />
        </g>
        <circle cx={CX} cy={CY} r="10" className="gauge-hub" />
        <circle cx={CX} cy={CY} r="4" className="gauge-hub-dot" />
      </svg>
      <figcaption className={`gauge-label tone-text-${level.toLowerCase().replace(/_/g, "-")}`}>{label}</figcaption>
    </figure>
  );
}
