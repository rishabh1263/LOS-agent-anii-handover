import { useRef, type ChangeEvent, type KeyboardEvent } from 'react'
import { Mic, Paperclip, Send, Square, X } from 'lucide-react'
import type { ChatAttachment } from '../../../runtime/chatbot'
import { formatFileSize } from '../../../runtime/chatbot'

interface ChatComposerProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onStop: () => void
  isGenerating: boolean
  isListening: boolean
  onStartListen: () => void
  onStopListen: () => void
  attachments: ChatAttachment[]
  onAddFiles: (files: FileList | File[]) => void
  onRemoveAttachment: (id: string) => void
  /** When true, Enter sends; Shift+Enter always inserts newline. */
  sendWithEnter?: boolean
  /** When false, mic button is hidden. */
  voiceInputEnabled?: boolean
  disabled?: boolean
}

export function ChatComposer({
  value,
  onChange,
  onSend,
  onStop,
  isGenerating,
  isListening,
  onStartListen,
  onStopListen,
  attachments,
  onAddFiles,
  onRemoveAttachment,
  sendWithEnter = true,
  voiceInputEnabled = true,
  disabled,
}: ChatComposerProps) {
  const fileRef = useRef<HTMLInputElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  const handleKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter') return
    if (e.shiftKey) return // always allow Shift+Enter newline
    if (sendWithEnter) {
      e.preventDefault()
      if (!isGenerating) onSend()
    }
    // if sendWithEnter is off, Enter inserts newline (default textarea behavior)
  }

  const handleInput = (e: ChangeEvent<HTMLTextAreaElement>) => {
    onChange(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`
  }

  const canSend = !disabled && !isGenerating && (!!value.trim() || attachments.length > 0)

  return (
    <div className="shrink-0 border-t border-line bg-surface px-3 pb-3 pt-2">
      {/* Attachments preview */}
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {attachments.map((a) => (
            <div
              key={a.id}
              className="flex max-w-[220px] items-center gap-2 rounded-sm border border-line bg-raised px-2.5 py-1.5"
            >
              <span className="truncate font-sans text-[12px] font-medium text-content">
                {a.name}
              </span>
              <span className="shrink-0 font-sans text-[11px] text-content-secondary">
                {formatFileSize(a.size)}
              </span>
              <button
                type="button"
                aria-label={`Remove ${a.name}`}
                onClick={() => onRemoveAttachment(a.id)}
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded-xs text-content-secondary hover:bg-surface hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
              >
                <X className="h-3.5 w-3.5" strokeWidth={2} />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Listening banner */}
      {isListening && (
        <div className="mb-2 flex items-center gap-2 rounded-sm border border-line bg-ember-subtle px-3 py-2">
          <span className="h-2 w-2 animate-pulse rounded-full bg-ember" />
          <span className="font-sans text-[13px] font-medium text-ember-text">Listening…</span>
          <button
            type="button"
            onClick={onStopListen}
            className="ml-auto font-sans text-[12px] font-medium text-ember-text hover:underline"
          >
            Stop
          </button>
        </div>
      )}

      <div className="flex items-end gap-1.5 rounded-md border border-line bg-surface px-2 py-2 focus-within:border-ember">
        <button
          type="button"
          aria-label="Attach file"
          onClick={() => fileRef.current?.click()}
          disabled={disabled}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm text-content-secondary transition-colors hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-40"
        >
          <Paperclip className="h-[18px] w-[18px]" strokeWidth={2} />
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          className="hidden"
          accept=".pdf,.doc,.docx,.xls,.xlsx,.csv,.txt,image/*"
          onChange={(e) => {
            if (e.target.files?.length) onAddFiles(e.target.files)
            e.target.value = ''
          }}
        />

        <textarea
          ref={taRef}
          value={value}
          onChange={handleInput}
          onKeyDown={handleKey}
          placeholder="Ask anything…"
          rows={1}
          disabled={disabled || isListening}
          className="max-h-[140px] min-h-[40px] flex-1 resize-none bg-transparent py-2 font-sans text-[15px] leading-[22px] text-content placeholder:text-content-disabled focus:outline-none disabled:opacity-50"
          aria-label="Message input"
        />

        {voiceInputEnabled && (
          <button
            type="button"
            aria-label={isListening ? 'Stop listening' : 'Voice input'}
            onClick={isListening ? onStopListen : onStartListen}
            disabled={disabled || isGenerating}
            className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-40 ${
              isListening
                ? 'bg-ember text-oncolor'
                : 'text-content-secondary hover:bg-raised hover:text-content'
            }`}
          >
            <Mic className="h-[18px] w-[18px]" strokeWidth={2} />
          </button>
        )}

        {isGenerating ? (
          <button
            type="button"
            aria-label="Stop generating"
            onClick={onStop}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-ink text-on-ink transition-colors hover:bg-ink-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            <Square className="h-3.5 w-3.5 fill-current" strokeWidth={0} />
          </button>
        ) : (
          <button
            type="button"
            aria-label="Send message"
            onClick={onSend}
            disabled={!canSend}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-ink text-on-ink transition-colors hover:bg-ink-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:bg-ink-disabled disabled:text-surface"
          >
            <Send className="h-[16px] w-[16px]" strokeWidth={2.2} />
          </button>
        )}
      </div>
      <p className="mt-1.5 text-center font-sans text-[11px] text-content-disabled">
        {sendWithEnter
          ? 'Enter to send · Shift+Enter for new line'
          : 'Click send to post · Enter for new line'}
      </p>
    </div>
  )
}
