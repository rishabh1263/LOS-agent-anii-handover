import type { ReactNode } from "react";
import { tone } from "../utils/format";
import { t } from "../utils/text";
import { Icon } from "./Icon";

export function Badge({ value, label, size = "md" }: { value: string | null | undefined; label: string; size?: "md" | "lg" }) {
  return <span className={`badge tone-${tone(value)} badge-${size}`}><span className="dot" aria-hidden="true" />{label || "—"}</span>;
}

export function Card({ title, subtitle, actions, children, className, id }: {
  title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; id?: string;
}) {
  return (
    <section className={`card${className ? ` ${className}` : ""}`} id={id}>
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p className="card-sub">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Spinner({ label = t("common.loading") }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label} />;
}

export function Notice({ kind, title, children }: { kind: "error" | "info" | "warn" | "ok"; title?: string; children?: ReactNode }) {
  const icon = kind === "error" ? "alert" : kind === "warn" ? "alert" : kind === "ok" ? "check" : "info";
  return (
    <div className={`notice notice-${kind}`} role={kind === "error" ? "alert" : "status"}>
      <Icon name={icon} />
      <div>{title && <strong>{title}</strong>}{children && <div>{children}</div>}</div>
    </div>
  );
}

export function Empty({ icon = "folder", title, children }: { icon?: string; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <span className="empty-icon"><Icon name={icon} size={20} /></span>
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton" aria-busy="true" aria-label={t("common.loading")}>
      {Array.from({ length: lines }, (_, i) => <span key={i} style={{ width: `${90 - i * 15}%` }} />)}
    </div>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v]) => (
        <div key={k}><dt>{k}</dt><dd>{v || "—"}</dd></div>
      ))}
    </dl>
  );
}
