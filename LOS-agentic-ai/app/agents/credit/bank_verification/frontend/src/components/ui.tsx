import type { ReactNode } from "react";
import { tone } from "../utils/format";
import type { Provenance } from "../types/api";
import { Icon } from "./Icon";

/** Every status in plain words -- never a code on screen. */
const WORDS: Record<string, string> = {
  PASS: "Passed", FAIL: "Problem", REVIEW_REQUIRED: "Needs review", NOT_APPLICABLE: "Not applicable",
  POSITIVE: "Positive", NEGATIVE: "Negative", WAIVED: "Waived", READY_FOR_FINALIZATION: "Ready for agency",
  PENDING_VERIFICATION: "Not started", READING: "Reading statement", CONFIRMED: "Confirmed", NOT_CONFIRMED: "Not confirmed", PENDING: "Pending",
  COMPLETE: "Read fully", PARTIAL: "Partly read", UNREADABLE: "Could not read", INVALID: "Not a statement",
  HIGH: "High", MEDIUM: "Medium", LOW: "Low", NONE: "None",
};

export function statusWord(status: string | null | undefined): string {
  if (!status) return "—";
  return WORDS[status] ?? status.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}

export function StatusBadge({ status, label, large }: { status: string | null | undefined; label?: string; large?: boolean }) {
  return <span className={`badge badge-${tone(status)} ${large ? "badge-lg" : ""}`}>{label ?? statusWord(status)}</span>;
}

const SOURCE_LABEL: Record<Provenance, string> = {
  AA: "From AA", EXTRACTED: "From statement", MANUAL: "Entered", AGENCY: "Agency confirmed",
  APPLICATION: "From application", CONFIRMED: "Checked",
};

export function SourceTag({ source }: { source: Provenance | undefined }) {
  if (!source) return null;
  return <span className={`source source-${source.toLowerCase()}`}>{SOURCE_LABEL[source]}</span>;
}

export function Card({ icon, title, subtitle, actions, foot, children, className = "", bodyClass = "card-body" }:
  { icon?: string; title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; foot?: ReactNode; children: ReactNode;
    className?: string; bodyClass?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div className="card-title">
            {icon && <Icon name={icon} size={18} />}
            <div>{title && <h2>{title}</h2>}{subtitle && <p>{subtitle}</p>}</div>
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className={bodyClass}>{children}</div>
      {foot && <footer className="card-foot">{foot}</footer>}
    </section>
  );
}

/** A read-only form value, like the LOS form: label above, value below. */
export function Value({ label, value, tag }: { label: string; value: ReactNode; tag?: ReactNode }) {
  return <div className="field"><span>{label}{tag}</span><div className="value">{value || "—"}</div></div>;
}

export function Loading({ what }: { what: string }) {
  return <div className="state" role="status"><span className="spinner" aria-hidden /> Loading {what}…</div>;
}

export function Notice({ tone: kind, children }: { tone: "bad" | "warn" | "ok"; children: ReactNode }) {
  return <div className={`notice notice-${kind}`} role={kind === "bad" ? "alert" : "status"}>
    <Icon name={kind === "ok" ? "check" : "alert"} /><div>{children}</div></div>;
}

export function ErrorNote({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <Notice tone="bad">
      <strong>Could not load.</strong> {error.message}{" "}
      {onRetry && <button className="btn-text" onClick={onRetry}>Try again</button>}
    </Notice>
  );
}

export function Field({ label, children, tag }: { label: string; children: ReactNode; tag?: ReactNode }) {
  return (
    <label className="field">
      <span>{label}{tag}</span>
      {children}
    </label>
  );
}
