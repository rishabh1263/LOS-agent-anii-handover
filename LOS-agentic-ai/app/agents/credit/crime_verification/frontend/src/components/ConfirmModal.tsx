import { useEffect, useRef } from "react";
import type { UiConfig } from "../types/api";
import { dob } from "../utils/format";
import { t } from "../utils/text";
import { Icon } from "./Icon";
import { Notice } from "./ui";

export interface ConfirmSubject {
  party: string;
  role: string;
  label: string;
  full_name: string;
  father_name: string;
  date_of_birth: string;
  permanent_address: string;
  pan_masked: string;
}

/**
 * "Confirm Applicant Details and Initiate Crime Verification?" (config screen.confirm) -- nothing is sent to the
 * server before Confirm. Every selected person (the applicant and up to N co-applicants) is listed. A real dialog:
 * focus moves in and stays in (Tab cycles), Escape cancels (not while submitting), focus returns where it was.
 */
export function ConfirmModal({ subjects, config, vendorName, retrigger, submitting, error, onCancel, onConfirm }: {
  subjects: ConfirmSubject[];
  config: UiConfig;
  vendorName: string;
  retrigger: boolean;
  submitting: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    cancel.current?.focus();
    return () => opener?.focus?.();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) { e.preventDefault(); onCancel(); }
      if (e.key !== "Tab" || !dialog.current) return;
      const items = Array.from(dialog.current.querySelectorAll<HTMLElement>("button:not([disabled])"));
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [submitting, onCancel]);

  const f = config.fields;
  return (
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget && !submitting) onCancel(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-desc"
           ref={dialog}>
        <header className="modal-head">
          <span className="modal-icon"><Icon name="shieldCheck" size={20} /></span>
          <div>
            <h2 id="confirm-title">{t("confirm.title")}</h2>
            <p className="muted small">{vendorName}{retrigger ? ` · ${t("verify.retrigger")}` : ""} ·{" "}
              {t("confirm.people", { n: subjects.length })}</p>
          </div>
        </header>
        <div className="modal-body">
          <div className={`confirm-grid${subjects.length > 1 ? " cols-2" : ""}`}>
            {subjects.map((s) => (
              <section key={s.party} className="confirm-subject" aria-label={s.label}>
                <h3><Icon name="user" /> {s.role === "PRIMARY" ? t("confirm.primary_heading") : s.label}</h3>
                <dl>
                  <dt>{f.full_name}</dt><dd>{s.full_name}</dd>
                  <dt>{f.father_name}</dt><dd>{s.father_name}</dd>
                  <dt>{f.date_of_birth}</dt><dd>{dob(s.date_of_birth)}</dd>
                  <dt>{f.permanent_address}</dt><dd>{s.permanent_address}</dd>
                  <dt>{f.pan}</dt><dd className="mono">{s.pan_masked}</dd>
                </dl>
              </section>
            ))}
          </div>
          <div className="confirm-warning" id="confirm-desc">
            <Icon name="alert" />
            <p>{t("confirm.message")}</p>
          </div>
          {error && <Notice kind="error" title={t("confirm.not_initiated")}>{error}</Notice>}
        </div>
        <footer className="modal-foot">
          <button type="button" className="btn btn-ghost" ref={cancel} onClick={onCancel} disabled={submitting}>
            {t("confirm.cancel")}
          </button>
          <button type="button" className="btn btn-primary" onClick={onConfirm} disabled={submitting} aria-busy={submitting}>
            {submitting ? <><span className="spinner spinner-sm" aria-hidden="true" /> {t("confirm.working")}</>
                        : <><Icon name="send" /> {t("confirm.confirm")}</>}
          </button>
        </footer>
      </div>
    </div>
  );
}
