/**
 * runtime/chatbot/utils
 */

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
  languageLabel,
  speakUtterance,
  hasVoiceForLanguage,
} from './speech'

export { translateForSpeech } from './translate'

export {
  loadConversations,
  saveConversations,
  flushConversations,
  loadSettings,
  saveSettings,
  prefersReducedMotion,
} from './storage'

export { getUploadTargets, type UploadTarget } from './uploadTargets'

export {
  getSpeechRecognitionCtor,
  mapMicError,
  type SpeechRecognitionLike,
  type SpeechRecognitionResultEvent,
} from './mic'
