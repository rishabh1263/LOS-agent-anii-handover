/**
 * Translate text for TTS.
 *
 * Production default: NO third-party calls (case/PII safety).
 * Optional paths:
 *   1) VITE_CHAT_TRANSLATE_PATH — your backend (preferred)
 *   2) VITE_ENABLE_CLIENT_TRANSLATE=true — MyMemory (demo only)
 */

import { ENABLE_CLIENT_TRANSLATE, apiUrl, PATHS } from '../../config'

const cache = new Map<string, string>()

function primaryLang(tag: string): string {
  return tag.replace('_', '-').split('-')[0].toLowerCase()
}

export function isEnglishLang(tag: string): boolean {
  return primaryLang(tag) === 'en'
}

export function looksLikeLatin(text: string): boolean {
  const letters = text.replace(
    /[^A-Za-z\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF\u0B00-\u0B7F\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F]/g,
    '',
  )
  if (!letters.length) return true
  const latin = (letters.match(/[A-Za-z]/g) || []).length
  return latin / letters.length > 0.55
}

/**
 * Prepare text for speech in `targetLang`.
 * Returns original text when translation is disabled or fails.
 */
export async function translateForSpeech(
  text: string,
  targetLang: string,
  token?: string,
): Promise<string> {
  const target = primaryLang(targetLang)
  if (!text.trim()) return text
  if (isEnglishLang(targetLang) || target === 'en') return text
  if (!looksLikeLatin(text)) return text

  const cacheKey = `${target}::${text}`
  const hit = cache.get(cacheKey)
  if (hit) return hit

  try {
    const server = await translateViaBackend(text, target, token)
    if (server) {
      remember(cacheKey, server)
      return server
    }
  } catch {
    /* fall through */
  }

  if (ENABLE_CLIENT_TRANSLATE) {
    try {
      const chunks = splitChunks(text, 400)
      const out: string[] = []
      for (const chunk of chunks) {
        try {
          out.push(await translateChunkMyMemory(chunk, target))
        } catch {
          out.push(chunk)
        }
      }
      const result = out.join(' ').replace(/\s+/g, ' ').trim() || text
      remember(cacheKey, result)
      return result
    } catch {
      return text
    }
  }

  return text
}

function remember(key: string, value: string) {
  if (cache.size > 80) cache.clear()
  cache.set(key, value)
}

async function translateViaBackend(
  text: string,
  target: string,
  token?: string,
): Promise<string | null> {
  const url = apiUrl(PATHS.chatTranslate)
  const headers: HeadersInit = { 'Content-Type': 'application/json' }
  if (token?.trim()) headers.Authorization = `Bearer ${token.trim()}`

  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({ text, target_lang: target }),
  })
  if (res.status === 404) return null
  if (!res.ok) return null
  const data = (await res.json()) as { translated?: string; text?: string }
  const t = (data.translated || data.text || '').trim()
  return t || null
}

function splitChunks(text: string, max: number): string[] {
  if (text.length <= max) return [text]
  const parts: string[] = []
  let rest = text
  while (rest.length > max) {
    let cut = rest.lastIndexOf('. ', max)
    if (cut < max * 0.4) cut = rest.lastIndexOf(' ', max)
    if (cut < max * 0.4) cut = max
    parts.push(rest.slice(0, cut + 1).trim())
    rest = rest.slice(cut + 1).trim()
  }
  if (rest) parts.push(rest)
  return parts
}

async function translateChunkMyMemory(text: string, target: string): Promise<string> {
  const url =
    `https://api.mymemory.translated.net/get?q=${encodeURIComponent(text)}` +
    `&langpair=en|${encodeURIComponent(target)}`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`translate ${res.status}`)
  const data = (await res.json()) as {
    responseData?: { translatedText?: string }
    responseStatus?: number
  }
  const t = data.responseData?.translatedText?.trim()
  if (!t || data.responseStatus === 403) throw new Error('translate empty')
  if (/QUERY LENGTH|MYMEMORY WARNING/i.test(t)) throw new Error('limit')
  return t
}
