import { useEffect, useState, type ReactNode } from 'react'
import { ArrowRight, ChevronDown, Loader2, Trash2, User, Users } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import { useLosProcess } from '../../../runtime/api-tester'
import type { PartyRole, UploadFileItem } from '../../../runtime/api-tester'
import { RequireAuth } from '../../auth'
import {
  AuthSection,
  CaseDetailsSection,
  FileDropZone,
  ResultsPanel,
} from '../components'

const ACCORDION = { duration: 0.28, ease: [0.22, 1, 0.36, 1] as const }
const FADE = { duration: 0.22, ease: [0.22, 1, 0.36, 1] as const }

const VERIFY_STAGES = [
  { at: 25, label: 'Uploading documents', short: 'Upload' },
  { at: 55, label: 'Extracting fields', short: 'Extract' },
  { at: 80, label: 'Running KYC checks', short: 'KYC' },
  { at: 100, label: 'Reconciling documents', short: 'Reconcile' },
] as const

/**
 * Simulated verification progress while the LOS request is in flight.
 * Eases toward 92% over ~22s, then completes when loading ends.
 */
function useVerificationProgress(loading: boolean) {
  const [progress, setProgress] = useState(0)

  useEffect(() => {
    if (!loading) {
      if (progress > 0 && progress < 100) {
        setProgress(100)
        const t = window.setTimeout(() => setProgress(0), 400)
        return () => window.clearTimeout(t)
      }
      setProgress(0)
      return
    }

    setProgress(4)
    const started = Date.now()
    const durationMs = 22_000
    const tick = window.setInterval(() => {
      const t = Math.min(1, (Date.now() - started) / durationMs)
      const eased = 1 - Math.pow(1 - t, 3)
      setProgress(Math.min(92, Math.round(4 + eased * 88)))
    }, 120)

    return () => window.clearInterval(tick)
    // Only restart on loading edge — progress is intentionally omitted
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading])

  const stage =
    VERIFY_STAGES.find((s) => progress < s.at)?.label ??
    (progress >= 100 ? 'Complete' : VERIFY_STAGES[VERIFY_STAGES.length - 1].label)

  return { progress, stage }
}

