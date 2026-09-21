import type { LosProcessResponse, Operation, ApiErrorBody } from '../types'

const DEFAULT_BASE = '/api/v1/los'

export class LosApiError extends Error {
  status: number
  body: ApiErrorBody

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || body.detail || body.error || 'Request failed')
    this.name = 'LosApiError'
    this.status = status
    this.body = body
  }
}

export interface ProcessParams {
  /** PRIMARY_APPLICANT files → form field `files` */
  files: File[]
  /** Expected types aligned 1:1 with `files` → `expected_types` */
  expectedTypes: string[]
  /** CO_APPLICANT files → form field `co_applicant_files` */
  coApplicantFiles?: File[]
  /** Expected types aligned 1:1 with co files → `co_applicant_expected_types` */
  coApplicantExpectedTypes?: string[]
  operation?: Operation
  applicantId?: string
  coApplicantId?: string
  caseId?: string
  token?: string
  baseUrl?: string
}

/**
 * Matches Swagger multipart contract:
 * - files                  → primary applicant docs
 * - expected_types         → one type per primary file (order matched)
 * - co_applicant_files     → co-applicant docs
 * - co_applicant_expected_types → one type per co file
 * - co_applicant_id        → required when co files present
 */
export async function processDocuments(params: ProcessParams): Promise<LosProcessResponse> {
  const {
    files,
    expectedTypes,
    coApplicantFiles = [],
    coApplicantExpectedTypes = [],
    operation = 'PROCESS',
    applicantId,
    coApplicantId,
    caseId,
    token,
    baseUrl = DEFAULT_BASE,
  } = params

  if (files.length === 0 && coApplicantFiles.length === 0) {
    throw new LosApiError(400, {
      error: 'NO_DOCUMENTS',
      message: 'At least one document is required.',
    })
  }

  if (coApplicantFiles.length > 0 && !coApplicantId?.trim()) {
    throw new LosApiError(400, {
      error: 'CO_APPLICANT_ID_REQUIRED',
      message: 'co_applicant_id is required when uploading co-applicant documents.',
    })
  }

  const form = new FormData()
  form.append('operation', operation)

  if (applicantId?.trim()) form.append('applicant_id', applicantId.trim())
  if (caseId?.trim()) form.append('case_id', caseId.trim())
  if (coApplicantId?.trim()) form.append('co_applicant_id', coApplicantId.trim())

  // Primary applicant — field name: files
  for (const file of files) {
    form.append('files', file, file.name)
  }
  // One expected type per primary file (Swagger: array, order-matched)
  for (let i = 0; i < files.length; i++) {
    const t = expectedTypes[i] || 'AUTO'
    form.append('expected_types', t)
  }

  // Co-applicant — separate field names (do NOT mix into `files`)
  for (const file of coApplicantFiles) {
    form.append('co_applicant_files', file, file.name)
  }
  for (let i = 0; i < coApplicantFiles.length; i++) {
    const t = coApplicantExpectedTypes[i] || 'AUTO'
    form.append('co_applicant_expected_types', t)
  }

  const headers: HeadersInit = {}
  if (token?.trim()) {
    headers.Authorization = `Bearer ${token.trim()}`
  }

  const res = await fetch(`${baseUrl}/process`, {
    method: 'POST',
    headers,
    body: form,
  })

  const text = await res.text()
  let body: unknown
  try {
    body = text ? JSON.parse(text) : {}
  } catch {
    body = { message: text || 'Invalid response' }
  }

  if (!res.ok) {
    throw new LosApiError(res.status, body as ApiErrorBody)
  }

  return body as LosProcessResponse
}
