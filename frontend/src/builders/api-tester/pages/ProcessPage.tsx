import { useEffect, useState } from 'react'
import { ArrowRight, ChevronDown, Loader2, Lock, Trash2, User, Users } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import { useLosProcess } from '../../../runtime/api-tester'
import {
  AuthSection,
  CaseDetailsSection,
  FileDropZone,
  ResultsPanel,
} from '../components'

function StepBadge({
  n,
  active,
  locked,
}: {
  n: number
  active?: boolean
  locked?: boolean
}) {
  return (
    <span
      className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-xs font-sans text-[11px] font-bold tabular-nums transition-colors ${
        locked
          ? 'bg-raised text-content-disabled'
          : active
            ? 'bg-ember text-oncolor'
            : 'bg-raised text-content-secondary'
      }`}
      aria-hidden
    >
      {locked ? <Lock className="h-3 w-3" /> : n}
    </span>
  )
}

const accordionTransition = { duration: 0.28, ease: [0.22, 1, 0.36, 1] as const }

export function ProcessPage() {
  const {
    primaryItems,
    coItems,
    setItemsForParty,
    operation,
    setOperation,
    applicantId,
    setApplicantId,
    coApplicantId,
    setCoApplicantId,
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

  // Progressive disclosure: unlock next step after previous has content
  const applicantReady = primaryItems.length > 0
  const coUnlocked = applicantReady
  const configUnlocked = applicantReady // config available once applicant docs exist

  const [isCaseOpen, setIsCaseOpen] = useState(true)
  const [isPrimaryOpen, setIsPrimaryOpen] = useState(true)
  const [isCoOpen, setIsCoOpen] = useState(false)
  const [isAuthOpen, setIsAuthOpen] = useState(false)

  // Auto-open next section when previous completes
  useEffect(() => {
    if (applicantReady) {
      setIsCoOpen(true)
      setIsAuthOpen(true)
    }
  }, [applicantReady])

  const totalFiles = primaryItems.length + coItems.length
  const step2Active = primaryItems.length > 0
  const step3Active = coItems.length > 0
  const step4Active = token.trim().length > 0

  return (
    <AnimatePresence mode="wait">
      {result ? (
        <motion.div
          key="results"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
          className="w-full"
        >
          <ResultsPanel result={result} onReset={reset} />
        </motion.div>
      ) : (
        <motion.div
          key="form"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
          className="space-y-6"
        >
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
              <p className="mt-1 max-w-xl text-[14px] leading-relaxed text-content-secondary">
                Upload applicant documents first, then co-applicant (optional).
                Extraction, KYC, and cross-checks run on the live LOS API.
              </p>
            </div>

            {/* Step Indicator */}
            <nav
              aria-label="Form steps"
              className="flex flex-wrap items-center gap-2 border-t border-line-divider pt-4 text-[12px]"
            >
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
                <span>Applicant ({primaryItems.length})</span>
              </div>

              <svg className="h-3 w-3 text-content-disabled" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>

              <div
                className={`flex items-center gap-2 rounded-xs px-2.5 py-1.5 transition ${
                  step3Active
                    ? 'bg-ember-tint text-ember-text font-semibold'
                    : !coUnlocked
                      ? 'bg-raised text-content-disabled font-medium'
                      : 'bg-raised text-content-secondary font-medium'
                }`}
              >
                <StepBadge n={3} active={step3Active} locked={!coUnlocked} />
                <span>Co-applicant ({coItems.length})</span>
              </div>

              <svg className="h-3 w-3 text-content-disabled" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>

              <div
                className={`flex items-center gap-2 rounded-xs px-2.5 py-1.5 transition ${
                  step4Active
                    ? 'bg-ember-tint text-ember-text font-semibold'
                    : !configUnlocked
                      ? 'bg-raised text-content-disabled font-medium'
                      : 'bg-raised text-content-secondary font-medium'
                }`}
              >
                <StepBadge n={4} active={step4Active} locked={!configUnlocked} />
                <span>Config</span>
              </div>
            </nav>
          </div>

          <form onSubmit={submit} className="space-y-4" noValidate>
            {/* Step 1: Case details */}
            <CaseDetailsSection
              applicantId={applicantId}
              setApplicantId={setApplicantId}
              coApplicantId={coApplicantId}
              setCoApplicantId={setCoApplicantId}
              caseId={caseId}
              setCaseId={setCaseId}
              loading={loading}
              isOpen={isCaseOpen}
              onToggle={() => setIsCaseOpen((v) => !v)}
            />

            {/* Step 2: Applicant documents — always available */}
            <section aria-labelledby="section-primary-docs" className="card overflow-hidden">
              <button
                type="button"
                onClick={() => setIsPrimaryOpen((v) => !v)}
                aria-expanded={isPrimaryOpen}
                className="flex w-full items-center justify-between text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-ember rounded-sm py-0.5"
              >
                <div className="flex items-center gap-2.5">
                  <StepBadge n={2} active={step2Active} />
                  <div className="flex items-center gap-2">
                    <User className="h-4 w-4 text-content-secondary" aria-hidden />
                    <div>
                      <h2
                        id="section-primary-docs"
                        className="font-display text-[16px] font-semibold text-content"
                      >
                        Applicant documents
                      </h2>
                      <p className="font-sans text-[12px] text-content-secondary">
                        Required · PDF or images · up to 25 MB each
                      </p>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2.5">
                  <span className="chip font-sans tabular-nums" aria-live="polite">
                    {primaryItems.length}/5
                  </span>
                  <div className="flex h-7 w-7 items-center justify-center rounded-xs bg-raised text-icon-default transition-colors hover:bg-raised-hover">
                    <ChevronDown
                      className={`h-4 w-4 transition-transform duration-200 ${
                        isPrimaryOpen ? 'rotate-180' : ''
                      }`}
                      aria-hidden="true"
                    />
                  </div>
                </div>
              </button>

              <AnimatePresence initial={false}>
                {isPrimaryOpen && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={accordionTransition}
                    className="overflow-hidden"
                  >
                    <div className="pt-4 border-t border-line-divider mt-4">
                      <FileDropZone
                        items={primaryItems}
                        onChange={(next) => setItemsForParty('PRIMARY_APPLICANT', next)}
                        partyRole="PRIMARY_APPLICANT"
                        disabled={loading}
                      />
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </section>

            {/* Step 3: Co-applicant — locked until applicant has files */}
            <section
              aria-labelledby="section-co-docs"
              className={`card overflow-hidden transition-opacity ${
                !coUnlocked ? 'opacity-60' : ''
              }`}
            >
              <button
                type="button"
                onClick={() => coUnlocked && setIsCoOpen((v) => !v)}
                aria-expanded={isCoOpen && coUnlocked}
                aria-disabled={!coUnlocked}
                disabled={!coUnlocked}
                className={`flex w-full items-center justify-between text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-ember rounded-sm py-0.5 ${
                  !coUnlocked ? 'cursor-not-allowed' : ''
                }`}
              >
                <div className="flex items-center gap-2.5">
                  <StepBadge n={3} active={step3Active} locked={!coUnlocked} />
                  <div className="flex items-center gap-2">
                    <Users className="h-4 w-4 text-content-secondary" aria-hidden />
                    <div>
                      <h2
                        id="section-co-docs"
                        className="font-display text-[16px] font-semibold text-content"
                      >
                        Co-applicant documents
                      </h2>
                      <p className="font-sans text-[12px] text-content-secondary">
                        {!coUnlocked
                          ? 'Unlocks after applicant documents are added'
                          : 'Optional · PDF or images · up to 25 MB each'}
                      </p>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2.5">
                  <span className="chip font-sans tabular-nums" aria-live="polite">
                    {coItems.length}/5
                  </span>
                  {coUnlocked && (
                    <div className="flex h-7 w-7 items-center justify-center rounded-xs bg-raised text-icon-default transition-colors hover:bg-raised-hover">
                      <ChevronDown
                        className={`h-4 w-4 transition-transform duration-200 ${
                          isCoOpen ? 'rotate-180' : ''
                        }`}
                        aria-hidden="true"
                      />
                    </div>
                  )}
                </div>
              </button>

              <AnimatePresence initial={false}>
                {isCoOpen && coUnlocked && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={accordionTransition}
                    className="overflow-hidden"
                  >
                    <div className="pt-4 border-t border-line-divider mt-4 space-y-3">
                      {coItems.length > 0 && !coApplicantId.trim() && (
                        <div
                          role="alert"
                          className="rounded-sm border border-warning/30 bg-warning-subtle px-3 py-2 text-[12px] text-warning-text"
                        >
                          Co-applicant ID is required in Case details when you upload
                          co-applicant documents (API field: co_applicant_id).
                        </div>
                      )}
                      <FileDropZone
                        items={coItems}
                        onChange={(next) => setItemsForParty('CO_APPLICANT', next)}
                        partyRole="CO_APPLICANT"
                        disabled={loading}
                      />
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </section>

            {/* Step 4: Config — locked until applicant has files */}
            <div
              className={`transition-opacity ${!configUnlocked ? 'opacity-60 pointer-events-none' : ''}`}
            >
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
                isOpen={isAuthOpen && configUnlocked}
                onToggle={() => configUnlocked && setIsAuthOpen((v) => !v)}
              />
            </div>

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
            {!applicantReady && totalFiles === 0 && (
              <p className="text-[12px] text-content-secondary">
                Add at least one applicant document to continue.
              </p>
            )}

            {/* Actions */}
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
              {totalFiles > 0 && !loading && (
                <button
                  type="button"
                  onClick={clearFiles}
                  className="btn btn-secondary inline-flex items-center gap-1.5"
                >
                  <Trash2 className="h-3.5 w-3.5 text-content-secondary" />
                  <span>Clear all files</span>
                </button>
              )}
            </div>
          </form>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
