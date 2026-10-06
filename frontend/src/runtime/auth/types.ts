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
  /** Optional Case ID and APP ID when opening an existing case during login */
  case_id?: string
  app_id?: string
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  /** Present when backend returns role/stage */
  stage?: AuthStage | string
  /** Present when logging in with an existing case */
  case_id?: string
  app_id?: string
  case_data?: Record<string, unknown>
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
