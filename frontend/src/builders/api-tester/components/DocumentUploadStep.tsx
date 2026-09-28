import { useRef, useState, type ChangeEvent } from 'react'
import {
  ArrowLeft,
  CheckCircle2,
  FileText,
  Loader2,
  Trash2,
  User,
  Users,
  AlertCircle,
  AlertTriangle,
} from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import {
  ACCEPTED_EXT,
  DOC_CATEGORIES,
  formatBytes,
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

function DocTypeChip({
  label,
  disabled,
  onPick,
}: {
  label: string
  disabled: boolean
  onPick: () => void
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onPick}
      className="rounded-xs border border-line bg-surface px-3 py-2 text-left text-[12px] font-medium text-content transition hover:border-ember hover:bg-ember-tint/30 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-50"
    >
      {label}
    </button>
  )
}

function DocRow({ doc, onRemove }: { doc: VerifiedDoc; onRemove: () => void }) {
  const busy =
    doc.status === 'uploading' || doc.status === 'verifying' || doc.status === 'extracting'
  const progress = Math.min(100, Math.max(0, doc.progress ?? (busy ? 20 : 100)))

  const statusIcon = busy ? (
    <Loader2 className="h-4 w-4 animate-spin text-ember" />
  ) : doc.status === 'success' ? (
    <CheckCircle2 className="h-4 w-4 text-success" />
  ) : doc.status === 'review' || doc.status === 'type_mismatch' ? (
    <AlertTriangle className="h-4 w-4 text-warning" />
  ) : doc.status === 'error' ? (
    <AlertCircle className="h-4 w-4 text-danger" />
  ) : (
    <FileText className="h-4 w-4 text-content-secondary" />
  )

  const statusText = busy
    ? doc.status === 'extracting'
      ? `EXTRACT… ${progress}%`
      : `VERIFY… ${progress}%`
    : doc.status === 'success'
      ? doc.detectedType
        ? `OK · ${doc.detectedType}`
        : 'OK'
      : doc.status === 'review'
        ? doc.error || 'REVIEW — process blocked'
        : doc.status === 'type_mismatch'
          ? doc.error || 'Type mismatch'
          : doc.error || 'Failed'

  const barColor =
    doc.status === 'success'
      ? 'bg-success'
      : doc.status === 'error'
        ? 'bg-danger'
        : doc.status === 'review' || doc.status === 'type_mismatch'
          ? 'bg-warning'
          : 'bg-ember'

  return (
    <div className="overflow-hidden rounded-md border border-line bg-surface shadow-xs">
      <div className="flex items-center gap-3 px-3 py-2.5">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-raised">
          {statusIcon}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate font-sans text-[13px] font-medium text-content">
            {doc.item.file.name}
          </p>
          <p className="mt-0.5 truncate text-[11px] text-content-secondary">
            {doc.item.expectedType} · {formatBytes(doc.item.file.size)} · {statusText}
          </p>
        </div>
        {!busy && (
          <button
            type="button"
            onClick={onRemove}
            className="rounded-sm p-1.5 text-content-secondary hover:bg-raised hover:text-danger focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
            aria-label={`Remove ${doc.item.file.name}`}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      <div
        className="h-1 w-full bg-raised"
        role="progressbar"
        aria-valuenow={progress}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Document processing progress"
      >
        <div
          className={`h-full transition-[width] duration-300 ease-out ${barColor}`}
          style={{ width: `${progress}%` }}
        />
      </div>
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
  // Only final run locks the grid; uploads stay parallel
  const lockGrid = verifying

  const openPicker = (type: DocumentTypeHint) => {
    if (lockGrid) return
    setPendingType(type)
    if (inputRef.current) inputRef.current.value = ''
    inputRef.current?.click()
  }

  const onFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    const type = pendingType
    setPendingType(null)
    if (!file || !type) return
    // Fire-and-forget so another upload can start immediately
    void onUpload(file, type, activeParty)
  }

  return (
    <section className="space-y-4" aria-labelledby="step-docs-title">
      <div className="card space-y-5">
        <div>
          <h2 id="step-docs-title" className="font-display text-[18px] font-semibold text-content">
            Upload documents
          </h2>
          <p className="mt-1 text-[13px] text-content-secondary">
            Choose a document type, then select the file. On upload the API runs VERIFY (authenticity),
            then EXTRACT (fields). Selected type must match the detected type. Run verification runs
            PROCESS for the full report.
          </p>
        </div>

        {bothSelected && (
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => onActivePartyChange('PRIMARY_APPLICANT')}
              className={`inline-flex items-center gap-2 rounded-xs px-3 py-2 text-[13px] font-semibold transition ${activeParty === 'PRIMARY_APPLICANT'
                  ? 'bg-ember text-oncolor'
                  : 'bg-raised text-content-secondary hover:bg-raised-hover'
                }`}
            >
              <User className="h-4 w-4" />
              Applicant
              {primaryDocs.length > 0 && (
                <span className="rounded-full bg-oncolor/20 px-1.5 text-[11px] tabular-nums">
                  {primaryDocs.length}
                </span>
              )}
            </button>
            <button
              type="button"
              onClick={() => onActivePartyChange('CO_APPLICANT')}
              className={`inline-flex items-center gap-2 rounded-xs px-3 py-2 text-[13px] font-semibold transition ${activeParty === 'CO_APPLICANT'
                  ? 'bg-ember text-oncolor'
                  : 'bg-raised text-content-secondary hover:bg-raised-hover'
                }`}
            >
              <Users className="h-4 w-4" />
              Co-applicant
              {coDocs.length > 0 && (
                <span className="rounded-full bg-oncolor/20 px-1.5 text-[11px] tabular-nums">
                  {coDocs.length}
                </span>
              )}
            </button>
          </div>
        )}

        {!bothSelected && (
          <p className="inline-flex items-center gap-2 text-[13px] font-medium text-content">
            {activeParty === 'PRIMARY_APPLICANT' ? (
              <>
                <User className="h-4 w-4 text-content-secondary" />
                Applicant
              </>
            ) : (
              <>
                <Users className="h-4 w-4 text-content-secondary" />
                Co-applicant
              </>
            )}
          </p>
        )}

        {/* Categorized document types */}
        <div className="space-y-4">
          {DOC_CATEGORIES.map((cat) => (
            <div key={cat.id}>
              <p className="mb-2 text-[11px] font-bold uppercase tracking-wider text-content-secondary">
                {cat.label}
              </p>
              <div className="flex flex-wrap gap-2">
                {cat.types.map((dt) => (
                  <DocTypeChip
                    key={`${cat.id}-${dt.value}-${dt.label}`}
                    label={dt.label}
                    disabled={lockGrid}
                    onPick={() => openPicker(dt.value)}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>

        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED_EXT}
          className="hidden"
          onChange={onFileChange}
        />

        {activeDocs.length > 0 && (
          <div className="space-y-2 border-t border-line-divider pt-4">
            <p className="text-[12px] font-semibold uppercase tracking-wider text-content-secondary">
              Uploaded ({activeDocs.length})
              {loading && (
                <span className="ml-2 font-normal normal-case text-content-secondary">
                  · verification in progress
                </span>
              )}
            </p>
            <div className="space-y-2">
              {activeDocs.map((d) => (
                <DocRow key={d.item.id} doc={d} onRemove={() => onRemoveDoc(d.item.id)} />
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
              <button
                type="button"
                className="btn btn-secondary"
                onClick={onDismissOtherPartyPrompt}
              >
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
          disabled={!canRunVerification || verifying || loading}
          onClick={onRunVerification}
          title={
            loading
              ? 'Wait until all documents finish VERIFY / EXTRACT'
              : !canRunVerification
                ? 'Upload at least one accepted document'
                : undefined
          }
        >
          {verifying ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" />
              Running verification…
            </>
          ) : loading ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" />
              Processing docs…
            </>
          ) : (
            <span>Run verification</span>
          )}
        </button>
      </div>
    </section>
  )
}
