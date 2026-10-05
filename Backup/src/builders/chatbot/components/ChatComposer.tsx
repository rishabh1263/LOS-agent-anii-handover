import { useRef, type ChangeEvent, type KeyboardEvent } from 'react'
import { AlertCircle, Mic, MicOff, Send, Square } from 'lucide-react'

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
  sendWithEnter = true,
  voiceInputEnabled = true,
  disabled,
}: ChatComposerProps) {
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

  const canSend = !disabled && !isGenerating && !isListening && !!value.trim()

  return (
    <div
      data-role="chat-composer"
      className="shrink-0 border-t border-line px-3 pt-2.5 pb-[max(0.75rem,env(safe-area-inset-bottom))]"
    >
      {voiceError && !isListening && (
        <div
          role="alert"
          className="mb-2 flex items-start gap-2.5 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5"
        >
          <AlertCircle
            className="mt-0.5 h-4 w-4 shrink-0 text-red-600"
            strokeWidth={2}
            aria-hidden
          />
          <div className="min-w-0 flex-1">
            <p className="font-sans text-[13px] leading-snug text-red-700">{voiceError}</p>
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={onStartListen}
                disabled={disabled || isGenerating}
                className="cursor-pointer rounded-md bg-white px-2.5 py-1 font-sans text-[12px] font-medium text-content shadow-sm ring-1 ring-line transition-colors hover:bg-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-40"
              >
                Try again
              </button>
              <button
                type="button"
                onClick={onDismissVoiceError}
                className="cursor-pointer rounded-md px-2 py-1 font-sans text-[12px] font-medium text-red-600 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
              >
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      {isListening && (
        <div
          className="mb-2 flex items-start gap-2.5 rounded-xl border border-blue-200 bg-blue-50 px-3 py-2.5"
          aria-live="polite"
        >
          <span className="relative mt-1.5 flex h-2 w-2 shrink-0">
            <span className="absolute inset-0 animate-ping rounded-full bg-blue-500 opacity-60" />
            <span className="relative h-2 w-2 rounded-full bg-blue-500" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="font-sans text-[13px] font-semibold text-blue-700">Listening…</div>
            <div className="mt-0.5 break-words font-sans text-[12px] leading-snug text-content-secondary">
              {interimTranscript || 'Speak clearly — transcript appears here'}
            </div>
          </div>
          <button
            type="button"
            onClick={onStopListen}
            className="shrink-0 cursor-pointer rounded-md px-2 py-1 font-sans text-[12px] font-medium text-blue-700 hover:bg-blue-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            Stop
          </button>
        </div>
      )}

      <div
        className={`flex min-w-0 items-end gap-1 rounded-2xl border bg-raised px-2 py-1.5 transition-all duration-200 focus-within:border-ember/60 focus-within:bg-surface focus-within:shadow-[0_0_0_3px_rgba(37,99,235,0.08)] focus-within:ring-1 focus-within:ring-ember/25 ${
          isListening ? 'border-blue-400 ring-1 ring-blue-400/30' : 'border-line'
        }`}
      >
        <textarea
          ref={taRef}
          value={value}
          onChange={handleInput}
          onKeyDown={handleKey}
          placeholder={isListening ? 'Listening…' : 'Ask anything…'}
          rows={1}
          disabled={disabled || isListening}
          className="max-h-[140px] min-h-[32px] min-w-0 flex-1 resize-none overflow-y-auto bg-transparent px-2 py-1.5 font-sans text-[14.5px] leading-[1.45] text-content placeholder:text-content-disabled focus:outline-none disabled:opacity-60"
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
            className={`flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-xl transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-40 ${isListening
              ? 'bg-blue-500 text-white shadow-sm'
              : 'text-content-secondary hover:bg-surface hover:text-content'
              }`}
          >
            {isListening ? (
              <MicOff className="h-4 w-4" strokeWidth={2} />
            ) : (
              <Mic className="h-4 w-4" strokeWidth={2} />
            )}
          </button>
        )}

        {isGenerating ? (
          <button
            type="button"
            aria-label="Stop generating"
            onClick={onStop}
            className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-xl bg-content text-surface transition-colors hover:opacity-90 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            <Square className="h-3 w-3 fill-current" strokeWidth={0} />
          </button>
        ) : (
          <button
            type="button"
            aria-label="Send message"
            onClick={onSend}
            disabled={!canSend}
            className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-xl bg-ember text-oncolor transition-colors hover:opacity-90 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-30"
          >
            <Send className="h-3.5 w-3.5" strokeWidth={2.2} />
          </button>
        )}
      </div>
      <p className="mt-1.5 truncate text-center font-sans text-[11px] text-content-disabled">
        {isListening
          ? 'Tap Stop or the mic when you are done speaking'
          : sendWithEnter
            ? 'Enter to send · Shift+Enter for new line'
            : 'Click send to post · Enter for new line'}
      </p>
    </div>
  )
}
