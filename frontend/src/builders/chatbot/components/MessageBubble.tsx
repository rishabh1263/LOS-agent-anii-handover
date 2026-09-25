import { useState, type ReactNode } from 'react'
import { Check, Copy, RefreshCw, Volume2, VolumeX } from 'lucide-react'
import type { ChatMessage } from '../../../runtime/chatbot'
import { formatTime } from '../../../runtime/chatbot'

interface MessageBubbleProps {
  message: ChatMessage
  showTimestamp?: boolean
  isSpeaking?: boolean
  onCopy?: () => void
  onListen?: () => void
  onRegenerate?: () => void
  onSuggested?: (q: string) => void
}

function ActionBtn({
  label,
  onClick,
  children,
  active,
}: {
  label: string
  onClick: () => void
  children: ReactNode
  active?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      aria-pressed={active}
      className={`relative flex h-8 w-8 items-center justify-center rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember ${
        active
          ? 'bg-ember-tint text-ember'
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
    // Code fence
    if (line.startsWith('```')) {
      const lang = line.slice(3).trim()
      const code: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) {
        code.push(lines[i])
        i++
      }
      nodes.push(
        <div key={`c-${i}`} className="my-2 overflow-hidden rounded-sm border border-line bg-raised">
          <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
            <span className="font-sans text-[11px] font-medium text-content-secondary">
              {lang || 'code'}
            </span>
            <button
              type="button"
              className="font-sans text-[11px] text-link hover:underline"
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
    // Bullet
    if (line.startsWith('• ') || line.startsWith('- ')) {
      nodes.push(
        <li key={`li-${i}`} className="ml-4 list-disc">
          {inlineFormat(line.slice(2))}
        </li>,
      )
      i++
      continue
    }
    // Bold heading-ish
    if (line.startsWith('**') && line.endsWith('**') && line.length > 4) {
      nodes.push(
        <p key={`h-${i}`} className="mt-2 font-medium text-content">
          {line.slice(2, -2)}
        </p>,
      )
      i++
      continue
    }
    if (line.trim() === '') {
      nodes.push(<div key={`sp-${i}`} className="h-2" />)
    } else {
      nodes.push(
        <p key={`p-${i}`} className="leading-[22px]">
          {inlineFormat(line)}
        </p>,
      )
    }
    i++
  }
  return nodes
}

function inlineFormat(text: string): ReactNode {
  // **bold** and `code`
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
        <code key={i} className="rounded-xs bg-raised px-1 py-0.5 font-mono text-[13px]">
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
  isSpeaking,
  onCopy,
  onListen,
  onRegenerate,
  onSuggested,
}: MessageBubbleProps) {
  const [copied, setCopied] = useState(false)
  const isUser = message.role === 'user'
  const isSystem = message.role === 'system'

  if (isSystem) {
    return (
      <div className="flex justify-center px-4 py-2">
        <span className="rounded-xs bg-raised px-3 py-1 font-sans text-[12px] text-content-secondary">
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

  return (
    <div className={`flex flex-col gap-1 px-4 py-2 ${isUser ? 'items-end' : 'items-start'}`}>
      <div className={`flex max-w-[88%] gap-2 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
        {!isUser && (
          <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-xs bg-raised text-content">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" aria-hidden>
              <rect x="4" y="6" width="16" height="13" rx="4" fill="currentColor" opacity="0.9" />
              <circle cx="10" cy="12" r="1.3" fill="#4F5AC7" />
              <circle cx="14" cy="12" r="1.3" fill="#4F5AC7" />
            </svg>
          </div>
        )}

        <div
          className={`rounded-md border px-3.5 py-2.5 font-sans text-[15px] transition-shadow duration-300 ${
            isUser
              ? 'border-line bg-raised text-content'
              : isSpeaking
                ? 'border-ember/40 bg-surface text-content shadow-[0_0_0_3px_rgba(var(--ember-rgb,200,80,40),0.12)] ring-1 ring-ember/30'
                : 'border-line bg-surface text-content'
          }`}
        >
          {message.error ? (
            <p className="text-danger-text">{message.error}</p>
          ) : (
            <div className="space-y-0.5">
              {message.content ? (
                renderContent(message.content)
              ) : message.isStreaming ? (
                <span className="text-content-secondary">…</span>
              ) : null}
              {message.isStreaming && (
                <span
                  className="ml-0.5 inline-block h-[1.1em] w-[2px] translate-y-[2px] animate-pulse bg-ember align-text-bottom"
                  aria-hidden
                />
              )}
            </div>
          )}
          {!message.isStreaming && message.routeTo && (
            <p className="mt-2 rounded-sm bg-warning-subtle px-2 py-1 text-[12px] font-medium text-warning-text">
              Route to {message.routeTo}
            </p>
          )}
          {!message.isStreaming && message.grounded === false && !message.routeTo && (
            <p className="mt-1 text-[11px] text-content-secondary">Limited evidence</p>
          )}
        </div>
      </div>

      {showTimestamp && (
        <span
          className={`px-1 font-sans text-[12px] text-content-disabled ${
            isUser ? 'mr-0' : 'ml-9'
          }`}
        >
          {formatTime(message.timestamp)}
        </span>
      )}

      {/* AI actions */}
      {!isUser && !message.isStreaming && (
        <div className="ml-9 flex items-center gap-0.5">
          <ActionBtn label={copied ? 'Copied' : 'Copy'} onClick={handleCopy}>
            {copied ? (
              <Check className="h-3.5 w-3.5 text-success" strokeWidth={2.5} />
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
              <span className="relative flex h-8 w-8 items-center justify-center">
                <span className="absolute inset-0 animate-ping rounded-full bg-ember/30" />
                <span className="absolute inset-1 animate-pulse rounded-full bg-ember/20" />
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

      {/* Suggested questions */}
      {message.suggestedQuestions && message.suggestedQuestions.length > 0 && (
        <div className="ml-9 mt-1 flex flex-wrap gap-2">
          {message.suggestedQuestions.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => onSuggested?.(q)}
              className="rounded-sm border border-line bg-surface px-3 py-1.5 font-sans text-[12px] font-medium text-content-secondary transition-colors hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
            >
              {q}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
