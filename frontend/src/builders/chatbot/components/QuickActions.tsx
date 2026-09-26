import type { ComponentType } from 'react'
import {
  BarChart3,
  ClipboardList,
  FileText,
  HelpCircle,
  Scan,
  Search,
} from 'lucide-react'

const ICONS: Record<string, ComponentType<{ className?: string; strokeWidth?: number }>> = {
  FileText,
  HelpCircle,
  Search,
  ClipboardList,
  Scan,
  BarChart3,
}

interface QuickAction {
  id: string
  label: string
  icon: string
}

interface QuickActionsProps {
  actions: QuickAction[]
  onSelect: (label: string) => void
}

export function QuickActions({ actions, onSelect }: QuickActionsProps) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center px-5 py-10">
      <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-ember text-oncolor">
        <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" aria-hidden>
          <path
            d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z"
            fill="currentColor"
          />
        </svg>
      </div>
      <h3 className="mb-1 font-sans text-[17px] font-semibold tracking-[-0.02em] text-content">
        How can I help?
      </h3>
      <p className="mb-7 max-w-[260px] text-center font-sans text-[13px] leading-relaxed text-content-secondary">
        Ask about documents, KYC, application status, or anything else to get started.
      </p>
      <div className="grid w-full max-w-[340px] grid-cols-1 gap-2 sm:grid-cols-2">
        {actions.map((a) => {
          const Icon = ICONS[a.icon] ?? FileText
          return (
            <button
              key={a.id}
              type="button"
              onClick={() => onSelect(a.label)}
              className="flex items-center gap-2.5 rounded-xl border border-line bg-surface px-3 py-2.5 text-left transition-all hover:border-ember/30 hover:bg-raised hover:shadow-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
            >
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-raised text-content-secondary">
                <Icon className="h-4 w-4" strokeWidth={1.75} />
              </span>
              <span className="font-sans text-[13px] font-medium text-content">{a.label}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
