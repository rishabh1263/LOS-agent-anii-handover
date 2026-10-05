import type { LoginRequest, TokenResponse, RefreshRequest, LogoutRequest } from './types'
import { apiUrl, PATHS } from '../config'

export class AuthApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'AuthApiError'
    this.status = status
    this.detail = detail
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (res.status === 204) {
    return {} as T
  }

  const text = await res.text()
  let data: Record<string, unknown>
  try {
    data = text ? JSON.parse(text) : {}
  } catch {
    data = { detail: text || 'Unknown response' }
  }

  if (!res.ok) {
    let detail = 'Authentication request failed'
    if (typeof data.detail === 'string') {
      detail = data.detail
    } else if (Array.isArray(data.detail)) {
      detail = data.detail
        .map((err: { msg?: string }) => err.msg || 'Invalid field')
        .join(', ')
    } else if (typeof data.message === 'string') {
      detail = data.message
    } else if (typeof data.error === 'string') {
      detail = data.error
    }
    throw new AuthApiError(res.status, detail)
  }

  return data as T
}

const defaultFetchInit: RequestInit = {
  credentials: 'include',
  headers: { 'Content-Type': 'application/json' },
}

export async function loginApi(payload: LoginRequest): Promise<TokenResponse> {
  const res = await fetch(apiUrl(PATHS.authLogin), {
    ...defaultFetchInit,
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return handleResponse<TokenResponse>(res)
}

export async function refreshApi(refreshToken: string): Promise<TokenResponse> {
  const payload: RefreshRequest = { refresh_token: refreshToken }
  const res = await fetch(apiUrl(PATHS.authRefresh), {
    ...defaultFetchInit,
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return handleResponse<TokenResponse>(res)
}

export async function logoutApi(refreshToken: string): Promise<void> {
  const payload: LogoutRequest = { refresh_token: refreshToken }
  const res = await fetch(apiUrl(PATHS.authLogout), {
    ...defaultFetchInit,
    method: 'POST',
    body: JSON.stringify(payload),
  })
  await handleResponse<void>(res)
}
