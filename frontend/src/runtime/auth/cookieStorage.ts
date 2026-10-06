/**
 * Cookie helpers for auth tokens.
 * Client-set cookies cannot be HttpOnly (server must Set-Cookie for that).
 * We still use Secure + SameSite=Strict and short path scope.
 */

const isBrowser = typeof document !== 'undefined'

function isSecureContext(): boolean {
  if (typeof window === 'undefined') return false
  return window.location.protocol === 'https:' || window.location.hostname === 'localhost'
}

export function getCookie(name: string): string | null {
  if (!isBrowser) return null
  try {
    const match = document.cookie.match(
      new RegExp('(?:^|; )' + name.replace(/([.$?*|{}()[\]\\/+^])/g, '\\$1') + '=([^;]*)'),
    )
    return match ? decodeURIComponent(match[1]) : null
  } catch {
    return null
  }
}

export function setCookie(
  name: string,
  value: string,
  options: {
    /** Seconds from now. Omit for session cookie. */
    maxAge?: number
    path?: string
    sameSite?: 'Strict' | 'Lax' | 'None'
  } = {},
): void {
  if (!isBrowser) return
  try {
    const path = options.path ?? '/'
    const sameSite = options.sameSite ?? 'Strict'
    let cookie = `${encodeURIComponent(name)}=${encodeURIComponent(value)}; Path=${path}; SameSite=${sameSite}`
    if (typeof options.maxAge === 'number' && options.maxAge >= 0) {
      cookie += `; Max-Age=${Math.floor(options.maxAge)}`
    }
    if (isSecureContext()) {
      cookie += '; Secure'
    }
    document.cookie = cookie
  } catch {
    /* private mode / blocked */
  }
}

export function removeCookie(name: string, path = '/'): void {
  if (!isBrowser) return
  try {
    document.cookie = `${encodeURIComponent(name)}=; Path=${path}; Max-Age=0; SameSite=Strict`
    if (isSecureContext()) {
      document.cookie = `${encodeURIComponent(name)}=; Path=${path}; Max-Age=0; SameSite=Strict; Secure`
    }
  } catch {
    /* ignore */
  }
}
