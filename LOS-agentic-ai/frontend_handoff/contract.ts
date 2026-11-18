/**
 * LOS chatbot frontend contract, version 1.1 (frontend_handoff/API_CONTRACT.md).
 * Hand-written from the live OpenAPI (openapi.json) and the reply contract; pinned by the backend contract test.
 */

export const CONTRACT_VERSION = '1.1'
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
  /** true = the reply as Server-Sent Events from this same endpoint (StreamEvent) */
  stream?: boolean
  /** true = a handbook answer also carries `sources` */
  with_sources?: boolean
}

/** Where a handbook answer came from (only with with_sources: true). No URL / page: the handbook has neither. */
export interface ChatSource {
  type: 'handbook' | 'configuration'
  /** the handbook file, e.g. "address_proof"; the configured fact's kind for type "configuration" */
  document: string
  /** a real heading of that file, or null */
  section: string | null
  /** show this as the chip */
  title: string
}

/** A 👍 / 👎 on an earlier reply -- POST /api/v1/fos/copilot. */
export interface FeedbackRequest {
  action: 'FEEDBACK'
  /** the request_id of the reply being rated */
  reply_to: string
  rating: 'up' | 'down'
  /** optional, <= 1000 chars; masked and cut by the server */
  reason?: string
  chat_id?: string
}

/** The answer to a FeedbackRequest -- not a chat reply, never shown in the chat. */
export interface FeedbackResult {
  request_id: string
  status: 'RECORDED' | 'NOT_RECORDED'
  reply_to: string
  rating: 'up' | 'down'
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
  /** only when the request sent with_sources: true AND the answer came from the handbook */
  sources?: ChatSource[]
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

/** Server-sent events of POST /api/v1/fos/copilot with stream: true, in order. */
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
