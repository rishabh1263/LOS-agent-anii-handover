import type { ReactNode } from 'react'
import {
  Expand,
  Minimize2,
  Plus,
  Settings,
  X,
  Shrink,
  PanelLeft,
} from 'lucide-react'
import type { AiStatus } from '../../../runtime/chatbot'

interface ChatHeaderProps {
  status: AiStatus
  mode: 'panel' | 'expanded' | 'mobile'
  botName?: string
  onClose: () => void
  onMinimize: () => void
  onToggleExpand: () => void
  onNewChat: () => void
  onOpenSettings: () => void
  onToggleSidebar?: () => void
  showSidebarToggle?: boolean
}

const STATUS_LABEL: Record<AiStatus, string> = {
  online: 'Online • Ready to help',
  thinking: 'Thinking…',
  generating: 'Generating…',
  listening: 'Listening…',
  speaking: 'Speaking…',
  offline: 'Offline',
  error: 'Something went wrong',
}

const STATUS_DOT: Record<AiStatus, string> = {
  online: 'bg-success',
  thinking: 'bg-warning',
  generating: 'bg-ember',
  listening: 'bg-ember',
  speaking: 'bg-link',
  offline: 'bg-content-disabled',
  error: 'bg-danger',
}

function IconBtn({
  label,
  onClick,
  children,
}: {
  label: string
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="flex h-11 w-11 shrink-0 items-center justify-center rounded-sm text-content-secondary transition-colors hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
    >
      {children}
    </button>
  )
}

export function ChatHeader({
  status,
  mode,
  botName = 'AI Assistant',
  onClose,
  onMinimize,
  onToggleExpand,
  onNewChat,
  onOpenSettings,
  onToggleSidebar,
  showSidebarToggle,
}: ChatHeaderProps) {
  return (
    <header data-role="chat-header" className="flex shrink-0 items-center gap-2 border-b border-line bg-surface px-3 py-2">
      {/* Avatar + title */}
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        {showSidebarToggle && onToggleSidebar && (
          <IconBtn label="Conversations" onClick={onToggleSidebar}>
            <PanelLeft className="h-[18px] w-[18px]" strokeWidth={2} />
          </IconBtn>
        )}
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-raised text-content">
          <svg viewBox="0 0 32 32" className="h-5 w-5" aria-hidden>
            <rect x="6" y="8" width="20" height="17" rx="5" fill="currentColor" opacity="0.9" />
            <rect x="9" y="13" width="14" height="7" rx="2.5" fill="#0C0C0D" opacity="0.7" />
            <circle cx="13" cy="16.5" r="1.6" fill="#4F5AC7" />
            <circle cx="19" cy="16.5" r="1.6" fill="#4F5AC7" />
          </svg>
        </div>
        <div className="min-w-0">
          <div className="truncate font-display text-[16px] font-semibold leading-tight tracking-[-0.015em] text-content">
            {botName}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 font-sans text-[12px] text-content-secondary">
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_DOT[status]}`}
              aria-hidden
            />
            <span>{STATUS_LABEL[status]}</span>
          </div>
        </div>
      </div>

      {/* Controls */}
      <div className="flex shrink-0 items-center">
        <IconBtn label="New conversation" onClick={onNewChat}>
          <Plus className="h-[18px] w-[18px]" strokeWidth={2} />
        </IconBtn>
        <IconBtn label="Settings" onClick={onOpenSettings}>
          <Settings className="h-[18px] w-[18px]" strokeWidth={2} />
        </IconBtn>
        {mode !== 'mobile' && (
          <IconBtn
            label={mode === 'expanded' ? 'Exit expanded' : 'Expand'}
            onClick={onToggleExpand}
          >
            {mode === 'expanded' ? (
              <Shrink className="h-[18px] w-[18px]" strokeWidth={2} />
            ) : (
              <Expand className="h-[18px] w-[18px]" strokeWidth={2} />
            )}
          </IconBtn>
        )}
        {mode !== 'mobile' && (
          <IconBtn label="Minimize" onClick={onMinimize}>
            <Minimize2 className="h-[18px] w-[18px]" strokeWidth={2} />
          </IconBtn>
        )}
        <IconBtn label="Close" onClick={onClose}>
          <X className="h-[18px] w-[18px]" strokeWidth={2} />
        </IconBtn>
      </div>
    </header>
  )
}
