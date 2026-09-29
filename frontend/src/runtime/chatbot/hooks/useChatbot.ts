import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  queryChat,
  formatChatAnswer,
  ChatApiError,
  type ChatApiConfig,
} from '../api'

import type {
  AiStatus,
  ChatMessage,
  ChatPanelMode,
  ChatSettings,
  Conversation,
} from '../types'
import { DEFAULT_SETTINGS } from '../types'
import {
  uid,
  ensureVoicesLoaded,
  pickBestVoice,
  textForSpeech,
  naturalSpeechParams,
  speakUtterance,
  translateForSpeech,
  loadConversations,
  saveConversations,
  flushConversations,
  loadSettings,
  saveSettings,
  prefersReducedMotion,
  hasVoiceForLanguage,
} from '../utils'

/** Context + optional API config — only this needs changing in another project. */
export interface ChatbotContext {
  caseId?: string
  applicantId?: string
  partyId?: string
  stage?: string
  accessToken?: string
  /** Override default `/api/v1/copilot` */
  apiBaseUrl?: string
  /** Override default `/query` */
  apiQueryPath?: string
}

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

const QUICK_ACTIONS = [
  { id: 'help', label: 'How can you help?', icon: 'Search' },
  { id: 'status', label: 'Check application status', icon: 'ClipboardList' },
]

function createConversation(): Conversation {
  return {
    id: uid('conv'),
    title: 'New conversation',
    updatedAt: Date.now(),
    messages: [],
  }
}

function getSpeechRecognitionCtor(): (new () => SpeechRecognitionLike) | undefined {
  if (typeof window === 'undefined') return undefined
  const w = window as unknown as Record<string, unknown>
  return (w.SpeechRecognition || w.webkitSpeechRecognition) as
    | (new () => SpeechRecognitionLike)
    | undefined
}

function mapMicError(code: string): string {
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
    case 'insecure':
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
    default:
      return 'Voice input failed. Check microphone access and try again.'
  }
}

