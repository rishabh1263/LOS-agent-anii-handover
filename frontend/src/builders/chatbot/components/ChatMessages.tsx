import { useEffect, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import type { ChatMessage, ChatUploadResult } from '../../../runtime/chatbot'
import { MessageBubble } from './MessageBubble'
import { QuickActions } from './QuickActions'
import { TypingDots } from './TypingDots'

interface ChatMessagesProps {
  messages: ChatMessage[]
  showTimestamps: boolean
  showSuggestedQuestions?: boolean
  speakingMessageId?: string | null
  onListen: (text: string, messageId: string) => void
  onRegenerate: (id: string) => void
  onEdit?: (messageId: string, newContent: string) => void
  onSuggested: (q: string) => void
  onUploadDocuments?: (files: { file: File; documentType: string }[]) => Promise<{
    results: Array<{
      status: 'pass' | 'review' | 'fail' | 'validation'
      detail?: string
      detectedType?: string
    }>
  }>
  onPersistUploadResults?: (messageId: string, results: ChatUploadResult[]) => void
  quickActions: { id: string; label: string; icon: string }[]
  onQuickAction: (label: string) => void
  status: string
}

const messageEnter = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.22, ease: [0.22, 1, 0.36, 1] as const },
}

export function ChatMessages({
  messages,
  showTimestamps,
  showSuggestedQuestions = true,
  speakingMessageId = null,
  onListen,
  onRegenerate,
  onEdit,
  onSuggested,
  onUploadDocuments,
  onPersistUploadResults,
  quickActions,
  onQuickAction,
  status,
}: ChatMessagesProps) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const reduceMotion = useReducedMotion()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({
      behavior: reduceMotion ? 'auto' : 'smooth',
    })
  }, [messages, status, reduceMotion])

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

  const showThinking =
    (status === 'thinking' || status === 'generating') &&
    !messages.some((m) => m.isStreaming)

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        className="h-full overflow-y-auto scroll-smooth py-3"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
      >
        {messages.map((m, index) => {
          const isRecent = index >= messages.length - 4
          const props = {
            message: m,
            showTimestamp: showTimestamps,
            showSuggestedQuestions,
            isSpeaking: speakingMessageId === m.id,
            editDisabled: status === 'generating' || status === 'thinking',
            onListen: () => onListen(m.content, m.id),
            onRegenerate: () => onRegenerate(m.id),
            onEdit,
            onSuggested,
            onUploadDocuments,
            onPersistUploadResults,
          }

          if (reduceMotion || !isRecent) {
            return <MessageBubble key={m.id} {...props} />
          }

          return (
            <motion.div
              key={m.id}
              initial={messageEnter.initial}
              animate={messageEnter.animate}
              transition={messageEnter.transition}
            >
              <MessageBubble {...props} />
            </motion.div>
          )
        })}

        <AnimatePresence>
          {showThinking && (
            <motion.div
              key="thinking"
              initial={reduceMotion ? false : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduceMotion ? undefined : { opacity: 0, y: 4 }}
              transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
              className="flex items-start gap-2 px-4 py-1.5"
              aria-live="polite"
              aria-label="Loading"
            >
              <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-raised text-content-secondary">
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" aria-hidden>
                  <path
                    d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z"
                    fill="currentColor"
                  />
                </svg>
              </div>
              <div className="rounded-2xl rounded-bl-md border border-line bg-raised px-4 py-3">
                <TypingDots />
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <div ref={bottomRef} />
      </div>

      <AnimatePresence>
        {showScrollBtn && (
          <motion.button
            type="button"
            initial={reduceMotion ? false : { opacity: 0, y: 8, scale: 0.9 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduceMotion ? undefined : { opacity: 0, y: 6, scale: 0.9 }}
            transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
            onClick={() =>
              bottomRef.current?.scrollIntoView({
                behavior: reduceMotion ? 'auto' : 'smooth',
              })
            }
            aria-label="Scroll to bottom"
            className="absolute bottom-3 left-1/2 z-10 flex h-8 w-8 -translate-x-1/2 cursor-pointer items-center justify-center rounded-full border border-line bg-surface/95 text-content-secondary shadow-md backdrop-blur-sm transition-colors hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            <ChevronDown className="h-4 w-4" strokeWidth={2} />
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  )
}
