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
  listIndianLanguages,
  listAvailableLanguages,
  languageLabel,
  speakUtterance,
  hasVoiceForLanguage,
  isVoicesReady,
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

export {
  getUploadTargets,
  getStatusChip,
  type UploadTarget,
  type UploadDecision,
  type FosUploadResponse,
} from './uploadTargets'
