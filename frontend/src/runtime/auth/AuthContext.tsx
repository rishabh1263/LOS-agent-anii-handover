import React, { createContext, useEffect, useState, useCallback, useRef } from 'react'
import type { LoginRequest, AuthUser, AuthStage } from './types'
import { normalizeStage } from './types'
import { loginApi, logoutApi, refreshApi, AuthApiError } from './authClient'
import { getCookie, setCookie, removeCookie } from './cookieStorage'

export interface AuthContextValue {
  user: AuthUser | null
  accessToken: string | null
  refreshToken: string | null
  isAuthenticated: boolean
  isLoading: boolean
  login: (credentials: LoginRequest) => Promise<void>
  logout: () => Promise<void>
  refreshTokens: () => Promise<boolean>
}

export const AuthContext = createContext<AuthContextValue | undefined>(undefined)

/** Cookie names for auth session (not HttpOnly — JS must read access token for Bearer). */
const COOKIE_USER = 'los_auth_user'
const COOKIE_ACCESS = 'los_auth_access'
const COOKIE_REFRESH = 'los_auth_refresh'
const COOKIE_EXPIRES_AT = 'los_auth_expires_at'
const COOKIE_STAGE = 'los_auth_stage'

/** Legacy localStorage keys — migrated once then removed */
const LEGACY_KEYS = [
  'los_auth_user',
  'los_auth_access_token',
  'los_auth_refresh_token',
  'los_auth_expires_at',
  'los_auth_stage',
] as const

/** KYC wizard draft — clear on logout so next session starts clean */
const WIZARD_DRAFT_KEY = 'los.kyc.wizard.v1'

/** Refresh this many ms before access token expires */
const REFRESH_SKEW_MS = 60_000

/** Default max-age for refresh cookie (7 days) if expires_in missing */
const DEFAULT_REFRESH_MAX_AGE_SEC = 7 * 24 * 60 * 60

function clearWizardDraft(): void {
  try {
    if (typeof window !== 'undefined' && window.localStorage) {
      window.localStorage.removeItem(WIZARD_DRAFT_KEY)
    }
  } catch {
    /* ignore */
  }
}

function readExpiresAt(): number | null {
  const raw = getCookie(COOKIE_EXPIRES_AT)
  if (!raw) return null
  const n = parseInt(raw, 10)
  return Number.isFinite(n) ? n : null
}

function isAccessExpired(skewMs = 0): boolean {
  const expiresAt = readExpiresAt()
  if (expiresAt === null) return false
  return Date.now() >= expiresAt - skewMs
}

function readStoredUser(): AuthUser | null {
  try {
    const saved = getCookie(COOKIE_USER)
    if (!saved) return null
    const parsed = JSON.parse(saved) as AuthUser
    if (!parsed?.username) return null
    return {
      username: parsed.username,
      stage: normalizeStage(parsed.stage || getCookie(COOKIE_STAGE)),
    }
  } catch {
    return null
  }
}

/** One-time migrate from localStorage → cookies, then clear legacy keys. */
function migrateLegacyLocalStorage(): void {
  if (typeof window === 'undefined' || !window.localStorage) return
  try {
    const access = localStorage.getItem('los_auth_access_token')
    const refresh = localStorage.getItem('los_auth_refresh_token')
    if (!access && !refresh) {
      for (const k of LEGACY_KEYS) localStorage.removeItem(k)
      return
    }

    const expiresRaw = localStorage.getItem('los_auth_expires_at')
    const expiresAt = expiresRaw ? parseInt(expiresRaw, 10) : NaN
    const maxAgeSec =
      Number.isFinite(expiresAt) && expiresAt > Date.now()
        ? Math.ceil((expiresAt - Date.now()) / 1000) + DEFAULT_REFRESH_MAX_AGE_SEC
        : DEFAULT_REFRESH_MAX_AGE_SEC

    if (access) setCookie(COOKIE_ACCESS, access, { maxAge: maxAgeSec })
    if (refresh) setCookie(COOKIE_REFRESH, refresh, { maxAge: maxAgeSec })
    if (expiresRaw) setCookie(COOKIE_EXPIRES_AT, expiresRaw, { maxAge: maxAgeSec })

    const userRaw = localStorage.getItem('los_auth_user')
    const stageRaw = localStorage.getItem('los_auth_stage')
    if (userRaw) setCookie(COOKIE_USER, userRaw, { maxAge: maxAgeSec })
    if (stageRaw) setCookie(COOKIE_STAGE, stageRaw, { maxAge: maxAgeSec })

    for (const k of LEGACY_KEYS) localStorage.removeItem(k)
  } catch {
    /* ignore migration errors */
  }
}

