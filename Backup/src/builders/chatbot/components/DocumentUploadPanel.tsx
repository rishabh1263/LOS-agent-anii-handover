/**
 * In-chat document upload panel.
 *
 * Flow:
 *  1. Compact CTA under the assistant message
 *  2. Expand → one row per pending document
 *  3. Choose a file for every row, then Submit
 *  4. Show PASS / REVIEW / FAIL per row
 *
 * Rendered only when the backend provides concrete upload targets.
 */

import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Loader2,
  Paperclip,
  Upload,
  X,
} from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import type { UploadTarget } from '../../../runtime/chatbot/utils/uploadTargets'

// ── Types ──────────────────────────────────────────────────────────────────

export type DocRowStatus =
  | 'idle'
  | 'picked'
  | 'uploading'
  | 'pass'
  | 'review'
  | 'fail'
  | 'validation'

export interface DocRowState {
  slot: string
  label: string
  acceptedTypes: string[]
  status: DocRowStatus
  file?: File
  detectedType?: string
  progress?: number
  detail?: string
}

export interface DocumentUploadPanelProps {
  targets: UploadTarget[]
  onSubmit: (files: { file: File; documentType: string }[]) => Promise<{
    results: Array<{
      status: 'pass' | 'review' | 'fail' | 'validation'
      detail?: string
      detectedType?: string
    }>
  }>
  disabled?: boolean
}

const ACCEPT =
  '.pdf,.jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp,application/pdf,image/jpeg,image/png,image/bmp,image/tiff,image/webp'
const MAX_BYTES = 25 * 1024 * 1024

const PENDING_STATUSES: DocRowStatus[] = ['idle', 'picked', 'validation', 'fail']