export function useChatbot(context: ChatbotContext = {}) {
  const [mode, setMode] = useState<ChatPanelMode>('closed')
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [status, setStatus] = useState<AiStatus>('online')
  const [settings, setSettings] = useState<ChatSettings>(() => loadSettings())
  const [input, setInput] = useState('')
  const [isListening, setIsListening] = useState(false)
  const [isSpeaking, setIsSpeaking] = useState(false)
  const [speakingMessageId, setSpeakingMessageId] = useState<string | null>(null)
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const [voiceWarning, setVoiceWarning] = useState<string | null>(null)
  const [interimTranscript, setInterimTranscript] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  const [confirmNew, setConfirmNew] = useState(false)
  const [hydrated, setHydrated] = useState(false)

  const stopRef = useRef(false)
  const speakGenRef = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null)
  const settingsRef = useRef(settings)
  settingsRef.current = settings
  const speakTextRef = useRef<(text: string, messageId?: string) => void>(() => { })
  const contextRef = useRef(context)
  contextRef.current = context
  const conversationApiIdRef = useRef<string | null>(null)

  // Restore history + settings once on mount (drop empty conversations)
  useEffect(() => {
    const { conversations: saved, activeId: savedActive } = loadConversations()
    if (saved.length) {
      const nonEmpty = saved.filter((c) => c.messages.length > 0)
      setConversations(nonEmpty)
      setActiveId(
        savedActive && nonEmpty.some((c) => c.id === savedActive)
          ? savedActive
          : nonEmpty[0]?.id ?? null,
      )
    }
    setSettings(loadSettings())
    setHydrated(true)
  }, [])

  // Persist conversations
  useEffect(() => {
    if (!hydrated) return
    saveConversations(conversations, activeId)
  }, [conversations, activeId, hydrated])

  // Persist settings
  useEffect(() => {
    if (!hydrated) return
    saveSettings(settings)
  }, [settings, hydrated])

  // Warn when selected language has no system voice
  useEffect(() => {
    let cancelled = false
    void ensureVoicesLoaded().then((voices) => {
      if (cancelled) return
      const lang = settings.speechLanguage || 'hi-IN'
      if (!hasVoiceForLanguage(lang, voices)) {
        setVoiceWarning(
          `No system voice found for ${lang}. Speech may fall back or stay silent. Install a voice for this language in OS / browser settings.`,
        )
      } else {
        setVoiceWarning(null)
      }
    })
    return () => {
      cancelled = true
    }
  }, [settings.speechLanguage])

  const activeConversation = useMemo(
    () => conversations.find((c) => c.id === activeId) ?? null,
    [conversations, activeId],
  )
  const messages = activeConversation?.messages ?? []

  const teardownVoice = useCallback(() => {
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
    setIsListening(false)
    setInterimTranscript('')
    setIsSpeaking(false)
    setSpeakingMessageId(null)
  }, [])

  const open = useCallback(() => {
    setMode((m) => (m === 'closed' ? 'panel' : m))
    if (!activeId) {
      const conv = createConversation()
      setConversations((prev) => [conv, ...prev.filter((c) => c.messages.length > 0)])
      setActiveId(conv.id)
    }
  }, [activeId])

  const close = useCallback(() => {
    setMode('closed')
    setShowSettings(false)
    teardownVoice()
    setStatus('online')
  }, [teardownVoice])

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
    // Already on an empty chat — nothing to do
    setConfirmNew(false)
    setShowSettings(false)
  }, [messages.length])

  const confirmNewConversation = useCallback(() => {
    const conv = createConversation()
    conversationApiIdRef.current = null
    // Keep only conversations that have messages, plus the new empty one
    setConversations((prev) => [conv, ...prev.filter((c) => c.messages.length > 0)])
    setActiveId(conv.id)
    setConfirmNew(false)
    setShowSettings(false)
  }, [])

  const deleteConversation = useCallback(
    (id: string) => {
      setConversations((prev) => prev.filter((c) => c.id !== id))
      if (activeId === id) setActiveId(null)
    },
    [activeId],
  )

  const sendMessage = useCallback(
    async (text?: string) => {
      const content = (text ?? input).trim()
      if (!content) return
      if (status === 'generating' || status === 'thinking') return

      let convId = activeId
      if (!convId) {
        const conv = createConversation()
        setConversations((prev) => [conv, ...prev.filter((c) => c.messages.length > 0)])
        setActiveId(conv.id)
        convId = conv.id
      }

      const userMsg: ChatMessage = {
        id: uid('msg'),
        role: 'user',
        content,
        timestamp: Date.now(),
      }

      setConversations((prev) =>
        prev.map((c) =>
          c.id === convId
            ? {
              ...c,
              title:
                c.messages.length === 0
                  ? content.slice(0, 40) || 'New conversation'
                  : c.title,
              updatedAt: Date.now(),
              messages: [...c.messages, userMsg],
            }
            : c,
        ),
      )
      setInput('')
      setStatus('thinking')
      stopRef.current = false
      abortRef.current?.abort()
      const ac = new AbortController()
      abortRef.current = ac

      const assistantId = uid('msg')
      setConversations((prev) =>
        prev.map((c) =>
          c.id === convId
            ? {
              ...c,
              updatedAt: Date.now(),
              messages: [
                ...c.messages,
                {
                  id: assistantId,
                  role: 'assistant',
                  content: '',
                  timestamp: Date.now(),
                  isStreaming: true,
                },
              ],
            }
            : c,
        ),
      )
      setStatus('generating')

      try {
        const ctx = contextRef.current
        const apiConfig: ChatApiConfig = {
          baseUrl: ctx.apiBaseUrl,
          queryPath: ctx.apiQueryPath,
        }
        const res = await queryChat(
          {
            message: content,
            case_id: ctx.caseId,
            applicant_id: ctx.applicantId,
            party_id: ctx.partyId,
            stage: ctx.stage,
            conversation_id: conversationApiIdRef.current || convId,
          },
          ctx.accessToken,
          apiConfig,
          ac.signal,
        )

        if (stopRef.current || ac.signal.aborted) {
          setStatus('online')
          return
        }

        if (res.conversation_id) {
          conversationApiIdRef.current = String(res.conversation_id)
        }

        const formatted = formatChatAnswer(res)
        const answer = formatted.text
        const meta = {
          suggestedQuestions: settingsRef.current.showSuggestedQuestions
            ? formatted.suggestedQuestions
            : undefined,
          routeTo: formatted.routed,
          grounded: formatted.grounded,
        }

        // Typing animation (skipped when user prefers reduced motion)
        const total = answer.length
        const reduceMotion = prefersReducedMotion()
        if (total === 0 || reduceMotion) {
          setConversations((prev) =>
            prev.map((c) =>
              c.id === convId
                ? {
                  ...c,
                  updatedAt: Date.now(),
                  messages: c.messages.map((m) =>
                    m.id === assistantId
                      ? { ...m, content: answer, isStreaming: false, ...meta }
                      : m,
                  ),
                }
                : c,
            ),
          )
          setStatus('online')
        } else {
          const step = total > 400 ? 4 : total > 180 ? 3 : 2
          const delay = total > 400 ? 12 : 16
          let i = 0
          await new Promise<void>((resolve) => {
            const tick = () => {
              if (stopRef.current) {
                setConversations((prev) =>
                  prev.map((c) =>
                    c.id === convId
                      ? {
                        ...c,
                        messages: c.messages.map((m) =>
                          m.id === assistantId
                            ? { ...m, content: answer, isStreaming: false, ...meta }
                            : m,
                        ),
                      }
                      : c,
                  ),
                )
                resolve()
                return
              }
              i = Math.min(total, i + step)
              const slice = answer.slice(0, i)
              const done = i >= total
              setConversations((prev) =>
                prev.map((c) =>
                  c.id === convId
                    ? {
                      ...c,
                      updatedAt: Date.now(),
                      messages: c.messages.map((m) =>
                        m.id === assistantId
                          ? {
                            ...m,
                            content: slice,
                            isStreaming: !done,
                            ...(done ? meta : {}),
                          }
                          : m,
                      ),
                    }
                    : c,
                ),
              )
              if (done) {
                resolve()
                return
              }
              window.setTimeout(tick, delay)
            }
            tick()
          })
          setStatus('online')
        }

        // Speak only after typing finishes (sync)
        if (settingsRef.current.autoReadResponses && answer && !stopRef.current) {
          setTimeout(() => speakTextRef.current(answer, assistantId), 120)
        }
      } catch (err) {
        if (stopRef.current || (err instanceof DOMException && err.name === 'AbortError')) {
          setStatus('online')
          return
        }
        let message = 'Failed to get a response from the AI assistant.'
        if (err instanceof ChatApiError) {
          message =
            err.body.message || err.body.detail || err.body.error || `Error ${err.status}`
        } else if (err instanceof Error) {
          message = err.message
        }
        setConversations((prev) =>
          prev.map((c) =>
            c.id === convId
              ? {
                ...c,
                messages: c.messages.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: '', isStreaming: false, error: message }
                    : m,
                ),
              }
              : c,
          ),
        )
        setStatus('error')
        setTimeout(() => setStatus('online'), 1500)
      }
    },
    [input, activeId, status],
  )

  const stopGenerating = useCallback(() => {
    stopRef.current = true
    try {
      abortRef.current?.abort()
    } catch {
      /* ignore */
    }
    abortRef.current = null
    setStatus('online')
  }, [])

  const regenerate = useCallback(
    (messageId: string) => {
      if (!activeId) return
      const conv = conversations.find((c) => c.id === activeId)
      if (!conv) return
      const idx = conv.messages.findIndex((m) => m.id === messageId)
      if (idx < 0) return

      let userContent = ''
      for (let i = idx - 1; i >= 0; i--) {
        if (conv.messages[i].role === 'user') {
          userContent = conv.messages[i].content
          break
        }
      }

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

  /**
   * Edit a user message like ChatGPT: truncate the conversation from that
   * message onward, then re-send the new text so a fresh assistant reply is generated.
   */
  const editMessage = useCallback(
    (messageId: string, newContent: string) => {
      const content = newContent.trim()
      if (!content || !activeId) return
      if (status === 'generating' || status === 'thinking') return

      const conv = conversations.find((c) => c.id === activeId)
      if (!conv) return
      const idx = conv.messages.findIndex((m) => m.id === messageId)
      if (idx < 0) return
      const target = conv.messages[idx]
      if (target.role !== 'user') return

      // Keep messages before the edited one; drop the edited message and everything after
      setConversations((prev) =>
        prev.map((c) =>
          c.id === activeId
            ? {
                ...c,
                updatedAt: Date.now(),
                messages: c.messages.slice(0, idx),
                title:
                  idx === 0
                    ? content.slice(0, 40) || 'New conversation'
                    : c.title,
              }
            : c,
        ),
      )

      // Reset API conversation continuity so the backend sees a clean branch
      conversationApiIdRef.current = null

      void sendMessage(content)
    },
    [activeId, conversations, sendMessage, status],
  )

  const stopSpeaking = useCallback(() => {
    speakGenRef.current += 1
    try {
      window.speechSynthesis?.cancel()
    } catch {
      /* ignore */
    }
    setIsSpeaking(false)
    setSpeakingMessageId(null)
    setStatus((s) => (s === 'speaking' ? 'online' : s))
  }, [])

  const speakText = useCallback((text: string, messageId?: string) => {
    const cleaned = textForSpeech(text)
    if (!cleaned) return
    if (typeof window === 'undefined' || !window.speechSynthesis) return

    const gen = ++speakGenRef.current

    try {
      window.speechSynthesis.cancel()
    } catch {
      /* ignore */
    }

    // Mark active immediately so UI animates while translating
    setIsSpeaking(true)
    setSpeakingMessageId(messageId ?? null)
    setStatus('speaking')

    const finish = () => {
      if (speakGenRef.current !== gen) return
      setIsSpeaking(false)
      setSpeakingMessageId(null)
      setStatus((st) => (st === 'speaking' ? 'online' : st))
    }

    void (async () => {
      const s = settingsRef.current
      const lang = s.speechLanguage || 'hi-IN'
      const gender = s.speechGender || 'any'

      let speakBody = cleaned
      try {
        speakBody = await translateForSpeech(
          cleaned,
          lang,
          contextRef.current.accessToken,
        )
      } catch {
        speakBody = cleaned
      }

      if (speakGenRef.current !== gen) return

      const voices = await ensureVoicesLoaded()
      if (speakGenRef.current !== gen) return

      const utterance = new SpeechSynthesisUtterance(speakBody)
      utterance.lang = lang

      const voice = pickBestVoice(voices, { lang, gender })
      if (voice) {
        utterance.voice = voice
        if (voice.lang) utterance.lang = voice.lang
      }

      const params = naturalSpeechParams(s.speechSpeed || 1, gender)
      utterance.rate = params.rate
      utterance.pitch = params.pitch
      utterance.volume = params.volume
      utterance.onend = finish
      utterance.onerror = finish

      speakUtterance(utterance)
    })()
  }, [])
  speakTextRef.current = speakText

  // Preload voices so the first speak is not delayed / robotic default
  useEffect(() => {
    void ensureVoicesLoaded()
  }, [])

  const stopListening = useCallback(() => {
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
  }, [])

  const ensureMicAccess = useCallback(async (): Promise<string | null> => {
    if (typeof window === 'undefined') return mapMicError('unsupported')
    if (!window.isSecureContext) return mapMicError('insecure')

    try {
      if (navigator.permissions?.query) {
        const status = await navigator.permissions.query({
          name: 'microphone' as PermissionName,
        })
        if (status.state === 'denied') return mapMicError('not-allowed')
      }
    } catch {
      /* some browsers reject microphone permission queries */
    }

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

    return null
  }, [])

  const startListening = useCallback(async () => {
    if (!settings.voiceInput || isListening) return
    setVoiceError(null)
    setInterimTranscript('')
    stopSpeaking()

    const SpeechRecognitionCtor = getSpeechRecognitionCtor()
    if (!SpeechRecognitionCtor) {
      setVoiceError(mapMicError('unsupported'))
      return
    }

    const accessError = await ensureMicAccess()
    if (accessError) {
      setVoiceError(accessError)
      return
    }

    try {
      const recognition = new SpeechRecognitionCtor()
      recognition.continuous = false
      recognition.interimResults = true
      recognition.lang =
        settingsRef.current.speechLanguage ||
        (typeof navigator !== 'undefined' ? navigator.language : undefined) ||
        'en-IN'

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
  }, [settings.voiceInput, isListening, stopSpeaking, ensureMicAccess])

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
  const dismissVoiceWarning = useCallback(() => setVoiceWarning(null), [])

  /** Settings “Test voice” — short sample in current language/gender. */
  const testVoice = useCallback(() => {
    const lang = settingsRef.current.speechLanguage || 'hi-IN'
    const primary = lang.split('-')[0].toLowerCase()
    const samples: Record<string, string> = {
      hi: 'नमस्ते, मैं आपका सहायक हूँ। आवाज़ सही से काम कर रही है।',
      mr: 'नमस्कार, मी तुमचा सहाय्यक आहे. आवाज व्यवस्थित काम करत आहे.',
      bn: 'নমস্কার, আমি আপনার সহায়ক। কণ্ঠস্বর ঠিকভাবে কাজ করছে।',
      ta: 'வணக்கம், நான் உங்கள் உதவியாளர். குரல் சரியாக வேலை செய்கிறது.',
      te: 'నమస్కారం, నేను మీ సహాయకుడిని. వాయిస్ సరిగ్గా పని చేస్తోంది.',
      gu: 'નમસ્તે, હું તમારો સહાયક છું. અવાજ સારી રીતે કામ કરે છે.',
      kn: 'ನಮಸ್ಕಾರ, ನಾನು ನಿಮ್ಮ ಸಹಾಯಕ. ಧ್ವನಿ ಸರಿಯಾಗಿ ಕೆಲಸ ಮಾಡುತ್ತಿದೆ.',
      ml: 'നമസ്കാരം, ഞാൻ നിങ്ങളുടെ സഹായി. ശബ്ദം ശരിയായി പ്രവർത്തിക്കുന്നു.',
      pa: 'ਸਤ ਸ੍ਰੀ ਅਕਾਲ, ਮੈਂ ਤੁਹਾਡਾ ਸਹਾਇਕ ਹਾਂ। ਆਵਾਜ਼ ਠੀਕ ਕੰਮ ਕਰ ਰਹੀ ਹੈ।',
      en: 'Hello, I am your assistant. The voice is working correctly.',
    }
    const sample = samples[primary] || samples.en
    speakText(sample)
  }, [speakText])

  useEffect(() => {
    return () => {
      flushConversations()
      teardownVoice()
    }
  }, [teardownVoice])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && mode !== 'closed') {
        if (showSettings) setShowSettings(false)
        else close()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mode, showSettings, close])

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
    sendMessage,
    stopGenerating,
    regenerate,
    editMessage,
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
    dismissVoiceWarning,
    testVoice,
    isSpeaking,
    speakingMessageId,
    toggleSpeak,
    stopSpeaking,
    showSettings,
    setShowSettings,
    quickActions: QUICK_ACTIONS,
  }
}

export type ChatbotApi = ReturnType<typeof useChatbot>
