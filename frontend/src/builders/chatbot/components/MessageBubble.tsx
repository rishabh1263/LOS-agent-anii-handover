import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Check, Copy, Pencil, RefreshCw, Volume2, VolumeX, X } from 'lucide-react'
import type { ChatMessage, ChatUploadResult } from '../../../runtime/chatbot'
import { formatTime } from '../../../runtime/chatbot'
import { DocumentUploadPanel } from './DocumentUploadPanel'
import { TypingDots } from './TypingDots'

interface MessageBubbleProps {
  message: ChatMessage
  showTimestamp?: boolean
  showSuggestedQuestions?: boolean
  isSpeaking?: boolean
  /** Disable edit while generating / thinking */
  editDisabled?: boolean
  onCopy?: () => void
  onListen?: () => void
  onRegenerate?: () => void
  onEdit?: (messageId: string, newContent: string) => void
  onSuggested?: (q: string) => void
  onUploadDocuments?: (files: { file: File; documentType: string }[]) => Promise<{
    results: Array<{
      status: 'pass' | 'review' | 'fail' | 'validation'
      detail?: string
      detectedType?: string
    }>
  }>
  onPersistUploadResults?: (messageId: string, results: ChatUploadResult[]) => void
}

function ActionBtn({
  label,
  onClick,
  children,
  active,
  disabled,
}: {
  label: string
  onClick: () => void
  children: ReactNode
  active?: boolean
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      aria-pressed={active}
      className={`relative flex h-7 w-7 cursor-pointer items-center justify-center rounded-md transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-40 ${active
        ? 'bg-ember/10 text-ember'
        : 'text-content-secondary hover:bg-raised hover:text-content'
        }`}
    >
      {children}
    </button>
  )
}

/** Lightweight markdown renderer (bold, code fences, lists). */
function renderContent(text: string) {
  const lines = text.split('\n')
  const nodes: ReactNode[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (line.startsWith('```')) {
      const lang = line.slice(3).trim()
      const code: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) {
        code.push(lines[i])
        i++
      }
      nodes.push(
        <div
          key={`c-${i}`}
          className="my-2 overflow-hidden rounded-lg border border-line bg-raised"
        >
          <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
            <span className="font-sans text-[11px] font-medium text-content-secondary">
              {lang || 'code'}
            </span>
            <button
              type="button"
              className="cursor-pointer font-sans text-[11px] text-ember hover:underline"
              onClick={() => navigator.clipboard?.writeText(code.join('\n'))}
            >
              Copy
            </button>
          </div>
          <pre className="overflow-x-auto p-3 font-mono text-[12px] leading-relaxed text-content">
            {code.join('\n')}
          </pre>
        </div>,
      )
      i++
      continue
    }
    if (line.startsWith('• ') || line.startsWith('- ')) {
      nodes.push(
        <li key={`li-${i}`} className="ml-4 list-disc break-words [overflow-wrap:anywhere]">
          {inlineFormat(line.slice(2))}
        </li>,
      )
      i++
      continue
    }
    if (line.startsWith('**') && line.endsWith('**') && line.length > 4) {
      nodes.push(
        <p key={`h-${i}`} className="mt-2 break-words font-medium text-content [overflow-wrap:anywhere]">
          {line.slice(2, -2)}
        </p>,
      )
      i++
      continue
    }
    if (line.trim() === '') {
      nodes.push(<div key={`sp-${i}`} className="h-1.5" />)
    } else {
      nodes.push(
        <p key={`p-${i}`} className="break-words leading-[1.55] [overflow-wrap:anywhere]">
          {inlineFormat(line)}
        </p>,
      )
    }
    i++
  }
  return nodes
}

function inlineFormat(text: string): ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) {
      return (
        <strong key={i} className="font-medium">
          {p.slice(2, -2)}
        </strong>
      )
    }
    if (p.startsWith('`') && p.endsWith('`')) {
      return (
        <code
          key={i}
          className="rounded px-1 py-0.5 font-mono text-[13px] bg-black/5 dark:bg-white/10"
        >
          {p.slice(1, -1)}
        </code>
      )
    }
    return p
  })
}

