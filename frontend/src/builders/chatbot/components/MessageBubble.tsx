import { useState, type ReactNode } from 'react'
import { Check, Copy, RefreshCw, Volume2, VolumeX } from 'lucide-react'
import type { ChatMessage } from '../../../runtime/chatbot'
import { formatTime } from '../../../runtime/chatbot'

interface MessageBubbleProps {
  message: ChatMessage
  showTimestamp?: boolean
  showSuggestedQuestions?: boolean
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
      className={`relative flex h-7 w-7 items-center justify-center rounded-md transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember ${
        active
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
              className="font-sans text-[11px] text-ember hover:underline"
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
        <li key={`li-${i}`} className="ml-4 list-disc">
          {inlineFormat(line.slice(2))}
        </li>,
      )
      i++
      continue
    }
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
      nodes.push(<div key={`sp-${i}`} className="h-1.5" />)
    } else {
      nodes.push(
        <p key={`p-${i}`} className="leading-[1.55]">
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

  return (
    <div
      className={`group flex flex-col gap-1 px-4 py-1.5 ${isUser ? 'items-end' : 'items-start'}`}
    >
      <div className={`flex max-w-[85%] gap-2 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
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
          className={`rounded-2xl px-3.5 py-2.5 font-sans text-[14.5px] leading-[1.5] ${
            isUser
              ? 'rounded-br-md text-white'
              : isSpeaking
                ? 'rounded-bl-md border border-ember/30 shadow-[0_0_0_3px_rgba(37,99,235,0.08)]'
                : 'rounded-bl-md'
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
                  className="ml-0.5 inline-block h-[1em] w-[2px] translate-y-[1px] animate-pulse bg-current align-text-bottom opacity-60"
                  aria-hidden
                />
              )}
            </div>
          )}
          {!message.isStreaming && message.routeTo && (
            <p className="mt-2 rounded-md bg-amber-50 px-2 py-1 text-[12px] font-medium text-amber-800">
              Route to {message.routeTo}
            </p>
          )}
          {!message.isStreaming && message.grounded === false && !message.routeTo && (
            <p className="mt-1 text-[11px] opacity-60">Limited evidence</p>
          )}
        </div>
      </div>

      {showTimestamp && (
        <span
          className={`px-1 font-sans text-[11px] text-content-disabled ${
            isUser ? 'mr-0' : 'ml-9'
          }`}
        >
          {formatTime(message.timestamp)}
        </span>
      )}

      {/* AI actions — hover / focus / speaking only */}
      {!isUser && !message.isStreaming && (
        <div
          className={`ml-9 flex items-center gap-0.5 transition-opacity duration-150 ${
            isSpeaking
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
          <div className="ml-9 mt-1.5 flex flex-wrap gap-1.5">
            {message.suggestedQuestions.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => onSuggested?.(q)}
                className="rounded-full border border-line bg-surface px-3 py-1.5 font-sans text-[12px] font-medium text-content-secondary transition-colors hover:border-ember/40 hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
              >
                {q}
              </button>
            ))}
          </div>
        )}
    </div>
  )
}
