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

/** Minimal SpeechRecognition shape for browsers that expose it (incl. webkit prefix). */
interface SpeechRecognitionLike {
  continuous: boolean
  interimResults: boolean
  lang: string
  onresult: ((event: SpeechRecognitionResultEvent) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
}

interface SpeechRecognitionResultEvent {
  resultIndex: number
  results: ArrayLike<{ isFinal: boolean; 0?: { transcript: string } }>
}

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
  const [speakingMessageId, setSpeakingMessageId] = useState<string | null>(null)
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [showSidebar, setShowSidebar] = useState(false)
  const [confirmNew, setConfirmNew] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const stopRef = useRef(false)
  const replyIndex = useRef(0)
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null)
  const listenTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const speakTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const settingsRef = useRef(settings)
  settingsRef.current = settings
  const speakTextRef = useRef<(text: string, messageId?: string) => void>(() => {})
  const [interimTranscript, setInterimTranscript] = useState('')

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
    // Tear down voice so mic/TTS don't keep running in background
    try {
      recognitionRef.current?.stop()
    } catch {
      /* ignore */
    }
    recognitionRef.current = null
    try {
      window.speechSynthesis?.cancel()
    } catch {
      /* ignore */
    }
    if (listenTimeoutRef.current) {
      clearTimeout(listenTimeoutRef.current)
      listenTimeoutRef.current = null
    }
    if (speakTimeoutRef.current) {
      clearTimeout(speakTimeoutRef.current)
      speakTimeoutRef.current = null
    }
    setIsListening(false)
    setInterimTranscript('')
    setIsSpeaking(false)
    setSpeakingMessageId(null)
    setStatus('online')
  }, [])

  const toggleExpand = useCallback(() => {
    setMode((m) => (m === 'expanded' ? 'panel' : 'expanded'))
  }, [])

  const minimize = useCallback(() => {
    close()
  }, [close])

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

      const finalContent = stopRef.current ? undefined : replyText
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

      // Auto-read finished reply when enabled and not stopped mid-stream
      if (finalContent && settingsRef.current.autoReadResponses) {
        setTimeout(() => speakTextRef.current(finalContent, assistantId), 80)
      }
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

  const clearListenTimeout = useCallback(() => {
    if (listenTimeoutRef.current) {
      clearTimeout(listenTimeoutRef.current)
      listenTimeoutRef.current = null
    }
  }, [])

  const clearSpeakTimeout = useCallback(() => {
    if (speakTimeoutRef.current) {
      clearTimeout(speakTimeoutRef.current)
      speakTimeoutRef.current = null
    }
  }, [])

  const stopSpeaking = useCallback(() => {
    clearSpeakTimeout()
    try {
      window.speechSynthesis?.cancel()
    } catch {
      /* ignore */
    }
    setIsSpeaking(false)
    setSpeakingMessageId(null)
    setStatus((s) => (s === 'speaking' ? 'online' : s))
  }, [clearSpeakTimeout])

  const speakText = useCallback(
    (text: string, messageId?: string) => {
      const trimmed = text.replace(/```[\s\S]*?```/g, ' ').replace(/\s+/g, ' ').trim()
      if (!trimmed) return

      clearSpeakTimeout()
      try {
        window.speechSynthesis?.cancel()
      } catch {
        /* ignore */
      }

      const rate = settingsRef.current.speechSpeed || 1

      if (typeof window !== 'undefined' && window.speechSynthesis) {
        const utterance = new SpeechSynthesisUtterance(trimmed)
        utterance.rate = Math.min(2, Math.max(0.5, rate))
        utterance.onend = () => {
          setIsSpeaking(false)
          setSpeakingMessageId(null)
          setStatus((s) => (s === 'speaking' ? 'online' : s))
        }
        utterance.onerror = () => {
          setIsSpeaking(false)
          setSpeakingMessageId(null)
          setStatus((s) => (s === 'speaking' ? 'online' : s))
        }
        setIsSpeaking(true)
        setSpeakingMessageId(messageId ?? null)
        setStatus('speaking')
        window.speechSynthesis.speak(utterance)
        return
      }

      // Fallback: timed status only (no audio)
      setIsSpeaking(true)
      setSpeakingMessageId(messageId ?? null)
      setStatus('speaking')
      const ms = Math.min(8000, Math.max(1200, trimmed.length * (40 / rate)))
      speakTimeoutRef.current = setTimeout(() => {
        setIsSpeaking(false)
        setSpeakingMessageId(null)
        setStatus((s) => (s === 'speaking' ? 'online' : s))
        speakTimeoutRef.current = null
      }, ms)
    },
    [clearSpeakTimeout],
  )
  speakTextRef.current = speakText

  const stopListening = useCallback(() => {
    clearListenTimeout()
    const rec = recognitionRef.current
    if (rec) {
      try {
        rec.onresult = null
        rec.onerror = null
        rec.onend = null
        rec.stop()
      } catch {
        /* ignore */
      }
      recognitionRef.current = null
    }
    setInterimTranscript('')
    setIsListening(false)
    setStatus((s) => (s === 'listening' ? 'online' : s))
  }, [clearListenTimeout])

  const mapMicError = useCallback((code: string): string => {
    switch (code) {
      case 'not-allowed':
      case 'permission-denied':
      case 'PermissionDeniedError':
      case 'NotAllowedError':
        return 'Microphone access denied. Allow mic permission in your browser settings, then try again.'
      case 'service-not-allowed':
        return 'Microphone blocked by the browser or site policy. Check site permissions and try again.'
      case 'audio-capture':
      case 'NotFoundError':
      case 'DevicesNotFoundError':
        return 'No microphone found. Connect a mic and try again.'
      case 'NotReadableError':
      case 'TrackStartError':
        return 'Microphone is in use by another app. Close it and try again.'
      case 'OverconstrainedError':
        return 'Could not access this microphone. Try a different device.'
      case 'SecurityError':
        return 'Microphone requires a secure connection (HTTPS). Open the app over HTTPS and try again.'
      case 'network':
        return 'Network error during voice input. Check your connection and try again.'
      case 'no-speech':
        return 'No speech detected. Click the mic and speak clearly.'
      case 'aborted':
        return ''
      case 'language-not-supported':
        return 'Speech recognition is not available for this language.'
      case 'unsupported':
        return 'Voice input is not supported in this browser. Try Chrome or Edge.'
      case 'insecure':
        return 'Microphone requires HTTPS. Open the app on a secure origin and try again.'
      default:
        return 'Voice input failed. Check microphone access and try again.'
    }
  }, [])

  /** Probe mic permission before starting recognition. Returns error message or null if OK. */
  const ensureMicAccess = useCallback(async (): Promise<string | null> => {
    if (typeof window === 'undefined') {
      return mapMicError('unsupported')
    }

    // SpeechRecognition / getUserMedia require a secure context in modern browsers
    if (!window.isSecureContext) {
      return mapMicError('insecure')
    }

    // Permissions API (optional — not all browsers support microphone query)
    try {
      if (navigator.permissions?.query) {
        const status = await navigator.permissions.query({
          name: 'microphone' as PermissionName,
        })
        if (status.state === 'denied') {
          return mapMicError('not-allowed')
        }
      }
    } catch {
      // Ignore — some browsers reject microphone permission queries
    }

    // getUserMedia is the most reliable pre-check for actual device access
    if (navigator.mediaDevices?.getUserMedia) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        stream.getTracks().forEach((t) => t.stop())
        return null
      } catch (err) {
        const name =
          err && typeof err === 'object' && 'name' in err
            ? String((err as { name: string }).name)
            : 'not-allowed'
        return mapMicError(name)
      }
    }

    // No getUserMedia — still allow SpeechRecognition to try; it may prompt on its own
    return null
  }, [mapMicError])

  const startListening = useCallback(async () => {
    if (!settings.voiceInput || isListening) return
    setVoiceError(null)
    setInterimTranscript('')
    stopSpeaking()

    type SpeechRecognitionCtor = new () => SpeechRecognitionLike
    const w = typeof window !== 'undefined' ? (window as unknown as Record<string, unknown>) : null
    const SpeechRecognitionCtor = (w?.SpeechRecognition || w?.webkitSpeechRecognition) as
      | SpeechRecognitionCtor
      | undefined

    if (!SpeechRecognitionCtor) {
      // Demo fallback only when API is missing (e.g. some automated environments)
      setIsListening(true)
      setStatus('listening')
      setInterimTranscript('Listening…')
      clearListenTimeout()
      listenTimeoutRef.current = setTimeout(() => {
        const sample = 'Check KYC status for the current application'
        setInput((prev) => {
          const base = prev.trim()
          return base ? `${base} ${sample}` : sample
        })
        setInterimTranscript('')
        setIsListening(false)
        setStatus((s) => (s === 'listening' ? 'online' : s))
        listenTimeoutRef.current = null
      }, 2000)
      return
    }

    // Pre-check mic access so we surface a clear error before recognition starts
    const accessError = await ensureMicAccess()
    if (accessError) {
      setVoiceError(accessError)
      setIsListening(false)
      setStatus((s) => (s === 'listening' ? 'online' : s))
      return
    }

    try {
      const recognition = new SpeechRecognitionCtor()
      recognition.continuous = false
      recognition.interimResults = true
      recognition.lang =
        typeof navigator !== 'undefined' ? navigator.language || 'en-US' : 'en-US'

      recognition.onresult = (event: SpeechRecognitionResultEvent) => {
        let interim = ''
        let finalText = ''
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const result = event.results[i]
          const transcript = result[0]?.transcript ?? ''
          if (result.isFinal) finalText += transcript
          else interim += transcript
        }
        if (finalText) {
          setInterimTranscript('')
          setInput((prev) => {
            const base = prev.trim()
            return base ? `${base} ${finalText.trim()}` : finalText.trim()
          })
        } else {
          setInterimTranscript(interim.trim())
        }
      }

      recognition.onerror = (event: { error: string }) => {
        const message = mapMicError(event.error)
        if (message) setVoiceError(message)
        recognitionRef.current = null
        setInterimTranscript('')
        setIsListening(false)
        setStatus((s) => (s === 'listening' ? 'online' : s))
      }

      recognition.onend = () => {
        recognitionRef.current = null
        setInterimTranscript('')
        setIsListening(false)
        setStatus((s) => (s === 'listening' ? 'online' : s))
      }

      recognitionRef.current = recognition
      setIsListening(true)
      setStatus('listening')
      recognition.start()
    } catch (err) {
      const name =
        err && typeof err === 'object' && 'name' in err
          ? String((err as { name: string }).name)
          : 'unsupported'
      setVoiceError(mapMicError(name === 'InvalidStateError' ? 'unsupported' : name))
      recognitionRef.current = null
      setIsListening(false)
      setStatus((s) => (s === 'listening' ? 'online' : s))
    }
  }, [
    settings.voiceInput,
    isListening,
    stopSpeaking,
    clearListenTimeout,
    ensureMicAccess,
    mapMicError,
  ])

  const toggleSpeak = useCallback(
    (text: string, messageId?: string) => {
      if (isSpeaking && (messageId == null || speakingMessageId === messageId)) {
        stopSpeaking()
        return
      }
      speakText(text, messageId)
    },
    [isSpeaking, speakingMessageId, stopSpeaking, speakText],
  )

  const dismissVoiceError = useCallback(() => setVoiceError(null), [])

  // Cleanup voice resources on unmount
  useEffect(() => {
    return () => {
      clearListenTimeout()
      clearSpeakTimeout()
      try {
        recognitionRef.current?.stop()
      } catch {
        /* ignore */
      }
      try {
        window.speechSynthesis?.cancel()
      } catch {
        /* ignore */
      }
    }
  }, [clearListenTimeout, clearSpeakTimeout])

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
    interimTranscript,
    voiceError,
    dismissVoiceError,
    isSpeaking,
    speakingMessageId,
    toggleSpeak,
    stopSpeaking,
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
