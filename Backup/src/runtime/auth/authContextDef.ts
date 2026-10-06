import { createContext } from 'react'
import type { LoginRequest, AuthUser } from './types'

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
