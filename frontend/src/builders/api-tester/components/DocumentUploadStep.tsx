import { memo, useMemo, useRef, useState, type ChangeEvent } from 'react'
import {
  ArrowLeft,
  CheckCircle2,
  FileText,
  Loader2,
  Search,
  Trash2,
  User,
  Users,
  AlertCircle,
  AlertTriangle,
  X,
} from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import {
  ACCEPTED_EXT,
  DOC_CATEGORIES,
  formatBytes,
  formatDocTypeLabel,
  normalizeDocType,
  type DocumentTypeHint,
  type PartyRole,
  type PartySelection,
  type VerifiedDoc,
} from '../../../runtime/api-tester'

export interface DocumentUploadStepProps {
  partySelection: PartySelection
  activeParty: PartyRole
  onActivePartyChange: (party: PartyRole) => void
  primaryDocs: VerifiedDoc[]
  coDocs: VerifiedDoc[]
  loading: boolean
  verifying: boolean
  error?: string | null
  showOtherPartyPrompt: boolean
  onUpload: (file: File, expectedType: DocumentTypeHint, partyRole: PartyRole) => Promise<void>
  onRemoveDoc: (id: string) => void
  onSwitchOtherParty: () => void
  onDismissOtherPartyPrompt: () => void
  onBack: () => void
  onRunVerification: () => void
  canRunVerification: boolean
}

const IN_FLIGHT: VerifiedDoc['status'][] = ['uploading', 'verifying', 'extracting']

function completedKeys(docs: VerifiedDoc[]): Set<string> {
  const keys = new Set<string>()
  for (const d of docs) {
    if (d.status !== 'success') continue
    const exp = normalizeDocType(d.item.expectedType)
    if (exp && exp !== 'AUTO') keys.add(exp)
    const det = normalizeDocType(d.detectedType)
    if (det) keys.add(det)
  }
  return keys
}

function isTypeDone(value: DocumentTypeHint, done: Set<string>): boolean {
  if (value === 'AUTO') return false
  const n = normalizeDocType(value)
  if (done.has(n)) return true
  const base = n.replace(/_SIGNATURE$/, '')
  for (const k of done) {
    if (k === base || k.replace(/_SIGNATURE$/, '') === base) return true
  }
  return false
}

/** Map system-detected type string → selectable DocumentTypeHint */
function resolveHint(detected: string | null | undefined): DocumentTypeHint | null {
  if (!detected) return null
  const n = normalizeDocType(detected)
  for (const cat of DOC_CATEGORIES) {
    for (const t of cat.types) {
      if (
        normalizeDocType(t.value) === n ||
        normalizeDocType(t.short) === n ||
        normalizeDocType(t.label) === n
      ) {
        return t.value
      }
    }
  }
  if (n.includes('PAN')) return 'PAN'
  if (n.includes('VOTER')) return 'VOTER_ID'
  if (n.includes('PASSPORT')) return 'PASSPORT'
  if (n.includes('DRIVING') || n.includes('DL')) return 'DRIVING_LICENCE'
  if (n.includes('BANK_STATEMENT') || n.includes('BANK')) return 'BANK_STATEMENT'
  if (n.includes('ITR')) return 'ITR'
  if (n.includes('SALARY')) return 'SALARY_SLIP'
  if (n.includes('SALE')) return 'SALE_DEED'
  if (n.includes('AADHAAR') || n.includes('AADHAR') || n.includes('UIDAI')) return 'AUTO'
  return null
}

function stageLabel(doc: VerifiedDoc): string {
  if (doc.status === 'uploading' || doc.status === 'verifying') return 'VERIFY…'
  if (doc.status === 'extracting') return 'EXTRACT…'
  if (doc.status === 'success') {
    return doc.detectedType ? `OK · ${formatDocTypeLabel(doc.detectedType)}` : 'OK'
  }
  if (doc.status === 'type_mismatch') return 'Type mismatch'
  return doc.error || 'Failed'
}

function progressOf(doc: VerifiedDoc): number {
  if (typeof doc.progress === 'number') return doc.progress
  if (doc.status === 'success' || doc.status === 'error' || doc.status === 'type_mismatch') return 100
  if (doc.status === 'extracting') return 55
  return 15
}

