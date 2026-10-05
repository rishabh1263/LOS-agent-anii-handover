/**
 * FOS API client — aligned with backend FosResponse shape.
 * All FOS endpoints return the same envelope; unused fields are null / [].
 */

import type { ApiErrorBody } from '../types'
import { apiUrl, PATHS } from '../../config'

const FOS_BASE = apiUrl(PATHS.fos)

export class FosApiError extends Error {
  status: number
  body: ApiErrorBody

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || body.detail || body.error || 'Request failed')
    this.name = 'FosApiError'
    this.status = status
    this.body = body
  }
}

export interface FosApplicantPayload {
  full_name?: string
  mobile?: string
  email?: string
  date_of_birth?: string
  address?: string
  [key: string]: string | undefined
}

export interface FosApplicationPayload {
  product?: string
  loan_amount?: number
  employment_type?: string
  tenure_months?: number
  interest_rate_pct?: number
  declared_monthly_obligations?: number
  property_value?: number
  [key: string]: string | number | undefined
}

export interface CreateApplicantRequest {
  applicant: FosApplicantPayload
  application?: FosApplicationPayload
  applicant_id?: string | null
  case_id?: string | null
}

/** Applicant record inside FosResponse.applicant */
export interface FosApplicant {
  applicant_id?: string
  full_name?: string
  mobile?: string
  email?: string
  date_of_birth?: string
  address?: string
  missing_fields?: string[]
  is_complete?: boolean
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

/** Application record inside FosResponse.application */
export interface FosApplication {
  case_id?: string
  applicant_id?: string
  status?: string
  product?: string
  loan_amount?: string | number
  employment_type?: string
  missing_fields?: string[]
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

export type ChecklistSlotStatus =
  | 'MISSING'
  | 'UPLOADED'
  | 'PROCESSING'
  | 'VERIFIED'
  | 'REVIEW'
  | 'REJECTED'
  | string

export interface FosChecklistItem {
  slot: string
  accepts?: string[]
  mandatory?: boolean
  requirement?: string
  rule_ids?: string[]
  reason?: string
  policy_status?: string
  status?: ChecklistSlotStatus
  [key: string]: unknown
}

export interface FosPolicy {
  policy_id?: string
  policy_version?: string
  status?: string
  applied_rules?: string[]
  unevaluated_rules?: unknown[]
  [key: string]: unknown
}

export interface FosCaseState {
  required?: number
  satisfied?: number
  missing?: number
  under_review?: number
  failed?: number
  [key: string]: unknown
}

export interface FosDocumentItem {
  source_id?: string
  document_type?: string
  verification?: string
  reason_codes?: string[]
  [key: string]: unknown
}

export interface FosErrorItem {
  code?: string
  message?: string
}

/**
 * Common envelope for every FOS endpoint.
 * Fields the endpoint does not fill are null or [].
 */
export interface FosResponse {
  request_id?: string
  applicant_id?: string | null
  case_id?: string | null
  action?: string
  intent?: string
  answer?: string | null

  applicant?: FosApplicant | null
  application?: FosApplication | null
  stage?: string | null

  documents?: FosDocumentItem[]
  checklist?: FosChecklistItem[]
  required_documents?: string[]
  policy?: FosPolicy | null
  pending_items?: unknown[]

  query_type?: string | null
  case_state?: FosCaseState | null
  suggested_questions?: string[]
  available_actions?: unknown[]
  document_highlights?: unknown[]
  followed_up?: unknown
  context?: unknown
  clarification_required?: unknown

  verification?: unknown
  kyc?: unknown
  knowledge?: unknown

  summary?: string | null
  status?: unknown
  processing_queue?: unknown[]
  grounded?: boolean

  next_action?: unknown
  readiness?: unknown
  actions?: unknown[]
  route_to?: string | null

  category?: string | null
  response_source?: string | null
  processing_ms?: number
  errors?: FosErrorItem[]

  [key: string]: unknown
}

async function parseResponse(res: Response): Promise<FosResponse> {
  const text = await res.text()
  let body: unknown
  try {
    body = text ? JSON.parse(text) : {}
  } catch {
    body = { message: text || 'Invalid response' }
  }
  if (!res.ok) {
    throw new FosApiError(res.status, body as ApiErrorBody)
  }
  return body as FosResponse
}

function authHeaders(token?: string): HeadersInit {
  const headers: HeadersInit = { 'Content-Type': 'application/json' }
  if (token?.trim()) headers.Authorization = `Bearer ${token.trim()}`
  return headers
}

/** Pull applicant_id / case_id from top-level or nested objects. */
export function extractIds(res: FosResponse): {
  applicantId: string
  caseId: string
} {
  const applicantId = String(
    res.applicant_id || res.applicant?.applicant_id || '',
  ).trim()
  const caseId = String(
    res.case_id || res.application?.case_id || '',
  ).trim()
  return { applicantId, caseId }
}

/** POST /api/v1/fos/applicants — open a case (201) */
export async function createApplicant(
  payload: CreateApplicantRequest,
  token?: string,
): Promise<FosResponse> {
  const res = await fetch(`${FOS_BASE}/applicants`, {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  })
  return parseResponse(res)
}

/** GET /api/v1/fos/applicants/{applicant_id} */
export async function getApplicant(
  applicantId: string,
  token?: string,
  query?: { case_id?: string },
): Promise<FosResponse> {
  const id = encodeURIComponent(applicantId.trim())
  const qs = query?.case_id
    ? `?case_id=${encodeURIComponent(query.case_id)}`
    : ''
  const res = await fetch(`${FOS_BASE}/applicants/${id}${qs}`, {
    method: 'GET',
    headers: authHeaders(token),
  })
  return parseResponse(res)
}

/** GET /api/v1/fos/checklist/{case_id} */
export async function getChecklist(
  caseId: string,
  token?: string,
  query?: { applicant_id?: string },
): Promise<FosResponse> {
  const id = encodeURIComponent(caseId.trim())
  const qs = query?.applicant_id
    ? `?applicant_id=${encodeURIComponent(query.applicant_id)}`
    : ''
  const res = await fetch(`${FOS_BASE}/checklist/${id}${qs}`, {
    method: 'GET',
    headers: authHeaders(token),
  })
  return parseResponse(res)
}