function formatBytes(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function slotLabel(slot: string) {
  return slot
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

function rowsFromTargets(targets: UploadTarget[]): DocRowState[] {
  return targets.map((t) => ({
    slot: t.slot,
    label: t.label || slotLabel(t.slot),
    acceptedTypes: t.acceptedTypes,
    status: 'idle' as DocRowStatus,
  }))
}

function isPending(status: DocRowStatus) {
  return PENDING_STATUSES.includes(status)
}

function isReadyToUpload(row: DocRowState) {
  return Boolean(row.file && (row.status === 'picked' || row.status === 'validation'))
}

// ── Status pill ────────────────────────────────────────────────────────────

function StatusPill({ status, detail }: { status: DocRowStatus; detail?: string }) {
  // Idle is marked with a red * on the title instead of a chip
  if (status === 'idle') return null

  const cfg: Record<
    Exclude<DocRowStatus, 'idle'>,
    { label: string; icon: typeof Paperclip; className: string }
  > = {
    picked: {
      label: 'Ready',
      icon: Paperclip,
      className: 'bg-ember/10 text-ember-text ring-1 ring-ember/20',
    },
    uploading: {
      label: 'Uploading',
      icon: Loader2,
      className: 'bg-ember/10 text-ember-text ring-1 ring-ember/20',
    },
    pass: {
      label: 'Verified',
      icon: CheckCircle2,
      className: 'bg-success/10 text-success-text ring-1 ring-success/20',
    },
    review: {
      label: 'In review',
      icon: AlertTriangle,
      className: 'bg-warning/10 text-warning-text ring-1 ring-warning/25',
    },
    fail: {
      label: 'Failed',
      icon: AlertCircle,
      className: 'bg-danger/10 text-danger-text ring-1 ring-danger/20',
    },
    validation: {
      label: detail?.slice(0, 28) || 'Invalid file',
      icon: AlertCircle,
      className: 'bg-danger/10 text-danger-text ring-1 ring-danger/20',
    },
  }

  const c = cfg[status]
  const Icon = c.icon

  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10.5px] font-semibold ${c.className}`}
    >
      <Icon
        className={`h-3 w-3 shrink-0 ${status === 'uploading' ? 'animate-spin' : ''}`}
        strokeWidth={2.25}
      />
      {c.label}
    </span>
  )
}

// ── Single document row ────────────────────────────────────────────────────

function DocRow({
  row,
  onPick,
  onClear,
  disabled,
}: {
  row: DocRowState
  onPick: (file: File) => void
  onClear: () => void
  disabled?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const needsUpload = isPending(row.status)
  const isBad = row.status === 'fail' || row.status === 'validation'

  const borderClass =
    row.status === 'pass'
      ? 'border-success/25 bg-success/[0.03]'
      : isBad
        ? 'border-danger/25 bg-danger/[0.03]'
        : row.status === 'review'
          ? 'border-warning/30 bg-warning/[0.03]'
          : row.status === 'picked' || row.status === 'uploading'
            ? 'border-ember/25 bg-ember/[0.02]'
            : 'border-line bg-surface'

  return (
    <div className={`rounded-xl border px-3 py-2.5 ${borderClass}`}>
      <div className="flex items-center gap-2.5">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="truncate text-[13px] font-semibold text-content">
              {row.label || slotLabel(row.slot)}
              {needsUpload && (
                <span className="ml-0.5 font-bold text-danger" aria-label="required">
                  *
                </span>
              )}
            </p>
            <StatusPill status={row.status} detail={row.detail} />
          </div>

          {row.file ? (
            <p className="mt-0.5 truncate text-[11.5px] text-content-secondary">
              {row.file.name}
              <span className="mx-1 text-content-disabled">·</span>
              {formatBytes(row.file.size)}
              {row.detectedType && (
                <>
                  <span className="mx-1 text-content-disabled">·</span>
                  {slotLabel(row.detectedType)}
                </>
              )}
            </p>
          ) : (
            <p className="mt-0.5 text-[11.5px] text-content-secondary">
              {row.acceptedTypes.length
                ? `Accepts ${row.acceptedTypes.map(slotLabel).join(', ')}`
                : 'PDF, JPG, PNG · max 25 MB'}
            </p>
          )}

          {row.detail && (isBad || row.status === 'review' || row.status === 'pass') && (
            <p
              className={`mt-1 text-[11.5px] leading-snug ${
                row.status === 'pass'
                  ? 'text-success-text'
                  : row.status === 'review'
                    ? 'text-warning-text'
                    : 'text-danger-text'
              }`}
            >
              {row.detail}
            </p>
          )}
        </div>

        {needsUpload && (
          <div className="flex shrink-0 items-center gap-1">
            {row.file && (
              <button
                type="button"
                onClick={onClear}
                disabled={disabled}
                aria-label="Remove file"
                className="cursor-pointer rounded-lg p-1.5 text-content-secondary hover:bg-danger/10 hover:text-danger disabled:cursor-not-allowed disabled:opacity-40"
              >
                <X className="h-3.5 w-3.5" strokeWidth={2} />
              </button>
            )}
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              disabled={disabled}
              className="cursor-pointer rounded-lg border border-line bg-raised px-2.5 py-1.5 text-[12px] font-semibold text-content hover:border-ember/35 hover:text-ember-text disabled:cursor-not-allowed disabled:opacity-40"
            >
              {row.file ? 'Change' : 'Choose'}
            </button>
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              disabled={disabled}
              onChange={(e: ChangeEvent<HTMLInputElement>) => {
                const f = e.target.files?.[0]
                if (f) onPick(f)
                e.target.value = ''
              }}
            />
          </div>
        )}

        {row.status === 'pass' && (
          <CheckCircle2 className="h-5 w-5 shrink-0 text-success" strokeWidth={2} />
        )}
        {row.status === 'uploading' && (
          <Loader2 className="h-5 w-5 shrink-0 animate-spin text-ember" strokeWidth={2} />
        )}
      </div>

      {row.status === 'uploading' && (
        <div
          className="mt-2 h-1 w-full overflow-hidden rounded-full bg-raised"
          role="progressbar"
          aria-valuenow={row.progress ?? 40}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full rounded-full bg-ember transition-all duration-300"
            style={{ width: `${row.progress ?? 40}%` }}
          />
        </div>
      )}
    </div>
  )
}

// ── Main panel ─────────────────────────────────────────────────────────────

export function DocumentUploadPanel({
  targets,
  onSubmit,
  disabled = false,
}: DocumentUploadPanelProps) {
  const [expanded, setExpanded] = useState(false)
  const [rows, setRows] = useState<DocRowState[]>(() => rowsFromTargets(targets))
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const targetsKey = targets.map((t) => t.slot).join('|')
  useEffect(() => {
    if (targets.length === 0) return
    setRows(rowsFromTargets(targets))
  }, [targetsKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const pickedCount = rows.filter(isReadyToUpload).length
  const verifiedCount = rows.filter((r) => r.status === 'pass').length
  const pendingRows = rows.filter((r) => isPending(r.status))
  const allMandatoryPicked =
    pendingRows.length > 0 && pendingRows.every(isReadyToUpload)
  const canSubmit = allMandatoryPicked && !submitting && !disabled

  const updateRow = useCallback((slot: string, patch: Partial<DocRowState>) => {
    setRows((prev) => prev.map((r) => (r.slot === slot ? { ...r, ...patch } : r)))
  }, [])

  const handlePick = useCallback(
    (slot: string, file: File) => {
      if (file.size > MAX_BYTES) {
        setError(`${file.name} exceeds the 25 MB limit`)
        return
      }
      setError(null)
      updateRow(slot, {
        file,
        status: 'picked',
        detail: undefined,
        progress: undefined,
        detectedType: undefined,
      })
    },
    [updateRow],
  )

  const handleClear = useCallback(
    (slot: string) => {
      updateRow(slot, {
        file: undefined,
        status: 'idle',
        detail: undefined,
        progress: undefined,
        detectedType: undefined,
      })
    },
    [updateRow],
  )

  const handleSubmit = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)

    const toUpload = rows.filter(isReadyToUpload).map((r) => ({
      slot: r.slot,
      file: r.file!,
      // Type is optional for the user; send first accepted type or slot for the API
      documentType: r.acceptedTypes[0] || r.slot,
    }))

    toUpload.forEach((u) => updateRow(u.slot, { status: 'uploading', progress: 35 }))

    try {
      const { results } = await onSubmit(
        toUpload.map((u) => ({ file: u.file, documentType: u.documentType })),
      )

      results.forEach((res, i) => {
        const slot = toUpload[i]?.slot
        if (!slot) return
        updateRow(slot, {
          status: res.status,
          detail: res.detail,
          progress: 100,
          detectedType: res.detectedType,
        })
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed. Please try again.')
      toUpload.forEach((u) => updateRow(u.slot, { status: 'picked', progress: undefined }))
    } finally {
      setSubmitting(false)
    }
  }

  if (targets.length === 0 && rows.length === 0) return null

  // ── Collapsed CTA ──────────────────────────────────────────────────────
  if (!expanded) {
    const count = targets.length
    if (count === 0) return null

    const label =
      count === 1 ? 'Upload 1 pending document' : `Upload ${count} pending documents`

    return (
      <div className="mt-2">
        <button
          type="button"
          onClick={() => setExpanded(true)}
          disabled={disabled}
          className="flex w-full cursor-pointer items-center gap-3 rounded-xl border border-ember/20 bg-ember/[0.04] px-3 py-2.5 text-left transition hover:border-ember/35 hover:bg-ember/[0.07] focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-50"
        >
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-ember text-oncolor">
            <Upload className="h-4 w-4" strokeWidth={2.25} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex items-center gap-1.5">
              <span className="text-[13px] font-semibold text-content">{label}</span>
              <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-ember px-1.5 text-[10px] font-bold text-oncolor">
                {count}
              </span>
            </span>
            <span className="mt-0.5 block text-[11.5px] text-content-secondary">
              PDF, JPG, PNG · up to 25 MB each
            </span>
          </span>
          <ChevronDown className="h-4 w-4 shrink-0 text-content-secondary" />
        </button>
      </div>
    )
  }

  // ── Expanded panel ─────────────────────────────────────────────────────
  const subtitle =
    verifiedCount > 0 && pickedCount === 0
      ? `${verifiedCount} verified`
      : allMandatoryPicked
        ? 'All files chosen · ready to submit'
        : pickedCount > 0
          ? `${pickedCount} of ${pendingRows.length} chosen · pick remaining to submit`
          : `${rows.length} document${rows.length === 1 ? '' : 's'} · choose a file for each`

  return (
    <div className="mt-2 overflow-hidden rounded-xl border border-line bg-surface shadow-sm">
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2.5">
        <div>
          <p className="text-[13px] font-semibold text-content">Documents</p>
          <p className="text-[11px] text-content-secondary">{subtitle}</p>
        </div>
        <button
          type="button"
          onClick={() => setExpanded(false)}
          aria-label="Collapse panel"
          className="cursor-pointer rounded-lg p-1.5 text-content-secondary hover:bg-raised hover:text-content"
        >
          <ChevronDown className="h-4 w-4 rotate-180" strokeWidth={2} />
        </button>
      </div>

      <div className="flex flex-col gap-2 p-3">
        {rows.map((row) => (
          <DocRow
            key={row.slot}
            row={row}
            disabled={disabled || submitting}
            onPick={(f) => handlePick(row.slot, f)}
            onClear={() => handleClear(row.slot)}
          />
        ))}
      </div>

      <AnimatePresence>
        {error && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="px-3 pb-2"
          >
            <div className="flex items-start gap-2 rounded-lg bg-danger/10 px-3 py-2 ring-1 ring-danger/20">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
              <p className="text-[12px] leading-snug text-danger-text">{error}</p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="flex items-center justify-between gap-3 border-t border-line px-3 py-2.5">
        <p className="text-[11px] text-content-secondary">PDF · JPG · PNG · max 25 MB</p>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={!canSubmit}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg bg-ember px-3.5 py-2 text-[12.5px] font-semibold text-oncolor transition hover:bg-ember-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitting ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2.25} />
          ) : (
            <Upload className="h-3.5 w-3.5" strokeWidth={2.25} />
          )}
          Submit
        </button>
      </div>
    </div>
  )
}
