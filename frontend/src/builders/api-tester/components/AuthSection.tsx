import { ChevronDown, SlidersHorizontal } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import type { Operation } from '../../../runtime/api-tester'
import { useAuth } from '../../../runtime/auth'

export interface AuthSectionProps {
  token?: string
  setToken?: (val: string) => void
  operation: Operation
  setOperation: (val: Operation) => void
  baseUrl: string
  setBaseUrl: (val: string) => void
  showAdvanced: boolean
  setShowAdvanced: (val: boolean | ((prev: boolean) => boolean)) => void
  loading: boolean
  error?: string | null
  isOpen: boolean
  onToggle: () => void
}

export function AuthSection({
  operation,
  setOperation,
  showAdvanced,
  setShowAdvanced,
  loading,
  isOpen,
  onToggle,
}: AuthSectionProps) {
  const { user } = useAuth()

  return (
    <section aria-labelledby="section-auth" className="card overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={isOpen}
        className="flex w-full items-center justify-between text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-ember rounded-sm py-0.5"
      >
        <div className="flex items-center gap-2.5">
          <span
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-xs font-sans text-[11px] font-bold tabular-nums transition-colors bg-ember text-oncolor"
            aria-hidden
          >
            3
          </span>
          <div>
            <h2 id="section-auth" className="font-display text-[16px] font-semibold text-content">
              API configuration
            </h2>
            <p className="font-sans text-[12px] text-content-secondary">
              {user ? `Authenticated as ${user.username}` : 'Ready for live requests'}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-medium text-content-secondary hidden sm:inline">
            {isOpen ? 'Collapse' : 'Expand'}
          </span>
          <div className="flex h-7 w-7 items-center justify-center rounded-xs bg-raised text-icon-default transition-colors hover:bg-raised-hover">
            <ChevronDown
              className={`h-4 w-4 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''
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
            <div className="space-y-3.5 pt-4 border-t border-line-divider mt-4">
              {/* Advanced Options Toggle */}
              <button
                type="button"
                onClick={() => setShowAdvanced((v) => !v)}
                aria-expanded={showAdvanced}
                className="flex w-full items-center justify-between rounded-sm py-1 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-ember hover:text-content"
              >
                <span className="inline-flex items-center gap-1.5 text-[13px] font-medium text-content-secondary">
                  <SlidersHorizontal className="h-3.5 w-3.5" />
                  Advanced API options
                </span>
                <ChevronDown
                  className={`h-4 w-4 text-content-disabled transition-transform duration-200 ${showAdvanced ? 'rotate-180' : ''
                    }`}
                  aria-hidden="true"
                />
              </button>

              <AnimatePresence initial={false}>
                {showAdvanced && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.18 }}
                    className="overflow-hidden"
                  >
                    <div className="space-y-3.5 rounded-sm border border-line-divider bg-raised/50 p-4">
                      <div>
                        <label htmlFor="operation" className="label">
                          Operation
                        </label>
                        <select
                          id="operation"
                          className="input h-11"
                          value={operation}
                          onChange={(e) => setOperation(e.target.value as Operation)}
                          disabled={loading}
                        >
                          <option value="PROCESS">PROCESS — full pipeline</option>
                          <option value="EXTRACT">EXTRACT — same as PROCESS</option>
                          <option value="VERIFY">VERIFY — no extraction</option>
                        </select>
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}
