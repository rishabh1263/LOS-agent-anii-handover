import { useState } from 'react'
import { ArrowRight, Plus, Trash2 } from 'lucide-react'
import type { ProfileField } from '../../../runtime/api-tester'

export interface BasicDetailsStepProps {
  fields: ProfileField[]
  onChange: (key: string, value: string) => void
  onAddField: (label: string) => void
  onRemoveField: (key: string) => void
  onContinue: () => void
  canContinue: boolean
  error?: string | null
}

export function BasicDetailsStep({
  fields,
  onChange,
  onAddField,
  onRemoveField,
  onContinue,
  canContinue,
  error,
}: BasicDetailsStepProps) {
  const [newLabel, setNewLabel] = useState('')
  const [showAdd, setShowAdd] = useState(false)

  const handleAdd = () => {
    if (!newLabel.trim()) return
    onAddField(newLabel)
    setNewLabel('')
    setShowAdd(false)
  }

  return (
    <section className="card space-y-5" aria-labelledby="step-details-title">
      <div>
        <h2 id="step-details-title" className="font-display text-[18px] font-semibold text-content">
          Basic details
        </h2>
        <p className="mt-1 text-[13px] text-content-secondary">
          Enter profile data. Optional fields can be added. Values are matched against document
          extraction after verification.
        </p>
      </div>

      <div className="grid gap-3.5 sm:grid-cols-2">
        {fields.map((f) => (
          <div key={f.key} className={f.key === 'name' ? 'sm:col-span-2' : undefined}>
            <label htmlFor={`pf-${f.key}`} className="label flex items-center justify-between gap-2">
              <span>
                {f.label}
                {f.key === 'name' && <span className="text-danger"> *</span>}
              </span>
              {!f.builtin && (
                <button
                  type="button"
                  onClick={() => onRemoveField(f.key)}
                  className="text-content-secondary hover:text-danger"
                  aria-label={`Remove ${f.label}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </label>
            <input
              id={`pf-${f.key}`}
              className="input"
              value={f.value}
              onChange={(e) => onChange(f.key, e.target.value)}
              placeholder={
                f.key === 'dob' ? 'YYYY-MM-DD' : f.key === 'pan' ? 'ABCDE1234F' : f.label
              }
              autoComplete="off"
            />
          </div>
        ))}
      </div>

      {showAdd ? (
        <div className="flex flex-wrap items-end gap-2 rounded-sm border border-line bg-raised/40 p-3">
          <div className="min-w-[180px] flex-1">
            <label htmlFor="custom-field-label" className="label">
              New field label
            </label>
            <input
              id="custom-field-label"
              className="input"
              value={newLabel}
              onChange={(e) => setNewLabel(e.target.value)}
              placeholder="e.g. Father name"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  handleAdd()
                }
              }}
            />
          </div>
          <button type="button" className="btn btn-primary" onClick={handleAdd}>
            Add
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              setShowAdd(false)
              setNewLabel('')
            }}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setShowAdd(true)}
          className="inline-flex items-center gap-1.5 text-[13px] font-medium text-ember-text hover:underline"
        >
          <Plus className="h-3.5 w-3.5" />
          Add custom field
        </button>
      )}

      {error && (
        <div role="alert" className="rounded-sm border border-danger/30 bg-danger-subtle p-3 text-[13px] text-danger-text">
          {error}
        </div>
      )}

      <div className="flex justify-end pt-1">
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
