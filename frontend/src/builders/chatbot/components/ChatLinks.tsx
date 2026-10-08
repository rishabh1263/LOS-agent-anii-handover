/**
 * ChatMarkdown -- renders the bot's reply markdown and makes every link work (typed port of
 * LOS-agentic-ai/docs/frontend/ChatLinks.jsx). No markdown library: the same line-based approach as
 * MessageBubble's renderContent, extended with what the bot emits -- links, tables, numbered lists, quotes.
 *
 *   [Label](ask:<text>)        -> onAsk(<text>)       (sent as the next chat message)
 *   [Label](action:<name>?...) -> POST /api/v1/fos/action, then file / onOpenUi / onUpload / onReply
 *   [Label](https://...)       -> a normal link, new tab; any other scheme is shown as text
 *
 * The server checks the user's access on every action; this component never decides scope.
 */

import { useState, type ReactNode } from 'react'
import {
  askText,
  copyIndex,
  deliverFile,
  isActionLink,
  isAskLink,
  isSafeUrl,
  runChatAction,
  type ContractReply,
  type OpenUiResult,
  type UploadResult,
} from '../../../runtime/chatbot/api/chatActions'
import { ChatApiError } from '../../../runtime/chatbot/api/client'

export interface ChatMarkdownProps {
  markdown: string
  /** The user's access token (the same one the chat requests send) */
  token?: string
  chatId?: string
  lang?: string
  /** An `ask:` link: send this text as the next chat message */
  onAsk: (text: string) => void
  /** An action answered with a chat reply (open_case, list_more, confirm_write ...): append it */
  onReply?: (reply: ContractReply) => void
  /** show_in_ui / show_list_in_ui / new_case: open a screen of this app */
  onOpenUi?: (route: OpenUiResult['route']) => void
  /** upload: open a file picker, then POST multipart to `post_to` */
  onUpload?: (target: UploadResult) => void
  /** The server's plain message (401 / 403 / 404 / 409 / 422 / 429) */
  onError?: (message: string, status: number) => void
  /** Links stay inert (e.g. while a reply is streaming) */
  disabled?: boolean
}

/** The link's href and the clicked chip (its reply's own quotes are found from it) */
type LinkHandler = (href: string, el: HTMLElement) => void

// --------------------------------------------------------------------------
// inline: links, bold, code
// --------------------------------------------------------------------------

