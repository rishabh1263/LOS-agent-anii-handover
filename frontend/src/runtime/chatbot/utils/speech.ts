/**
 * Speech helpers — Indian-language voice pick + reliable speak.
 */

import type { SpeechGender } from '../types'

interface VoicePickOptions {
  lang?: string
  gender?: SpeechGender
}

/** Candidate Indian locales (BCP-47). Dropdown only shows ones the browser has. */
const INDIAN_SPEECH_LOCALES = [
  'en-IN',
  'hi-IN',
  'mr-IN',
  'bn-IN',
  'ta-IN',
  'te-IN',
  'gu-IN',
  'kn-IN',
  'ml-IN',
  'pa-IN',
  'ur-IN',
  'or-IN',
  'as-IN',
] as const

const MALE_HINT =
  /male|\bman\b|david|daniel|mark|george|james|thomas|ravi|rishi|prabhat|hemant|guy|eric|ryan|alex|fred|ralph|kumar/i
const FEMALE_HINT =
  /female|woman|zira|hazel|susan|samantha|karen|moira|veena|lekha|kalpana|aria|jenny|sara|sonia|natasha|heera|swara|neerja/i

function genderOf(v: SpeechSynthesisVoice): 'female' | 'male' | 'unknown' {
  const n = v.name
  if (FEMALE_HINT.test(n)) return 'female'
  if (MALE_HINT.test(n)) return 'male'
  return 'unknown'
}

function normalizeLang(tag: string): string {
  return tag.replace('_', '-').toLowerCase()
}

function langPrimary(tag: string): string {
  return normalizeLang(tag).split('-')[0]
}

function langMatches(voiceLang: string, wanted: string): boolean {
  const a = normalizeLang(voiceLang)
  const b = normalizeLang(wanted)
  return a === b || langPrimary(a) === langPrimary(b)
}

function qualityScore(v: SpeechSynthesisVoice): number {
  let s = 0
  const n = v.name.toLowerCase()
  if (/neural|natural|premium|enhanced|wavenet|online/.test(n)) s += 20
  if (v.localService) s += 8
  if (/google|microsoft|apple/.test(n)) s += 6
  return s
}

function getVoices(voices?: SpeechSynthesisVoice[]): SpeechSynthesisVoice[] {
  if (voices) return voices
  if (typeof window === 'undefined') return []
  return window.speechSynthesis?.getVoices() ?? []
}

let voicesReady = false

/** Load system voices (Chrome may fire voiceschanged asynchronously). */
export function ensureVoicesLoaded(): Promise<SpeechSynthesisVoice[]> {
  if (typeof window === 'undefined' || !window.speechSynthesis) {
    return Promise.resolve([])
  }
  try {
    window.speechSynthesis.resume()
  } catch {
    /* ignore */
  }
  const existing = window.speechSynthesis.getVoices()
  if (existing.length > 0) {
    voicesReady = true
    return Promise.resolve(existing)
  }
  return new Promise((resolve) => {
    const done = () => {
      voicesReady = true
      window.speechSynthesis.removeEventListener('voiceschanged', done)
      resolve(window.speechSynthesis.getVoices())
    }
    window.speechSynthesis.addEventListener('voiceschanged', done)
    window.setTimeout(() => {
      window.speechSynthesis.removeEventListener('voiceschanged', done)
      voicesReady = true
      resolve(window.speechSynthesis.getVoices())
    }, 800)
  })
}

/**
 * Indian language tags this browser actually supports (has a matching voice).
 * Used for the Settings language dropdown.
 */
export function listIndianLanguages(voices?: SpeechSynthesisVoice[]): string[] {
  const list = getVoices(voices)
  if (!list.length) return []

  return INDIAN_SPEECH_LOCALES.filter((tag) =>
    list.some((v) => langMatches(v.lang, tag)),
  ) as unknown as string[]
}

export function languageLabel(tag: string, uiLocale?: string): string {
  try {
    const dn = new Intl.DisplayNames([uiLocale || 'en-IN', 'en'], { type: 'language' })
    return dn.of(tag) || dn.of(tag.split('-')[0]) || tag
  } catch {
    return tag
  }
}

/**
 * Pick the best installed voice for language + preferred gender.
 * Returns null if no voice matches the language family.
 */
export function pickBestVoice(
  voices?: SpeechSynthesisVoice[],
  options: VoicePickOptions = {},
): SpeechSynthesisVoice | null {
  const list = getVoices(voices)
  if (!list.length) return null

  const lang = (options.lang || 'en-IN').replace('_', '-')
  const gender = options.gender || 'any'
  const pool = list.filter((v) => langMatches(v.lang, lang))
  if (!pool.length) return null

  const scored = pool.map((v) => {
    let score = qualityScore(v)
    if (normalizeLang(v.lang) === normalizeLang(lang)) score += 30
    if (gender !== 'any') {
      const g = genderOf(v)
      if (g === gender) score += 40
      else if (g === 'unknown') score += 8
    }
    return { v, score }
  })

  scored.sort((a, b) => b.score - a.score)
  return scored[0]?.v ?? null
}

/** Strip markdown / noise so TTS reads cleanly. */
export function textForSpeech(raw: string): string {
  return raw
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`[^`]+`/g, (m) => m.slice(1, -1))
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/_([^_]+)_/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*•]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/_Note:.*?_/gi, '')
    .replace(/\s+/g, ' ')
    .trim()
}

export function naturalSpeechParams(
  userRate = 1,
  gender: SpeechGender = 'any',
): { rate: number; pitch: number; volume: number } {
  const rate = Math.min(1.5, Math.max(0.7, 0.95 * (userRate || 1)))
  let pitch = 1
  if (gender === 'female') pitch = 1.08
  else if (gender === 'male') pitch = 0.88
  return { rate, pitch, volume: 1 }
}

/** Speak with Chrome-safe cancel / resume handling. */
export function speakUtterance(utterance: SpeechSynthesisUtterance): void {
  if (typeof window === 'undefined' || !window.speechSynthesis) return
  const synth = window.speechSynthesis
  try {
    synth.cancel()
  } catch {
    /* ignore */
  }
  try {
    synth.resume()
  } catch {
    /* ignore */
  }
  window.setTimeout(() => {
    try {
      synth.resume()
      synth.speak(utterance)
    } catch {
      /* ignore */
    }
  }, 60)
}

/** True if any installed voice matches this language family. */
export function hasVoiceForLanguage(
  lang: string,
  voices?: SpeechSynthesisVoice[],
): boolean {
  return getVoices(voices).some((v) => langMatches(v.lang, lang))
}
