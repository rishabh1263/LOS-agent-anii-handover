/**
 * Browser speech-recognition helpers (Web Speech API).
 */

/** Minimal SpeechRecognition shape (incl. webkit prefix). */
export interface SpeechRecognitionLike {
  continuous: boolean
  interimResults: boolean
  lang: string
  onresult: ((event: SpeechRecognitionResultEvent) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
}

export interface SpeechRecognitionResultEvent {
  resultIndex: number
  results: ArrayLike<{ isFinal: boolean; 0?: { transcript: string } }>
}

export function getSpeechRecognitionCtor():
  | (new () => SpeechRecognitionLike)
  | undefined {
  if (typeof window === 'undefined') return undefined
  const w = window as unknown as Record<string, unknown>
  return (w.SpeechRecognition || w.webkitSpeechRecognition) as
    | (new () => SpeechRecognitionLike)
    | undefined
}

export function mapMicError(code: string): string {
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
