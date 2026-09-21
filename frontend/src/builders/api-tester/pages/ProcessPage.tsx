import { useState } from 'react'
import { ArrowRight, ChevronDown, Loader2, Trash2 } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import { useLosProcess } from '../../../runtime/api-tester'
import {
  AuthSection,
  CaseDetailsSection,
  FileDropZone,
  ResultsPanel,
} from '../components'

function StepBadge({ n, active }: { n: number; active?: boolean }) {
  return (
    <span
      className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-xs font-sans text-[11px] font-bold tabular-nums transition-colors ${
        active
          ? 'bg-ember text-oncolor'
          : 'bg-raised text-content-secondary'
      }`}
      aria-hidden
    >
      {n}
    </span>
  )
}

export function ProcessPage() {
  const {
    items,
    setItems,
    operation,
    setOperation,
    applicantId,
    setApplicantId,
    caseId,
    setCaseId,
    token,
    setToken,
    baseUrl,
    setBaseUrl,
    showAdvanced,
    setShowAdvanced,
    loading,
    error,
    result,
    issues,
    canSubmit,
    submit,
    reset,
    clearFiles,
  } = useLosProcess()

  const [isCaseOpen, setIsCaseOpen] = useState(true)
  const [isDocsOpen, setIsDocsOpen] = useState(true)
  const [isAuthOpen, setIsAuthOpen] = useState(true)

  if (result) {
    return (
      <div className="w-full">
        <ResultsPanel result={result} onReset={reset} />
      </div>
    )
  }

  const step2Active = items.length > 0
  const step3Active = token.trim().length > 0

  return (
    <div className="space-y-6">
      {/* Page Header & Step Navigator */}
      <div className="card space-y-5">
        <div>
          <div className="inline-flex items-center gap-2 rounded-xs bg-ember-subtle px-2.5 py-1 text-[11px] font-medium text-ember-text">
            <span className="h-1.5 w-1.5 rounded-full bg-ember" />
            AI-Powered Document Verification
          </div>
          <h1 className="mt-3 font-display text-[24px] font-bold tracking-tight text-content sm:text-[28px]">
            Process documents
          </h1>
          <p className="mt-1 max-w-lg text-[14px] leading-relaxed text-content-secondary">
            Upload identity and income docs. Extraction, KYC, and cross-checks run on the live LOS API.
          </p>
        </div>

        {/* Step Indicator */}
        <nav aria-label="Form steps" className="flex flex-wrap items-center gap-2 border-t border-line-divider pt-4 text-[12px]">
          <div className="flex items-center gap-2 rounded-xs bg-ember-tint px-2.5 py-1.5 text-ember-text font-semibold">
            <StepBadge n={1} active />
            <span>Case</span>
          </div>

          <svg className="h-3 w-3 text-content-disabled" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>

          <div
            className={`flex items-center gap-2 rounded-xs px-2.5 py-1.5 transition ${
              step2Active
                ? 'bg-ember-tint text-ember-text font-semibold'
                : 'bg-raised text-content-secondary font-medium'
            }`}
          >
            <StepBadge n={2} active={step2Active} />
            <span>Files ({items.length})</span>
          </div>

          <svg className="h-3 w-3 text-content-disabled" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>

          <div
            className={`flex items-center gap-2 rounded-xs px-2.5 py-1.5 transition ${
              step3Active
                ? 'bg-ember-tint text-ember-text font-semibold'
                : 'bg-raised text-content-secondary font-medium'
            }`}
          >
            <StepBadge n={3} active={step3Active} />
            <span>Config</span>
          </div>
        </nav>
      </div>

      <form onSubmit={submit} className="space-y-4" noValidate>
        {/* Step 1: Case details — Collapsible Component */}
        <CaseDetailsSection
          applicantId={applicantId}
          setApplicantId={setApplicantId}
          caseId={caseId}
          setCaseId={setCaseId}
          loading={loading}
          isOpen={isCaseOpen}
          onToggle={() => setIsCaseOpen((v) => !v)}
        />

        {/* Step 2: Documents — Collapsible Component */}
        <section aria-labelledby="section-docs" className="card overflow-hidden">
          <button
            type="button"
            onClick={() => setIsDocsOpen((v) => !v)}
            aria-expanded={isDocsOpen}
            className="flex w-full items-center justify-between text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-ember rounded-sm py-0.5"
          >
            <div className="flex items-center gap-2.5">
              <StepBadge n={2} active={step2Active} />
              <div>
                <h2 id="section-docs" className="font-display text-[16px] font-semibold text-content">
                  Documents
                </h2>
                <p className="font-sans text-[12px] text-content-secondary">
                  PDF or images · up to 25 MB each
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2.5">
              <span className="chip font-sans tabular-nums" aria-live="polite">
                {items.length}/10
              </span>
              <div className="flex h-7 w-7 items-center justify-center rounded-xs bg-raised text-icon-default transition-colors hover:bg-raised-hover">
                <ChevronDown
                  className={`h-4 w-4 transition-transform duration-200 ${
                    isDocsOpen ? 'rotate-180' : ''
                  }`}
                  aria-hidden="true"
                />
              </div>
            </div>
          </button>

          <AnimatePresence initial={false}>
            {isDocsOpen && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: 'easeInOut' }}
                className="overflow-hidden"
              >
                <div className="pt-4 border-t border-line-divider mt-4">
                  <FileDropZone items={items} onChange={setItems} disabled={loading} />
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </section>

        {/* Step 3: Auth — Collapsible Component */}
        <AuthSection
          token={token}
          setToken={setToken}
          operation={operation}
          setOperation={setOperation}
          baseUrl={baseUrl}
          setBaseUrl={setBaseUrl}
          showAdvanced={showAdvanced}
          setShowAdvanced={setShowAdvanced}
          loading={loading}
          error={error}
          isOpen={isAuthOpen}
          onToggle={() => setIsAuthOpen((v) => !v)}
        />

        {/* Alerts */}
        {issues.length > 0 && (
          <div
            role="alert"
            className="rounded-sm border border-warning/30 bg-warning-subtle p-3.5"
          >
            <p className="mb-1 text-[13px] font-semibold text-warning-text">Check these files</p>
            <ul className="space-y-0.5">
              {issues.map((iss, i) => (
                <li key={i} className="text-[13px] text-warning-text">
                  {iss.message}
                </li>
              ))}
            </ul>
          </div>
        )}
        {error && (
          <div
            role="alert"
            className="rounded-sm border border-danger/30 bg-danger-subtle p-3.5 text-[13px] text-danger-text"
          >
            {error}
          </div>
        )}

        {/* Actions — Primary Ink Button */}
        <div className="flex flex-wrap items-center gap-3 pt-2">
          <button
            type="submit"
            disabled={!canSubmit}
            className="group btn btn-primary min-w-[160px]"
          >
            {loading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                <span>Running verification…</span>
              </>
            ) : (
              <>
                <span>Run verification</span>
                <ArrowRight
                  className="h-4 w-4 opacity-75 transition-transform duration-150 group-hover:translate-x-0.5"
                  aria-hidden="true"
                />
              </>
            )}
          </button>
          {items.length > 0 && !loading && (
            <button
              type="button"
              onClick={clearFiles}
              className="btn btn-secondary inline-flex items-center gap-1.5"
            >
              <Trash2 className="h-3.5 w-3.5 text-content-secondary" />
              <span>Clear files</span>
            </button>
          )}
        </div>
      </form>
    </div>
  )
}