function cookieMaxAgeFromExpiresIn(expiresInSec: number): number {
  return Math.max(expiresInSec, DEFAULT_REFRESH_MAX_AGE_SEC)
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const migrated = useRef(false)
  if (!migrated.current) {
    migrateLegacyLocalStorage()
    migrated.current = true
  }

  const [user, setUser] = useState<AuthUser | null>(() => readStoredUser())
  const [accessToken, setAccessToken] = useState<string | null>(() => getCookie(COOKIE_ACCESS))
  const [refreshToken, setRefreshToken] = useState<string | null>(() => getCookie(COOKIE_REFRESH))
  const [isLoading, setIsLoading] = useState(false)
  const [isHydrating, setIsHydrating] = useState(true)
  const refreshInFlight = useRef<Promise<boolean> | null>(null)

  const clearSession = useCallback(() => {
    setUser(null)
    setAccessToken(null)
    setRefreshToken(null)
    removeCookie(COOKIE_USER)
    removeCookie(COOKIE_ACCESS)
    removeCookie(COOKIE_REFRESH)
    removeCookie(COOKIE_EXPIRES_AT)
    removeCookie(COOKIE_STAGE)
  }, [])

  const saveSession = useCallback(
    (username: string, access: string, refresh: string, expiresIn: number, stage: AuthStage) => {
      const authUser: AuthUser = { username, stage: normalizeStage(stage) }
      const expiresAt = Date.now() + expiresIn * 1000
      const maxAge = cookieMaxAgeFromExpiresIn(expiresIn)

      setUser(authUser)
      setAccessToken(access)
      setRefreshToken(refresh)

      setCookie(COOKIE_USER, JSON.stringify(authUser), { maxAge })
      setCookie(COOKIE_ACCESS, access, { maxAge })
      setCookie(COOKIE_REFRESH, refresh, { maxAge })
      setCookie(COOKIE_EXPIRES_AT, String(expiresAt), { maxAge })
      setCookie(COOKIE_STAGE, authUser.stage, { maxAge })
    },
    [],
  )

  const refreshTokens = useCallback(async (): Promise<boolean> => {
    if (refreshInFlight.current) {
      return refreshInFlight.current
    }

    const run = (async (): Promise<boolean> => {
      const currentRefresh = getCookie(COOKIE_REFRESH) || refreshToken
      if (!currentRefresh) {
        clearSession()
        return false
      }

      try {
        const data = await refreshApi(currentRefresh)
        const currentUsername =
          user?.username || readStoredUser()?.username || 'User'
        const stage = normalizeStage(
          data.stage || getCookie(COOKIE_STAGE) || user?.stage || 'FOS',
        )
        saveSession(
          currentUsername,
          data.access_token,
          data.refresh_token,
          data.expires_in,
          stage,
        )
        return true
      } catch {
        clearSession()
        return false
      } finally {
        refreshInFlight.current = null
      }
    })()

    refreshInFlight.current = run
    return run
  }, [clearSession, saveSession, user, refreshToken])

  const login = useCallback(
    async (credentials: LoginRequest) => {
      setIsLoading(true)
      try {
        const data = await loginApi(credentials)
        const stage = normalizeStage(data.stage || credentials.stage || 'FOS')
        saveSession(
          credentials.username,
          data.access_token,
          data.refresh_token,
          data.expires_in,
          stage,
        )
      } catch (err) {
        if (err instanceof AuthApiError) throw err
        if (err instanceof Error) throw err
        throw new AuthApiError(0, 'Failed to log in. Please try again.')
      } finally {
        setIsLoading(false)
      }
    },
    [saveSession],
  )

  const logout = useCallback(async () => {
    setIsLoading(true)
    try {
      const currentRefresh = getCookie(COOKIE_REFRESH) || refreshToken
      if (currentRefresh) {
        try {
          await logoutApi(currentRefresh)
        } catch {
          // Still clear local session if backend logout fails
        }
      }
    } finally {
      clearSession()
      clearWizardDraft()
      setIsLoading(false)
    }
  }, [clearSession, refreshToken])

  useEffect(() => {
    let cancelled = false

    async function hydrate() {
      const access = getCookie(COOKIE_ACCESS)
      const refresh = getCookie(COOKIE_REFRESH)

      if (!access) {
        if (refresh || getCookie(COOKIE_USER)) {
          clearSession()
        }
        if (!cancelled) setIsHydrating(false)
        return
      }

      if (isAccessExpired(REFRESH_SKEW_MS)) {
        await refreshTokens()
      }

      if (!cancelled) setIsHydrating(false)
    }

    void hydrate()
    return () => {
      cancelled = true
    }
  }, [clearSession, refreshTokens])

  useEffect(() => {
    if (!accessToken) return

    const interval = window.setInterval(() => {
      if (isAccessExpired(REFRESH_SKEW_MS)) {
        void refreshTokens()
      }
    }, 30_000)

    return () => window.clearInterval(interval)
  }, [accessToken, refreshTokens])

  const value: AuthContextValue = {
    user,
    accessToken,
    refreshToken,
    isAuthenticated: Boolean(accessToken && user),
    isLoading: isLoading || isHydrating,
    login,
    logout,
    refreshTokens,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
