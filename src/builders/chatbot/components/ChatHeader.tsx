import type { ReactNode } from 'react'
import {
  Expand,
  Minimize2,
  Plus,
  Settings,
  X,
  Shrink,
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
}

const STATUS_LABEL: Record<AiStatus, string> = {
  online: 'Online',
  thinking: 'Thinking…',
  generating: 'Generating…',
  listening: 'Listening…',
  speaking: 'Speaking…',
  offline: 'Offline',
  error: 'Error',
}

const STATUS_DOT: Record<AiStatus, string> = {
  online: 'bg-emerald-500',
  thinking: 'bg-amber-400',
  generating: 'bg-blue-500',
  listening: 'bg-blue-500',
  speaking: 'bg-violet-500',
  offline: 'bg-neutral-400',
  error: 'bg-red-500',
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
      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-content-secondary transition-colors hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
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
}: ChatHeaderProps) {
  return (
    <header
      data-role="chat-header"
      className="flex shrink-0 items-center gap-2 border-b border-line px-3.5 py-2.5"
    >
      {/* Avatar + title */}
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-ember text-oncolor">
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" aria-hidden>
            <path
              d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z"
              fill="currentColor"
            />
          </svg>
        </div>
        <div className="min-w-0">
          <div className="truncate font-sans text-[14px] font-semibold leading-tight tracking-[-0.01em] text-content">
            {botName}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 font-sans text-[11px] text-content-secondary">
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_DOT[status]}`}
              aria-hidden
            />
            <span>{STATUS_LABEL[status]}</span>
          </div>
        </div>
      </div>

      {/* Controls */}
      <div className="flex shrink-0 items-center gap-0.5">
        <IconBtn label="New conversation" onClick={onNewChat}>
          <Plus className="h-4 w-4" strokeWidth={2} />
        </IconBtn>
        <IconBtn label="Settings" onClick={onOpenSettings}>
          <Settings className="h-4 w-4" strokeWidth={2} />
        </IconBtn>
        {mode !== 'mobile' && (
          <IconBtn
            label={mode === 'expanded' ? 'Exit expanded' : 'Expand'}
            onClick={onToggleExpand}
          >
            {mode === 'expanded' ? (
              <Shrink className="h-4 w-4" strokeWidth={2} />
            ) : (
              <Expand className="h-4 w-4" strokeWidth={2} />
            )}
          </IconBtn>
        )}
        {mode !== 'mobile' && (
          <IconBtn label="Minimize" onClick={onMinimize}>
            <Minimize2 className="h-4 w-4" strokeWidth={2} />
          </IconBtn>
        )}
        <IconBtn label="Close" onClick={onClose}>
          <X className="h-4 w-4" strokeWidth={2} />
        </IconBtn>
      </div>
    </header>
  )
}
