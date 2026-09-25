import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { ArrowLeft } from 'lucide-react'
import type { ChatSettings, SpeechGender } from '../../../runtime/chatbot'
import {
  ensureVoicesLoaded,
  listIndianLanguages,
  languageLabel,
} from '../../../runtime/chatbot'

interface SettingsProps {
  settings: ChatSettings
  onChange: (patch: Partial<ChatSettings>) => void
  onBack: () => void
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
    <div className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="font-sans text-[14px] font-medium text-content">{label}</div>
        {description && (
          <div className="mt-0.5 font-sans text-[12px] text-content-secondary">{description}</div>
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
      className={`relative h-6 w-11 shrink-0 overflow-hidden rounded-full p-0.5 transition-colors duration-140 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember focus-visible:ring-offset-2 ${
        checked ? 'bg-ember' : 'bg-line-strong'
      }`}
    >
      <span
        className={`block h-5 w-5 rounded-full bg-oncolor shadow-sm transition-transform duration-140 ${
          checked ? 'translate-x-5' : 'translate-x-0'
        }`}
      />
    </button>
  )
}

const selectClass =
  'max-w-[12rem] h-9 rounded-sm border border-line bg-surface px-2 font-sans text-[13px] text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember'

export function Settings({ settings, onChange, onBack }: SettingsProps) {
  const [indianLangs, setIndianLangs] = useState<{ tag: string; hasVoice: boolean }[]>(
    [],
  )

  useEffect(() => {
    let cancelled = false
    void ensureVoicesLoaded().then((voices) => {
      if (cancelled) return
      setIndianLangs(listIndianLanguages(voices))
    })
    return () => {
      cancelled = true
    }
  }, [])

  const languageOptions = useMemo(() => {
    const rows =
      indianLangs.length > 0
        ? indianLangs
        : listIndianLanguages([]).map((x) => ({ ...x, hasVoice: false }))
    return rows.map(({ tag }) => ({
      value: tag,
      label: languageLabel(tag),
    }))
  }, [indianLangs])

  return (
    <div className="flex h-full flex-col bg-surface">
      <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
        <button
          type="button"
          onClick={onBack}
          aria-label="Back"
          className="flex h-11 w-11 items-center justify-center rounded-sm text-content-secondary hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
        >
          <ArrowLeft className="h-[18px] w-[18px]" strokeWidth={2} />
        </button>
        <h2 className="font-display text-[16px] font-semibold tracking-[-0.015em] text-content">
          Settings
        </h2>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-2">
        <section className="mb-4">
          <h3 className="mb-1 font-sans text-[11px] font-semibold uppercase tracking-[0.08em] text-content-disabled">
            Appearance
          </h3>
          <div className="rounded-md border border-line bg-surface px-3">
            <Row label="Compact mode">
              <Toggle
                checked={settings.compactMode}
                onChange={(v) => onChange({ compactMode: v })}
                label="Compact mode"
              />
            </Row>
          </div>
        </section>

        <section className="mb-4">
          <h3 className="mb-1 font-sans text-[11px] font-semibold uppercase tracking-[0.08em] text-content-disabled">
            Voice
          </h3>
          <div className="rounded-md border border-line bg-surface px-3">
            <Row label="Voice input" description="Microphone in composer">
              <Toggle
                checked={settings.voiceInput}
                onChange={(v) => onChange({ voiceInput: v })}
                label="Voice input"
              />
            </Row>
            <div className="border-t border-divider" />
            <Row label="Auto-read responses" description="Speak AI replies automatically">
              <Toggle
                checked={settings.autoReadResponses}
                onChange={(v) => onChange({ autoReadResponses: v })}
                label="Auto-read responses"
              />
            </Row>
            <div className="border-t border-divider" />
            <Row label="Language" description="Indian languages only">
              <select
                value={settings.speechLanguage}
                onChange={(e) => onChange({ speechLanguage: e.target.value })}
                className={selectClass}
                aria-label="Speech language"
              >
                {languageOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Row>
            <div className="border-t border-divider" />
            <Row
              label="Gender"
              description="Pitch adjusts if only one voice is installed"
            >
              <select
                value={settings.speechGender}
                onChange={(e) =>
                  onChange({ speechGender: e.target.value as SpeechGender })
                }
                className={selectClass}
                aria-label="Speech gender"
              >
                <option value="female">Female</option>
                <option value="male">Male</option>
                <option value="any">Any</option>
              </select>
            </Row>
            <div className="border-t border-divider" />
            <Row label="Speech speed">
              <select
                value={settings.speechSpeed}
                onChange={(e) => onChange({ speechSpeed: Number(e.target.value) })}
                className={selectClass}
                aria-label="Speech speed"
              >
                <option value={0.85}>0.85×</option>
                <option value={1}>1×</option>
                <option value={1.15}>1.15×</option>
                <option value={1.35}>1.35×</option>
              </select>
            </Row>
          </div>
        </section>

        <section className="mb-4">
          <h3 className="mb-1 font-sans text-[11px] font-semibold uppercase tracking-[0.08em] text-content-disabled">
            Chat
          </h3>
          <div className="rounded-md border border-line bg-surface px-3">
            <Row label="Send with Enter">
              <Toggle
                checked={settings.sendWithEnter}
                onChange={(v) => onChange({ sendWithEnter: v })}
                label="Send with Enter"
              />
            </Row>
            <div className="border-t border-divider" />
            <Row label="Show timestamps">
              <Toggle
                checked={settings.showTimestamps}
                onChange={(v) => onChange({ showTimestamps: v })}
                label="Show timestamps"
              />
            </Row>
            <div className="border-t border-divider" />
            <Row label="Suggested questions">
              <Toggle
                checked={settings.showSuggestedQuestions}
                onChange={(v) => onChange({ showSuggestedQuestions: v })}
                label="Suggested questions"
              />
            </Row>
          </div>
        </section>
      </div>
    </div>
  )
}
