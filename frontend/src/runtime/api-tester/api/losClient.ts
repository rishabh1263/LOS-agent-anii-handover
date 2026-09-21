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
  files: File[]
  expectedTypes: string[]
  operation?: Operation
  applicantId?: string
  caseId?: string
  token?: string
  baseUrl?: string
}

export async function processDocuments(params: ProcessParams): Promise<LosProcessResponse> {
  const {
    files,
    expectedTypes,
    operation = 'PROCESS',
    applicantId,
    caseId,
    token,
    baseUrl = DEFAULT_BASE,
  } = params

  if (files.length === 0) {
    throw new LosApiError(400, {
      error: 'NO_DOCUMENTS',
      message: 'At least one document is required.',
    })
  }

  const form = new FormData()
  form.append('operation', operation)
  if (applicantId?.trim()) form.append('applicant_id', applicantId.trim())
  if (caseId?.trim()) form.append('case_id', caseId.trim())
  if (expectedTypes.length > 0) {
    form.append('expected_types', expectedTypes.join(','))
  }
  for (const file of files) {
    form.append('files', file, file.name)
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
