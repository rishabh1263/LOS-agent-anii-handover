import { ArrowLeft, ArrowRight, Loader2 } from 'lucide-react'
import type { ApplicationDetails } from '../../../runtime/api-tester'
import { EMPLOYMENT_OPTIONS, PRODUCT_OPTIONS } from '../../../runtime/api-tester'

export interface ApplicationDetailsStepProps {
  values: ApplicationDetails
  onChange: (key: keyof ApplicationDetails, value: string) => void
  onBack: () => void
  onContinue: () => void
  canContinue: boolean
  submitting?: boolean
  error?: string | null
}

export function ApplicationDetailsStep({
  values,
  onChange,
  onBack,
  onContinue,
  canContinue,
  submitting,
  error,
}: ApplicationDetailsStepProps) {
  return (
    <section className="card space-y-5" aria-labelledby="step-application-title">
      <div>
        <h2
          id="step-application-title"
          className="font-display text-[18px] font-semibold text-content"
        >
          Application details
        </h2>
        <p className="mt-1 text-[13px] text-content-secondary">
          Product, loan amount, employment, and related fields used when creating the FOS
          applicant record.
        </p>
      </div>

      <div className="grid gap-3.5 sm:grid-cols-2">
        <div>
          <label htmlFor="app-product" className="label">
            Product <span className="text-danger">*</span>
          </label>
          <select
            id="app-product"
            className="input"
            value={values.product}
            onChange={(e) => onChange('product', e.target.value)}
            disabled={submitting}
          >
            {PRODUCT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="app-employment" className="label">
            Employment type <span className="text-danger">*</span>
          </label>
          <select
            id="app-employment"
            className="input"
            value={values.employment_type}
            onChange={(e) => onChange('employment_type', e.target.value)}
            disabled={submitting}
          >
            {EMPLOYMENT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="app-loan-amount" className="label">
            Loan amount <span className="text-danger">*</span>
          </label>
          <input
            id="app-loan-amount"
            className="input"
            type="number"
            min={0}
            step={1000}
            value={values.loan_amount}
            onChange={(e) => onChange('loan_amount', e.target.value)}
            placeholder="e.g. 500000"
            disabled={submitting}
          />
        </div>

        <div>
          <label htmlFor="app-tenure" className="label">
            Tenure (months) <span className="text-danger">*</span>
          </label>
          <input
            id="app-tenure"
            className="input"
            type="number"
            min={1}
            step={1}
            value={values.tenure_months}
            onChange={(e) => onChange('tenure_months', e.target.value)}
            placeholder="e.g. 36"
            disabled={submitting}
          />
        </div>

        <div>
          <label htmlFor="app-irp" className="label">
            Interest rate (%) <span className="text-danger">*</span>
          </label>
          <input
            id="app-irp"
            className="input"
            type="number"
            min={0}
            step={0.1}
            value={values.interest_rate_pct}
            onChange={(e) => onChange('interest_rate_pct', e.target.value)}
            placeholder="e.g. 12.5"
            disabled={submitting}
          />
        </div>

        <div>
          <label htmlFor="app-obligations" className="label">
            Declared monthly obligations
          </label>
          <input
            id="app-obligations"
            className="input"
            type="number"
            min={0}
            step={100}
            value={values.declared_monthly_obligations}
            onChange={(e) => onChange('declared_monthly_obligations', e.target.value)}
            placeholder="e.g. 8000"
            disabled={submitting}
          />
        </div>

        <div className="sm:col-span-2">
          <label htmlFor="app-property" className="label">
            Property value
          </label>
          <input
            id="app-property"
            className="input"
            type="number"
            min={0}
            step={1000}
            value={values.property_value}
            onChange={(e) => onChange('property_value', e.target.value)}
            placeholder="e.g. 8000000"
            disabled={submitting}
          />
        </div>
      </div>

      {error && (
        <div
          role="alert"
          className="rounded-sm border border-danger/30 bg-danger-subtle px-3 py-2 text-[13px] text-danger-text"
        >
          {error}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
        <button
          type="button"
          className="btn btn-secondary"
          onClick={onBack}
          disabled={submitting}
        >
          <ArrowLeft className="h-4 w-4" />
          Back
        </button>
        <button
          type="button"
          className="btn btn-primary"
          onClick={onContinue}
          disabled={!canContinue || submitting}
        >
          {submitting ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" />
              Creating applicant…
            </>
          ) : (
            <>
              Continue
              <ArrowRight className="h-4 w-4" />
            </>
          )}
        </button>
      </div>
    </section>
  )
}
