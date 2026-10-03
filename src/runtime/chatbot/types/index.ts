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
  speechSpeed: number
  /** BCP-47 tag from system voices, e.g. en-IN, hi-IN */
  speechLanguage: string
  speechGender: SpeechGender
  /** Chatbot shell only: light | dark | orange */
  chatTheme: ChatThemeId
  sendWithEnter: boolean
  showTimestamps: boolean
  compactMode: boolean
  showSuggestedQuestions: boolean
}

export const DEFAULT_SETTINGS: ChatSettings = {
  voiceInput: true,
  autoReadResponses: false,
  speechSpeed: 1,
  speechLanguage: 'hi-IN',
  speechGender: 'female',
  chatTheme: 'light',
  sendWithEnter: true,
  showTimestamps: true,
  compactMode: false,
  showSuggestedQuestions: true,
}

export type ChatPanelMode = 'closed' | 'panel' | 'expanded' | 'mobile'
