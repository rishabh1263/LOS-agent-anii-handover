import type { Case } from "../types/api";
import { t } from "../utils/text";
import { Icon } from "./Icon";

export interface Step { key: string; label: string; icon: string }

/** Where the application stands: done steps ticked, the current one glowing. */
export function Stepper({ steps, current, failed }: { steps: Step[]; current: number; failed?: boolean }) {
  return (
    <ol className="stepper" aria-label={t("common.progress")}>
      {steps.map((s, i) => {
        const state = i < current ? "done" : i === current ? (failed ? "failed" : "current") : "todo";
        return (
          <li key={s.key} className={`step step-${state}`} aria-current={state === "current" ? "step" : undefined}>
            <span className="step-dot"><Icon name={state === "done" ? "check" : state === "failed" ? "x" : s.icon} size={15} /></span>
            <span className="step-label">{s.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

/** The five steps of a verification (labels: config screen.steps). */
export function steps(): Step[] {
  return [
    { key: "details", label: t("steps.details"), icon: "user" },
    { key: "confirm", label: t("steps.confirm"), icon: "shieldCheck" },
    { key: "provider", label: t("steps.provider"), icon: "send" },
    { key: "results", label: t("steps.results"), icon: "gavel" },
    { key: "decision", label: t("steps.decision"), icon: "check" },
  ];
}

/** The stepper position of a case: 0 details .. 4 decision, 5 = all done. */
export function stepOf(kase: Case | null): { current: number; failed: boolean } {
  if (!kase || kase.status === "WAIVED") return { current: 0, failed: false };
  if (kase.status === "INITIATED") return { current: 1, failed: false };
  if (kase.status === "PROCESSING") return { current: 2, failed: false };
  if (kase.final_decision) return { current: 5, failed: false };
  return { current: kase.status === "FAILED" ? 2 : 4, failed: kase.status === "FAILED" };
}
