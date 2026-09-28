/**
 * Speech helpers — Indian-language focused voice pick + reliable speak.
 */

import type { SpeechGender } from '../types'

export interface VoicePickOptions {
  lang?: string
  gender?: SpeechGender
}

/** Indian locales only (BCP-47). Shown in settings; not foreign languages. */
export const INDIAN_SPEECH_LOCALES = [
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
  if (a === b) return true
  return langPrimary(a) === langPrimary(b)
}

function qualityScore(v: SpeechSynthesisVoice): number {
  let s = 0
  const n = v.name.toLowerCase()
  if (/neural|natural|premium|enhanced|wavenet|online/.test(n)) s += 20
  if (v.localService) s += 8
  if (/google|microsoft|apple/.test(n)) s += 6
  return s
}

let voicesReady = false

export function ensureVoicesLoaded(): Promise<SpeechSynthesisVoice[]> {
  if (typeof window === 'undefined' || !window.speechSynthesis) {
    return Promise.resolve([])
  }
  // Chrome may keep synthesis paused after cancel
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
    setTimeout(() => {
      window.speechSynthesis.removeEventListener('voiceschanged', done)
      voicesReady = true
      resolve(window.speechSynthesis.getVoices())
    }, 800)
  })
}

/**
 * Indian languages for the settings dropdown.
 * Always lists Indian locales; marks which ones have a system voice.
 */
export function listIndianLanguages(voices?: SpeechSynthesisVoice[]): {
  tag: string
  hasVoice: boolean
}[] {
  const list =
    voices ??
    (typeof window !== 'undefined' ? window.speechSynthesis?.getVoices() : []) ??
    []
  const installed = new Set(list.map((v) => normalizeLang(v.lang)))

  return INDIAN_SPEECH_LOCALES.map((tag) => {
    const n = normalizeLang(tag)
    const primary = langPrimary(tag)
    const hasVoice = [...installed].some(
      (v) => v === n || langPrimary(v) === primary,
    )
    return { tag, hasVoice }
  })
}

/** @deprecated use listIndianLanguages */
export function listAvailableLanguages(voices?: SpeechSynthesisVoice[]): string[] {
  return listIndianLanguages(voices).map((x) => x.tag)
}

export function languageLabel(tag: string, uiLocale?: string): string {
  try {
    const dn = new Intl.DisplayNames([uiLocale || 'en-IN', 'en'], {
      type: 'language',
    })
    const name = dn.of(tag) || dn.of(tag.split('-')[0]) || tag
    return name
  } catch {
    return tag
  }
}

/**
 * Pick voice: language first, then gender within that language.
 * Never prefers a foreign-language voice over a matching Indian language.
 */
export function pickBestVoice(
  voices?: SpeechSynthesisVoice[],
  options: VoicePickOptions = {},
): SpeechSynthesisVoice | null {
  const list =
    voices ??
    (typeof window !== 'undefined' ? window.speechSynthesis?.getVoices() : []) ??
    []
  if (!list.length) return null

  const lang = (options.lang || 'hi-IN').replace('_', '-')
  const gender = options.gender || 'any'

  const sameLang = list.filter((v) => langMatches(v.lang, lang))
  const pool = sameLang.length > 0 ? sameLang : []

  if (!pool.length) {
    // No voice for this language — return null; caller still sets utterance.lang
    return null
  }

  const scored = pool.map((v) => {
    let score = qualityScore(v)
    const exact = normalizeLang(v.lang) === normalizeLang(lang)
    if (exact) score += 30
    const g = genderOf(v)
    if (gender !== 'any') {
      if (g === gender) score += 40
      else if (g === 'unknown') score += 8
      // wrong gender still kept — better Hindi female than silence for male request
    }
    return { v, score }
  })

  scored.sort((a, b) => b.score - a.score)
  return scored[0]?.v ?? null
}

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
  const base = 0.95
  const rate = Math.min(1.5, Math.max(0.7, base * (userRate || 1)))
  let pitch = 1
  if (gender === 'female') pitch = 1.08
  else if (gender === 'male') pitch = 0.88
  return { rate, pitch, volume: 1 }
}

/** Speak with Chrome-safe resume/cancel handling. */
export function speakUtterance(utterance: SpeechSynthesisUtterance): void {
  if (typeof window === 'undefined' || !window.speechSynthesis) return
  const synth = window.speechSynthesis
  try {
    synth.cancel()
  } catch {
    /* ignore */
  }
  // Chrome bug: queue stays paused after cancel
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

export function isVoicesReady(): boolean {
  return voicesReady
}

/** Whether the device has any voice matching this language family. */
export function hasVoiceForLanguage(
  lang: string,
  voices?: SpeechSynthesisVoice[],
): boolean {
  const list =
    voices ??
    (typeof window !== 'undefined' ? window.speechSynthesis?.getVoices() : []) ??
    []
  return list.some((v) => langMatches(v.lang, lang))
}
