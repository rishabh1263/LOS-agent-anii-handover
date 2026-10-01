import { useEffect, useRef, type PointerEvent as ReactPointerEvent } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import type { ChatbotApi } from '../../../runtime/chatbot'
import { ChatHeader } from './ChatHeader'
import { ChatMessages } from './ChatMessages'
import { ChatComposer } from './ChatComposer'
import { Settings } from './Settings'
import { ConfirmModal } from './ConfirmModal'
import { ensureChatThemeStyles } from './ChatThemeStyles'

interface ChatPanelProps {
  api: ChatbotApi
}

const spring = { type: 'spring' as const, stiffness: 360, damping: 34, mass: 0.85 }
const softSpring = { type: 'spring' as const, stiffness: 280, damping: 30, mass: 0.9 }

export function ChatPanel({ api }: ChatPanelProps) {
  const {
    mode,
    close,
    minimize,
    toggleExpand,
    status,
    messages,
    settings,
    updateSettings,
    input,
    setInput,
    sendMessage,
    stopGenerating,
    regenerate,
    editMessage,
    startNewConversation,
    confirmNewConversation,
    confirmNew,
    setConfirmNew,
    isListening,
    startListening,
    stopListening,
    interimTranscript,
    voiceError,
    dismissVoiceError,
    voiceWarning,
    testVoice,
    speakingMessageId,
    toggleSpeak,
    showSettings,
    setShowSettings,
    quickActions,
  } = api

  const panelRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()
  const open = mode !== 'closed'
  const isExpanded = mode === 'expanded'
  const isMobile = mode === 'mobile'

  const dragY = useRef(0)
  const dragStartY = useRef(0)
  const isDraggingSheet = useRef(false)

  useEffect(() => {
    ensureChatThemeStyles()
  }, [])

  useEffect(() => {
    if (open) panelRef.current?.focus()
  }, [open, mode])

  useEffect(() => {
    if (!isMobile || !open) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [isMobile, open])

  const onSheetPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!isMobile) return
    const target = e.target as HTMLElement
    if (!target.closest('[data-role="sheet-handle"]')) return
    isDraggingSheet.current = true
    dragStartY.current = e.clientY
    dragY.current = 0
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const onSheetPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!isDraggingSheet.current || !isMobile) return
    const dy = Math.max(0, e.clientY - dragStartY.current)
    dragY.current = dy
    if (panelRef.current) {
      panelRef.current.style.transform = `translateY(${dy}px)`
      panelRef.current.style.transition = 'none'
    }
  }

  const onSheetPointerUp = () => {
    if (!isDraggingSheet.current || !isMobile) return
    isDraggingSheet.current = false
    if (panelRef.current) {
      panelRef.current.style.transition = ''
      if (dragY.current > 120) {
        close()
      } else {
        panelRef.current.style.transform = ''
      }
    }
    dragY.current = 0
  }

  const theme = settings.chatTheme || 'light'
  const panelLayoutClass = isMobile
    ? 'chatbot-shell fixed inset-x-0 bottom-0 top-[max(0.5rem,env(safe-area-inset-top))] z-[70] flex max-h-[100dvh] flex-col overflow-hidden rounded-t-2xl border border-line bg-surface shadow-[0_-8px_40px_rgba(0,0,0,0.08)]'
    : isExpanded
      ? 'chatbot-shell fixed inset-4 z-[70] flex flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl sm:inset-6'
      : 'chatbot-shell fixed bottom-5 right-5 z-[70] flex h-[min(640px,calc(100dvh-5rem))] max-h-[calc(100dvh-4.5rem)] w-[min(400px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-[0_12px_48px_rgba(0,0,0,0.12)] md:bottom-6 md:right-6 md:w-[min(420px,calc(100vw-3rem))]'

  const enterExit = reduceMotion
    ? { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 } }
    : isMobile
      ? {
        initial: { opacity: 0, y: '24%' },
        animate: { opacity: 1, y: 0 },
        exit: { opacity: 0, y: '16%' },
      }
      : {
        initial: { opacity: 0, scale: 0.94, y: 12 },
        animate: { opacity: 1, scale: 1, y: 0 },
        exit: { opacity: 0, scale: 0.96, y: 8 },
      }

  return (
    <AnimatePresence>
      {open && (
        <>
          {(isMobile || isExpanded) && (
            <motion.button
              type="button"
              aria-label="Close assistant"
              className="fixed inset-0 z-[65] bg-black/20 backdrop-blur-[1px]"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
              onClick={close}
            />
          )}

          <motion.div
            ref={panelRef}
            key="chat-panel"
            tabIndex={-1}
            role="dialog"
            aria-label="AI Assistant"
            className={panelLayoutClass}
            data-chat-theme={theme}
            initial={enterExit.initial}
            animate={enterExit.animate}
            exit={enterExit.exit}
            transition={reduceMotion ? { duration: 0.15 } : spring}
            onPointerDown={onSheetPointerDown}
            onPointerMove={onSheetPointerMove}
            onPointerUp={onSheetPointerUp}
            onPointerCancel={onSheetPointerUp}
          >
            {isMobile && (
              <div
                data-role="sheet-handle"
                className="flex shrink-0 cursor-grab items-center justify-center py-2 active:cursor-grabbing"
              >
                <div className="h-1 w-10 rounded-full bg-content-disabled/40" />
              </div>
            )}

            <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
              <AnimatePresence mode="wait" initial={false}>
                {showSettings ? (
                  <motion.div
                    key="settings"
                    className="flex min-h-0 flex-1 flex-col"
                    initial={reduceMotion ? false : { opacity: 0, x: 12 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={reduceMotion ? undefined : { opacity: 0, x: 12 }}
                    transition={softSpring}
                  >
                    <Settings
                      settings={settings}
                      onChange={updateSettings}
                      onBack={() => setShowSettings(false)}
                      onTestVoice={testVoice}
                      voiceWarning={voiceWarning}
                    />
                  </motion.div>
                ) : (
                  <motion.div
                    key="chat"
                    className="flex min-h-0 flex-1 flex-col"
                    initial={reduceMotion ? false : { opacity: 0, x: -8 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={reduceMotion ? undefined : { opacity: 0, x: -8 }}
                    transition={softSpring}
                  >
                    <ChatHeader
                      status={status}
                      mode={isMobile ? 'mobile' : isExpanded ? 'expanded' : 'panel'}
                      onClose={close}
                      onMinimize={minimize}
                      onToggleExpand={toggleExpand}
                      onNewChat={startNewConversation}
                      onOpenSettings={() => setShowSettings(true)}
                    />

                    <div
                      className={`flex min-h-0 flex-1 flex-col overflow-hidden ${settings.compactMode
                          ? '[&_.py-1\\.5]:py-1 [&_.px-4]:px-3 [&_.text-\\[14\\.5px\\]]:text-[13.5px]'
                          : ''
                        }`}
                    >
                      <ChatMessages
                        messages={messages}
                        showTimestamps={settings.showTimestamps}
                        showSuggestedQuestions={settings.showSuggestedQuestions}
                        speakingMessageId={speakingMessageId}
                        onListen={(text, id) => toggleSpeak(text, id)}
                        onRegenerate={regenerate}
                        onEdit={editMessage}
                        onSuggested={(q) => void sendMessage(q)}
                        quickActions={quickActions}
                        onQuickAction={(label) => void sendMessage(label)}
                        status={status}
                      />
                    </div>

                    <ChatComposer
                      value={input}
                      onChange={setInput}
                      onSend={() => void sendMessage()}
                      onStop={stopGenerating}
                      isGenerating={status === 'generating' || status === 'thinking'}
                      isListening={isListening}
                      onStartListen={startListening}
                      onStopListen={stopListening}
                      interimTranscript={interimTranscript}
                      voiceError={voiceError}
                      onDismissVoiceError={dismissVoiceError}
                      sendWithEnter={settings.sendWithEnter}
                      voiceInputEnabled={settings.voiceInput}
                    />
                  </motion.div>
                )}
              </AnimatePresence>

              <ConfirmModal
                open={confirmNew}
                title="Start a new conversation?"
                description="This will clear the current chat and start fresh."
                confirmLabel="New conversation"
                cancelLabel="Cancel"
                onConfirm={confirmNewConversation}
                onCancel={() => setConfirmNew(false)}
              />
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
