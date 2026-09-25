import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type {
  AiStatus,
  ChatAttachment,
  ChatMessage,
  ChatPanelMode,
  ChatSettings,
  Conversation,
} from '../types'

import { DEFAULT_SETTINGS } from '../types'

import { uid } from '../utils'

const DEMO_REPLIES = [
  "I can help you with documents, KYC checks, application status, and more. What would you like to do?",
  "Based on the context available, here's a concise summary of the key points.\n\n• Primary applicant verified\n• Co-applicant documents pending\n• Cross-document name match: 94%\n\nWould you like me to dig deeper into any section?",
  "Here's what I found:\n\n```json\n{\n  \"status\": \"PASS\",\n  \"score\": 0.92,\n  \"checks\": 12\n}\n```\n\nEverything looks good. You can proceed to the next step.",
  "I've analyzed the uploaded file. The document appears valid and matches the applicant profile. Let me know if you need an extraction of specific fields.",
]

const QUICK_ACTIONS = [
  { id: 'summarize', label: 'Summarize a document', icon: 'FileText' },
  { id: 'explain', label: 'Explain something', icon: 'HelpCircle' },
  { id: 'analyze', label: 'Analyze a file', icon: 'Search' },
  { id: 'status', label: 'Check application status', icon: 'ClipboardList' },
  { id: 'extract', label: 'Extract information', icon: 'Scan' },
  { id: 'report', label: 'Generate a report', icon: 'BarChart3' },
]

function createWelcomeConversation(): Conversation {
  const now = Date.now()
  return {
    id: uid('conv'),
    title: 'New conversation',
    updatedAt: now,
    messages: [],
  }
}

