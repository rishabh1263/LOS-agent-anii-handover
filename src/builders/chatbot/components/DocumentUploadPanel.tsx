/**
 * In-chat document upload — professional, product-grade UI.
 *
 * Flow:
 *  1. Compact CTA under the assistant answer
 *  2. Expand → checklist rows with status
 *  3. Clean file pick / drag-drop + progress
 *  4. Post-verify PASS / REVIEW / FAIL details
 *
 * Show only when getUploadTargets() says so — never from answer text.
 */

import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  FileText,
  Loader2,
  Paperclip,
  Sparkles,
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
  documentType?: string
  detectedType?: string
  progress?: number
  detail?: string
  verdict?: string
}

export interface DocumentUploadPanelProps {
  targets: UploadTarget[]
  showGeneralUpload?: boolean
  onSubmit: (files: { file: File; documentType: string }[]) => Promise<{
    results: Array<{
      status: 'pass' | 'review' | 'fail' | 'validation'
      detail?: string
      detectedType?: string
    }>
    refreshedTargets?: UploadTarget[]
  }>
  onClose?: () => void
  disabled?: boolean
}

const ACCEPT =
  '.pdf,.jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp,application/pdf,image/jpeg,image/png,image/bmp,image/tiff,image/webp'
const MAX_BYTES = 25 * 1024 * 1024

const spring = { type: 'spring' as const, stiffness: 420, damping: 32 }
const easeOut = [0.22, 1, 0.36, 1] as const

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

// ── Status pill ────────────────────────────────────────────────────────────

