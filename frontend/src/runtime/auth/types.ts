/** LOS stage selected at login (FOS / CPA / HOPS / BOPS / CREDIT). */
export type AuthStage = 'FOS' | 'CPA' | 'HOPS' | 'BOPS' | 'CREDIT'

export const AUTH_STAGES: { value: AuthStage; label: string }[] = [
  { value: 'FOS', label: 'FOS' },
  { value: 'CPA', label: 'CPA' },
  { value: 'HOPS', label: 'HOPS' },
  { value: 'BOPS', label: 'BOPS' },
  { value: 'CREDIT', label: 'Credit' },
]

const VALID_STAGES: AuthStage[] = ['FOS', 'CPA', 'HOPS', 'BOPS', 'CREDIT']

export function normalizeStage(value: unknown): AuthStage {
  const s = String(value || '').toUpperCase()
  return (VALID_STAGES.includes(s as AuthStage) ? s : 'FOS') as AuthStage
}

export interface LoginRequest {
  username: string
  password: string
  /** Optional stage context; stored client-side for FOS / copilot APIs */
  stage?: AuthStage
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  /** Present when backend returns role/stage */
  stage?: AuthStage | string
}

export interface RefreshRequest {
  refresh_token: string
}

export interface LogoutRequest {
  refresh_token: string
}

export interface AuthUser {
  username: string
  stage: AuthStage
}
