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
  /** Optional: the chat's open case (session workspace) is used when omitted */
  applicant_id?: string
  case_id?: string
  action?: string
  message?: string
  context?: Record<string, unknown>
  /** One id per chat: the server keeps the open case, pending questions and history under it */
  chat_id?: string
  reply_language?: string
}

export type FosCopilotResponse = FosUploadResponse & {
  /** THE reply to render (links: ask: / action:) -- MASTER SPEC contract */
  markdown?: string
  tts?: string
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

  const body: Record<string, unknown> = {
    action: payload.action || 'CUSTOM_QUERY',
    message: payload.message || '',
    context: payload.context || {},
  }
  if (payload.applicant_id?.trim()) body.applicant_id = payload.applicant_id.trim()
  if (payload.case_id?.trim()) body.case_id = payload.case_id.trim()
  if (payload.chat_id) body.chat_id = payload.chat_id
  if (payload.reply_language) body.reply_language = payload.reply_language

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

/** Known API codes → short plain-English sentences */
const DETAIL_SENTENCES: Record<string, string> = {
  WRONG_DOCUMENT: 'This file is not the required document type. Please upload the correct one.',
  INVALID_DOCUMENT: 'This file could not be accepted. Please upload a valid document.',
  DOCUMENT_MISSING: 'Required document is missing.',
  DOCUMENT_REJECTED: 'This document was rejected. Please upload a clearer or correct file.',
  TYPE_MISMATCH: 'Detected document type does not match what was expected.',
  SIGNATURE_PRESENT: 'A signature was detected on the document.',
  SIGNATURE_CROPPED: 'The signature appears cropped or incomplete.',
  SIGNATURE_REFERENCE_MISSING: 'No reference signature is available for comparison.',
  REFERENCE_UNAVAILABLE: 'Reference signature is not available.',
  SIGNATURE_NOT_COMPARABLE: 'The signature could not be compared reliably.',
  AUTHENTICITY_NOT_ESTABLISHED: 'Authenticity of the signature could not be confirmed.',
  LOW_QUALITY: 'Image quality is too low. Please upload a clearer file.',
  BLURRY: 'The image looks blurry. Please upload a sharper photo or scan.',
  UNREADABLE: 'Text on the document could not be read clearly.',
}

function normalizeCode(part: string): string {
  return part
    .trim()
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .toUpperCase()
}

/**
 * Turn API codes / reason strings into readable sentences.
 * e.g. WRONG_DOCUMENT → "This file is not the required document type…"
 * Multiple codes are joined as short sentences.
 */
export function formatUploadDetail(raw?: string | null): string | undefined {
  if (!raw || !String(raw).trim()) return undefined

  const parts = String(raw)
    .split(/[,;|]+/)
    .map((p) => p.trim())
    .filter(Boolean)

  if (parts.length === 0) return undefined

  const sentences = parts.map((part) => {
    const code = normalizeCode(part)
    if (DETAIL_SENTENCES[code]) return DETAIL_SENTENCES[code]
    // Already a sentence?
    if (/[.!?]$/.test(part.trim()) || part.includes(' ')) {
      const t = part.trim()
      return t.charAt(0).toUpperCase() + t.slice(1)
    }
    // Fallback: Title Case words from CODE_NAME
    return code
      .replace(/_/g, ' ')
      .toLowerCase()
      .replace(/\b\w/g, (c) => c.toUpperCase())
  })

  // De-dupe while preserving order
  const seen = new Set<string>()
  const unique = sentences.filter((s) => {
    const k = s.toLowerCase()
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })

  return unique.join(' ')
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
      const codeOrReason = d.upload_validation.reason || d.upload_validation.code || 'Invalid upload'
      return {
        status: 'validation' as const,
        detail: formatUploadDetail(codeOrReason),
        detectedType: d.document_type,
      }
    }
    const v = (d.verification || '').toUpperCase()
    if (v === 'PASS' || v === 'VERIFIED') {
      return {
        status: 'pass' as const,
        detail: formatUploadDetail(d.reason),
        detectedType: d.document_type,
      }
    }
    if (v === 'REVIEW') {
      const raw = d.reason || d.reason_codes?.join(', ')
      return {
        status: 'review' as const,
        detail: formatUploadDetail(raw),
        detectedType: d.document_type,
      }
    }
    const rawFail = d.reason || d.reason_codes?.join(', ') || 'Verification failed'
    return {
      status: 'fail' as const,
      detail: formatUploadDetail(rawFail),
      detectedType: d.document_type,
    }
  })
}


/**
 * The Upload button of a reply (`action:upload` -> {post_to}): a multipart UPLOAD_DOCUMENT to `post_to`, which
 * already carries the chat id, so the file is filed on the chat's open case. Returns the reply to append.
 */
export async function uploadFromAction(
  postTo: string,
  files: File[],
  documentType: string | null,
  token?: string,
): Promise<FosCopilotResponse> {
  const form = new FormData()
  form.append('action', 'UPLOAD_DOCUMENT')
  files.forEach((f) => {
    form.append('files', f)
    if (documentType) form.append('document_types', documentType)
  })
  const headers: HeadersInit = {}
  if (token?.trim()) headers.Authorization = `Bearer ${token.trim()}`
  const url = /^https?:/i.test(postTo) ? postTo : apiUrl(postTo)
  const res = await fetch(url, { method: 'POST', headers, body: form })
  const text = await res.text()
  let parsed: unknown
  try {
    parsed = text ? JSON.parse(text) : {}
  } catch {
    parsed = { message: text || 'Invalid response' }
  }
  if (!res.ok) throw new ChatApiError(res.status, parsed as ChatApiErrorBody)
  return parsed as FosCopilotResponse
}