const INLINE = /(\[[^\]]+\]\([^)\s]+\)|\*\*[^*]+\*\*|`[^`]+`)/g
const LINK = /^\[([^\]]+)\]\(([^)\s]+)\)$/

function chipClass(action: boolean): string {
  return `mx-0.5 my-0.5 inline-flex max-w-full cursor-pointer items-center rounded-full border px-2.5 py-0.5 font-sans text-[12px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-40 [overflow-wrap:anywhere] ${
    action
      ? 'border-ember/40 bg-ember/10 text-ember hover:bg-ember/20'
      : 'border-line bg-surface text-content-secondary hover:border-ember/40 hover:text-content'
  }`
}

function inline(text: string, onLink: LinkHandler, disabled: boolean, keyBase: string): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    const key = `${keyBase}-${i}`
    const link = LINK.exec(part)
    if (link) {
      const [, label, href] = link
      if (isAskLink(href) || isActionLink(href)) {
        return (
          <button key={key} type="button" disabled={disabled} className={chipClass(isActionLink(href))}
                  onClick={(e) => onLink(href, e.currentTarget)}>
            {label}
          </button>
        )
      }
      if (isSafeUrl(href)) {
        return (
          <a key={key} href={href} target="_blank" rel="noopener noreferrer" className="text-ember underline">
            {label}
          </a>
        )
      }
      return <span key={key}>{label}</span>
    }
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={key} className="font-medium">{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={key} className="rounded bg-black/5 px-1 py-0.5 font-mono text-[13px] dark:bg-white/10">
          {part.slice(1, -1)}
        </code>
      )
    }
    return part
  })
}

// --------------------------------------------------------------------------
// blocks: code fence, table, quote, lists, heading, paragraph
// --------------------------------------------------------------------------

function cells(row: string): string[] {
  return row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
}

const SEPARATOR = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/

function blocks(markdown: string, onLink: LinkHandler, disabled: boolean): ReactNode[] {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n')
  const out: ReactNode[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const key = `b-${i}`

    if (line.startsWith('```')) {
      const code: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) code.push(lines[i++])
      i++
      out.push(
        <pre key={key} className="my-2 overflow-x-auto rounded-lg border border-line bg-raised p-3 font-mono text-[12px] leading-relaxed text-content">
          {code.join('\n')}
        </pre>,
      )
      continue
    }

    if (line.trim().startsWith('|') && i + 1 < lines.length && SEPARATOR.test(lines[i + 1].trim())) {
      const head = cells(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && lines[i].trim().startsWith('|')) rows.push(cells(lines[i++]))
      out.push(
        <div key={key} className="my-2 max-w-full overflow-x-auto rounded-lg border border-line">
          <table className="w-full border-collapse text-left text-[13px]">
            <thead className="bg-raised">
              <tr>
                {head.map((h, c) => (
                  <th key={c} className="border-b border-line px-2.5 py-1.5 font-medium">{inline(h, onLink, disabled, `${key}-h${c}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r} className="border-b border-line last:border-b-0">
                  {row.map((cell, c) => (
                    <td key={c} className="px-2.5 py-1.5 align-top">{inline(cell, onLink, disabled, `${key}-${r}-${c}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    if (line.startsWith('>')) {
      const quote: string[] = []
      while (i < lines.length && lines[i].startsWith('>')) quote.push(lines[i++].replace(/^>\s?/, ''))
      out.push(
        <blockquote key={key} className="my-2 whitespace-pre-wrap border-l-2 border-ember/40 pl-3 text-content-secondary">
          {quote.join('\n')}
        </blockquote>,
      )
      continue
    }

    const bullet = /^\s*(?:[-*•])\s+(.*)$/.exec(line)
    const numbered = /^\s*(\d+)[.)]\s+(.*)$/.exec(line)
    if (bullet || numbered) {
      const ordered = Boolean(numbered)
      const items: string[] = []
      const start = numbered ? Number(numbered[1]) : 1
      while (i < lines.length) {
        const m = ordered ? /^\s*\d+[.)]\s+(.*)$/.exec(lines[i]) : /^\s*(?:[-*•])\s+(.*)$/.exec(lines[i])
        if (!m) break
        items.push(m[1])
        i++
      }
      const lis = items.map((item, n) => (
        <li key={n} className="break-words [overflow-wrap:anywhere]">{inline(item, onLink, disabled, `${key}-${n}`)}</li>
      ))
      out.push(
        ordered ? (
          <ol key={key} start={start} className="ml-5 list-decimal space-y-0.5">{lis}</ol>
        ) : (
          <ul key={key} className="ml-5 list-disc space-y-0.5">{lis}</ul>
        ),
      )
      continue
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      out.push(
        <p key={key} className="mt-2 break-words font-medium text-content [overflow-wrap:anywhere]">
          {inline(heading[2], onLink, disabled, key)}
        </p>,
      )
    } else if (line.trim() === '') {
      out.push(<div key={key} className="h-1.5" />)
    } else {
      out.push(
        <p key={key} className="break-words leading-[1.55] [overflow-wrap:anywhere]">
          {inline(line, onLink, disabled, key)}
        </p>,
      )
    }
    i++
  }
  return out
}

// --------------------------------------------------------------------------
// the component
// --------------------------------------------------------------------------

export function ChatMarkdown({
  markdown,
  token,
  chatId,
  lang,
  onAsk,
  onReply,
  onOpenUi,
  onUpload,
  onError,
  disabled = false,
}: ChatMarkdownProps) {
  const [busy, setBusy] = useState(false)

  const copyQuote = async (el: HTMLElement, n: number) => {
    const quote = el.closest('[data-chat-markdown]')?.querySelectorAll('blockquote')[n - 1]
    if (quote) await navigator.clipboard?.writeText(quote.textContent ?? '')
  }

  const handle = async (href: string, el: HTMLElement) => {
    if (isAskLink(href)) {
      onAsk(askText(href))
      return
    }
    if (!isActionLink(href) || busy) return
    if (href.startsWith('action:copy')) {
      // copied locally from this reply's quotes; no server call is needed
      await copyQuote(el, copyIndex(new URLSearchParams(href.split('?')[1] ?? '').get('ref') ?? 'draft-1'))
      return
    }
    setBusy(true)
    try {
      const result = await runChatAction(href, { token, chatId, lang })
      switch (result.type) {
        case 'file':
          deliverFile(result)
          break
        case 'open_ui':
          onOpenUi?.(result.route)
          break
        case 'upload':
          onUpload?.(result)
          break
        case 'copy':
          await copyQuote(el, copyIndex(result.ref))
          break
        case 'reply':
          onReply?.({ request_id: result.request_id, markdown: result.markdown, tts: result.tts })
          break
      }
    } catch (err) {
      if (err instanceof ChatApiError) onError?.(err.message, err.status)
      else onError?.('The action could not be completed. Please try again.', 0)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div data-chat-markdown="" className="min-w-0 space-y-0.5 break-words [overflow-wrap:anywhere]" aria-busy={busy}>
      {blocks(markdown, (href, el) => void handle(href, el), disabled || busy)}
    </div>
  )
}

export default ChatMarkdown
