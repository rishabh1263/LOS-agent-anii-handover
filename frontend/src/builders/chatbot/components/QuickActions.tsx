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
    <div className="flex flex-1 flex-col items-center justify-center px-5 py-8">
      <div className="mb-2 flex h-14 w-14 items-center justify-center rounded-lg bg-raised text-content">
        <svg viewBox="0 0 40 40" className="h-7 w-7" aria-hidden>
          <rect x="8" y="10" width="24" height="20" rx="6" fill="currentColor" opacity="0.9" />
          <rect x="12" y="16" width="16" height="8" rx="3" fill="#0C0C0D" opacity="0.7" />
          <circle cx="16.5" cy="20" r="1.8" fill="#4F5AC7" />
          <circle cx="23.5" cy="20" r="1.8" fill="#4F5AC7" />
        </svg>
      </div>
      <h3 className="mb-1 font-display text-[18px] font-semibold tracking-[-0.015em] text-content">
        How can I help?
      </h3>
      <p className="mb-6 max-w-[280px] text-center font-sans text-[13px] text-content-secondary">
        Ask about documents, KYC, application status, or upload a file to get started.
      </p>
      <div className="grid w-full max-w-[360px] grid-cols-1 gap-2 sm:grid-cols-2">
        {actions.map((a) => {
          const Icon = ICONS[a.icon] ?? FileText
          return (
            <button
              key={a.id}
              type="button"
              onClick={() => onSelect(a.label)}
              className="flex items-center gap-2.5 rounded-md border border-line bg-surface px-3 py-2.5 text-left transition-colors hover:border-line-strong hover:bg-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
            >
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xs bg-raised text-content-secondary">
                <Icon className="h-4 w-4" strokeWidth={2} />
              </span>
              <span className="font-sans text-[13px] font-medium text-content">{a.label}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
