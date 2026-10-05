export type ChatRole = 'user' | 'assistant' | 'system'

export type AiStatus =
  | 'online'
  | 'thinking'
  | 'generating'
  | 'listening'
  | 'speaking'
  | 'offline'
  | 'error'

export type SpeechGender = 'female' | 'male' | 'any'

/** Chatbot panel only — does not change the host app theme */
export type ChatThemeId = 'light' | 'dark' | 'orange'

/** Upload targets attached from /fos/copilot structured fields only */
export interface ChatUploadTarget {
  slot: string
  acceptedTypes: string[]
  reason: string
  label?: string
  status?: string
}

/** Persisted per-slot upload outcome (no File — survives page refresh) */
export interface ChatUploadResult {
  slot: string
  status: 'pass' | 'review' | 'fail' | 'validation'
  detail?: string
  detectedType?: string
  fileName?: string
  fileSize?: number
}

export interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  timestamp: number
  isStreaming?: boolean
  error?: string
  suggestedQuestions?: string[]
  routeTo?: string | null
  grounded?: boolean
  /** From getUploadTargets() — never derived from answer text */
  uploadTargets?: ChatUploadTarget[]
  /** After submit: keeps verified / review / fail state across refresh */
  uploadResults?: ChatUploadResult[]
}

export interface Conversation {
  id: string
  title: string
  updatedAt: number
  messages: ChatMessage[]
}

export interface ChatSettings {
  voiceInput: boolean
  autoReadResponses: boolean
  /**
   * When true, finishing voice input (speech ends or user stops the mic)
   * automatically sends the transcribed message — no need to press Enter/Send.
   */
  autoSendOnVoice: boolean
  speechSpeed: number
  /** BCP-47 tag from system voices, e.g. en-IN, hi-IN */
  speechLanguage: string
  speechGender: SpeechGender
  /** Chatbot shell only: light | dark | orange */
  chatTheme: ChatThemeId
  sendWithEnter: boolean
  showTimestamps: boolean
  showSuggestedQuestions: boolean
}

export const DEFAULT_SETTINGS: ChatSettings = {
  voiceInput: true,
  autoReadResponses: false,
  autoSendOnVoice: false,
  speechSpeed: 1,
  speechLanguage: 'en-IN',
  speechGender: 'female',
  chatTheme: 'light',
  sendWithEnter: true,
  showTimestamps: true,
  showSuggestedQuestions: true,
}

export type ChatPanelMode = 'closed' | 'panel' | 'expanded' | 'mobile'