function StatusPill({ status, detail }: { status: DocRowStatus; detail?: string }) {
  const cfg: Record<
    DocRowStatus,
    { label: string; icon: typeof Check; className: string; pulse?: boolean }
  > = {
    idle: {
      label: 'Required',
      icon: FileText,
      className: 'bg-raised/80 text-content-secondary ring-1 ring-line',
    },
    picked: {
      label: 'Ready',
      icon: Paperclip,
      className: 'bg-ember/10 text-ember-text ring-1 ring-ember/20',
    },
    uploading: {
      label: 'Uploading',
      icon: Loader2,
      className: 'bg-ember/10 text-ember-text ring-1 ring-ember/20',
      pulse: true,
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
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-sans text-[10.5px] font-semibold tracking-wide ${c.className}`}
    >
      <Icon
        className={`h-3 w-3 shrink-0 ${status === 'uploading' ? 'animate-spin' : ''}`}
        strokeWidth={2.25}
      />
      {c.label}
    </span>
  )
}

// ── Icon tile by status ────────────────────────────────────────────────────

function RowIcon({ status }: { status: DocRowStatus }) {
  const base =
    'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors duration-300'
  if (status === 'pass') {
    return (
      <div className={`${base} bg-success/12 text-success`}>
        <CheckCircle2 className="h-[18px] w-[18px]" strokeWidth={2} />
      </div>
    )
  }
  if (status === 'fail' || status === 'validation') {
    return (
      <div className={`${base} bg-danger/12 text-danger`}>
        <AlertCircle className="h-[18px] w-[18px]" strokeWidth={2} />
      </div>
    )
  }
  if (status === 'review') {
    return (
      <div className={`${base} bg-warning/12 text-warning`}>
        <AlertTriangle className="h-[18px] w-[18px]" strokeWidth={2} />
      </div>
    )
  }
  if (status === 'uploading') {
    return (
      <div className={`${base} bg-ember/12 text-ember`}>
        <Loader2 className="h-[18px] w-[18px] animate-spin" strokeWidth={2} />
      </div>
    )
  }
  if (status === 'picked') {
    return (
      <div className={`${base} bg-ember/12 text-ember`}>
        <Paperclip className="h-[18px] w-[18px]" strokeWidth={2} />
      </div>
    )
  }
  return (
    <div className={`${base} bg-raised text-content-secondary ring-1 ring-line/80`}>
      <FileText className="h-[18px] w-[18px]" strokeWidth={1.75} />
    </div>
  )
}

// ── Single document row ────────────────────────────────────────────────────

function DocRow({
  row,
  index,
  onPick,
  onClear,
  onTypeChange,
  disabled,
}: {
  row: DocRowState
  index: number
  onPick: (file: File) => void
  onClear: () => void
  onTypeChange: (type: string) => void
  disabled?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const needsUpload =
    row.status === 'idle' || row.status === 'picked' || row.status === 'validation'
  const multiType = row.acceptedTypes.length > 1
  const isDone = row.status === 'pass' || row.status === 'review'
  const isBad = row.status === 'fail' || row.status === 'validation'

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 10, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.15 } }}
      transition={{ ...spring, delay: index * 0.04 }}
      className={`group relative overflow-hidden rounded-2xl border transition-shadow duration-300 ${
        row.status === 'pass'
          ? 'border-success/25 bg-success/[0.04] shadow-[0_0_0_1px_rgba(30,142,62,0.06)]'
          : isBad
            ? 'border-danger/25 bg-danger/[0.04]'
            : row.status === 'review'
              ? 'border-warning/30 bg-warning/[0.04]'
              : row.status === 'picked' || row.status === 'uploading'
                ? 'border-ember/30 bg-ember/[0.03] shadow-sm'
                : 'border-line bg-surface hover:border-line-strong hover:shadow-sm'
      }`}
    >
      <div className="flex items-start gap-3 px-3.5 py-3">
        <RowIcon status={row.status} />

        <div className="min-w-0 flex-1 pt-0.5">
          <div className="flex flex-wrap items-center gap-2">
            <p className="font-sans text-[13.5px] font-semibold tracking-[-0.01em] text-content">
              {row.label || slotLabel(row.slot)}
            </p>
            <StatusPill status={row.status} detail={row.detail} />
          </div>

          {row.file ? (
            <p className="mt-1 truncate font-sans text-[11.5px] text-content-secondary">
              <span className="text-content/80">{row.file.name}</span>
              <span className="mx-1.5 text-content-disabled">·</span>
              {formatBytes(row.file.size)}
              {(row.detectedType || row.documentType) && (
                <>
                  <span className="mx-1.5 text-content-disabled">·</span>
                  <span className="font-medium text-content-secondary">
                    {slotLabel(row.detectedType || row.documentType || '')}
                  </span>
                </>
              )}
            </p>
          ) : (
            <p className="mt-1 font-sans text-[11.5px] text-content-secondary">
              Accepts {row.acceptedTypes.map(slotLabel).join(', ')}
            </p>
          )}

          {/* Result detail */}
          <AnimatePresence>
            {row.detail && (isBad || row.status === 'review' || row.status === 'pass') && (
              <motion.p
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className={`mt-1.5 font-sans text-[11.5px] leading-snug ${
                  row.status === 'pass'
                    ? 'text-success-text'
                    : row.status === 'review'
                      ? 'text-warning-text'
                      : 'text-danger-text'
                }`}
              >
                {row.detail}
              </motion.p>
            )}
          </AnimatePresence>

          {/* Type selector */}
          {needsUpload && multiType && (
            <select
              value={row.documentType || ''}
              onChange={(e) => onTypeChange(e.target.value)}
              disabled={disabled}
              className="mt-2 w-full max-w-[220px] appearance-none rounded-xl border border-line bg-raised/80 px-2.5 py-1.5 font-sans text-[12px] text-content shadow-xs transition focus:border-ember/40 focus:outline-none focus:ring-2 focus:ring-ember/20 disabled:opacity-50"
            >
              <option value="">Auto-detect type</option>
              {row.acceptedTypes.map((t) => (
                <option key={t} value={t}>
                  {slotLabel(t)}
                </option>
              ))}
            </select>
          )}
        </div>

        {/* Actions */}
        {needsUpload && (
          <div className="flex shrink-0 items-center gap-1 pt-0.5">
            <AnimatePresence>
              {row.file && (
                <motion.button
                  type="button"
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                  onClick={onClear}
                  disabled={disabled}
                  aria-label="Remove file"
                  className="rounded-xl p-2 text-content-secondary transition hover:bg-danger/10 hover:text-danger focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-40"
                >
                  <X className="h-3.5 w-3.5" strokeWidth={2.25} />
                </motion.button>
              )}
            </AnimatePresence>
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              disabled={disabled}
              className="rounded-xl border border-line bg-raised px-3 py-1.5 font-sans text-[12px] font-semibold text-content shadow-xs transition hover:border-ember/35 hover:bg-ember/5 hover:text-ember-text focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-40"
            >
              {row.file ? 'Replace' : 'Choose'}
            </button>
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={(e: ChangeEvent<HTMLInputElement>) => {
                const f = e.target.files?.[0]
                if (f) onPick(f)
                e.target.value = ''
              }}
            />
          </div>
        )}

        {isDone && (
          <div className="flex shrink-0 items-center pt-1">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-success/15 text-success">
              <Check className="h-3.5 w-3.5" strokeWidth={2.5} />
            </span>
          </div>
        )}
      </div>

      {/* Progress track */}
      <AnimatePresence>
        {(row.status === 'uploading' ||
          (row.progress !== undefined && row.status !== 'idle')) && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="h-[3px] w-full bg-raised"
            role="progressbar"
            aria-valuenow={row.progress ?? 0}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <motion.div
              className={`h-full rounded-full ${
                row.status === 'pass'
                  ? 'bg-success'
                  : isBad
                    ? 'bg-danger'
                    : row.status === 'review'
                      ? 'bg-warning'
                      : 'bg-gradient-to-r from-ember to-ember-hover'
              }`}
              initial={{ width: '0%' }}
              animate={{
                width: `${row.progress ?? (row.status === 'uploading' ? 45 : 100)}%`,
              }}
              transition={{ duration: 0.5, ease: easeOut }}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ── Main panel ─────────────────────────────────────────────────────────────

export function DocumentUploadPanel({
  targets,
  showGeneralUpload = false,
  onSubmit,
  onClose,
  disabled = false,
}: DocumentUploadPanelProps) {
  const [expanded, setExpanded] = useState(false)
  const [rows, setRows] = useState<DocRowState[]>(() =>
    targets.map((t) => ({
      slot: t.slot,
      label: t.label || slotLabel(t.slot),
      acceptedTypes: t.acceptedTypes,
      status: 'idle' as DocRowStatus,
      documentType: t.acceptedTypes.length === 1 ? t.acceptedTypes[0] : undefined,
    })),
  )
  const [submitting, setSubmitting] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const targetsKey = targets.map((t) => t.slot).join('|')
  useEffect(() => {
    if (targets.length === 0) return
    setRows(
      targets.map((t) => ({
        slot: t.slot,
        label: t.label || slotLabel(t.slot),
        acceptedTypes: t.acceptedTypes,
        status: 'idle' as DocRowStatus,
        documentType: t.acceptedTypes.length === 1 ? t.acceptedTypes[0] : undefined,
      })),
    )
  }, [targetsKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const pickedCount = rows.filter(
    (r) => r.file && (r.status === 'picked' || r.status === 'validation'),
  ).length
  const verifiedCount = rows.filter((r) => r.status === 'pass').length
  const canSubmit = pickedCount > 0 && !submitting && !disabled

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
        verdict: undefined,
        detectedType: undefined,
      })
    },
    [updateRow],
  )

  const handleSubmit = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)

    const toUpload = rows
      .filter((r) => r.file && (r.status === 'picked' || r.status === 'validation'))
      .map((r) => ({
        slot: r.slot,
        file: r.file!,
        documentType: r.documentType || '',
      }))

    toUpload.forEach((u) => updateRow(u.slot, { status: 'uploading', progress: 28 }))

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
          verdict: res.status,
        })
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed. Please try again.')
      toUpload.forEach((u) => updateRow(u.slot, { status: 'picked', progress: undefined }))
    } finally {
      setSubmitting(false)
    }
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files?.[0]
    if (!file) return
    const target = rows.find(
      (r) => r.status === 'idle' || r.status === 'picked' || r.status === 'validation',
    )
    if (target) handlePick(target.slot, file)
  }

  // ── Collapsed CTA ──────────────────────────────────────────────────────
  if (!expanded) {
    const count = targets.length
    const label =
      count > 0
        ? count === 1
          ? 'Upload 1 pending document'
          : `Upload ${count} pending documents`
        : showGeneralUpload
          ? 'Upload documents'
          : null

    if (!label) return null

    return (
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25, ease: easeOut }}
        className="mt-2.5 px-0.5"
      >
        <button
          type="button"
          onClick={() => setExpanded(true)}
          disabled={disabled}
          className="group relative flex w-full items-center gap-3 overflow-hidden rounded-2xl border border-ember/20 bg-gradient-to-br from-ember/[0.07] via-ember/[0.04] to-transparent px-3.5 py-3 text-left shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all duration-300 hover:border-ember/40 hover:shadow-[0_4px_16px_rgba(244,81,30,0.12)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ember focus-visible:ring-offset-2 disabled:opacity-50"
        >
          {/* Soft glow */}
          <span
            aria-hidden
            className="pointer-events-none absolute -left-4 -top-4 h-20 w-20 rounded-full bg-ember/15 blur-2xl transition-opacity duration-500 group-hover:opacity-100"
          />
          <span className="relative flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-ember text-oncolor shadow-[0_2px_8px_rgba(244,81,30,0.35)] transition-transform duration-300 group-hover:scale-[1.04]">
            <Upload className="h-4.5 w-4.5" strokeWidth={2.25} />
          </span>
          <span className="relative min-w-0 flex-1">
            <span className="flex items-center gap-1.5">
              <span className="block font-sans text-[13.5px] font-semibold tracking-[-0.01em] text-content">
                {label}
              </span>
              {count > 0 && (
                <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-ember px-1.5 font-sans text-[10px] font-bold text-oncolor">
                  {count}
                </span>
              )}
            </span>
            <span className="mt-0.5 block font-sans text-[11.5px] text-content-secondary">
              PDF, JPG, PNG · up to 25 MB each
            </span>
          </span>
          <ChevronDown className="relative h-4 w-4 shrink-0 text-content-secondary transition-transform duration-300 group-hover:translate-y-0.5 group-hover:text-content" />
        </button>
      </motion.div>
    )
  }

  // ── Expanded panel ─────────────────────────────────────────────────────
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: easeOut }}
      className="mt-2.5"
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={onDrop}
    >
      <div
        className={`overflow-hidden rounded-2xl border bg-surface shadow-[0_4px_24px_rgba(0,0,0,0.06)] transition-all duration-300 ${
          dragOver
            ? 'border-ember ring-2 ring-ember/25'
            : 'border-line'
        }`}
      >
        {/* Header */}
        <div className="relative flex items-center justify-between gap-3 border-b border-line bg-gradient-to-b from-raised/60 to-transparent px-4 py-3">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-ember/12 text-ember ring-1 ring-ember/15">
              <Sparkles className="h-4 w-4" strokeWidth={2} />
            </span>
            <div>
              <p className="font-sans text-[13.5px] font-semibold tracking-[-0.01em] text-content">
                Document checklist
              </p>
              <p className="font-sans text-[11px] text-content-secondary">
                {verifiedCount > 0 && pickedCount === 0
                  ? `${verifiedCount} verified`
                  : pickedCount > 0
                    ? `${pickedCount} ready to upload`
                    : `${rows.length} document${rows.length === 1 ? '' : 's'} required`}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              setExpanded(false)
              onClose?.()
            }}
            aria-label="Collapse panel"
            className="rounded-xl p-2 text-content-secondary transition hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            <ChevronDown className="h-4 w-4 rotate-180" strokeWidth={2} />
          </button>
        </div>

        {/* Rows */}
        <div className="flex flex-col gap-2.5 p-3.5">
          <AnimatePresence mode="popLayout">
            {rows.map((row, i) => (
              <DocRow
                key={row.slot}
                row={row}
                index={i}
                disabled={disabled || submitting}
                onPick={(f) => handlePick(row.slot, f)}
                onClear={() => handleClear(row.slot)}
                onTypeChange={(t) => updateRow(row.slot, { documentType: t || undefined })}
              />
            ))}
          </AnimatePresence>

          {/* Drop zone */}
          {pickedCount === 0 && !submitting && (
            <motion.div
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.08, duration: 0.25 }}
              className={`relative flex flex-col items-center justify-center rounded-2xl border border-dashed px-4 py-6 transition-all duration-300 ${
                dragOver
                  ? 'border-ember bg-ember/[0.06] scale-[1.01]'
                  : 'border-line/90 bg-raised/40 hover:border-ember/30 hover:bg-ember/[0.03]'
              }`}
            >
              <div
                className={`mb-2.5 flex h-11 w-11 items-center justify-center rounded-2xl transition-colors ${
                  dragOver ? 'bg-ember text-oncolor' : 'bg-raised text-content-secondary ring-1 ring-line'
                }`}
              >
                <Upload className="h-5 w-5" strokeWidth={1.75} />
              </div>
              <p className="font-sans text-[13px] font-medium text-content">
                {dragOver ? 'Drop to attach' : 'Drag & drop a file'}
              </p>
              <p className="mt-0.5 font-sans text-[11.5px] text-content-secondary">
                or use Choose on a row above
              </p>
            </motion.div>
          )}
        </div>

        {/* Error banner */}
        <AnimatePresence>
          {error && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="mx-3.5 mb-2 overflow-hidden"
            >
              <div className="flex items-start gap-2 rounded-xl bg-danger/10 px-3 py-2 ring-1 ring-danger/20">
                <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" />
                <p className="font-sans text-[12px] leading-snug text-danger-text">{error}</p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Footer */}
        <div className="flex items-center justify-between gap-3 border-t border-line bg-raised/30 px-4 py-3">
          <p className="font-sans text-[11px] text-content-secondary">
            PDF · JPG · PNG · max 25 MB
          </p>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="inline-flex items-center gap-2 rounded-xl bg-ember px-4 py-2.5 font-sans text-[12.5px] font-semibold text-oncolor shadow-[0_2px_8px_rgba(244,81,30,0.3)] transition-all duration-200 hover:bg-ember-hover hover:shadow-[0_4px_14px_rgba(244,81,30,0.35)] active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-ember focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none disabled:active:scale-100"
          >
            {submitting ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Uploading…
              </>
            ) : (
              <>
                <Upload className="h-3.5 w-3.5" strokeWidth={2.5} />
                {pickedCount > 0 ? `Submit ${pickedCount}` : 'Submit'}
              </>
            )}
          </button>
        </div>
      </div>
    </motion.div>
  )
}
