import React, { useEffect, useState, useCallback } from 'react'
import type { LoginRequest, AuthUser } from './types'
import { loginApi, logoutApi, refreshApi } from './authClient'
import { AuthContext, type AuthContextValue } from './authContextDef'

const STORAGE_KEY_USER = 'los_auth_user'
const STORAGE_KEY_ACCESS = 'los_auth_access_token'
const STORAGE_KEY_REFRESH = 'los_auth_refresh_token'
const STORAGE_KEY_EXPIRES_AT = 'los_auth_expires_at'

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY_USER)
      return saved ? JSON.parse(saved) : null
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

  const clearSession = useCallback(() => {
    setUser(null)
    setAccessToken(null)
    setRefreshToken(null)
    localStorage.removeItem(STORAGE_KEY_USER)
    localStorage.removeItem(STORAGE_KEY_ACCESS)
    localStorage.removeItem(STORAGE_KEY_REFRESH)
    localStorage.removeItem(STORAGE_KEY_EXPIRES_AT)
  }, [])

  const saveSession = useCallback(
    (username: string, access: string, refresh: string, expiresIn: number) => {
      const authUser: AuthUser = { username }
      const expiresAt = Date.now() + expiresIn * 1000

      setUser(authUser)
      setAccessToken(access)
      setRefreshToken(refresh)

      localStorage.setItem(STORAGE_KEY_USER, JSON.stringify(authUser))
      localStorage.setItem(STORAGE_KEY_ACCESS, access)
      localStorage.setItem(STORAGE_KEY_REFRESH, refresh)
      localStorage.setItem(STORAGE_KEY_EXPIRES_AT, String(expiresAt))
    },
    [],
  )

  const refreshTokens = useCallback(async (): Promise<boolean> => {
    const currentRefresh = localStorage.getItem(STORAGE_KEY_REFRESH)
    if (!currentRefresh) {
      clearSession()
      return false
    }

    try {
      const data = await refreshApi(currentRefresh)
      const currentUsername = user?.username || 'User'
      saveSession(currentUsername, data.access_token, data.refresh_token, data.expires_in)
      return true
    } catch {
      clearSession()
      return false
    }
  }, [clearSession, saveSession, user])

  const login = useCallback(
    async (credentials: LoginRequest) => {
      setIsLoading(true)
      try {
        const data = await loginApi(credentials)
        saveSession(
          credentials.username,
          data.access_token,
          data.refresh_token,
          data.expires_in,
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
      setIsLoading(false)
    }
  }, [clearSession])

  // Check token expiry on mount using an asynchronous scheduled call
  useEffect(() => {
    const expiresAtStr = localStorage.getItem(STORAGE_KEY_EXPIRES_AT)
    if (expiresAtStr && refreshToken) {
      const expiresAt = parseInt(expiresAtStr, 10)
      // If token will expire in less than 60 seconds, attempt a refresh
      if (Date.now() >= expiresAt - 60000) {
        const timer = setTimeout(() => {
          refreshTokens()
        }, 0)
        return () => clearTimeout(timer)
      }
    }
  }, [refreshToken, refreshTokens])

  const value: AuthContextValue = {
    user,
    accessToken,
    refreshToken,
    isAuthenticated: Boolean(accessToken && user),
    isLoading,
    login,
    logout,
    refreshTokens,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
