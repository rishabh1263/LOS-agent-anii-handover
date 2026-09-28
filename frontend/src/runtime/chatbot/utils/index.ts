export function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
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
  hasVoiceForLanguage,
  INDIAN_SPEECH_LOCALES,
  type VoicePickOptions,
} from './speech'

export { translateForSpeech, isEnglishLang, looksLikeLatin } from './translate'

export {
  loadConversations,
  saveConversations,
  flushConversations,
  loadSettings,
  saveSettings,
  prefersReducedMotion,
} from './storage'
