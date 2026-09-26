import { useEffect, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import type { ChatMessage } from '../../../runtime/chatbot'
import { MessageBubble } from './MessageBubble'
import { QuickActions } from './QuickActions'

interface ChatMessagesProps {
  messages: ChatMessage[]
  showTimestamps: boolean
  showSuggestedQuestions?: boolean
  speakingMessageId?: string | null
  onListen: (text: string, messageId: string) => void
  onRegenerate: (id: string) => void
  onSuggested: (q: string) => void
  quickActions: { id: string; label: string; icon: string }[]
  onQuickAction: (label: string) => void
  status: string
}

export function ChatMessages({
  messages,
  showTimestamps,
  showSuggestedQuestions = true,
  speakingMessageId = null,
  onListen,
  onRegenerate,
  onSuggested,
  quickActions,
  onQuickAction,
  status,
}: ChatMessagesProps) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [showScrollBtn, setShowScrollBtn] = useState(false)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, status])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      setShowScrollBtn(distFromBottom > 120)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [messages.length])

  if (messages.length === 0) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto">
        <QuickActions actions={quickActions} onSelect={onQuickAction} />
      </div>
    )
  }

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        className="h-full overflow-y-auto py-3"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
      >
        {messages.map((m) => (
          <MessageBubble
            key={m.id}
            message={m}
            showTimestamp={showTimestamps}
            showSuggestedQuestions={showSuggestedQuestions}
            isSpeaking={speakingMessageId === m.id}
            onListen={() => onListen(m.content, m.id)}
            onRegenerate={() => onRegenerate(m.id)}
            onSuggested={onSuggested}
          />
        ))}
        {(status === 'thinking' || status === 'generating') &&
          !messages.some((m) => m.isStreaming) && (
            <div className="flex items-center gap-2.5 px-4 py-3 pl-[52px]">
              <span className="flex items-center gap-1">
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-disabled [animation-delay:0ms]" />
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-disabled [animation-delay:150ms]" />
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-disabled [animation-delay:300ms]" />
              </span>
              <span className="font-sans text-[13px] text-content-secondary">Thinking…</span>
            </div>
          )}
        <div ref={bottomRef} />
      </div>

      {showScrollBtn && (
        <button
          type="button"
          onClick={() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' })}
          aria-label="Scroll to bottom"
          className="absolute bottom-3 left-1/2 z-10 flex h-8 w-8 -translate-x-1/2 items-center justify-center rounded-full border border-line bg-surface text-content-secondary shadow-md transition-colors hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
        >
          <ChevronDown className="h-4 w-4" strokeWidth={2} />
        </button>
      )}
    </div>
  )
}
