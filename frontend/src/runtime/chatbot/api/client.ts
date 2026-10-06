import { apiUrl, PATHS } from '../../config'

export interface ChatApiErrorBody {
  message?: string
  detail?: string
  error?: string
  [key: string]: unknown
}

export class ChatApiError extends Error {
  status: number
  body: ChatApiErrorBody

  constructor(status: number, body: ChatApiErrorBody) {
    super(body.message || body.detail || body.error || 'Request failed')
    this.name = 'ChatApiError'
    this.status = status
    this.body = body
  }
}

export interface ChatQueryRequest {
  message: string
  case_id?: string
  applicant_id?: string
  party_id?: string | null
  stage?: string
  conversation_id?: string
}

export interface ChatSource {
  type?: string
  kind?: string
  finding_kind?: string
  reason_code?: string
  document_id?: string
  source_id?: string
  party_id?: string | null
  decision?: string
  status?: string | null
  source_type?: string
  case_id?: string
  stage?: string
  document_type?: string
  [key: string]: unknown
}

/**
 * POST /api/v1/copilot/query response.
 * Credit/risk/KYC questions may return route_to without a full answer.
 */
export interface ChatQueryResponse {
  request_id?: string
  case_id?: string | null
  applicant_id?: string | null
  party_id?: string | null
  conversation_id?: string
  stage?: string
  stage_resolution?: string
  category?: string
  intent?: string
  answer?: string
  response_source?: string
  grounded?: boolean
  sources?: ChatSource[]
  tool_invoked?: string[]
  status?: string | null
  route_to?: string | null
  suggested_questions?: string[]
  errors?: Array<{ code?: string; message?: string }>
  [key: string]: unknown
}

export interface ChatApiConfig {
  /** e.g. `/api/v1/copilot` or full origin */
  baseUrl?: string
  /** Path appended to baseUrl. Default: `/query` */
  queryPath?: string
}

const DEFAULT_BASE = apiUrl(PATHS.copilot)
const DEFAULT_QUERY_PATH = '/query'

/**
 * Build display text from copilot response.
 * Handles route_to / capability unavailable / empty answer.
 */
export function formatChatAnswer(res: ChatQueryResponse): {
  text: string
  suggestedQuestions?: string[]
  routed?: string | null
  grounded?: boolean
} {
  const routed = res.route_to ? String(res.route_to) : null
  const answer = (res.answer || '').trim()

  if (res.status === 'CAPABILITY_UNAVAILABLE') {
    return {
      text:
        answer ||
        (routed
          ? `This question is handled by the ${routed} stage. Route the case there for a full answer.`
          : 'This assistant cannot answer that type of question for the current stage.'),
      suggestedQuestions: res.suggested_questions,
      routed,
      grounded: res.grounded,
    }
  }

  if (routed && !answer) {
    return {
      text: `This is a ${routed} question and is not answered at the current stage. Please route to ${routed}.`,
      suggestedQuestions: res.suggested_questions,
      routed,
      grounded: res.grounded,
    }
  }

  if (!answer) {
    return {
      text: 'No answer returned for this question.',
      suggestedQuestions: res.suggested_questions,
      routed,
      grounded: res.grounded,
    }
  }

  return {
    text: answer,
    suggestedQuestions: res.suggested_questions,
    routed,
    grounded: res.grounded,
  }
}

/**
 * POST {baseUrl}{queryPath}
 * Default: POST /api/v1/copilot/query
 */
export async function queryChat(
  payload: ChatQueryRequest,
  token?: string,
  config: ChatApiConfig = {},
  signal?: AbortSignal,
): Promise<ChatQueryResponse> {
  const base = (config.baseUrl ?? DEFAULT_BASE).replace(/\/$/, '')
  const path = config.queryPath ?? DEFAULT_QUERY_PATH
  const url = `${base}${path.startsWith('/') ? path : `/${path}`}`

  const headers: HeadersInit = { 'Content-Type': 'application/json' }
  if (token?.trim()) headers.Authorization = `Bearer ${token.trim()}`

  const body = {
    message: payload.message,
    case_id: payload.case_id || undefined,
    applicant_id: payload.applicant_id || undefined,
    party_id: payload.party_id ?? null,
    stage: payload.stage || undefined,
    conversation_id: payload.conversation_id || undefined,
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

  return parsed as ChatQueryResponse
}
