import { useEffect, useRef, type DragEvent } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import type { ChatbotApi } from '../../../runtime/chatbot'
import { ChatHeader } from './ChatHeader'
import { ChatMessages } from './ChatMessages'
import { ChatComposer } from './ChatComposer'
import { Settings } from './Settings'
import { ConversationSidebar } from './ConversationSidebar'
import { ConfirmModal } from './ConfirmModal'
import { ensureChatThemeStyles } from './ChatThemeStyles'

interface ChatPanelProps {
  api: ChatbotApi
}

/** Apple-like spring — snappy but soft */
const spring = { type: 'spring' as const, stiffness: 380, damping: 32, mass: 0.85 }
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
    attachments,
    addAttachment,
    removeAttachment,
    sendMessage,
    stopGenerating,
    regenerate,
    conversations,
    activeId,
    selectConversation,
    startNewConversation,
    confirmNewConversation,
    deleteConversation,
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
    showSidebar,
    setShowSidebar,
    isDragging,
    setIsDragging,
    quickActions,
  } = api

  const panelRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()
  const open = mode !== 'closed'
  const isExpanded = mode === 'expanded'
  const isMobile = mode === 'mobile'

  useEffect(() => {
    ensureChatThemeStyles()
  }, [])

  useEffect(() => {
    if (open) panelRef.current?.focus()
  }, [open, mode])

  // Body scroll lock on mobile sheet
  useEffect(() => {
    if (!isMobile || !open) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [isMobile, open])

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
    if (e.dataTransfer.files?.length) {
      Array.from(e.dataTransfer.files).forEach((f) => addAttachment(f))
    }
  }

  const theme = settings.chatTheme || 'light'
  const panelLayoutClass = isMobile
    ? 'chatbot-shell fixed inset-x-0 bottom-0 top-[max(0.5rem,env(safe-area-inset-top))] z-[70] flex max-h-[100dvh] flex-col overflow-hidden rounded-t-[20px] border border-line bg-surface/95 shadow-2xl backdrop-blur-xl'
    : isExpanded
      ? 'chatbot-shell fixed inset-4 z-[70] flex flex-col overflow-hidden rounded-2xl border border-line bg-surface/95 shadow-2xl backdrop-blur-xl sm:inset-5'
      : 'chatbot-shell fixed bottom-4 right-4 z-[70] flex h-[min(680px,calc(100dvh-5.5rem))] max-h-[calc(100dvh-5rem)] w-[min(420px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-line bg-surface/95 shadow-2xl backdrop-blur-xl md:bottom-6 md:right-6 md:w-[min(460px,calc(100vw-3rem))]'

  const enterExit = reduceMotion
    ? { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 } }
    : isMobile
      ? {
        initial: { opacity: 0, y: '28%' },
        animate: { opacity: 1, y: 0 },
        exit: { opacity: 0, y: '18%' },
      }
      : {
        initial: { opacity: 0, scale: 0.92, y: 16 },
        animate: { opacity: 1, scale: 1, y: 0 },
        exit: { opacity: 0, scale: 0.94, y: 10 },
      }

  return (
    <AnimatePresence>
      {open && (
        <>
          {/* Dim scrim — mobile + expanded */}
          {(isMobile || isExpanded) && (
            <motion.button
              type="button"
              aria-label="Close assistant"
              className="fixed inset-0 z-[65] bg-black/25 backdrop-blur-[2px]"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
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
            onDragOver={(e) => {
              e.preventDefault()
              setIsDragging(true)
            }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={onDrop}
          >
            {isDragging && (
              <div className="absolute inset-0 z-30 flex flex-col items-center justify-center rounded-[inherit] border-2 border-dashed border-link bg-info-subtle/90">
                <p className="font-display text-[18px] font-semibold text-content">
                  Drop your file here
                </p>
                <p className="mt-1 font-sans text-[13px] text-content-secondary">
                  Upload document to AI
                </p>
              </div>
            )}

            <div className="flex min-h-0 flex-1">
              {(isExpanded || showSidebar) && !isMobile && (
                <motion.div
                  initial={reduceMotion ? false : { opacity: 0, x: -12 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={softSpring}
                  className="flex h-full shrink-0"
                >
                  <ConversationSidebar
                    open
                    conversations={conversations}
                    activeId={activeId}
                    onSelect={selectConversation}
                    onNew={startNewConversation}
                    onDelete={deleteConversation}
                  />
                </motion.div>
              )}

              <div
                className={`relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden ${settings.compactMode ? '[&_.py-2]:py-1.5 [&_.px-4]:px-3' : ''
                  }`}
              >
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
                        showSidebarToggle={isExpanded}
                        onToggleSidebar={() => setShowSidebar((s) => !s)}
                      />

                      <ChatMessages
                        messages={messages}
                        showTimestamps={settings.showTimestamps}
                        speakingMessageId={speakingMessageId}
                        onListen={(text, id) => toggleSpeak(text, id)}
                        onRegenerate={regenerate}
                        onSuggested={(q) => void sendMessage(q)}
                        quickActions={quickActions}
                        onQuickAction={(label) => void sendMessage(label)}
                        status={status}
                      />

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
                        attachments={attachments}
                        onAddFiles={(files) => Array.from(files).forEach((f) => addAttachment(f))}
                        onRemoveAttachment={removeAttachment}
                        sendWithEnter={settings.sendWithEnter}
                        voiceInputEnabled={settings.voiceInput}
                      />
                    </motion.div>
                  )}
                </AnimatePresence>

                <ConfirmModal
                  open={confirmNew}
                  title="Start a new conversation?"
                  description="Your current conversation will remain in your history."
                  confirmLabel="New conversation"
                  cancelLabel="Cancel"
                  onConfirm={confirmNewConversation}
                  onCancel={() => setConfirmNew(false)}
                />
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
