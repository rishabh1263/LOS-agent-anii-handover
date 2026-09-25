export function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function groupConversationsByDate(conversations: { id: string; title: string; updatedAt: number; pinned?: boolean }[]) {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const yesterday = today - 86400000

  const groups: { label: string; items: typeof conversations }[] = [
    { label: 'Pinned', items: [] },
    { label: 'Today', items: [] },
    { label: 'Yesterday', items: [] },
    { label: 'Earlier', items: [] },
  ]

  for (const c of conversations) {
    if (c.pinned) {
      groups[0].items.push(c)
      continue
    }
    if (c.updatedAt >= today) groups[1].items.push(c)
    else if (c.updatedAt >= yesterday) groups[2].items.push(c)
    else groups[3].items.push(c)
  }

  return groups.filter((g) => g.items.length > 0)
}

export function uid(prefix = 'id'): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`
}

export {
  ensureVoicesLoaded,
  pickBestVoice,
  textForSpeech,
  naturalSpeechParams,
  listAvailableLanguages,
  listIndianLanguages,
  languageLabel,
  speakUtterance,
  INDIAN_SPEECH_LOCALES,
  type SpeechGender,
  type VoicePickOptions,
} from './speech'

export { translateForSpeech, isEnglishLang, looksLikeLatin } from './translate'
