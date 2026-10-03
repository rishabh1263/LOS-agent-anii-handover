import React, { useEffect, useState, useCallback, useRef } from 'react'
import type { LoginRequest, AuthUser, AuthStage } from './types'

const VALID_STAGES: AuthStage[] = ['FOS', 'CPA', 'HOPS', 'BOPS', 'CREDIT']

function normalizeStage(value: unknown): AuthStage {
  const s = String(value || '').toUpperCase()
  return (VALID_STAGES.includes(s as AuthStage) ? s : 'FOS') as AuthStage
}
import { loginApi, logoutApi, refreshApi } from './authClient'
import { AuthContext, type AuthContextValue } from './authContextDef'

const STORAGE_KEY_USER = 'los_auth_user'
const STORAGE_KEY_ACCESS = 'los_auth_access_token'
const STORAGE_KEY_REFRESH = 'los_auth_refresh_token'
const STORAGE_KEY_EXPIRES_AT = 'los_auth_expires_at'
const STORAGE_KEY_STAGE = 'los_auth_stage'

/** Milliseconds before expiry when we proactively refresh */
const REFRESH_SKEW_MS = 60_000

function readExpiresAt(): number | null {
  const raw = localStorage.getItem(STORAGE_KEY_EXPIRES_AT)
  if (!raw) return null
  const n = parseInt(raw, 10)
  return Number.isFinite(n) ? n : null
}

function isAccessExpired(skewMs = 0): boolean {
  const expiresAt = readExpiresAt()
  if (expiresAt === null) {
    // No expiry stored but token present — treat as valid until API rejects
    return false
  }
  return Date.now() >= expiresAt - skewMs
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY_USER)
      if (!saved) return null
      const parsed = JSON.parse(saved) as AuthUser
      return {
        username: parsed.username,
        stage: normalizeStage(parsed.stage || localStorage.getItem(STORAGE_KEY_STAGE)),
      }
    } catch {
      return null
    }
  })

  const [accessToken, setAccessToken] = useState<string | null>(() => {
    return localStorage.getItem(STORAGE_KEY_ACCESS)
  })

  const [refreshToken, setRefreshToken] = useState<string | null>(() => {
    return localStorage.getItem(STORAGE_KEY_REFRESH)
  })

  const [isLoading, setIsLoading] = useState(false)
  /** True while the initial expiry / refresh check is in flight */
  const [isHydrating, setIsHydrating] = useState(true)
  const refreshInFlight = useRef<Promise<boolean> | null>(null)

  const clearSession = useCallback(() => {
    setUser(null)
    setAccessToken(null)
    setRefreshToken(null)
    localStorage.removeItem(STORAGE_KEY_USER)
    localStorage.removeItem(STORAGE_KEY_ACCESS)
    localStorage.removeItem(STORAGE_KEY_REFRESH)
    localStorage.removeItem(STORAGE_KEY_EXPIRES_AT)
    localStorage.removeItem(STORAGE_KEY_STAGE)
  }, [])

  const saveSession = useCallback(
    (username: string, access: string, refresh: string, expiresIn: number, stage: AuthStage) => {
      const authUser: AuthUser = { username, stage: normalizeStage(stage) }
      const expiresAt = Date.now() + expiresIn * 1000

      setUser(authUser)
      setAccessToken(access)
      setRefreshToken(refresh)

      localStorage.setItem(STORAGE_KEY_USER, JSON.stringify(authUser))
      localStorage.setItem(STORAGE_KEY_ACCESS, access)
      localStorage.setItem(STORAGE_KEY_REFRESH, refresh)
      localStorage.setItem(STORAGE_KEY_EXPIRES_AT, String(expiresAt))
      localStorage.setItem(STORAGE_KEY_STAGE, authUser.stage)
    },
    [],
  )

  const refreshTokens = useCallback(async (): Promise<boolean> => {
    // Deduplicate concurrent refresh attempts
    if (refreshInFlight.current) {
      return refreshInFlight.current
    }

    const run = (async (): Promise<boolean> => {
      const currentRefresh = localStorage.getItem(STORAGE_KEY_REFRESH)
      if (!currentRefresh) {
        clearSession()
        return false
      }

      try {
        const data = await refreshApi(currentRefresh)
        const currentUsername =
          user?.username ||
          (() => {
            try {
              const saved = localStorage.getItem(STORAGE_KEY_USER)
              return saved ? (JSON.parse(saved) as AuthUser).username : 'User'
            } catch {
              return 'User'
            }
          })()
        const stage = normalizeStage(
          data.stage || localStorage.getItem(STORAGE_KEY_STAGE) || user?.stage || 'FOS',
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
  }, [clearSession, saveSession, user])

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
      } finally {
        setIsLoading(false)
      }
    },
    [saveSession],
  )

  const logout = useCallback(async () => {
    setIsLoading(true)
    try {
      const currentRefresh = localStorage.getItem(STORAGE_KEY_REFRESH)
      if (currentRefresh) {
        try {
          await logoutApi(currentRefresh)
        } catch {
          // Invalidate local session even if backend call fails
        }
      }
    } finally {
      clearSession()
      // Drop KYC draft so next login starts clean
      try {
        localStorage.removeItem('los.kyc.wizard.v1')
      } catch {
        /* ignore */
      }
      setIsLoading(false)
    }
  }, [clearSession])

  /**
   * On mount: if access token is missing, clear any stale keys.
   * If access is expired (or about to be), try refresh; on failure clear session
   * so the UI can send the user to the login page.
   */
  useEffect(() => {
    let cancelled = false

    async function hydrate() {
      const access = localStorage.getItem(STORAGE_KEY_ACCESS)
      const refresh = localStorage.getItem(STORAGE_KEY_REFRESH)

      if (!access) {
        // Empty token — ensure session is fully cleared
        if (refresh || localStorage.getItem(STORAGE_KEY_USER)) {
          clearSession()
        }
        if (!cancelled) setIsHydrating(false)
        return
      }

      if (isAccessExpired(REFRESH_SKEW_MS)) {
        const ok = await refreshTokens()
        if (!ok && !cancelled) {
          // Expired and refresh failed — session already cleared
        }
      }

      if (!cancelled) setIsHydrating(false)
    }

    void hydrate()
    return () => {
      cancelled = true
    }
  }, [clearSession, refreshTokens])

  /**
   * Periodic check: when the access token crosses the expiry skew window,
   * attempt a silent refresh. If it fails, clearSession so isAuthenticated
   * becomes false and protected pages redirect to login.
   */
  useEffect(() => {
    if (!accessToken) return

    const interval = window.setInterval(() => {
      if (isAccessExpired(REFRESH_SKEW_MS)) {
        void refreshTokens()
      }
    }, 30_000)

    return () => window.clearInterval(interval)
  }, [accessToken, refreshTokens])

  // Authenticated when we have both access token and user.
  // Expiry is handled by mount/interval refresh; failed refresh calls clearSession.
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
