/**
 * Typed client for the LOS chatbot contract 1.1 (frontend_handoff/API_CONTRACT.md). No dependencies.
 *
 *   const chat = new ChatClient({ getToken, refresh })
 *   const reply = await chat.send(chatId, 'what is pending')        // { request_id, markdown, tts }
 *   for await (const ev of chat.stream(chatId, 'docs')) { ... }      // typing / status / delta / final
 *   await chat.feedback(reply.request_id, 'down', 'wrong document')   // 👍 / 👎, same endpoint
 */

import {
  ACTION_PREFIX,
  CONTRACT_HEADER,
  CONTRACT_VERSION,
  type ActionResult,
  type ApiErrorBody,
  type ChatReply,
  type ChatRequest,
  type FeedbackRequest,
  type FeedbackResult,
  type StreamEvent,
} from './contract'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean
  readonly requestId: string | undefined

  constructor(status: number, body: Partial<ApiErrorBody>) {
    super(body.error?.message || `Request failed (${status})`)
    this.name = 'ApiError'
    this.status = status
    this.code = body.error?.code || 'ERROR'
    this.retryable = Boolean(body.error?.retryable)
    this.requestId = body.error?.request_id
  }
}

export interface ChatClientOptions {
  /** '' = same origin (the Vite proxy) */
  baseUrl?: string
  getToken: () => string | undefined
  /** refresh the access token once after a 401; return false to send the user to sign-in */
  refresh?: () => Promise<boolean>
  onContractMismatch?: (serverVersion: string) => void
}

export class ChatClient {
  private readonly base: string
  private warned = false

  constructor(private readonly opts: ChatClientOptions) {
    this.base = (opts.baseUrl ?? '').replace(/\/$/, '')
  }

  /** One typed message in one chat. */
  send(chatId: string, message: string, extra: Partial<ChatRequest> = {}, signal?: AbortSignal): Promise<ChatReply> {
    const body: ChatRequest = {
      action: 'CUSTOM_QUERY', message, chat_id: chatId, reply_language: 'en', with_sources: true, ...extra,
    }
    return this.json<ChatReply>('/api/v1/fos/copilot', body, signal)
  }

