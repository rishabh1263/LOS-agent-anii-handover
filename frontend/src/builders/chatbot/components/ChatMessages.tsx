import { useEffect, useRef } from 'react'
import type { ChatMessage } from '../../../runtime/chatbot'
import { MessageBubble } from './MessageBubble'
import { QuickActions } from './QuickActions'

interface ChatMessagesProps {
  messages: ChatMessage[]
  showTimestamps: boolean
  isSpeaking: boolean
  onListen: (text: string) => void
  onRegenerate: (id: string) => void
  onSuggested: (q: string) => void
  quickActions: { id: string; label: string; icon: string }[]
  onQuickAction: (label: string) => void
  status: string
}

export function ChatMessages({
  messages,
  showTimestamps,
  isSpeaking,
  onListen,
  onRegenerate,
  onSuggested,
  quickActions,
  onQuickAction,
  status,
}: ChatMessagesProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, status])

  if (messages.length === 0) {
    return <QuickActions actions={quickActions} onSelect={onQuickAction} />
  }

  return (
    <div
      className="flex-1 overflow-y-auto py-2"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
    >
      {messages.map((m) => (
        <MessageBubble
          key={m.id}
          message={m}
          showTimestamp={showTimestamps}
          isSpeaking={isSpeaking}
          onListen={() => onListen(m.content)}
          onRegenerate={() => onRegenerate(m.id)}
          onSuggested={onSuggested}
        />
      ))}
      {(status === 'thinking' || status === 'generating') &&
        !messages.some((m) => m.isStreaming) && (
          <div className="flex items-center gap-2 px-4 py-3 pl-[52px]">
            <span className="flex gap-1">
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-disabled [animation-delay:0ms]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-disabled [animation-delay:120ms]" />
              <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-disabled [animation-delay:240ms]" />
            </span>
            <span className="font-sans text-[13px] text-content-secondary">AI is thinking…</span>
          </div>
        )}
      <div ref={bottomRef} />
    </div>
  )
}
