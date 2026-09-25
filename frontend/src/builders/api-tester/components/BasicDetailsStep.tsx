import { useMemo, useState } from 'react'
import { ArrowRight, Plus, Trash2 } from 'lucide-react'
import type { ProfileField } from '../../../runtime/api-tester'
import { validateProfileFields } from '../../../runtime/api-tester'

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
  const [touched, setTouched] = useState(false)

  const issues = useMemo(() => validateProfileFields(fields), [fields])
  const issueByKey = useMemo(() => {
    const map: Record<string, string> = {}
    for (const i of issues) {
      if (i.id && !map[i.id]) map[i.id] = i.message
    }
    return map
  }, [issues])

  const handleAdd = () => {
    if (!newLabel.trim()) return
    onAddField(newLabel)
    setNewLabel('')
    setShowAdd(false)
  }

  const handleContinue = () => {
    setTouched(true)
    if (!canContinue) return
    onContinue()
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
        {fields.map((f) => {
          const required = ['full_name', 'mobile', 'email', 'date_of_birth', 'address'].includes(
            f.key,
          )
          const wide = f.key === 'full_name' || f.key === 'address'
          const fieldError = touched ? issueByKey[f.key] : undefined
          return (
            <div key={f.key} className={wide ? 'sm:col-span-2' : undefined}>
              <label
                htmlFor={`pf-${f.key}`}
                className="label flex items-center justify-between gap-2"
              >
                <span>
                  {f.label}
                  {required && <span className="text-danger"> *</span>}
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
                className={`input ${fieldError ? 'border-danger focus:border-danger' : ''}`}
                type={f.inputType || 'text'}
                value={f.value}
                onChange={(e) => onChange(f.key, e.target.value)}
                onBlur={() => setTouched(true)}
                placeholder={
                  f.key === 'date_of_birth' || f.key === 'dob'
                    ? 'YYYY-MM-DD'
                    : f.key === 'pan'
                      ? 'ABCDE1234F'
                      : f.key === 'mobile'
                        ? '9876543210'
                        : f.key === 'email'
                          ? 'name@example.com'
                          : f.label
                }
                autoComplete="off"
                aria-invalid={Boolean(fieldError)}
                aria-describedby={fieldError ? `pf-err-${f.key}` : undefined}
              />
              {fieldError && (
                <p id={`pf-err-${f.key}`} className="mt-1 text-[12px] text-danger-text">
                  {fieldError}
                </p>
              )}
            </div>
          )
        })}
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

      {(error || (touched && issues.length > 0)) && (
        <div
          role="alert"
          className="rounded-sm border border-danger/30 bg-danger-subtle p-3 text-[13px] text-danger-text"
        >
          {error || (
            <ul className="list-disc space-y-1 pl-4">
              {issues.map((i) => (
                <li key={i.id || i.message}>{i.message}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="flex justify-end pt-1">
        <button
          type="button"
          className={`group btn btn-primary min-w-[140px] ${
            !canContinue ? 'opacity-80' : ''
          }`}
          onClick={handleContinue}
        >
          <span>Continue</span>
          <ArrowRight className="h-4 w-4 opacity-75 transition-transform group-hover:translate-x-0.5" />
        </button>
      </div>
    </section>
  )
}