  /** A clicked `action:` link. Files come back as { type: 'file' }. */
  async action(href: string, chatId: string, signal?: AbortSignal): Promise<ActionResult> {
    if (!href.startsWith(ACTION_PREFIX)) throw new Error('not an action link')
    const res = await this.fetch('/api/v1/fos/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ href, chat_id: chatId, reply_language: 'en' }),
      signal,
    })
    const type = res.headers.get('content-type') ?? ''
    if (!type.includes('application/json')) {
      const disposition = res.headers.get('content-disposition') ?? ''
      const filename = /filename="?([^";]+)"?/i.exec(disposition)?.[1] ?? 'download'
      return { type: 'file', blob: await res.blob(), filename, inline: disposition.toLowerCase().startsWith('inline') }
    }
    return (await res.json()) as ActionResult
  }

  /** Upload files to the chat (the upload link's post_to, or the plain endpoint). */
  async upload(chatId: string, files: File[], documentTypes: string[] = [], postTo = '/api/v1/fos/copilot'):
    Promise<ChatReply> {
    const form = new FormData()
    form.append('action', 'UPLOAD_DOCUMENT')
    form.append('chat_id', chatId)
    files.forEach((file, i) => {
      form.append('files', file)
      form.append('document_types', documentTypes[i] ?? '')
    })
    const res = await this.fetch(postTo, { method: 'POST', body: form })
    return (await res.json()) as ChatReply
  }

  /** The streamed reply: typing -> status? -> delta* -> final (or cancelled / error). */
  async *stream(chatId: string, message: string, signal?: AbortSignal): AsyncGenerator<StreamEvent> {
    const res = await this.fetch('/api/v1/fos/copilot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({
        action: 'CUSTOM_QUERY', message, chat_id: chatId, reply_language: 'en', stream: true, with_sources: true,
      }),
      signal,
    })
    const reader = res.body?.getReader()
    if (!reader) return
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let cut = buffer.indexOf('\n\n')
      while (cut >= 0) {
        const block = buffer.slice(0, cut)
        buffer = buffer.slice(cut + 2)
        const event = /^event: (.+)$/m.exec(block)?.[1]?.trim()
        const data = /^data: (.+)$/m.exec(block)?.[1]
        if (event && data) yield { event, data: JSON.parse(data) } as StreamEvent
        cut = buffer.indexOf('\n\n')
      }
    }
  }

  /** 👍 / 👎 on a reply (its request_id). Not a chat reply: never append the result to the chat. */
  feedback(replyTo: string, rating: 'up' | 'down', reason?: string, chatId?: string): Promise<FeedbackResult> {
    const body: FeedbackRequest = { action: 'FEEDBACK', reply_to: replyTo, rating, reason, chat_id: chatId }
    return this.json<FeedbackResult>('/api/v1/fos/copilot', body)
  }

  stop(chatId: string): Promise<unknown> {
    return this.json('/api/v1/fos/copilot/stop', { chat_id: chatId })
  }

  replay(requestId: string): Promise<ChatReply> {
    return this.fetch(`/api/v1/fos/copilot/replay/${encodeURIComponent(requestId)}`, { method: 'GET' })
      .then((r) => r.json() as Promise<ChatReply>)
  }

  /**
   * Open a case (the UI form: POST /api/v1/fos/applicants), double-submit safe.
   *
   * Pass the SAME `submission` on a retry (double click, timeout, network error): the server returns the case the
   * first call created (`replayed: true`), never a second case. Build it with `caseSubmission(previous, body)`: the
   * key is kept while the body is unchanged and renewed when the officer edits the form or starts a new case. A key
   * reused with a different body is refused 409 IDEMPOTENCY_KEY_REUSED (ApiError) -- never a wrong case.
   */
  async createCase(submission: CaseSubmission, signal?: AbortSignal): Promise<CreateCaseResult> {
    const res = await this.fetch('/api/v1/fos/applicants', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': submission.key },
      body: JSON.stringify(submission.body),
      signal,
    })
    const reply = (await res.json()) as Record<string, unknown>
    return { ...reply, replayed: res.headers.get('Idempotent-Replayed') === 'true' } as CreateCaseResult
  }

  private async json<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    const res = await this.fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    })
    return (await res.json()) as T
  }

  /** fetch with the Bearer token, one refresh-and-retry on 401, the contract-version check, typed errors. */
  private async fetch(path: string, init: RequestInit, retried = false): Promise<Response> {
    const headers = new Headers(init.headers)
    const token = this.opts.getToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    const res = await fetch(`${this.base}${path}`, { ...init, headers })
    const version = res.headers.get(CONTRACT_HEADER)
    if (version && version !== CONTRACT_VERSION && !this.warned) {
      this.warned = true
      this.opts.onContractMismatch?.(version)
    }
    if (res.status === 401 && !retried && this.opts.refresh && (await this.opts.refresh())) {
      return this.fetch(path, init, true)
    }
    if (!res.ok) {
      let body: Partial<ApiErrorBody> = {}
      try {
        body = (await res.json()) as ApiErrorBody
      } catch {
        /* not JSON */
      }
      throw new ApiError(res.status, body)
    }
    return res
  }
}

/** One logical "open this case" request: the body and the Idempotency-Key that travels with it on every retry. */
export interface CaseSubmission {
  readonly key: string
  readonly body: Record<string, unknown>
}

/** The create reply, plus whether it was the earlier case returned for a retry. */
export type CreateCaseResult = Record<string, unknown> & { case_id?: string; applicant_id?: string; replayed: boolean }

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined && v !== null)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`
  }
  return JSON.stringify(value)
}

/**
 * The submission for this form body: `previous` (same key) when the body is unchanged -- a retry of the same
 * logical request -- else a new key. Call with `undefined` for a new case form; keep the result until the case is
 * created, then discard it.
 */
export function caseSubmission(previous: CaseSubmission | undefined, body: Record<string, unknown>): CaseSubmission {
  if (previous && canonical(previous.body) === canonical(body)) return previous
  return { key: crypto.randomUUID(), body: JSON.parse(JSON.stringify(body)) as Record<string, unknown> }
}
