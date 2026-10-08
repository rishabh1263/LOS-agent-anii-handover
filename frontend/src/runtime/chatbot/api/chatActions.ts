/**
 * The chat reply contract + the ONE action endpoint (backend: MASTER SPEC section 8, FINAL FIX D).
 *
 * Every reply of /fos/copilot and /copilot/query is { request_id, markdown, tts }. Links inside the markdown:
 *   [Label](ask:<text>)       -> send <text> as the next chat message (no server call here)
 *   [Label](action:<name>?..) -> POST /api/v1/fos/action { href, chat_id, reply_language }
 *
 * The server re-checks scope on every action; the frontend never decides access.
 */

import { apiUrl, PATHS } from '../../config'
import { ChatApiError, type ChatApiErrorBody } from './client'

/** One chat reply, as both chat endpoints publish it. */
export interface ContractReply {
  request_id: string
  markdown: string
  tts: string
}

export interface OpenUiResult {
  type: 'open_ui'
  /** A route of this app, e.g. "/cases/CASE-852C1A2B3C4D" or "/cases?status=pending" */
  route: string
}

export interface UploadResult {
  type: 'upload'
  document_type: string | null
  party: string | null
  /** Where to POST the multipart upload (already carries chat_id) */
  post_to: string
}

export interface CopyResult {
  type: 'copy'
  /** "draft-N": the Nth quoted block of the reply */
  ref: string
}

export interface ReplyResult extends ContractReply {
  type: 'reply'
}

export interface FileResult {
  type: 'file'
  blob: Blob
  filename: string
  /** Content-Disposition: inline (view_document) -> open, else save */
  inline: boolean
}

export type ActionResult = OpenUiResult | UploadResult | CopyResult | ReplyResult | FileResult

export interface ActionOptions {
  token?: string
  chatId?: string
  lang?: string
  signal?: AbortSignal
}

export const ASK_PREFIX = 'ask:'
export const ACTION_PREFIX = 'action:'

export function isAskLink(href: string): boolean {
  return href.startsWith(ASK_PREFIX)
}

export function isActionLink(href: string): boolean {
  return href.startsWith(ACTION_PREFIX)
}

/** The message an `ask:` link sends. */
export function askText(href: string): string {
  try {
    return decodeURIComponent(href.slice(ASK_PREFIX.length))
  } catch {
    return href.slice(ASK_PREFIX.length)
  }
}

/** 1-based index of the quote an `action:copy?ref=draft-N` link copies. */
export function copyIndex(ref: string): number {
  const n = Number.parseInt(ref.split('-')[1] ?? '1', 10)
  return Number.isFinite(n) && n > 0 ? n : 1
}

/** Only these ordinary URLs are rendered as real links; anything else stays plain text. */
export function isSafeUrl(href: string): boolean {
  return /^(https?:|mailto:)/i.test(href)
}

function filenameOf(disposition: string): string {
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(disposition)
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))
    } catch {
      /* fall through to the plain filename */
    }
  }
  return /filename="?([^";]+)"?/i.exec(disposition)?.[1] ?? 'download'
}

/**
 * POST /api/v1/fos/action for one `action:` link.
 * Throws ChatApiError with the server's plain message on 401 / 403 / 404 / 409 / 422 / 429.
 */
export async function runChatAction(href: string, options: ActionOptions = {}): Promise<ActionResult> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (options.token?.trim()) headers.Authorization = `Bearer ${options.token.trim()}`

  const res = await fetch(apiUrl(`${PATHS.fos}/action`), {
    method: 'POST',
    headers,
    body: JSON.stringify({ href, chat_id: options.chatId ?? null, reply_language: options.lang ?? null }),
    signal: options.signal,
  })
  const type = res.headers.get('content-type') ?? ''

  if (!res.ok) {
    let body: ChatApiErrorBody = {}
    if (type.includes('json')) {
      const parsed = (await res.json().catch(() => ({}))) as { detail?: unknown } & ChatApiErrorBody
      const detail = parsed.detail
      body =
        detail && typeof detail === 'object'
          ? (detail as ChatApiErrorBody)
          : { ...parsed, detail: typeof detail === 'string' ? detail : undefined }
    }
    throw new ChatApiError(res.status, { message: `Error ${res.status}`, ...body })
  }

  if (!type.includes('application/json')) {
    const disposition = res.headers.get('content-disposition') ?? ''
    return {
      type: 'file',
      blob: await res.blob(),
      filename: filenameOf(disposition),
      inline: disposition.toLowerCase().startsWith('inline'),
    }
  }
  return (await res.json()) as ActionResult
}

/** Save (attachment) or open in a new tab (inline) a file returned by an action. */
export function deliverFile(file: FileResult): void {
  const url = URL.createObjectURL(file.blob)
  if (file.inline) {
    window.open(url, '_blank', 'noopener')
  } else {
    const a = Object.assign(document.createElement('a'), { href: url, download: file.filename })
    document.body.appendChild(a)
    a.click()
    a.remove()
  }
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000)
}