const DocTypeChip = memo(function DocTypeChip({
  label,
  disabled,
  done,
  onPick,
}: {
  label: string
  disabled: boolean
  done: boolean
  onPick: () => void
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onPick}
      aria-pressed={done}
      title={done ? `${label} — already uploaded` : `Upload ${label}`}
      className={[
        'inline-flex items-center gap-1.5 rounded-xs border px-3 py-2 text-[12px] font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-ember',
        done
          ? 'border-success/50 bg-success-subtle text-success-text'
          : 'border-line bg-surface text-content hover:border-ember hover:bg-ember-tint/30',
        disabled ? 'cursor-not-allowed opacity-50' : '',
      ].join(' ')}
    >
      {done && <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" aria-hidden />}
      {label}
    </button>
  )
})

function ProgressBar({ value, status }: { value: number; status: VerifiedDoc['status'] }) {
  const pct = Math.max(0, Math.min(100, value))
  const bad = status === 'error' || status === 'type_mismatch'
  const ok = status === 'success'
  return (
    <div className="mt-1.5 flex items-center gap-2">
      <div
        className="h-1.5 flex-1 overflow-hidden rounded-full bg-raised"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={`h-full rounded-full transition-[width] duration-300 ${
            bad ? 'bg-danger' : ok ? 'bg-success' : 'bg-ember'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span
        className={`min-w-[2.5rem] text-right text-[11px] font-semibold tabular-nums ${
          bad ? 'text-danger' : ok ? 'text-success' : 'text-content-secondary'
        }`}
      >
        {pct}%
      </span>
    </div>
  )
}

const DocRow = memo(function DocRow({
  doc,
  onRemove,
  onRetryAs,
}: {
  doc: VerifiedDoc
  onRemove: () => void
  onRetryAs?: (hint: DocumentTypeHint) => void
}) {
  const busy = IN_FLIGHT.includes(doc.status)
  const mismatch = doc.status === 'type_mismatch'
  const failed = doc.status === 'error'
  const selected = formatDocTypeLabel(doc.item.expectedType)
  const detected = doc.detectedType ? formatDocTypeLabel(doc.detectedType) : null
  const retryHint = mismatch ? resolveHint(doc.detectedType) : null

  const icon = busy ? (
    <Loader2 className="h-4 w-4 animate-spin text-ember" />
  ) : doc.status === 'success' ? (
    <CheckCircle2 className="h-4 w-4 text-success" />
  ) : mismatch ? (
    <AlertTriangle className="h-4 w-4 text-warning" />
  ) : failed ? (
    <AlertCircle className="h-4 w-4 text-danger" />
  ) : (
    <FileText className="h-4 w-4 text-content-secondary" />
  )

  return (
    <div
      className={[
        'rounded-sm border px-3 py-2.5',
        mismatch
          ? 'border-warning/40 bg-warning-subtle/40'
          : failed
            ? 'border-danger/30 bg-danger-subtle/30'
            : 'border-line bg-raised/40',
      ].join(' ')}
      role={mismatch || failed ? 'alert' : undefined}
    >
      <div className="flex items-center gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xs bg-raised">
          {icon}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium text-content">{doc.item.file.name}</p>
          <p className="text-[11px] text-content-secondary">
            Selected: {selected} · {formatBytes(doc.item.file.size)} ·{' '}
            <span
              className={
                doc.status === 'success'
                  ? 'text-success'
                  : mismatch
                    ? 'font-semibold text-warning'
                    : failed
                      ? 'text-danger'
                      : ''
              }
            >
              {stageLabel(doc)}
            </span>
          </p>
        </div>
        {!busy && (
          <button
            type="button"
            onClick={onRemove}
            className="rounded-xs p-1.5 text-content-secondary hover:bg-raised hover:text-danger"
            aria-label={`Remove ${doc.item.file.name}`}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <ProgressBar value={progressOf(doc)} status={doc.status} />

      {/* System profile vs EXTRACT — shown right after extraction succeeds */}
      {doc.status === 'success' && doc.profileMatches && doc.profileMatches.length > 0 && (
        <div className="mt-2 space-y-1.5 border-t border-line-divider pt-2">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-content-secondary">
            Profile vs extraction
          </p>
          <ul className="space-y-1">
            {doc.profileMatches.map((m) => (
              <li
                key={m.key}
                className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[12px]"
              >
                <span className="min-w-[4.5rem] font-medium text-content">{m.label}</span>
                <span
                  className={
                    m.status === 'match'
                      ? 'font-semibold text-success'
                      : m.status === 'mismatch'
                        ? 'font-semibold text-danger'
                        : 'text-content-secondary'
                  }
                >
                  {m.status === 'match'
                    ? 'Match'
                    : m.status === 'mismatch'
                      ? 'Mismatch'
                      : 'Not in doc'}
                </span>
                <span className="text-content-secondary">
                  You: {m.entered}
                  {m.extracted != null && <> · Doc: {m.extracted}</>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {mismatch && (
        <div className="mt-2 space-y-1.5 border-t border-warning/20 pt-2">
          <p className="text-[12px] text-content">
            {doc.error ||
              `Selected “${selected}” ≠ system${detected ? ` “${detected}”` : ''}.`}
          </p>
          <div className="flex flex-wrap gap-2 text-[11px]">
            <span className="rounded-xs border border-line bg-surface px-2 py-0.5 text-content-secondary">
              Selected: <strong className="text-content">{selected}</strong>
            </span>
            {detected && (
              <span className="rounded-xs border border-warning/40 bg-warning-subtle px-2 py-0.5 text-warning">
                Detected: <strong>{detected}</strong>
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap gap-2">
            {retryHint && onRetryAs && (
              <button
                type="button"
                className="btn btn-primary inline-flex items-center gap-1.5 text-[12px]"
                onClick={() => {
                  onRemove()
                  onRetryAs(retryHint)
                }}
              >
                Re-upload as {formatDocTypeLabel(retryHint)}
              </button>
            )}
            <button
              type="button"
              onClick={onRemove}
              className="btn btn-secondary inline-flex items-center gap-1.5 text-[12px]"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Remove
            </button>
          </div>
        </div>
      )}

      {failed && doc.error && (
        <p className="mt-2 border-t border-danger/20 pt-2 text-[12px] text-danger-text">
          {doc.error}
        </p>
      )}
    </div>
  )
})

function SearchableDocPicker({
  disabled,
  done,
  onPick,
}: {
  disabled: boolean
  done: Set<string>
  onPick: (type: DocumentTypeHint) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const options = useMemo(
    () =>
      DOC_CATEGORIES.flatMap((cat) =>
        cat.types.map((dt) => ({
          value: dt.value,
          label: dt.label,
          category: cat.label,
          completed: isTypeDone(dt.value, done),
        })),
      ),
    [done],
  )

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return options
    return options.filter(
      (o) =>
        o.label.toLowerCase().includes(q) ||
        o.category.toLowerCase().includes(q) ||
        String(o.value).toLowerCase().includes(q),
    )
  }, [options, query])

  return (
    <div className="relative">
      <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wider text-content-secondary">
        Quick search
      </label>
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-content-secondary" />
        <input
          ref={inputRef}
          type="search"
          value={query}
          disabled={disabled}
          placeholder="Search document type (e.g. PAN, Aadhaar)…"
          onChange={(e) => {
            setQuery(e.target.value)
            setOpen(true)
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => window.setTimeout(() => setOpen(false), 150)}
          className="w-full rounded-xs border border-line bg-surface py-2 pl-9 pr-9 text-[13px] text-content placeholder:text-content-secondary focus:border-ember focus:outline-none focus:ring-2 focus:ring-ember/30 disabled:opacity-50"
        />
        {query && (
          <button
            type="button"
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-content-secondary hover:text-content"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              setQuery('')
              inputRef.current?.focus()
            }}
            aria-label="Clear search"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <AnimatePresence>
        {open && filtered.length > 0 && (
          <motion.ul
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.15 }}
            className="absolute z-20 mt-1 max-h-56 w-full overflow-auto rounded-sm border border-line bg-surface shadow-lg"
            role="listbox"
          >
            {filtered.map((opt, i) => (
              <li key={`${opt.category}-${opt.value}-${opt.label}-${i}`}>
                <button
                  type="button"
                  role="option"
                  aria-selected={opt.completed}
                  disabled={disabled}
                  className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-[13px] hover:bg-ember-tint/40 ${
                    opt.completed ? 'bg-success-subtle/60' : ''
                  }`}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => {
                    onPick(opt.value)
                    setQuery('')
                    setOpen(false)
                  }}
                >
                  <span className="min-w-0">
                    <span className="font-medium text-content">{opt.label}</span>
                    <span className="ml-2 text-[11px] text-content-secondary">{opt.category}</span>
                  </span>
                  {opt.completed && (
                    <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-success">
                      <CheckCircle2 className="h-3 w-3" />
                      Done
                    </span>
                  )}
                </button>
              </li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  )
}

