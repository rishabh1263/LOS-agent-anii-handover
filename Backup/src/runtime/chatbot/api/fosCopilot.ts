/**
 * FOS copilot + document upload — used when the chatbot needs upload signals.
 *
 * POST /api/v1/fos/copilot  → checklist, actions, pending_items, next_action, …
 * POST /api/v1/fos/documents → multipart upload; response includes fresh checklist
 *
 * General chat stays on /api/v1/copilot/query (no structured upload fields).
 */

import { apiUrl, PATHS } from '../../config'
import { ChatApiError, type ChatApiErrorBody } from './client'
import type { FosUploadResponse } from '../utils/uploadTargets'

const FOS_BASE = apiUrl(PATHS.fos)

export interface FosCopilotRequest {
  applicant_id: string
  case_id: string
  action?: string
  message?: string
  context?: Record<string, unknown>
}

export type FosCopilotResponse = FosUploadResponse & {
  answer?: string | null
  conversation_id?: string
  suggested_questions?: string[]
  route_to?: string | null
  grounded?: boolean
  status?: string | null
  intent?: string
  request_id?: string
}

/** Heuristic: route document / verify questions to /fos/copilot */
export function isDocumentRelatedQuery(message: string): boolean {
  const m = message.toLowerCase().trim()
  if (!m) return false
  const keys = [
    'document',
    'documents',
    'doc ',
    'docs',
    'upload',
    'reupload',
    're-upload',
    'verify',
    'verification',
    'checklist',
    'pending',
    'mandatory',
    'required',
    'missing',
    'reject',
    'rejected',
    'pan',
    'aadhaar',
    'aadhar',
    'passport',
    'voter',
    'licence',
    'license',
    'bank statement',
    'salary',
    'itr',
    'form 16',
    'photo',
    'signature',
    'address proof',
    'kyc',
    // Common alternate spellings / phrases users may type
    'document list',
    'which documents',
    'verify again',
    'upload document',
    'pending documents',
    'required documents',
  ]

  return keys.some((k) => m.includes(k))
}

/**
 * POST /api/v1/fos/copilot
 */
export async function queryFosCopilot(
  payload: FosCopilotRequest,
  token?: string,
  signal?: AbortSignal,
): Promise<FosCopilotResponse> {
  const url = `${FOS_BASE}/copilot`
  const headers: HeadersInit = { 'Content-Type': 'application/json' }
  if (token?.trim()) headers.Authorization = `Bearer ${token.trim()}`

  const body = {
    applicant_id: payload.applicant_id,
    case_id: payload.case_id,
    action: payload.action || 'CUSTOM_QUERY',
    message: payload.message || '',
    context: payload.context || {},
  }

  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal,
  })

  const text = await res.text()
  let parsed: unknown
  try {
    parsed = text ? JSON.parse(text) : {}
  } catch {
    parsed = { message: text || 'Invalid response' }
  }

  if (!res.ok) {
    throw new ChatApiError(res.status, parsed as ChatApiErrorBody)
  }

  return parsed as FosCopilotResponse
}

export interface UploadDocumentFile {
  file: File
  documentType?: string
}

/**
 * POST /api/v1/fos/documents — multipart
 * document_types aligned by position with files (blank = auto-detect)
 */
export async function uploadFosDocuments(
  params: {
    applicantId: string
    caseId: string
    files: UploadDocumentFile[]
  },
  token?: string,
  signal?: AbortSignal,
): Promise<FosCopilotResponse> {
  const form = new FormData()
  form.append('applicant_id', params.applicantId)
  form.append('case_id', params.caseId)
  params.files.forEach(({ file, documentType }) => {
    form.append('files', file)
    form.append('document_types', documentType ?? '')
  })

  const headers: HeadersInit = {}
  if (token?.trim()) headers.Authorization = `Bearer ${token.trim()}`
  // Do NOT set Content-Type — browser sets multipart boundary

  const res = await fetch(`${FOS_BASE}/documents`, {
    method: 'POST',
    headers,
    body: form,
    signal,
  })

  const text = await res.text()
  let parsed: unknown
  try {
    parsed = text ? JSON.parse(text) : {}
  } catch {
    parsed = { message: text || 'Invalid response' }
  }

  if (!res.ok) {
    throw new ChatApiError(res.status, parsed as ChatApiErrorBody)
  }

  return parsed as FosCopilotResponse
}

/** Map upload response documents_processed → panel results */
export function mapUploadResults(
  res: FosCopilotResponse,
  fileCount: number,
): Array<{
  status: 'pass' | 'review' | 'fail' | 'validation'
  detail?: string
  detectedType?: string
}> {
  const processed = (res.verification as { documents_processed?: unknown[] } | undefined)
    ?.documents_processed as
    | Array<{
        verification?: string
        document_type?: string
        upload_validation?: { reason?: string; code?: string }
        reason?: string
        reason_codes?: string[]
      }>
    | undefined

  if (!processed?.length) {
    // Fallback: treat all as pass if HTTP 200 and no per-file data
    return Array.from({ length: fileCount }, () => ({ status: 'pass' as const }))
  }

  return processed.slice(0, fileCount).map((d) => {
    if (d.upload_validation) {
      return {
        status: 'validation' as const,
        detail: d.upload_validation.reason || d.upload_validation.code || 'Invalid upload',
        detectedType: d.document_type,
      }
    }
    const v = (d.verification || '').toUpperCase()
    if (v === 'PASS' || v === 'VERIFIED') {
      return { status: 'pass' as const, detail: d.reason, detectedType: d.document_type }
    }
    if (v === 'REVIEW') {
      return {
        status: 'review' as const,
        detail: d.reason || d.reason_codes?.join(', '),
        detectedType: d.document_type,
      }
    }
    return {
      status: 'fail' as const,
      detail: d.reason || d.reason_codes?.join(', ') || 'Verification failed',
      detectedType: d.document_type,
    }
  })
}
