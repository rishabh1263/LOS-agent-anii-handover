import { useId, useState } from "react";

export interface Option {
  value: string;
  /** other words that find this option ("CNRB" finds Canara Bank) */
  keys?: string[];
}

const squash = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");

/** Options for what is typed: those starting with it first, then those containing it (spaces / case ignored). */
export function matches(options: Option[], typed: string, max: number): Option[] {
  const q = squash(typed);
  if (!q) return options.slice(0, max);
  const words = (o: Option) => [o.value, ...(o.keys ?? [])].map(squash);
  const starts = options.filter((o) => words(o).some((w) => w.startsWith(q)) ||
                                       o.value.toLowerCase().split(/\s+/).some((w) => squash(w).startsWith(q)));
  const inside = options.filter((o) => !starts.includes(o) && words(o).some((w) => w.includes(q)));
  return [...starts, ...inside].slice(0, max);
}

/**
 * A text box that suggests while typing (like the LOS form): type "canara" and "Canara Bank" is offered; arrow keys
 * and Enter pick one, a click picks one, Escape closes. Anything else typed is kept as typed.
 */
export function Suggest({ value, options, max, onChange, placeholder, label }:
  { value: string; options: Option[]; max: number; onChange: (v: string) => void; placeholder?: string; label: string }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const list = useId();
  const shown = matches(options, value, max).filter((o) => o.value !== value);
  const pick = (v: string) => { onChange(v); setOpen(false); };

  return (
    <div className="suggest">
      <input value={value} placeholder={placeholder} autoComplete="off" aria-label={label} role="combobox"
             aria-expanded={open && shown.length > 0} aria-controls={list}
             onChange={(e) => { onChange(e.target.value); setOpen(true); setActive(0); }}
             onFocus={() => setOpen(true)}
             onBlur={() => setTimeout(() => setOpen(false), 120)}
             onKeyDown={(e) => {
               if (!open || !shown.length) return;
               if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => (a + 1) % shown.length); }
               else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => (a - 1 + shown.length) % shown.length); }
               else if (e.key === "Enter") { e.preventDefault(); pick(shown[active].value); }
               else if (e.key === "Escape") setOpen(false);
             }} />
      {open && shown.length > 0 && (
        <ul className="suggest-list" id={list} role="listbox">
          {shown.map((o, i) => (
            <li key={o.value} role="option" aria-selected={i === active} className={i === active ? "is-active" : ""}
                onMouseDown={(e) => { e.preventDefault(); pick(o.value); }} onMouseEnter={() => setActive(i)}>
              {o.value}{o.keys?.length ? <span className="suggest-key">{o.keys.join(", ")}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