export function useChatbot() {
  const [mode, setMode] = useState<ChatPanelMode>('closed')
  const [conversations, setConversations] = useState<Conversation[]>(() => [
    {
      id: 'conv_demo_1',
      title: 'Document KYC',
      updatedAt: Date.now() - 3600000,
      pinned: true,
      messages: [
        {
          id: 'm1',
          role: 'user',
          content: 'Can you check the KYC status for this application?',
          timestamp: Date.now() - 3700000,
        },
        {
          id: 'm2',
          role: 'assistant',
          content:
            'I reviewed the latest KYC run.\n\n**Result:** PASS\n**Score:** 0.94\n\nAll primary documents verified. Co-applicant PAN is still pending upload.',
          timestamp: Date.now() - 3600000,
          suggestedQuestions: [
            'Show me the failed checks',
            'What documents are missing?',
            'Summarize in simple terms',
          ],
        },
      ],
    },
    {
      id: 'conv_demo_2',
      title: 'Loan Analysis',
      updatedAt: Date.now() - 90000000,
      messages: [],
    },
  ])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [status, setStatus] = useState<AiStatus>('online')
  const [settings, setSettings] = useState<ChatSettings>(DEFAULT_SETTINGS)
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState<ChatAttachment[]>([])
  const [isListening, setIsListening] = useState(false)
  const [isSpeaking, setIsSpeaking] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [showSidebar, setShowSidebar] = useState(false)
  const [confirmNew, setConfirmNew] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const stopRef = useRef(false)
  const replyIndex = useRef(0)

  const activeConversation = useMemo(
    () => conversations.find((c) => c.id === activeId) ?? null,
    [conversations, activeId],
  )

  const messages = activeConversation?.messages ?? []

  const open = useCallback(() => {
    setMode((m) => (m === 'closed' ? 'panel' : m))
    if (!activeId) {
      const conv = createWelcomeConversation()
      setConversations((prev) => [conv, ...prev])
      setActiveId(conv.id)
    }
  }, [activeId])

  const close = useCallback(() => {
    setMode('closed')
    setShowSettings(false)
    setShowSidebar(false)
  }, [])

  const toggleExpand = useCallback(() => {
    setMode((m) => (m === 'expanded' ? 'panel' : 'expanded'))
  }, [])

  const minimize = useCallback(() => {
    setMode('closed')
  }, [])

  const updateSettings = useCallback((patch: Partial<ChatSettings>) => {
    setSettings((s) => ({ ...s, ...patch }))
  }, [])

  const selectConversation = useCallback((id: string) => {
    setActiveId(id)
    setShowSettings(false)
  }, [])

  const startNewConversation = useCallback(() => {
    if (messages.length > 0) {
      setConfirmNew(true)
      return
    }
    const conv = createWelcomeConversation()
    setConversations((prev) => [conv, ...prev])
    setActiveId(conv.id)
    setConfirmNew(false)
  }, [messages.length])

  const confirmNewConversation = useCallback(() => {
    const conv = createWelcomeConversation()
    setConversations((prev) => [conv, ...prev])
    setActiveId(conv.id)
    setConfirmNew(false)
  }, [])

  const deleteConversation = useCallback(
    (id: string) => {
      setConversations((prev) => prev.filter((c) => c.id !== id))
      if (activeId === id) {
        setActiveId(null)
      }
    },
    [activeId],
  )

  const addAttachment = useCallback((file: File) => {
    const att: ChatAttachment = {
      id: uid('att'),
      name: file.name,
      size: file.size,
      type: file.type || 'application/octet-stream',
      progress: 100,
    }
    setAttachments((prev) => [...prev, att])
  }, [])

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id))
  }, [])

  const sendMessage = useCallback(
    async (text?: string) => {
      const content = (text ?? input).trim()
      if (!content && attachments.length === 0) return
      if (status === 'generating' || status === 'thinking') return

      let convId = activeId
      if (!convId) {
        const conv = createWelcomeConversation()
        setConversations((prev) => [conv, ...prev])
        setActiveId(conv.id)
        convId = conv.id
      }

      const userMsg: ChatMessage = {
        id: uid('msg'),
        role: 'user',
        content: content || (attachments.length ? `Uploaded ${attachments.length} file(s)` : ''),
        timestamp: Date.now(),
        attachments: attachments.length ? [...attachments] : undefined,
      }

      setConversations((prev) =>
        prev.map((c) =>
          c.id === convId
            ? {
              ...c,
              title: c.messages.length === 0 ? content.slice(0, 40) || 'New conversation' : c.title,
              updatedAt: Date.now(),
              messages: [...c.messages, userMsg],
            }
            : c,
        ),
      )
      setInput('')
      setAttachments([])
      setStatus('thinking')
      stopRef.current = false

      await new Promise((r) => setTimeout(r, 600))
      if (stopRef.current) {
        setStatus('online')
        return
      }

      setStatus('generating')
      const replyText = DEMO_REPLIES[replyIndex.current % DEMO_REPLIES.length]
      replyIndex.current += 1

      const assistantId = uid('msg')
      const streamingMsg: ChatMessage = {
        id: assistantId,
        role: 'assistant',
        content: '',
        timestamp: Date.now(),
        isStreaming: true,
      }

      setConversations((prev) =>
        prev.map((c) =>
          c.id === convId
            ? { ...c, updatedAt: Date.now(), messages: [...c.messages, streamingMsg] }
            : c,
        ),
      )

      // Simulate streaming
      for (let i = 0; i < replyText.length; i += 4) {
        if (stopRef.current) break
        await new Promise((r) => setTimeout(r, 28))
        const slice = replyText.slice(0, i + 4)
        setConversations((prev) =>
          prev.map((c) =>
            c.id === convId
              ? {
                ...c,
                messages: c.messages.map((m) =>
                  m.id === assistantId ? { ...m, content: slice } : m,
                ),
              }
              : c,
          ),
        )
      }

      setConversations((prev) =>
        prev.map((c) =>
          c.id === convId
            ? {
              ...c,
              messages: c.messages.map((m) =>
                m.id === assistantId
                  ? {
                    ...m,
                    content: stopRef.current ? m.content : replyText,
                    isStreaming: false,
                    suggestedQuestions: settings.showSuggestedQuestions
                      ? ['Explain in simple terms', 'Show key risks', 'What next?']
                      : undefined,
                  }
                  : m,
              ),
            }
            : c,
        ),
      )
      setStatus('online')
    },
    [input, attachments, activeId, status, settings.showSuggestedQuestions],
  )

  const stopGenerating = useCallback(() => {
    stopRef.current = true
    setStatus('online')
  }, [])

  const regenerate = useCallback(
    (messageId: string) => {
      if (!activeId) return
      const conv = conversations.find((c) => c.id === activeId)
      if (!conv) return
      const idx = conv.messages.findIndex((m) => m.id === messageId)
      if (idx < 0) return
      // Find previous user message
      let userContent = ''
      for (let i = idx - 1; i >= 0; i--) {
        if (conv.messages[i].role === 'user') {
          userContent = conv.messages[i].content
          break
        }
      }
      // Remove the assistant message and resend
      setConversations((prev) =>
        prev.map((c) =>
          c.id === activeId
            ? { ...c, messages: c.messages.filter((m) => m.id !== messageId) }
            : c,
        ),
      )
      void sendMessage(userContent)
    },
    [activeId, conversations, sendMessage],
  )

  const startListening = useCallback(() => {
    if (!settings.voiceInput) return
    setIsListening(true)
    setStatus('listening')
    // Dummy: after 2s insert sample text
    setTimeout(() => {
      setInput((prev) => (prev ? prev + ' ' : '') + 'Check KYC status for the current application')
      setIsListening(false)
      setStatus('online')
    }, 2000)
  }, [settings.voiceInput])

  const stopListening = useCallback(() => {
    setIsListening(false)
    setStatus('online')
  }, [])

  const toggleSpeak = useCallback(
    (text: string) => {
      if (isSpeaking) {
        setIsSpeaking(false)
        setStatus('online')
        return
      }
      setIsSpeaking(true)
      setStatus('speaking')
      // Dummy TTS duration based on length
      const ms = Math.min(8000, Math.max(1500, text.length * 40))
      setTimeout(() => {
        setIsSpeaking(false)
        setStatus('online')
      }, ms)
    },
    [isSpeaking],
  )

  // Responsive mode hint (consumer can override with media queries)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && mode !== 'closed') {
        if (showSettings) setShowSettings(false)
        else if (showSidebar) setShowSidebar(false)
        else close()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mode, showSettings, showSidebar, close])

  return {
    mode,
    setMode,
    open,
    close,
    toggleExpand,
    minimize,
    conversations,
    activeId,
    activeConversation,
    messages,
    status,
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
    quickActions: QUICK_ACTIONS,
  }
}

export type ChatbotApi = ReturnType<typeof useChatbot>
