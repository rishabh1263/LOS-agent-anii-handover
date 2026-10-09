/**
 * LOS chatbot frontend contract, version 1.0 (frontend_handoff/API_CONTRACT.md).
 * Hand-written from the live OpenAPI (openapi.json) and the reply contract; pinned by the backend contract test.
 */

export const CONTRACT_VERSION = '1.0'
export const CONTRACT_HEADER = 'X-Contract-Version'

/** Typed text or a button press, in one chat. */
export interface ChatRequest {
  action: ChatAction
  /** required for CUSTOM_QUERY, <= 1000 chars, non-empty */
  message?: string
  /** one id per chat window, persisted, sent on every message of that chat */
  chat_id: string
  reply_language?: 'en'
  new_chat?: boolean
  /** only when the UI has a case selected */
  case_id?: string
  applicant_id?: string
  /** a link's href posted back instead of /fos/action */
  action_link?: string
}

export type ChatAction =
  | 'CUSTOM_QUERY'
  | 'LIST_CASES'
  | 'OPEN_CASE'
  | 'EXIT_CASE'
  | 'NEW_CASE'
  | 'RAISE_QUERY'
  | 'LIST_QUERIES'
  | 'VIEW_DOCUMENT'
  | 'UPLOAD_DOCUMENT'

/** Every chat reply. Nothing else is part of the contract. */
export interface ChatReply {
  request_id: string
  markdown: string
  tts: string
}

/** POST /api/v1/fos/action */
export interface ActionRequest {
  href: string
  chat_id?: string
  reply_language?: 'en'
}

export type ActionResult =
  | { type: 'open_ui'; route: string }
  | { type: 'upload'; document_type: string | null; party: string | null; post_to: string }
  | { type: 'copy'; ref: string }
  | ({ type: 'reply' } & ChatReply)
  | { type: 'file'; blob: Blob; filename: string; inline: boolean }

/** Server-sent events of POST /api/v1/fos/copilot/stream, in order. */
export type StreamEvent =
  | { event: 'typing'; data: { request_id: string; ms: number } }
  | { event: 'status'; data: { request_id: string; text: string; ms: number } }
  | { event: 'delta'; data: { request_id: string; markdown: string } }
  | { event: 'final'; data: ChatReply }
  | { event: 'cancelled'; data: { request_id: string; ms: number } }
  | { event: 'error'; data: { request_id: string; status: number; detail: unknown } }

/** Every error body. Show error.message (written for a person); retry only when retryable. */
export interface ApiErrorBody {
  detail?: unknown
  error: { code: string; message: string; retryable: boolean; request_id: string }
}

export interface LoginRequest {
  username: string
  password: string
  stage?: string
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  expires_in: number
  token_type?: string
  stage?: string | null
}

/** Links inside markdown. */
export const ASK_PREFIX = 'ask:'
export const ACTION_PREFIX = 'action:'
