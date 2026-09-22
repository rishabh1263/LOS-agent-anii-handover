import type { ReactNode } from 'react'
import { ArrowLeft, ArrowRight, User, Users } from 'lucide-react'
import type { PartySelection } from '../../../runtime/api-tester'

export interface PartySelectStepProps {
  selection: PartySelection
  onChange: (next: PartySelection) => void
  onBack: () => void
  onContinue: () => void
  canContinue: boolean
  error?: string | null
}

function PartyCard({
  selected,
  onToggle,
  icon,
  title,
  subtitle,
}: {
  selected: boolean
  onToggle: () => void
  icon: ReactNode
  title: string
  subtitle: string
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={selected}
      className={`flex w-full flex-col items-start gap-3 rounded-sm border p-4 text-left transition focus:outline-none focus-visible:ring-2 focus-visible:ring-ember ${
        selected
          ? 'border-ember bg-ember-tint shadow-sm'
          : 'border-line bg-surface hover:border-line-strong hover:bg-raised/50'
      }`}
    >
      <div className="flex w-full items-center justify-between">
        <div
          className={`flex h-10 w-10 items-center justify-center rounded-xs ${
            selected ? 'bg-ember text-oncolor' : 'bg-raised text-content-secondary'
          }`}
        >
          {icon}
        </div>
        <span
          className={`flex h-5 w-5 items-center justify-center rounded-full border-2 ${
            selected ? 'border-ember bg-ember' : 'border-border-strong bg-surface'
          }`}
          aria-hidden
        >
          {selected && <span className="h-2 w-2 rounded-full bg-oncolor" />}
        </span>
      </div>
      <div>
        <p className="font-display text-[15px] font-semibold text-content">{title}</p>
        <p className="mt-0.5 text-[12px] text-content-secondary">{subtitle}</p>
      </div>
    </button>
  )
}

export function PartySelectStep({
  selection,
  onChange,
  onBack,
  onContinue,
  canContinue,
  error,
}: PartySelectStepProps) {
  return (
    <section className="card space-y-5" aria-labelledby="step-party-title">
      <div>
        <h2 id="step-party-title" className="font-display text-[18px] font-semibold text-content">
          Who are we verifying?
        </h2>
        <p className="mt-1 text-[13px] text-content-secondary">
          Select Applicant, Co-applicant, or both. You can upload documents for each party in the
          next step.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <PartyCard
          selected={selection.applicant}
          onToggle={() => onChange({ ...selection, applicant: !selection.applicant })}
          icon={<User className="h-5 w-5" />}
          title="Applicant"
          subtitle="Primary applicant documents & KYC"
        />
        <PartyCard
          selected={selection.coApplicant}
          onToggle={() => onChange({ ...selection, coApplicant: !selection.coApplicant })}
          icon={<Users className="h-5 w-5" />}
          title="Co-applicant"
          subtitle="Co-applicant documents & KYC"
        />
      </div>

      {error && (
        <div role="alert" className="rounded-sm border border-danger/30 bg-danger-subtle p-3 text-[13px] text-danger-text">
          {error}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
        <button type="button" className="btn btn-secondary inline-flex items-center gap-1.5" onClick={onBack}>
          <ArrowLeft className="h-4 w-4" />
          Back
        </button>
        <button
          type="button"
          className="group btn btn-primary min-w-[140px]"
          disabled={!canContinue}
          onClick={onContinue}
        >
          <span>Continue</span>
          <ArrowRight className="h-4 w-4 opacity-75 transition-transform group-hover:translate-x-0.5" />
        </button>
      </div>
    </section>
  )
}
