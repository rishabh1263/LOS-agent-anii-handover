/**
 * Shared runtime config — override via Vite env in the host app.
 *
 *   VITE_API_BASE_URL=https://api.example.com
 *   VITE_ENABLE_CLIENT_TRANSLATE=true   # only if you accept third-party TTS translate
 *   VITE_CHAT_TRANSLATE_PATH=/api/v1/copilot/translate  # preferred server-side path
 */

function env(key: string): string | undefined {
  try {
    // Vite
    const meta = import.meta as unknown as { env?: Record<string, string | undefined> }
    const v = meta.env?.[key]
    if (typeof v === 'string' && v.trim()) return v.trim()
  } catch {
    /* not Vite or unavailable */
  }
  try {
    // Optional process.env for non-Vite hosts
    const g = globalThis as unknown as { process?: { env?: Record<string, string | undefined> } }
    const v = g.process?.env?.[key]
    if (typeof v === 'string' && v.trim()) return v.trim()
  } catch {
    /* ignore */
  }
  return undefined
}

/** Origin only, no trailing slash. Empty = same-origin relative paths. */
export const API_BASE_URL = (env('VITE_API_BASE_URL') || '').replace(/\/$/, '')

export const PATHS = {
  authLogin: '/api/v1/auth/login',
  authRefresh: '/api/v1/auth/refresh',
  authLogout: '/api/v1/auth/logout',
  fos: '/api/v1/fos',
  los: '/api/v1/los',
  copilot: '/api/v1/copilot',
  copilotQuery: '/query',
  /** Optional backend translate for TTS — preferred over client third-party */
  chatTranslate: env('VITE_CHAT_TRANSLATE_PATH') || '/api/v1/copilot/translate',
} as const

/** Join base + path safely. */
export function apiUrl(path: string): string {
  const p = path.startsWith('/') ? path : `/${path}`
  return API_BASE_URL ? `${API_BASE_URL}${p}` : p
}

/**
 * Client-side third-party translate is OFF by default (PII / case data risk).
 * Enable only with VITE_ENABLE_CLIENT_TRANSLATE=true for local demos.
 */
export const ENABLE_CLIENT_TRANSLATE =
  env('VITE_ENABLE_CLIENT_TRANSLATE') === 'true' ||
  env('VITE_ENABLE_CLIENT_TRANSLATE') === '1'

export const IS_DEV = (() => {
  try {
    const meta = import.meta as unknown as { env?: { DEV?: boolean; MODE?: string } }
    if (meta.env?.DEV === true) return true
    if (meta.env?.MODE === 'development') return true
  } catch {
    /* ignore */
  }
  return false
})()
