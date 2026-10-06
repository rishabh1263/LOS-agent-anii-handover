import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { ArrowLeft } from 'lucide-react'
import type { ChatSettings, SpeechGender, ChatThemeId } from '../../../runtime/chatbot'
import {
  ensureVoicesLoaded,
  listIndianLanguages,
  languageLabel,
} from '../../../runtime/chatbot'

interface SettingsProps {
  settings: ChatSettings
  onChange: (patch: Partial<ChatSettings>) => void
  onBack: () => void
  onTestVoice?: () => void
  voiceWarning?: string | null
}

const THEMES: { id: ChatThemeId; label: string; swatch: string; ring: string }[] = [
  { id: 'light', label: 'Light', swatch: '#ffffff', ring: '#2563eb' },
  { id: 'dark', label: 'Dark', swatch: '#0f0f0f', ring: '#3b82f6' },
  { id: 'orange', label: 'Orange', swatch: '#ea580c', ring: '#ea580c' },
]

const SPEED_OPTIONS = [0.85, 1, 1.15, 1.35] as const

const selectClass =
  'max-w-[11rem] h-9 rounded-lg border border-line bg-raised px-2.5 font-sans text-[13px] text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember'

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mb-5 last:mb-4">
      <h3 className="mb-2 px-0.5 font-sans text-[11px] font-semibold uppercase tracking-[0.06em] text-content-disabled">
        {title}
      </h3>
      <div className="rounded-xl border border-line bg-surface px-3.5">{children}</div>
    </section>
  )
}

