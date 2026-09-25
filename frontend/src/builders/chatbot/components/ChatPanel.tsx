import { useEffect, useRef, type DragEvent } from 'react'
import type { ChatbotApi } from '../../../runtime/chatbot'
import { ChatHeader } from './ChatHeader'
import { ChatMessages } from './ChatMessages'
import { ChatComposer } from './ChatComposer'
import { Settings } from './Settings'
import { ConversationSidebar } from './ConversationSidebar'
import { ConfirmModal } from './ConfirmModal'

interface ChatPanelProps {
  api: ChatbotApi
}

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
    isSpeaking,
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

  useEffect(() => {
    if (mode !== 'closed') {
      // Focus panel for a11y
      panelRef.current?.focus()
    }
  }, [mode])

  if (mode === 'closed') return null

  const isExpanded = mode === 'expanded'
  const isMobile = mode === 'mobile'

  const panelClasses = isMobile
    ? 'fixed inset-x-0 bottom-0 top-0 z-[70] flex flex-col rounded-t-2xl border border-line bg-surface shadow-lg'
    : isExpanded
      ? 'fixed inset-5 z-[70] flex flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-lg'
      : 'fixed bottom-6 right-6 z-[70] flex h-[min(720px,calc(100vh-48px))] w-[min(460px,calc(100vw-48px))] flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-lg'

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
    if (e.dataTransfer.files?.length) {
      Array.from(e.dataTransfer.files).forEach((f) => addAttachment(f))
    }
  }

  return (
    <div
      ref={panelRef}
      tabIndex={-1}
      className={panelClasses}
      role="dialog"
      aria-label="AI Assistant"
      onDragOver={(e) => {
        e.preventDefault()
        setIsDragging(true)
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={onDrop}
    >
      {/* Drop overlay */}
      {isDragging && (
        <div className="absolute inset-0 z-30 flex flex-col items-center justify-center rounded-[inherit] border-2 border-dashed border-link bg-info-subtle/90">
          <p className="font-display text-[18px] font-semibold text-content">Drop your file here</p>
          <p className="mt-1 font-sans text-[13px] text-content-secondary">
            Upload document to AI
          </p>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        {/* Sidebar (expanded desktop) */}
        {(isExpanded || showSidebar) && !isMobile && (
          <ConversationSidebar
            open
            conversations={conversations}
            activeId={activeId}
            onSelect={selectConversation}
            onNew={startNewConversation}
            onDelete={deleteConversation}
          />
        )}

        <div
          className={`relative flex min-w-0 flex-1 flex-col ${
            settings.compactMode ? '[&_.py-2]:py-1.5 [&_.px-4]:px-3' : ''
          }`}
        >
          {showSettings ? (
            <Settings
              settings={settings}
              onChange={updateSettings}
              onBack={() => setShowSettings(false)}
            />
          ) : (
            <>
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
                isSpeaking={isSpeaking}
                onListen={toggleSpeak}
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
                attachments={attachments}
                onAddFiles={(files) => Array.from(files).forEach((f) => addAttachment(f))}
                onRemoveAttachment={removeAttachment}
                sendWithEnter={settings.sendWithEnter}
                voiceInputEnabled={settings.voiceInput}
              />
            </>
          )}

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
    </div>
  )
}
