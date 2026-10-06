import { AlertTriangle, Check, X } from 'lucide-react'

export interface StatusChipProps {
  label: string
  className?: string
}

export function StatusChip({ label, className = '' }: StatusChipProps) {
  const norm = label.toUpperCase().trim()

  const isSuccess = ['SUCCESS', 'PASS'].includes(norm)
  const isWarning = ['REVIEW', 'PARTIAL', 'MANUAL_REVIEW', 'WARNING'].includes(norm)
  const isDanger = ['FAIL', 'FAILED', 'REJECT', 'REJECTED'].includes(norm)

  return (
    <span
      className={`inline-flex h-[26px] items-center gap-1.5 rounded-xs px-2.5 font-sans text-[12px] font-semibold tracking-normal transition-colors ${className}`}
      role="status"
    >
      {isSuccess && <Check className="h-3 w-3 shrink-0 text-current" strokeWidth={2.5} />}
      {isWarning && <AlertTriangle className="h-3 w-3 shrink-0 text-current" strokeWidth={2.5} />}
      {isDanger && <X className="h-3 w-3 shrink-0 text-current" strokeWidth={2.5} />}
      <span>{label}</span>
    </span>
  )
}
