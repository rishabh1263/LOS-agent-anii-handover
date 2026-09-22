import { Check } from 'lucide-react'

const STEPS = [
  { id: 'details', label: 'Details', n: 1 },
  { id: 'party', label: 'Party', n: 2 },
  { id: 'documents', label: 'Documents', n: 3 },
  { id: 'report', label: 'Report', n: 4 },
] as const

export type WizardProgressStep = (typeof STEPS)[number]['id']

export function WizardProgress({ current }: { current: WizardProgressStep }) {
  const currentIdx = STEPS.findIndex((s) => s.id === current)

  return (
    <nav aria-label="Verification progress" className="flex flex-wrap items-center gap-2">
      {STEPS.map((step, i) => {
        const done = i < currentIdx
        const active = i === currentIdx
        return (
          <div key={step.id} className="flex items-center gap-2">
            {i > 0 && (
              <span
                className={`mx-0.5 h-px w-4 sm:w-6 ${done || active ? 'bg-ember/50' : 'bg-line-divider'}`}
                aria-hidden
              />
            )}
            <div
              className={`flex items-center gap-2 rounded-xs px-2.5 py-1.5 transition ${
                active
                  ? 'bg-ember-tint font-semibold text-ember-text'
                  : done
                    ? 'bg-raised font-medium text-content'
                    : 'bg-raised font-medium text-content-secondary'
              }`}
            >
              <span
                className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-xs font-sans text-[11px] font-bold tabular-nums ${
                  active
                    ? 'bg-ember text-oncolor'
                    : done
                      ? 'bg-success text-oncolor'
                      : 'bg-raised text-content-secondary'
                }`}
                aria-hidden
              >
                {done ? <Check className="h-3.5 w-3.5" /> : step.n}
              </span>
              <span className="text-[12px]">{step.label}</span>
            </div>
          </div>
        )
      })}
    </nav>
  )
}
