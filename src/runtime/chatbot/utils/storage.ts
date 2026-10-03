/**
 * Persist chatbot conversations + settings (localStorage).
 * Debounced writes; safe for SSR / private mode.
 */

import type { ChatSettings, Conversation } from '../types'
import { DEFAULT_SETTINGS } from '../types'

const KEY_CONVERSATIONS = 'chatbot.conversations.v1'
const KEY_ACTIVE = 'chatbot.activeId.v1'
const KEY_SETTINGS = 'chatbot.settings.v1'
const MAX_CONVERSATIONS = 40
const MAX_MESSAGES_PER = 80
const SAVE_DEBOUNCE_MS = 400

function canUseStorage(): boolean {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return false
    const k = '__cb_test__'
    window.localStorage.setItem(k, '1')
    window.localStorage.removeItem(k)
    return true
  } catch {
    return false
  }
}

export function loadConversations(): { conversations: Conversation[]; activeId: string | null } {
  if (!canUseStorage()) return { conversations: [], activeId: null }
  try {
    const raw = window.localStorage.getItem(KEY_CONVERSATIONS)
    const activeId = window.localStorage.getItem(KEY_ACTIVE)
    if (!raw) return { conversations: [], activeId }
    const parsed = JSON.parse(raw) as Conversation[]
    if (!Array.isArray(parsed)) return { conversations: [], activeId }
    const conversations = parsed
      .filter((c) => c && typeof c.id === 'string' && Array.isArray(c.messages))
      .map((c) => ({
        ...c,
        messages: c.messages.map((m) => ({ ...m, isStreaming: false })),
      }))
    return { conversations, activeId }
  } catch {
    return { conversations: [], activeId: null }
  }
}

function writeConversations(conversations: Conversation[], activeId: string | null): void {
  if (!canUseStorage()) return
  try {
    const trimmed = conversations.slice(0, MAX_CONVERSATIONS).map((c) => ({
      ...c,
      messages: c.messages.slice(-MAX_MESSAGES_PER).map((m) => ({
        ...m,
        isStreaming: false,
      })),
    }))
    window.localStorage.setItem(KEY_CONVERSATIONS, JSON.stringify(trimmed))
    if (activeId) window.localStorage.setItem(KEY_ACTIVE, activeId)
    else window.localStorage.removeItem(KEY_ACTIVE)
  } catch {
    /* quota / private mode */
  }
}

let saveTimer: ReturnType<typeof setTimeout> | null = null
let pending: { conversations: Conversation[]; activeId: string | null } | null = null

export function saveConversations(conversations: Conversation[], activeId: string | null): void {
  pending = { conversations, activeId }
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    saveTimer = null
    if (pending) {
      writeConversations(pending.conversations, pending.activeId)
      pending = null
    }
  }, SAVE_DEBOUNCE_MS)
}

export function flushConversations(): void {
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
  if (pending) {
    writeConversations(pending.conversations, pending.activeId)
    pending = null
  }
}

export function loadSettings(): ChatSettings {
  if (!canUseStorage()) return { ...DEFAULT_SETTINGS }
  try {
    const raw = window.localStorage.getItem(KEY_SETTINGS)
    if (!raw) return { ...DEFAULT_SETTINGS }
    const parsed = JSON.parse(raw) as Partial<ChatSettings>
    return { ...DEFAULT_SETTINGS, ...parsed }
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

export function saveSettings(settings: ChatSettings): void {
  if (!canUseStorage()) return
  try {
    window.localStorage.setItem(KEY_SETTINGS, JSON.stringify(settings))
  } catch {
    /* ignore */
  }
}

export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}
