/**
 * caseClient.ts
 *
 * Handles POST /api/v1/case/fetch.
 * This is a SEPARATE flow from authentication:
 *   - Any logged-in user can look up any valid Case ID + APP ID.
 *   - Case/APP IDs are never permanently linked to a username.
 */

import { apiUrl, PATHS } from '../../config'

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

export interface CaseFetchRequest {
  case_id: string
  app_id: string
}

export interface CaseDataResponse {
  case_id: string
  app_id: string
  found: boolean
  applicant?: Record<string, unknown> | null
  application?: Record<string, unknown> | null
  checklist?: unknown[] | null
  documents?: unknown[] | null
  required_documents?: string[] | null
  stage?: string | null
  data?: Record<string, unknown>
  message: string
}

export class CaseApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'CaseApiError'
    this.status = status
    this.detail = detail
  }
}

// --------------------------------------------------------------------------
// Helper
// --------------------------------------------------------------------------

async function handleResponse<T>(res: Response): Promise<T> {
  const text = await res.text()
  let data: Record<string, unknown>
  try {
    data = text ? (JSON.parse(text) as Record<string, unknown>) : {}
  } catch {
    data = { detail: text || 'Unknown response' }
  }

  if (!res.ok) {
    let detail = 'Case data request failed'
    if (typeof data.detail === 'string') {
      detail = data.detail
    } else if (Array.isArray(data.detail)) {
      detail = (data.detail as { msg?: string }[]).map((e) => e.msg || 'Invalid field').join(', ')
    } else if (typeof data.message === 'string') {
      detail = data.message
    }
    throw new CaseApiError(res.status, detail)
  }
  return data as T
}

// --------------------------------------------------------------------------
// API call
// --------------------------------------------------------------------------

/**
 * Fetch all data for a given Case ID + APP ID combination.
 * Requires a valid Bearer access token from the auth flow.
 * The IDs are NOT validated against the logged-in user — any valid
 * combination will return data.
 */
export async function fetchCaseData(
  payload: CaseFetchRequest,
  accessToken: string,
): Promise<CaseDataResponse> {
  const res = await fetch(apiUrl(PATHS.caseFetch), {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify(payload),
  })
  return handleResponse<CaseDataResponse>(res)
}