export function MessageBubble({
  message,
  showTimestamp = true,
  showSuggestedQuestions = true,
  isSpeaking,
  editDisabled = false,
  onCopy,
  onListen,
  onRegenerate,
  onEdit,
  onSuggested,
  onUploadDocuments,
  onPersistUploadResults,
}: MessageBubbleProps) {
  const [copied, setCopied] = useState(false)
  const [isEditing, setIsEditing] = useState(false)
  const [draft, setDraft] = useState(message.content)
  const editRef = useRef<HTMLTextAreaElement>(null)
  const isUser = message.role === 'user'
  const isSystem = message.role === 'system'

  useEffect(() => {
    if (!isEditing) return
    const el = editRef.current
    if (!el) return
    el.focus()
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
    // Place caret at end
    const len = el.value.length
    el.setSelectionRange(len, len)
  }, [isEditing])

  if (isSystem) {
    return (
      <div className="flex justify-center px-4 py-2">
        <span className="rounded-full bg-raised px-3 py-1 font-sans text-[12px] text-content-secondary">
          {message.content}
        </span>
      </div>
    )
  }

  const handleCopy = () => {
    navigator.clipboard?.writeText(message.content)
    setCopied(true)
    onCopy?.()
    setTimeout(() => setCopied(false), 1600)
  }

  const startEdit = () => {
    if (editDisabled || !onEdit) return
    setDraft(message.content)
    setIsEditing(true)
  }

  const cancelEdit = () => {
    setIsEditing(false)
    setDraft(message.content)
  }

  const submitEdit = () => {
    const next = draft.trim()
    if (!next || next === message.content) {
      cancelEdit()
      return
    }
    setIsEditing(false)
    onEdit?.(message.id, next)
  }

  const onEditKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      cancelEdit()
      return
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submitEdit()
    }
  }

  // In-place edit UI for user messages (ChatGPT-style)
  if (isUser && isEditing) {
    return (
      <div className="flex flex-col items-end gap-1.5 px-4 py-1.5">
        <div className="w-full min-w-0 max-w-[92%] rounded-2xl rounded-br-md border border-ember/40 bg-raised px-3 py-2 shadow-sm">
          <textarea
            ref={editRef}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value)
              const el = e.target
              el.style.height = 'auto'
              el.style.height = `${Math.min(el.scrollHeight, 200)}px`
            }}
            onKeyDown={onEditKey}
            rows={1}
            className="max-h-[200px] min-h-[36px] w-full min-w-0 resize-none overflow-y-auto break-words bg-transparent font-sans text-[14.5px] leading-[1.5] text-content placeholder:text-content-disabled focus:outline-none [overflow-wrap:anywhere]"
            aria-label="Edit message"
          />
          <div className="mt-2 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={cancelEdit}
              className="flex cursor-pointer items-center gap-1 rounded-lg px-2.5 py-1.5 font-sans text-[12px] font-medium text-content-secondary transition-colors hover:bg-surface hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
            >
              <X className="h-3.5 w-3.5" strokeWidth={2} />
              Cancel
            </button>
            <button
              type="button"
              onClick={submitEdit}
              disabled={!draft.trim()}
              className="flex cursor-pointer items-center gap-1 rounded-lg bg-ember px-2.5 py-1.5 font-sans text-[12px] font-medium text-oncolor transition-colors hover:opacity-90 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Check className="h-3.5 w-3.5" strokeWidth={2.5} />
              Save &amp; submit
            </button>
          </div>
        </div>
        <p className="px-1 font-sans text-[11px] text-content-disabled">
          Enter to save · Esc to cancel · messages after this will be removed
        </p>
      </div>
    )
  }

  return (
    <div
      className={`group flex w-full min-w-0 max-w-full flex-col gap-1 overflow-x-hidden px-4 py-1.5 ${isUser ? 'items-end' : 'items-start'}`}
    >
      {/* min-w-0 lets the flex child shrink so long unbroken text can wrap */}
      <div className={`flex min-w-0 max-w-[85%] gap-2 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
        {!isUser && (
          <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-raised text-content-secondary">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" aria-hidden>
              <path
                d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z"
                fill="currentColor"
              />
            </svg>
          </div>
        )}

        <div
          data-role={isUser ? 'user-bubble' : 'ai-bubble'}
          className={`min-w-0 max-w-full break-words rounded-2xl px-3.5 py-2.5 font-sans text-[14.5px] leading-[1.5] [overflow-wrap:anywhere] ${isUser
            ? 'rounded-br-md text-white'
            : isSpeaking
              ? 'rounded-bl-md border border-ember/30 shadow-[0_0_0_3px_rgba(37,99,235,0.08)]'
              : 'rounded-bl-md'
            }`}
        >
          {message.error ? (
            <p className="break-words text-danger-text [overflow-wrap:anywhere]">{message.error}</p>
          ) : (
            <div className="min-w-0 space-y-0.5 break-words [overflow-wrap:anywhere]">
              {message.content ? (
                renderContent(message.content)
              ) : message.isStreaming ? (
                <TypingDots />
              ) : null}
              {message.isStreaming && message.content ? (
                <span
                  className="ml-0.5 inline-block h-[1em] w-[2px] translate-y-[1px] animate-pulse bg-current align-text-bottom opacity-60"
                  aria-hidden
                />
              ) : null}
            </div>
          )}
          {!message.isStreaming && message.routeTo && (
            <p className="mt-2 rounded-md bg-amber-50 px-2 py-1 text-[12px] font-medium text-amber-800">
              Route to {message.routeTo}
            </p>
          )}
        </div>
      </div>

      {showTimestamp && (
        <span
          className={`px-1 font-sans text-[11px] text-content-disabled ${isUser ? 'mr-0' : 'ml-9'
            }`}
        >
          {formatTime(message.timestamp)}
        </span>
      )}

      {/* User actions — edit (ChatGPT-style) */}
      {isUser && onEdit && (
        <div className="flex items-center gap-0.5 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100">
          <ActionBtn
            label="Edit message"
            onClick={startEdit}
            disabled={editDisabled}
          >
            <Pencil className="h-3.5 w-3.5" strokeWidth={2} />
          </ActionBtn>
          <ActionBtn label={copied ? 'Copied' : 'Copy'} onClick={handleCopy}>
            {copied ? (
              <Check className="h-3.5 w-3.5 text-emerald-500" strokeWidth={2.5} />
            ) : (
              <Copy className="h-3.5 w-3.5" strokeWidth={2} />
            )}
          </ActionBtn>
        </div>
      )}

      {/* AI actions — hover / focus / speaking only */}
      {!isUser && !message.isStreaming && (
        <div
          className={`ml-9 flex items-center gap-0.5 transition-opacity duration-150 ${isSpeaking
            ? 'opacity-100'
            : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'
            }`}
        >
          <ActionBtn label={copied ? 'Copied' : 'Copy'} onClick={handleCopy}>
            {copied ? (
              <Check className="h-3.5 w-3.5 text-emerald-500" strokeWidth={2.5} />
            ) : (
              <Copy className="h-3.5 w-3.5" strokeWidth={2} />
            )}
          </ActionBtn>
          <ActionBtn
            label={isSpeaking ? 'Stop reading' : 'Read aloud'}
            onClick={() => onListen?.()}
            active={isSpeaking}
          >
            {isSpeaking ? (
              <span className="relative flex h-7 w-7 items-center justify-center">
                <span className="absolute inset-0 animate-ping rounded-full bg-ember/20" />
                <VolumeX className="relative h-3.5 w-3.5 text-ember" strokeWidth={2} />
              </span>
            ) : (
              <Volume2 className="h-3.5 w-3.5" strokeWidth={2} />
            )}
          </ActionBtn>
          <ActionBtn label="Regenerate" onClick={() => onRegenerate?.()}>
            <RefreshCw className="h-3.5 w-3.5" strokeWidth={2} />
          </ActionBtn>
        </div>
      )}

      {showSuggestedQuestions &&
        message.suggestedQuestions &&
        message.suggestedQuestions.length > 0 && (
          <div className="ml-9 mt-1.5 flex max-w-full flex-wrap gap-1.5">
            {message.suggestedQuestions.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => onSuggested?.(q)}
                className="max-w-full cursor-pointer break-words rounded-full border border-line bg-surface px-3 py-1.5 font-sans text-[12px] font-medium text-content-secondary transition-all duration-150 hover:border-ember/40 hover:bg-raised hover:text-content hover:shadow-sm active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-ember [overflow-wrap:anywhere]"
              >
                {q}
              </button>
            ))}
          </div>
        )}

      {/* Upload option — only when backend lists specific pending docs */}
      {!isUser &&
        !message.isStreaming &&
        onUploadDocuments &&
        message.uploadTargets &&
        message.uploadTargets.length > 0 && (
          <div className="w-full min-w-0 max-w-full overflow-x-hidden pl-9 pr-1">
            <DocumentUploadPanel
              targets={message.uploadTargets}
              initialResults={message.uploadResults}
              onSubmit={onUploadDocuments}
              onResultsPersist={
                onPersistUploadResults
                  ? (results) => onPersistUploadResults(message.id, results)
                  : undefined
              }
              disabled={editDisabled}
            />
          </div>
        )}
    </div>
  )
}