export function DocumentUploadStep({
  partySelection,
  activeParty,
  onActivePartyChange,
  primaryDocs,
  coDocs,
  loading,
  verifying,
  error,
  showOtherPartyPrompt,
  onUpload,
  onRemoveDoc,
  onSwitchOtherParty,
  onDismissOtherPartyPrompt,
  onBack,
  onRunVerification,
  canRunVerification,
}: DocumentUploadStepProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [pendingType, setPendingType] = useState<DocumentTypeHint | null>(null)

  const activeDocs = activeParty === 'PRIMARY_APPLICANT' ? primaryDocs : coDocs
  const bothSelected = partySelection.applicant && partySelection.coApplicant
  const lock = verifying
  const done = useMemo(() => completedKeys(activeDocs), [activeDocs])

  const counts = useMemo(() => {
    let complete = 0
    let mismatch = 0
    let failed = 0
    let busy = 0
    for (const d of activeDocs) {
      if (d.status === 'success') complete++
      else if (d.status === 'type_mismatch') mismatch++
      else if (d.status === 'error') failed++
      else if (IN_FLIGHT.includes(d.status)) busy++
    }
    return { complete, mismatch, failed, busy }
  }, [activeDocs])

  const openPicker = (type: DocumentTypeHint) => {
    if (lock) return
    setPendingType(type)
    if (inputRef.current) inputRef.current.value = ''
    inputRef.current?.click()
  }

  // Fire each file independently — no await, so VERIFY/EXTRACT runs in parallel
  const onFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    const type = pendingType
    setPendingType(null)
    if (!files?.length || !type) return
    for (let i = 0; i < files.length; i++) {
      void onUpload(files[i], type, activeParty)
    }
  }

  return (
    <section className="space-y-4" aria-labelledby="step-docs-title">
      <div className="card space-y-5">
        <div>
          <h2 id="step-docs-title" className="font-display text-[18px] font-semibold text-content">
            Upload documents
          </h2>
          <p className="mt-1 text-[13px] text-content-secondary">
            Pick the correct document type first, then select one or more files. Uploads run in
            parallel (VERIFY → EXTRACT). Wrong type is rejected — re-upload under the detected
            type. Completed types stay highlighted. Run verification runs PROCESS for the full report.
          </p>
        </div>

        {bothSelected ? (
          <div className="flex gap-2">
            {(
              [
                ['PRIMARY_APPLICANT', 'Applicant', User, primaryDocs.length],
                ['CO_APPLICANT', 'Co-applicant', Users, coDocs.length],
              ] as const
            ).map(([role, label, Icon, n]) => (
              <button
                key={role}
                type="button"
                onClick={() => onActivePartyChange(role)}
                className={`inline-flex items-center gap-2 rounded-xs px-3 py-2 text-[13px] font-semibold transition ${
                  activeParty === role
                    ? 'bg-ember text-oncolor'
                    : 'bg-raised text-content-secondary hover:bg-raised-hover'
                }`}
              >
                <Icon className="h-4 w-4" />
                {label}
                {n > 0 && (
                  <span className="rounded-full bg-oncolor/20 px-1.5 text-[11px] tabular-nums">
                    {n}
                  </span>
                )}
              </button>
            ))}
          </div>
        ) : (
          <p className="inline-flex items-center gap-2 text-[13px] font-medium text-content">
            {activeParty === 'PRIMARY_APPLICANT' ? (
              <User className="h-4 w-4 text-content-secondary" />
            ) : (
              <Users className="h-4 w-4 text-content-secondary" />
            )}
            {activeParty === 'PRIMARY_APPLICANT' ? 'Applicant' : 'Co-applicant'}
          </p>
        )}

        <SearchableDocPicker disabled={lock} done={done} onPick={openPicker} />

        <div className="space-y-4">
          {DOC_CATEGORIES.map((cat) => {
            const covered = cat.types.some((dt) => isTypeDone(dt.value, done))
            return (
              <div key={cat.id}>
                <div className="mb-2 flex items-center gap-2">
                  <p className="text-[11px] font-bold uppercase tracking-wider text-content-secondary">
                    {cat.label}
                  </p>
                  {covered && (
                    <span className="rounded-full bg-success-subtle px-1.5 py-0.5 text-[10px] font-semibold text-success">
                      Covered
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap gap-2">
                  {cat.types.map((dt) => (
                    <DocTypeChip
                      key={`${cat.id}-${dt.value}-${dt.label}`}
                      label={dt.label}
                      disabled={lock}
                      done={isTypeDone(dt.value, done)}
                      onPick={() => openPicker(dt.value)}
                    />
                  ))}
                </div>
              </div>
            )
          })}
        </div>

        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED_EXT}
          multiple
          className="hidden"
          onChange={onFileChange}
        />

        {activeDocs.length > 0 && (
          <div className="space-y-2 border-t border-line-divider pt-4">
            <p className="text-[12px] font-semibold uppercase tracking-wider text-content-secondary">
              Uploaded ({activeDocs.length})
              {counts.complete > 0 && (
                <span className="ml-2 font-normal normal-case text-success">
                  · {counts.complete} complete
                </span>
              )}
              {counts.mismatch > 0 && (
                <span className="ml-2 font-normal normal-case text-warning">
                  · {counts.mismatch} mismatch
                </span>
              )}
              {counts.failed > 0 && (
                <span className="ml-2 font-normal normal-case text-danger">
                  · {counts.failed} failed
                </span>
              )}
              {counts.busy > 0 && (
                <span className="ml-2 font-normal normal-case text-content-secondary">
                  · {counts.busy} in progress
                </span>
              )}
              {loading && counts.busy === 0 && (
                <span className="ml-2 font-normal normal-case text-content-secondary">
                  · verification in progress
                </span>
              )}
            </p>

            {counts.mismatch > 0 && (
              <div
                role="alert"
                className="flex gap-2 rounded-sm border border-warning/35 bg-warning-subtle p-3 text-[13px]"
              >
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
                <div className="min-w-0 space-y-1">
                  <p className="font-semibold text-warning-text">
                    {counts.mismatch} type mismatch
                    {counts.mismatch > 1 ? 'es' : ''} — file not accepted
                  </p>
                  <p className="text-[12px] text-content-secondary">
                    Remove and re-upload under the correct type (suggested on each row).
                  </p>
                </div>
              </div>
            )}

            <div className="space-y-2">
              {activeDocs.map((d) => (
                <DocRow
                  key={d.item.id}
                  doc={d}
                  onRemove={() => onRemoveDoc(d.item.id)}
                  onRetryAs={openPicker}
                />
              ))}
            </div>
          </div>
        )}
      </div>

      <AnimatePresence>
        {showOtherPartyPrompt && bothSelected && (
          <motion.div
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="rounded-sm border border-ember/30 bg-ember-tint p-4"
            role="status"
          >
            <p className="text-[13px] font-medium text-content">
              {activeParty === 'PRIMARY_APPLICANT'
                ? 'Applicant document verified. Upload co-applicant documents?'
                : 'Co-applicant document verified. Upload applicant documents?'}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <button type="button" className="btn btn-primary" onClick={onSwitchOtherParty}>
                Yes
              </button>
              <button type="button" className="btn btn-secondary" onClick={onDismissOtherPartyPrompt}>
                Skip
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {error && (
        <div
          role="alert"
          className="rounded-sm border border-danger/30 bg-danger-subtle p-3 text-[13px] text-danger-text"
        >
          {error}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <button
          type="button"
          className="btn btn-secondary inline-flex items-center gap-1.5"
          onClick={onBack}
          disabled={verifying}
        >
          <ArrowLeft className="h-4 w-4" />
          Back
        </button>
        <button
          type="button"
          className="btn btn-primary min-w-[160px]"
          disabled={!canRunVerification || verifying}
          onClick={onRunVerification}
        >
          {verifying ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" />
              Running verification…
            </>
          ) : (
            'Run verification'
          )}
        </button>
      </div>
    </section>
  )
}