function Row({
  label,
  description,
  children,
}: {
  label: string
  description?: string
  children: ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-divider py-3.5 last:border-b-0">
      <div className="min-w-0 flex-1">
        <div className="font-sans text-[13.5px] font-medium text-content">{label}</div>
        {description && (
          <div className="mt-0.5 font-sans text-[12px] leading-snug text-content-secondary">
            {description}
          </div>
        )}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative h-7 w-12 shrink-0 cursor-pointer overflow-hidden rounded-full p-0.5 transition-colors duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember focus-visible:ring-offset-2 ${
        checked ? 'bg-ember' : 'bg-neutral-300'
      }`}
    >
      <span
        className={`block h-6 w-6 rounded-full bg-white shadow-sm transition-transform duration-150 ${
          checked ? 'translate-x-5' : 'translate-x-0'
        }`}
      />
    </button>
  )
}

function pickFallbackLang(current: string, tags: string[]): string {
  if (tags.includes(current)) return current
  const primary = current.split('-')[0]?.toLowerCase()
  return tags.find((t) => t.toLowerCase().startsWith(primary)) || tags[0]
}

export function Settings({
  settings,
  onChange,
  onBack,
  onTestVoice,
  voiceWarning,
}: SettingsProps) {
  const [supportedLangs, setSupportedLangs] = useState<string[]>([])
  const [voicesReady, setVoicesReady] = useState(false)

  useEffect(() => {
    let cancelled = false
    void ensureVoicesLoaded().then((voices) => {
      if (cancelled) return
      const tags = listIndianLanguages(voices)
      setSupportedLangs(tags)
      setVoicesReady(true)
      if (tags.length > 0 && !tags.includes(settings.speechLanguage)) {
        onChange({ speechLanguage: pickFallbackLang(settings.speechLanguage, tags) })
      }
    })
    return () => {
      cancelled = true
    }
    // Mount only — do not re-run when the user changes language
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const languageOptions = useMemo(
    () => supportedLangs.map((tag) => ({ value: tag, label: languageLabel(tag) })),
    [supportedLangs],
  )

  const selectedLang =
    languageOptions.find((o) => o.value === settings.speechLanguage)?.value ??
    languageOptions[0]?.value ??
    settings.speechLanguage

  return (
    <div className="flex h-full flex-col bg-surface">
      <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2.5">
        <button
          type="button"
          onClick={onBack}
          aria-label="Back"
          className="flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg text-content-secondary hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
        >
          <ArrowLeft className="h-4 w-4" strokeWidth={2} />
        </button>
        <h2 className="font-sans text-[14px] font-semibold tracking-[-0.01em] text-content">
          Settings
        </h2>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        <Section title="Appearance">
          <Row label="Theme" description="Chatbot only">
            <div className="flex items-center gap-2" role="radiogroup" aria-label="Chat theme">
              {THEMES.map((t) => {
                const active = (settings.chatTheme || 'light') === t.id
                return (
                  <button
                    key={t.id}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    aria-label={t.label}
                    title={t.label}
                    onClick={() => onChange({ chatTheme: t.id })}
                    className={`relative h-8 w-8 cursor-pointer rounded-full border-2 transition-transform focus:outline-none focus-visible:ring-2 focus-visible:ring-ember ${
                      active ? 'scale-105 border-transparent' : 'border-line'
                    }`}
                    style={{
                      background: t.swatch,
                      boxShadow: active
                        ? `0 0 0 2px var(--cb-surface, #fff), 0 0 0 4px ${t.ring}`
                        : undefined,
                    }}
                  />
                )
              })}
            </div>
          </Row>
        </Section>

        <Section title="Voice">
          <Row label="Voice input" description="Microphone in composer">
            <Toggle
              checked={settings.voiceInput}
              onChange={(v) => onChange({ voiceInput: v })}
              label="Voice input"
            />
          </Row>
          <Row label="Auto-read responses" description="Speak AI replies automatically">
            <Toggle
              checked={settings.autoReadResponses}
              onChange={(v) => onChange({ autoReadResponses: v })}
              label="Auto-read responses"
            />
          </Row>
          <Row
            label="Auto-send on voice"
            description="Send the message when you finish speaking"
          >
            <Toggle
              checked={settings.autoSendOnVoice}
              onChange={(v) => onChange({ autoSendOnVoice: v })}
              label="Auto-send on voice"
            />
          </Row>
          <Row label="Language" description="Only languages this browser has voices for">
            <select
              value={selectedLang}
              onChange={(e) => onChange({ speechLanguage: e.target.value })}
              className={selectClass}
              aria-label="Speech language"
              disabled={!voicesReady || languageOptions.length === 0}
            >
              {!voicesReady && <option value="">Loading voices…</option>}
              {voicesReady && languageOptions.length === 0 && (
                <option value="">No voices installed</option>
              )}
              {languageOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Row>
          <Row label="Gender" description="Pitch adjusts if only one voice is installed">
            <select
              value={settings.speechGender}
              onChange={(e) => onChange({ speechGender: e.target.value as SpeechGender })}
              className={selectClass}
              aria-label="Speech gender"
            >
              <option value="female">Female</option>
              <option value="male">Male</option>
              <option value="any">Any</option>
            </select>
          </Row>
          <Row label="Speech speed">
            <select
              value={settings.speechSpeed}
              onChange={(e) => onChange({ speechSpeed: Number(e.target.value) })}
              className={selectClass}
              aria-label="Speech speed"
            >
              {SPEED_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}×
                </option>
              ))}
            </select>
          </Row>
          <div className="py-3.5">
            <button
              type="button"
              onClick={() => onTestVoice?.()}
              className="flex h-9 w-full cursor-pointer items-center justify-center rounded-lg border border-line bg-raised font-sans text-[13px] font-medium text-content transition-colors hover:bg-surface focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
            >
              Test voice
            </button>
            {voiceWarning && (
              <p className="mt-2 text-[12px] text-warning-text" role="status">
                {voiceWarning}
              </p>
            )}
          </div>
        </Section>

        <Section title="Chat">
          <Row
            label="Send with Enter"
            description="Press Enter to send (Shift+Enter for new line)"
          >
            <Toggle
              checked={settings.sendWithEnter}
              onChange={(v) => onChange({ sendWithEnter: v })}
              label="Send with Enter"
            />
          </Row>
          <Row label="Show timestamps" description="Time under each message">
            <Toggle
              checked={settings.showTimestamps}
              onChange={(v) => onChange({ showTimestamps: v })}
              label="Show timestamps"
            />
          </Row>
          <Row label="Suggested questions" description="Follow-up chips under AI replies">
            <Toggle
              checked={settings.showSuggestedQuestions}
              onChange={(v) => onChange({ showSuggestedQuestions: v })}
              label="Suggested questions"
            />
          </Row>
        </Section>
      </div>
    </div>
  )
}
