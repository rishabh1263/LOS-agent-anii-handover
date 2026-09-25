import { useRef, type ChangeEvent, type KeyboardEvent } from 'react'
import { AlertCircle, Mic, MicOff, Paperclip, Send, Square, X } from 'lucide-react'
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
  /** Live interim speech transcript while listening */
  interimTranscript?: string
  /** Short error from last voice attempt */
  voiceError?: string | null
  onDismissVoiceError?: () => void
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
  interimTranscript = '',
  voiceError,
  onDismissVoiceError,
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
    if (e.shiftKey) return
    if (sendWithEnter) {
      e.preventDefault()
      if (!isGenerating && !isListening) onSend()
    }
  }

  const handleInput = (e: ChangeEvent<HTMLTextAreaElement>) => {
    onChange(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`
  }

  const canSend =
    !disabled && !isGenerating && !isListening && (!!value.trim() || attachments.length > 0)

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

      {/* Mic / voice error */}
      {voiceError && !isListening && (
        <div
          role="alert"
          className="mb-2 flex items-start gap-2.5 rounded-sm border border-danger/30 bg-danger-subtle px-3 py-2.5"
        >
          <AlertCircle
            className="mt-0.5 h-4 w-4 shrink-0 text-danger-text"
            strokeWidth={2}
            aria-hidden
          />
          <div className="min-w-0 flex-1">
            <p className="font-sans text-[13px] leading-snug text-danger-text">{voiceError}</p>
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={onStartListen}
                disabled={disabled || isGenerating}
                className="rounded-sm bg-surface px-2.5 py-1 font-sans text-[12px] font-medium text-content shadow-sm ring-1 ring-line transition-colors hover:bg-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-40"
              >
                Try again
              </button>
              <button
                type="button"
                onClick={onDismissVoiceError}
                className="rounded-sm px-2 py-1 font-sans text-[12px] font-medium text-danger-text hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
              >
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Listening banner with live interim text */}
      {isListening && (
        <div
          className="mb-2 flex items-start gap-2.5 rounded-sm border border-ember/30 bg-ember-subtle px-3 py-2.5"
          aria-live="polite"
        >
          <span className="mt-1.5 flex h-2 w-2 shrink-0">
            <span className="absolute h-2 w-2 animate-ping rounded-full bg-ember opacity-60" />
            <span className="relative h-2 w-2 rounded-full bg-ember" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="font-sans text-[13px] font-semibold text-ember-text">Listening…</div>
            <div className="mt-0.5 font-sans text-[12px] leading-snug text-content-secondary">
              {interimTranscript || 'Speak clearly — transcript appears here'}
            </div>
          </div>
          <button
            type="button"
            onClick={onStopListen}
            className="shrink-0 rounded-sm px-2 py-1 font-sans text-[12px] font-medium text-ember-text hover:bg-ember/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            Stop
          </button>
        </div>
      )}

      <div
        className={`flex items-end gap-1.5 rounded-md border bg-surface px-2 py-2 transition-colors focus-within:border-ember ${
          isListening ? 'border-ember' : 'border-line'
        }`}
      >
        <button
          type="button"
          aria-label="Attach file"
          onClick={() => fileRef.current?.click()}
          disabled={disabled || isListening}
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
          placeholder={isListening ? 'Listening…' : 'Ask anything…'}
          rows={1}
          disabled={disabled || isListening}
          className="max-h-[140px] min-h-[40px] flex-1 resize-none bg-transparent py-2 font-sans text-[15px] leading-[22px] text-content placeholder:text-content-disabled focus:outline-none disabled:opacity-60"
          aria-label="Message input"
        />

        {voiceInputEnabled && (
          <button
            type="button"
            aria-label={isListening ? 'Stop listening' : 'Start voice input'}
            aria-pressed={isListening}
            onClick={isListening ? onStopListen : onStartListen}
            disabled={disabled || isGenerating}
            title={isListening ? 'Stop listening' : 'Voice input'}
            className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-40 ${
              isListening
                ? 'bg-ember text-oncolor shadow-sm'
                : 'text-content-secondary hover:bg-raised hover:text-content'
            }`}
          >
            {isListening ? (
              <MicOff className="h-[18px] w-[18px]" strokeWidth={2} />
            ) : (
              <Mic className="h-[18px] w-[18px]" strokeWidth={2} />
            )}
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
        {isListening
          ? 'Tap Stop or the mic when you are done speaking'
          : sendWithEnter
            ? 'Enter to send · Shift+Enter for new line'
            : 'Click send to post · Enter for new line'}
      </p>
    </div>
  )
}
