/**
 * Translate text to a target BCP-47 language for TTS.
 * Uses MyMemory free endpoint (no API key). Falls back to original on failure.
 */

const cache = new Map<string, string>()

function primaryLang(tag: string): string {
  return tag.replace('_', '-').split('-')[0].toLowerCase()
}

/** English (any region) — no translation needed for typical API answers. */
export function isEnglishLang(tag: string): boolean {
  return primaryLang(tag) === 'en'
}

/**
 * Detect if text is mostly Latin script (likely English from API).
 * Hindi/Indian scripts use other Unicode blocks.
 */
export function looksLikeLatin(text: string): boolean {
  const letters = text.replace(/[^A-Za-z\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF\u0B00-\u0B7F\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F]/g, '')
  if (!letters.length) return true
  const latin = (letters.match(/[A-Za-z]/g) || []).length
  return latin / letters.length > 0.55
}

/**
 * Translate `text` into `targetLang` (e.g. hi-IN → hi).
 * Chunks long text to stay under free API limits.
 */
export async function translateForSpeech(
  text: string,
  targetLang: string,
): Promise<string> {
  const target = primaryLang(targetLang)
  if (!text.trim()) return text
  if (isEnglishLang(targetLang) || target === 'en') return text
  // Already in non-Latin script for Indic targets — skip
  if (!looksLikeLatin(text) && target !== 'en') return text

  const cacheKey = `${target}::${text}`
  const hit = cache.get(cacheKey)
  if (hit) return hit

  const chunks = splitChunks(text, 400)
  const out: string[] = []

  for (const chunk of chunks) {
    try {
      const translated = await translateChunk(chunk, target)
      out.push(translated)
    } catch {
      out.push(chunk)
    }
  }

  const result = out.join(' ').replace(/\s+/g, ' ').trim() || text
  if (cache.size > 80) cache.clear()
  cache.set(cacheKey, result)
  return result
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

async function translateChunk(text: string, target: string): Promise<string> {
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
  // MyMemory sometimes echoes "QUERY LENGTH LIMIT..." 
  if (/QUERY LENGTH|MYMEMORY WARNING/i.test(t)) throw new Error('limit')
  return t
}
