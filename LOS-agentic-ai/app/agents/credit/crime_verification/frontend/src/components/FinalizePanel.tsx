import { useState } from "react";
import { ApiError, api } from "../services/api";
import type { Case, UiConfig } from "../types/api";
import { when } from "../utils/format";
import { t } from "../utils/text";
import { Icon } from "./Icon";
import { Badge, Card, Notice } from "./ui";

/** API 2: the officer acknowledges the results (a decision + remarks). It never changes a provider result. */
export function FinalizePanel({ kase, config, onDone }: { kase: Case; config: UiConfig; onDone: (c: Case) => void }) {
  const [decision, setDecision] = useState("");
  const [remarks, setRemarks] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (kase.final_decision) {
    return (
      <Card title={t("finalize.done_title")} className="finalize done">
        <div className="final-line">
          <Badge value={kase.final_decision} label={kase.final_decision_label} />
          <span className="muted small">{t("finalize.by", { who: kase.finalized_by ?? "—", when: when(kase.finalized_at) })}</span>
        </div>
        {kase.final_remarks && <p className="remarks">“{kase.final_remarks}”</p>}
      </Card>
    );
  }
  if (!kase.can.finalize) return null;
  const needsRemarks = config.remarks_required_for.includes(decision);
  const submit = async () => {
    setError(null);
    if (!decision) return setError(t("finalize.choose"));
    if (needsRemarks && !remarks.trim()) return setError(t("finalize.remarks_needed"));
    setBusy(true);
    try {
      onDone(await api.finalize(kase.case_id, decision, remarks));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t("finalize.failed"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title={t("finalize.title")} subtitle={t("finalize.subtitle")}
          className="finalize">
      <fieldset className="decisions">
        <legend className="sr-only">{t("finalize.decision")}</legend>
        {config.decisions.map((d) => (
          <label key={d.id} className={`decision${decision === d.id ? " is-on" : ""}`}>
            <input type="radio" name="decision" value={d.id} checked={decision === d.id}
                   onChange={() => setDecision(d.id)} />
            {d.label}
          </label>
        ))}
      </fieldset>
      <label className="field">
        <span className="field-label">{t("finalize.remarks")} {needsRemarks && <span className="req">*</span>}</span>
        <textarea className="input" rows={2} maxLength={500} value={remarks} onChange={(e) => setRemarks(e.target.value)}
                  placeholder={t("finalize.remarks_placeholder")} />
      </label>
      {error && <Notice kind="error">{error}</Notice>}
      <div className="actions-right">
        <button type="button" className="btn btn-primary" onClick={submit} disabled={busy}>
          <Icon name="check" /> {busy ? t("finalize.saving") : t("finalize.submit")}
        </button>
      </div>
    </Card>
  );
}
