import { ChevronDown } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'

export interface CaseDetailsSectionProps {
  applicantId: string
  setApplicantId: (val: string) => void
  caseId: string
  setCaseId: (val: string) => void
  loading: boolean
  isOpen: boolean
  onToggle: () => void
}

export function CaseDetailsSection({
  applicantId,
  setApplicantId,
  caseId,
  setCaseId,
  loading,
  isOpen,
  onToggle,
}: CaseDetailsSectionProps) {
  return (
    <section aria-labelledby="section-case" className="card overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={isOpen}
        className="flex w-full items-center justify-between text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-ember rounded-sm py-0.5"
      >
        <div className="flex items-center gap-2.5">
          <span
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-xs font-sans text-[11px] font-bold tabular-nums bg-ember text-oncolor"
            aria-hidden
          >
            1
          </span>
          <div>
            <h2 id="section-case" className="font-display text-[16px] font-semibold text-content">
              Case details
            </h2>
            <p className="font-sans text-[12px] text-content-secondary">
              Optional · used to link this run
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-medium text-content-secondary hidden sm:inline">
            {isOpen ? 'Collapse' : 'Expand'}
          </span>
          <div className="flex h-7 w-7 items-center justify-center rounded-xs bg-raised text-icon-default transition-colors hover:bg-raised-hover">
            <ChevronDown
              className={`h-4 w-4 transition-transform duration-200 ${
                isOpen ? 'rotate-180' : ''
              }`}
              aria-hidden="true"
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
            transition={{ duration: 0.2, ease: 'easeInOut' }}
            className="overflow-hidden"
          >
            <div className="grid gap-3.5 sm:grid-cols-2 pt-4 border-t border-line-divider mt-4">
              <div>
                <label htmlFor="applicant_id" className="label">
                  Applicant ID
                </label>
                <input
                  id="applicant_id"
                  name="applicant_id"
                  className="input"
                  placeholder="CUST-8891"
                  value={applicantId}
                  onChange={(e) => setApplicantId(e.target.value)}
                  disabled={loading}
                  autoComplete="off"
                />
              </div>
              <div>
                <label htmlFor="case_id" className="label">
                  Case ID
                </label>
                <input
                  id="case_id"
                  name="case_id"
                  className="input"
                  placeholder="Auto if empty"
                  value={caseId}
                  onChange={(e) => setCaseId(e.target.value)}
                  disabled={loading}
                  autoComplete="off"
                />
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}