function StepBadge({ n, active }: { n: number; active?: boolean }) {
  return (
    <span
      className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-xs font-sans text-[11px] font-bold tabular-nums transition-colors ${
        active ? 'bg-ember text-oncolor' : 'bg-raised text-content-secondary'
      }`}
      aria-hidden
    >
      {n}
    </span>
  )
}

function ProgressChevron() {
  return (
    <svg className="h-3 w-3 shrink-0 text-content-disabled" viewBox="0 0 12 12" fill="none" aria-hidden>
      <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

function FormProgressNav({
  primaryCount,
  coCount,
  configActive,
}: {
  primaryCount: number
  coCount: number
  configActive: boolean
}) {
  const steps = [
    { n: 1, label: 'Case', active: true },
    { n: 2, label: `Applicant (${primaryCount})`, active: primaryCount > 0 },
    { n: 3, label: `Co-applicant (${coCount})`, active: coCount > 0 },
    { n: 4, label: 'Config', active: configActive },
  ]

  return (
    <nav
      aria-label="Form progress"
      className="flex flex-wrap items-center gap-2 border-t border-line-divider pt-4 text-[12px]"
    >
      {steps.map((step, i) => (
        <div key={step.n} className="flex items-center gap-2">
          {i > 0 && <ProgressChevron />}
          <div
            className={`flex items-center gap-2 rounded-xs px-2.5 py-1.5 transition ${
              step.active
                ? 'bg-ember-tint font-semibold text-ember-text'
                : 'bg-raised font-medium text-content-secondary'
            }`}
          >
            <StepBadge n={step.n} active={step.active} />
            <span>{step.label}</span>
          </div>
        </div>
      ))}
    </nav>
  )
}

function PartyDocsSection({
  step,
  active,
  title,
  subtitle,
  sectionId,
  icon,
  count,
  isOpen,
  onToggle,
  items,
  onChange,
  partyRole,
  disabled,
  alert,
}: {
  step: number
  active: boolean
  title: string
  subtitle: string
  sectionId: string
  icon: ReactNode
  count: number
  isOpen: boolean
  onToggle: () => void
  items: UploadFileItem[]
  onChange: (next: UploadFileItem[]) => void
  partyRole: PartyRole
  disabled: boolean
  alert?: ReactNode
}) {
  return (
    <section aria-labelledby={sectionId} className="card overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={isOpen}
        className="flex w-full items-center justify-between rounded-sm py-0.5 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
      >
        <div className="flex items-center gap-2.5">
          <StepBadge n={step} active={active} />
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-xs bg-raised text-content-secondary">
              {icon}
            </div>
            <div>
              <h2 id={sectionId} className="font-display text-[16px] font-semibold text-content">
                {title}
              </h2>
              <p className="font-sans text-[12px] text-content-secondary">{subtitle}</p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2.5">
          <span className="chip font-sans tabular-nums" aria-live="polite">
            {count}/5
          </span>
          <div className="flex h-7 w-7 items-center justify-center rounded-xs bg-raised text-icon-default transition-colors hover:bg-raised-hover">
            <ChevronDown
              className={`h-4 w-4 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`}
              aria-hidden
            />
          </div>
        </div>
      </button>

      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={ACCORDION}
            className="overflow-hidden"
          >
            <div className={`mt-4 border-t border-line-divider pt-4${alert ? ' space-y-3' : ''}`}>
              {alert}
              <FileDropZone
                items={items}
                onChange={onChange}
                partyRole={partyRole}
                disabled={disabled}
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}

function VerificationProgressMeter({ progress, stage }: { progress: number; stage: string }) {
  return (
    <motion.div
      key="verify-progress"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
      className="w-full max-w-md space-y-2"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <div className="flex items-center justify-between gap-3">
        <p className="font-sans text-[13px] font-medium text-content">{stage}</p>
        <p className="font-sans text-[13px] font-semibold tabular-nums text-content">
          {progress}
          <span className="font-medium text-content-secondary">%</span>
        </p>
      </div>

      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-raised"
        role="img"
        aria-label={`Verification ${progress} percent complete`}
      >
        <motion.div
          className="h-full rounded-full bg-ember"
          initial={{ width: '0%' }}
          animate={{ width: `${progress}%` }}
          transition={{ duration: 0.18, ease: 'easeOut' }}
        />
      </div>

      <div className="flex items-center gap-1.5 pt-0.5">
        {VERIFY_STAGES.map((step, i) => {
          const prevAt = i === 0 ? 0 : VERIFY_STAGES[i - 1].at
          const done = progress >= step.at
          const active = progress < step.at && progress >= prevAt
          return (
            <div key={step.short} className="flex min-w-0 items-center gap-1.5">
              <span
                className={`h-1.5 w-1.5 shrink-0 rounded-full transition-colors duration-150 ${
                  done
                    ? 'bg-ember'
                    : active
                      ? 'bg-ember/60 ring-2 ring-ember/25'
                      : 'bg-border-strong'
                }`}
                aria-hidden
              />
              <span
                className={`truncate font-sans text-[11px] ${
                  done || active ? 'font-medium text-content' : 'text-content-disabled'
                }`}
              >
                {step.short}
              </span>
              {i < VERIFY_STAGES.length - 1 && (
                <span
                  className={`mx-0.5 h-px w-3 shrink-0 ${done ? 'bg-ember/40' : 'bg-line-divider'}`}
                  aria-hidden
                />
              )}
            </div>
          )
        })}
      </div>
    </motion.div>
  )
}

/**
 * Process documents form. Wrapped in RequireAuth so an empty or expired
 * session redirects the user to the login page instead of showing the form.
 */
export function ProcessPage() {
  return (
    <RequireAuth>
      <ProcessPageContent />
    </RequireAuth>
  )
}

function ProcessPageContent() {
  const {
    items,
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
    addMoreDocuments,
    clearFiles,
  } = useLosProcess()

  const [isCaseOpen, setIsCaseOpen] = useState(true)
  const [isPrimaryOpen, setIsPrimaryOpen] = useState(true)
  const [isCoOpen, setIsCoOpen] = useState(true)
  const [isAuthOpen, setIsAuthOpen] = useState(false)

  const totalFiles = primaryItems.length + coItems.length
  const { progress, stage } = useVerificationProgress(loading)

  const coIdMissing = coItems.length > 0 && !coApplicantId.trim()

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
          <ResultsPanel
            result={result}
            onReset={reset}
            onAddDocuments={addMoreDocuments}
            uploadedFiles={items}
          />
        </motion.div>
      ) : (
        <motion.div
          key="form"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={FADE}
          className="space-y-6"
        >
          <div className="card space-y-5">
            <div>
              <p className="font-display text-[12px] font-semibold uppercase tracking-[0.09em] text-content-secondary">
                Document verification
              </p>
              <h1 className="mt-2 font-display text-[24px] font-bold tracking-tight text-content sm:text-[28px]">
                Process documents
              </h1>
              <p className="mt-1.5 max-w-2xl text-[14px] leading-relaxed text-content-secondary">
                Upload documents for the applicant and co-applicant independently.
                Extraction, KYC checks, and cross-document reconciliation run against the live LOS
                API.
              </p>
            </div>

            <FormProgressNav
              primaryCount={primaryItems.length}
              coCount={coItems.length}
              configActive={token.trim().length > 0}
            />
          </div>

          <form onSubmit={submit} className="space-y-4" noValidate>
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

            <div className="grid gap-4 xl:grid-cols-2">
              <PartyDocsSection
                step={2}
                active={primaryItems.length > 0}
                title="Applicant"
                subtitle="Optional · PDF or images · up to 25 MB each"
                sectionId="section-primary-docs"
                icon={<User className="h-4 w-4" aria-hidden />}
                count={primaryItems.length}
                isOpen={isPrimaryOpen}
                onToggle={() => setIsPrimaryOpen((v) => !v)}
                items={primaryItems}
                onChange={(next) => setItemsForParty('PRIMARY_APPLICANT', next)}
                partyRole="PRIMARY_APPLICANT"
                disabled={loading}
              />

              <PartyDocsSection
                step={3}
                active={coItems.length > 0}
                title="Co-applicant"
                subtitle="Optional · PDF or images · up to 25 MB each · ID required if used"
                sectionId="section-co-docs"
                icon={<Users className="h-4 w-4" aria-hidden />}
                count={coItems.length}
                isOpen={isCoOpen}
                onToggle={() => setIsCoOpen((v) => !v)}
                items={coItems}
                onChange={(next) => setItemsForParty('CO_APPLICANT', next)}
                partyRole="CO_APPLICANT"
                disabled={loading}
                alert={
                  coIdMissing ? (
                    <div
                      role="alert"
                      className="rounded-sm border border-warning/30 bg-warning-subtle px-3 py-2 text-[12px] text-warning-text"
                    >
                      Co-applicant ID is required in Case details when you upload co-applicant
                      documents (API field: co_applicant_id).
                    </div>
                  ) : null
                }
              />
            </div>

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

            {primaryItems.length === 0 && coItems.length === 0 && (
              <p className="text-[12px] text-content-secondary">
                Add at least one document (applicant and/or co-applicant) to run verification.
              </p>
            )}
            {primaryItems.length === 0 && coItems.length > 0 && !coIdMissing && (
              <p className="text-[12px] text-content-secondary">
                Running co-applicant only — applicant documents are optional.
              </p>
            )}

            <div className="space-y-3 pt-2">
              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="submit"
                  disabled={!canSubmit}
                  className="group btn btn-primary min-w-[160px]"
                >
                  {loading ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                      <span>Running verification…</span>
                    </>
                  ) : (
                    <>
                      <span>Run verification</span>
                      <ArrowRight
                        className="h-4 w-4 opacity-75 transition-transform duration-150 group-hover:translate-x-0.5"
                        aria-hidden
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

              <AnimatePresence>
                {loading && <VerificationProgressMeter progress={progress} stage={stage} />}
              </AnimatePresence>
            </div>
          </form>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
