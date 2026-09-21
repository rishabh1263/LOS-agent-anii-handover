import { ChevronDown, FileText } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import type { DocumentResult } from '../../../runtime/api-tester'
import { docStatusClass, verificationClass } from '../../../runtime/api-tester'
import { StatusChip } from './StatusChip'

export interface DocumentResultCardProps {
  doc: DocumentResult
  isOpen: boolean
  onToggle: () => void
}

function renderValue(val: unknown) {
  if (val == null) return '—'
  if (Array.isArray(val)) {
    return (
      <div className="flex flex-wrap gap-1">
        {val.map((item, idx) => (
          <span key={idx} className="chip text-[11px] py-0.5 px-2 font-mono">
            {String(item)}
          </span>
        ))}
      </div>
    )
  }
  return <span className="font-mono text-[13px] font-medium text-content">{String(val)}</span>
}

export function DocumentResultCard({ doc, isOpen, onToggle }: DocumentResultCardProps) {
  const extraction = doc.extraction || {}
  const entries = Object.entries(extraction).filter(([, v]) => v != null)

  return (
    <div className="card overflow-hidden border-line bg-surface p-0 shadow-xs">
      {/* Header bar */}
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={isOpen}
        className="w-full flex items-center justify-between px-5 py-3.5 text-left hover:bg-raised/30 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
      >
        <div className="flex items-center gap-3 min-w-0">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xs bg-raised border border-line text-icon-default">
            <FileText className="h-4 w-4 text-ember" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h4 className="font-mono text-[13px] font-bold text-content truncate">
                {doc.source_id}
              </h4>
              <span className="chip text-[10px] uppercase font-bold py-0.5 px-2">
                {doc.type}
              </span>
            </div>
            <p className="text-[11px] text-content-secondary mt-0.5">
              {entries.length} attributes extracted
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <StatusChip label={doc.status} className={docStatusClass(doc.status)} />
          <StatusChip label={doc.verification} className={verificationClass(doc.verification)} />
          <ChevronDown
            className={`h-4 w-4 text-content-secondary transition-transform duration-200 ${
              isOpen ? 'rotate-180' : ''
            }`}
          />
        </div>
      </button>

      {/* Accordion content */}
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden border-t border-line-divider"
          >
            {entries.length > 0 ? (
              <div className="p-5 grid gap-x-6 gap-y-3 sm:grid-cols-2 bg-raised/20">
                {entries.map(([k, v]) => {
                  const isLong = String(v).length > 35
                  return (
                    <div
                      key={k}
                      className={`flex flex-col py-1.5 border-b border-line-divider/60 ${
                        isLong ? 'sm:col-span-2' : ''
                      }`}
                    >
                      <span className="font-display text-[10px] font-bold uppercase tracking-wider text-content-secondary">
                        {k.replace(/_/g, ' ')}
                      </span>
                      <div className="mt-1">{renderValue(v)}</div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div className="p-5 text-center text-[12px] text-content-secondary">
                No attributes extracted from this document.
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
