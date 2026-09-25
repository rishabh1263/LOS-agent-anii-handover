export type ChatRole = 'user' | 'assistant' | 'system'

export type AiStatus =
  | 'online'
  | 'thinking'
  | 'generating'
  | 'listening'
  | 'speaking'
  | 'offline'
  | 'error'

export type ThemeMode = 'system' | 'light' | 'dark'

export interface ChatAttachment {
  id: string
  name: string
  size: number
  type: string
  url?: string
  progress?: number
  error?: string
}

export interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  timestamp: number
  attachments?: ChatAttachment[]
  isStreaming?: boolean
  error?: string
  suggestedQuestions?: string[]
}

export interface Conversation {
  id: string
  title: string
  updatedAt: number
  pinned?: boolean
  messages: ChatMessage[]
}

export interface ChatSettings {
  theme: ThemeMode
  voiceInput: boolean
  autoReadResponses: boolean
  speechSpeed: number
  sendWithEnter: boolean
  showTimestamps: boolean
  compactMode: boolean
  showSuggestedQuestions: boolean
}

export const DEFAULT_SETTINGS: ChatSettings = {
  theme: 'system',
  voiceInput: true,
  autoReadResponses: false,
  speechSpeed: 1,
  sendWithEnter: true,
  showTimestamps: true,
  compactMode: false,
  showSuggestedQuestions: true,
}

export type ChatPanelMode = 'closed' | 'panel' | 'expanded' | 'mobile'
