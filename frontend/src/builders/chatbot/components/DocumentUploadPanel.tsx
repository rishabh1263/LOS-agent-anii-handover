/**
 * In-chat document upload panel.
 *
 * Flow: expand → pick files → submit → show pass / review / fail per slot.
 * Outcomes persist on the message so refresh does not force re-upload.
 */

import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Eye,
  FileCheck,
  FileText,
  Loader2,
  Upload,
  X,
} from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import type { ChatUploadResult, UploadTarget } from '../../../runtime/chatbot'
import { formatUploadDetail } from '../../../runtime/chatbot'

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
  fileName?: string
  fileSize?: number
  detectedType?: string
  progress?: number
  detail?: string
  previewUrl?: string
}

export interface DocumentUploadPanelProps {
  targets: UploadTarget[]
  initialResults?: ChatUploadResult[]
  onSubmit: (files: { file: File; documentType: string }[]) => Promise<{
    results: Array<{
      status: 'pass' | 'review' | 'fail' | 'validation'
      detail?: string
      detectedType?: string
    }>
  }>
  onResultsPersist?: (results: ChatUploadResult[]) => void
  disabled?: boolean
}

// ── Constants & helpers ────────────────────────────────────────────────────

const ACCEPT =
  '.pdf,.jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp,application/pdf,image/jpeg,image/png,image/bmp,image/tiff,image/webp'
const MAX_BYTES = 25 * 1024 * 1024
const PENDING: DocRowStatus[] = ['idle', 'picked', 'validation', 'fail']
const REPLACEABLE: DocRowStatus[] = ['idle', 'picked', 'validation', 'fail', 'review']

type IconType = typeof FileCheck

const STATUS_PILL: Record<
  Exclude<DocRowStatus, 'idle' | 'uploading'>,
  { label: string; icon: IconType; className: string }
> = {
  picked: {
    label: 'Ready',
    icon: FileCheck,
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
    label: 'Invalid',
    icon: AlertCircle,
    className: 'bg-danger/10 text-danger-text ring-1 ring-danger/20',
  },
}

const ROW_BORDER: Record<string, string> = {
  pass: 'border-success/25 bg-success/[0.03]',
  fail: 'border-danger/25 bg-danger/[0.03]',
  validation: 'border-danger/25 bg-danger/[0.03]',
  review: 'border-warning/30 bg-warning/[0.03]',
  picked: 'border-ember/25 bg-ember/[0.02]',
  uploading: 'border-ember/25 bg-ember/[0.02]',
  idle: 'border-line bg-surface',
}

const DETAIL_TONE: Record<string, string> = {
  pass: 'text-success-text',
  review: 'text-warning-text',
  fail: 'text-danger-text',
  validation: 'text-danger-text',
}

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

function isPending(s: DocRowStatus) {
  return PENDING.includes(s)
}

function canReplace(s: DocRowStatus) {
  return REPLACEABLE.includes(s)
}

function isReadyToUpload(row: DocRowState) {
  return Boolean(row.file && (row.status === 'picked' || row.status === 'validation'))
}

function fileLabel(row: DocRowState) {
  return row.file?.name || row.fileName
}

function fileSizeLabel(row: DocRowState) {
  if (row.file) return formatBytes(row.file.size)
  if (row.fileSize != null) return formatBytes(row.fileSize)
  return null
}

function isImage(file?: File, name?: string) {
  const n = (file?.name || name || '').toLowerCase()
  const t = file?.type || ''
  return t.startsWith('image/') || /\.(jpe?g|png|gif|webp|bmp|tif{1,2})$/i.test(n)
}

function isPdf(file?: File, name?: string) {
  const n = (file?.name || name || '').toLowerCase()
  const t = file?.type || ''
  return t === 'application/pdf' || n.endsWith('.pdf')
}

function rowsFromTargets(
  targets: UploadTarget[],
  initialResults?: ChatUploadResult[],
): DocRowState[] {
  const bySlot = new Map((initialResults ?? []).map((r) => [r.slot, r]))
  return targets.map((t) => {
    const prev = bySlot.get(t.slot)
    const base = {
      slot: t.slot,
      label: t.label || slotLabel(t.slot),
      acceptedTypes: t.acceptedTypes,
    }
    if (!prev) return { ...base, status: 'idle' as DocRowStatus }
    return {
      ...base,
      status: prev.status,
      detail: formatUploadDetail(prev.detail) ?? prev.detail,
      detectedType: prev.detectedType,
      fileName: prev.fileName,
      fileSize: prev.fileSize,
    }
  })
}

function toPersistResult(row: DocRowState): ChatUploadResult | null {
  if (row.status === 'idle' || row.status === 'picked' || row.status === 'uploading') return null
  return {
    slot: row.slot,
    status: row.status as ChatUploadResult['status'],
    detail: row.detail,
    detectedType: row.detectedType,
    fileName: row.fileName || row.file?.name,
    fileSize: row.fileSize ?? row.file?.size,
  }
}

// ── Status pill ────────────────────────────────────────────────────────────

function StatusPill({ status, progress }: { status: DocRowStatus; progress?: number }) {
  if (status === 'idle') return null

  if (status === 'uploading') {
    const pct = Math.min(100, Math.max(0, progress ?? 0))
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-ember/10 px-2 py-0.5 text-[10.5px] font-semibold text-ember-text ring-1 ring-ember/20">
        <Loader2 className="h-3 w-3 shrink-0 animate-spin" strokeWidth={2.25} />
        Uploading {pct}%
      </span>
    )
  }

  const c = STATUS_PILL[status]
  const Icon = c.icon
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10.5px] font-semibold ${c.className}`}
    >
      <Icon className="h-3 w-3 shrink-0" strokeWidth={2.25} />
      {c.label}
    </span>
  )
}

// ── Preview modal ──────────────────────────────────────────────────────────

function DocPreviewModal({
  row,
  file,
  onClose,
}: {
  row: DocRowState
  /** Live File from panel ref — survives status updates */
  file?: File | null
  onClose: () => void
}) {
  const liveFile = file || row.file
  const [url, setUrl] = useState<string | null>(null)
  const [loadError, setLoadError] = useState(false)
  const name = liveFile?.name || fileLabel(row) || 'Document'
  const size =
    liveFile != null
      ? formatBytes(liveFile.size)
      : fileSizeLabel(row)
  const image = isImage(liveFile ?? undefined, row.fileName || name)
  const pdf = isPdf(liveFile ?? undefined, row.fileName || name)

  useEffect(() => {
    setLoadError(false)
    if (row.previewUrl) {
      setUrl(row.previewUrl)
      return
    }
    if (!liveFile) {
      setUrl(null)
      return
    }
    const created = URL.createObjectURL(liveFile)
    setUrl(created)
    return () => URL.revokeObjectURL(created)
  }, [liveFile, row.previewUrl])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [onClose])

  return (
    <motion.div
      key="doc-preview"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.15 }}
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-label={`Preview ${name}`}
    >
      <button
        type="button"
        className="absolute inset-0 bg-black/55 backdrop-blur-[2px]"
        aria-label="Close preview"
        onClick={onClose}
      />
      <motion.div
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 8 }}
        transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
        className="relative z-10 flex max-h-[min(92vh,900px)] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-xl"
      >
        <header className="flex shrink-0 items-center gap-3 border-b border-line px-4 py-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-ember/10 text-ember">
            <FileText className="h-4 w-4" strokeWidth={2} />
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate text-[14px] font-semibold text-content">{name}</p>
            <p className="truncate text-[11.5px] text-content-secondary">
              {row.label || slotLabel(row.slot)}
              {size ? ` · ${size}` : ''}
              {row.detectedType && row.detectedType.toLowerCase() !== 'unknown'
                ? ` · ${slotLabel(row.detectedType)}`
                : ''}
            </p>
          </div>
          {url && (
            <a
              href={url}
              download={name}
              className="shrink-0 rounded-lg border border-line bg-raised px-2.5 py-1.5 text-[12px] font-semibold text-content hover:border-ember/35 hover:text-ember-text"
            >
              Download
            </a>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="cursor-pointer rounded-lg p-2 text-content-secondary hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            <X className="h-4 w-4" strokeWidth={2} />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-auto bg-raised/40 p-3 sm:p-4">
          {!url || loadError ? (
            <div className="flex h-48 flex-col items-center justify-center gap-2 text-content-secondary">
              <FileText className="h-10 w-10 opacity-40" />
              <p className="text-[13px]">
                {loadError ? 'Could not load preview' : 'Preview not available'}
              </p>
              <p className="text-[11.5px]">
                {liveFile
                  ? 'Use Download to open the file'
                  : 'Re-select the file to preview it again'}
              </p>
            </div>
          ) : image ? (
            <div className="flex min-h-[240px] items-center justify-center">
              <img
                src={url}
                alt={name}
                onError={() => setLoadError(true)}
                className="max-h-[min(70vh,720px)] max-w-full rounded-lg object-contain shadow-sm"
              />
            </div>
          ) : pdf ? (
            <object
              data={url}
              type="application/pdf"
              title={name}
              className="h-[min(70vh,720px)] w-full rounded-lg border border-line bg-white"
            >
              <iframe
                title={name}
                src={url}
                className="h-[min(70vh,720px)] w-full rounded-lg border-0 bg-white"
              />
            </object>
          ) : (
            <div className="flex h-48 flex-col items-center justify-center gap-3 text-content-secondary">
              <FileText className="h-10 w-10 opacity-40" />
              <p className="text-[13px]">Inline preview not supported for this file type</p>
              <a
                href={url}
                download={name}
                className="rounded-lg bg-ember px-3 py-1.5 text-[12.5px] font-semibold text-oncolor hover:bg-ember-hover"
              >
                Download file
              </a>
            </div>
          )}
        </div>
      </motion.div>
    </motion.div>
  )
}

// ── Row ────────────────────────────────────────────────────────────────────

function DocRow({
  row,
  onPick,
  onClear,
  onView,
  canView,
  disabled,
}: {
  row: DocRowState
  onPick: (file: File) => void
  onClear: () => void
  onView?: () => void
  /** True when a live File is still available for this slot */
  canView?: boolean
  disabled?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const hasFile = Boolean(row.file || row.fileName)
  const showView = Boolean(canView) && row.status !== 'uploading'
  const showReplace = canReplace(row.status) && row.status !== 'uploading'
  const detail = formatUploadDetail(row.detail) ?? row.detail
  const showDetail =
    Boolean(detail) &&
    (row.status === 'fail' ||
      row.status === 'validation' ||
      row.status === 'review' ||
      row.status === 'pass')

  const openPicker = () => inputRef.current?.click()

  const onFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (f) onPick(f)
    e.target.value = ''
  }

  return (
    <div
      className={`w-full min-w-0 max-w-full overflow-hidden rounded-xl border px-3 py-2.5 ${
        ROW_BORDER[row.status] ?? ROW_BORDER.idle
      }`}
    >
      <div className="flex w-full min-w-0 max-w-full items-start gap-2">
        <div className="min-w-0 flex-1 overflow-hidden">
          <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1.5">
            <p className="min-w-0 max-w-full truncate text-[13px] font-semibold text-content">
              {row.label || slotLabel(row.slot)}
              {isPending(row.status) && (
                <span className="ml-0.5 font-bold text-danger" aria-label="required">
                  *
                </span>
              )}
            </p>
            <StatusPill status={row.status} progress={row.progress} />
          </div>

          {hasFile ? (
            <p className="mt-0.5 truncate text-[11.5px] text-content-secondary">
              {fileLabel(row)}
              {fileSizeLabel(row) && (
                <>
                  <span className="mx-1 text-content-disabled">·</span>
                  {fileSizeLabel(row)}
                </>
              )}
              {row.detectedType && row.detectedType.toLowerCase() !== 'unknown' && (
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

          {showDetail && detail && (
            <p
              className={`mt-1 max-w-full break-words text-[11.5px] leading-snug [overflow-wrap:anywhere] [word-break:break-word] ${
                DETAIL_TONE[row.status] ?? 'text-content-secondary'
              }`}
            >
              {detail}
            </p>
          )}
        </div>

        <div className="flex max-w-[40%] shrink-0 flex-wrap items-center justify-end gap-1">
          {showView && (
            <button
              type="button"
              onClick={onView}
              disabled={disabled}
              aria-label="View document"
              title="View document"
              className="cursor-pointer rounded-lg border border-line bg-raised p-1.5 text-content-secondary hover:border-ember/35 hover:text-ember-text disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Eye className="h-3.5 w-3.5" strokeWidth={2} />
            </button>
          )}

          {showReplace && hasFile && row.status !== 'review' && (
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

          {showReplace && (
            <>
              <button
                type="button"
                onClick={openPicker}
                disabled={disabled}
                className="cursor-pointer rounded-lg border border-line bg-raised px-2.5 py-1.5 text-[12px] font-semibold text-content hover:border-ember/35 hover:text-ember-text disabled:cursor-not-allowed disabled:opacity-40"
              >
                {hasFile ? 'Change' : 'Choose'}
              </button>
              <input
                ref={inputRef}
                type="file"
                accept={ACCEPT}
                className="hidden"
                disabled={disabled}
                onChange={onFileChange}
              />
            </>
          )}

          {row.status === 'pass' && (
            <CheckCircle2 className="h-5 w-5 shrink-0 text-success" strokeWidth={2} />
          )}
        </div>
      </div>

      {row.status === 'uploading' && (
        <div
          className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-raised"
          role="progressbar"
          aria-valuenow={row.progress ?? 0}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Upload progress"
        >
          <div
            className="h-full rounded-full bg-ember transition-all duration-300 ease-out"
            style={{ width: `${Math.min(100, Math.max(0, row.progress ?? 0))}%` }}
          />
        </div>
      )}
    </div>
  )
}

// ── Collapsed CTA ──────────────────────────────────────────────────────────

function CollapsedCta({
  allDone,
  verifiedCount,
  pendingCount,
  disabled,
  onExpand,
}: {
  allDone: boolean
  verifiedCount: number
  pendingCount: number
  disabled?: boolean
  onExpand: () => void
}) {
  if (allDone) {
    return (
      <button
        type="button"
        onClick={onExpand}
        className="flex w-full cursor-pointer items-center gap-3 rounded-xl border border-success/25 bg-success/[0.04] px-3 py-2.5 text-left transition hover:border-success/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
      >
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-success/15 text-success">
          <CheckCircle2 className="h-4 w-4" strokeWidth={2.25} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="text-[13px] font-semibold text-content">
            {verifiedCount} document{verifiedCount === 1 ? '' : 's'} uploaded
          </span>
          <span className="mt-0.5 block text-[11.5px] text-content-secondary">
            Tap to view status · re-upload not required
          </span>
        </span>
        <ChevronDown className="h-4 w-4 shrink-0 text-content-secondary" />
      </button>
    )
  }

  const label =
    pendingCount <= 1
      ? 'Upload 1 pending document'
      : `Upload ${pendingCount} pending documents`

  return (
    <button
      type="button"
      onClick={onExpand}
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
            {pendingCount}
          </span>
        </span>
        <span className="mt-0.5 block text-[11.5px] text-content-secondary">
          PDF, JPG, PNG · up to 25 MB each
        </span>
      </span>
      <ChevronDown className="h-4 w-4 shrink-0 text-content-secondary" />
    </button>
  )
}

// ── Main panel ─────────────────────────────────────────────────────────────

export function DocumentUploadPanel({
  targets,
  initialResults,
  onSubmit,
  onResultsPersist,
  disabled = false,
}: DocumentUploadPanelProps) {
  const [expanded, setExpanded] = useState(() => Boolean(initialResults?.length))
  const [rows, setRows] = useState<DocRowState[]>(() => rowsFromTargets(targets, initialResults))
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [previewSlot, setPreviewSlot] = useState<string | null>(null)
  /** Bump when filesRef changes so Eye buttons re-render */
  const [filesVersion, setFilesVersion] = useState(0)
  const progressTimers = useRef<ReturnType<typeof setInterval>[]>([])
  const rowsRef = useRef(rows)
  rowsRef.current = rows
  /** Slot → File kept outside React state so submit / persist cannot drop it */
  const filesRef = useRef<Map<string, File>>(new Map())
  const previewUrlsRef = useRef<Map<string, string>>(new Map())

  const targetsKey = targets.map((t) => t.slot).join('|')
  const resultsKey = (initialResults ?? []).map((r) => `${r.slot}:${r.status}`).join('|')

  const revokePreview = (slot: string) => {
    const url = previewUrlsRef.current.get(slot)
    if (url) {
      URL.revokeObjectURL(url)
      previewUrlsRef.current.delete(slot)
    }
  }

  const setSlotFile = (slot: string, file: File | null) => {
    if (file) {
      filesRef.current.set(slot, file)
      revokePreview(slot)
      const url = URL.createObjectURL(file)
      previewUrlsRef.current.set(slot, url)
    } else {
      filesRef.current.delete(slot)
      revokePreview(slot)
    }
    setFilesVersion((v) => v + 1)
  }

  useEffect(() => {
    if (targets.length === 0) return
    setRows((prev) => {
      const next = rowsFromTargets(targets, initialResults)
      const prevBySlot = new Map(prev.map((r) => [r.slot, r]))
      return next.map((r) => {
        const old = prevBySlot.get(r.slot)
        const live = filesRef.current.get(r.slot)
        const previewUrl = previewUrlsRef.current.get(r.slot) || old?.previewUrl
        const file = live || old?.file
        if (!file && !previewUrl && !old?.fileName && !r.fileName) return r
        return {
          ...r,
          file,
          previewUrl,
          fileName: r.fileName || old?.fileName || file?.name,
          fileSize: r.fileSize ?? old?.fileSize ?? file?.size,
        }
      })
    })
    if (initialResults?.length) setExpanded(true)
  }, [targetsKey, resultsKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    return () => {
      progressTimers.current.forEach(clearInterval)
      previewUrlsRef.current.forEach((url) => URL.revokeObjectURL(url))
      previewUrlsRef.current.clear()
      filesRef.current.clear()
    }
  }, [])

  const pendingRows = rows.filter((r) => isPending(r.status))
  const verifiedCount = rows.filter((r) => r.status === 'pass').length
  const pickedCount = rows.filter(isReadyToUpload).length
  const allDone =
    rows.length > 0 &&
    rows.every((r) => r.status === 'pass' || r.status === 'review') &&
    pendingRows.length === 0
  const canSubmit =
    pendingRows.length > 0 &&
    pendingRows.every(isReadyToUpload) &&
    !submitting &&
    !disabled

  const updateRow = useCallback((slot: string, patch: Partial<DocRowState>) => {
    setRows((prev) =>
      prev.map((r) => {
        if (r.slot !== slot) return r
        const next: DocRowState = { ...r, ...patch }
        // Always re-attach live file / blob from refs so status patches cannot drop them
        const live = filesRef.current.get(slot)
        if (live) {
          next.file = live
          next.previewUrl = previewUrlsRef.current.get(slot) || next.previewUrl
          next.fileName = next.fileName || live.name
          next.fileSize = next.fileSize ?? live.size
        }
        return next
      }),
    )
  }, [])

  const handlePick = useCallback(
    (slot: string, file: File) => {
      if (file.size > MAX_BYTES) {
        setError(`${file.name} exceeds the 25 MB limit`)
        return
      }
      setError(null)
      setSlotFile(slot, file)
      updateRow(slot, {
        file,
        status: 'picked',
        detail: undefined,
        progress: undefined,
        detectedType: undefined,
        fileName: file.name,
        fileSize: file.size,
        previewUrl: previewUrlsRef.current.get(slot),
      })
    },
    [updateRow],
  )

  const handleClear = useCallback(
    (slot: string) => {
      setSlotFile(slot, null)
      updateRow(slot, {
        file: undefined,
        status: 'idle',
        detail: undefined,
        progress: undefined,
        detectedType: undefined,
        fileName: undefined,
        fileSize: undefined,
        previewUrl: undefined,
      })
    },
    [updateRow],
  )

  const clearProgressTimers = () => {
    progressTimers.current.forEach(clearInterval)
    progressTimers.current = []
  }

  const handleSubmit = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    clearProgressTimers()

    const toUpload = rows.filter(isReadyToUpload).map((r) => ({
      slot: r.slot,
      file: r.file!,
      documentType: r.acceptedTypes[0] || r.slot,
    }))

    toUpload.forEach((u) => updateRow(u.slot, { status: 'uploading', progress: 8 }))
    toUpload.forEach((u) => {
      const timer = setInterval(() => {
        setRows((prev) =>
          prev.map((r) => {
            if (r.slot !== u.slot || r.status !== 'uploading') return r
            return {
              ...r,
              progress: Math.min(90, Math.round((r.progress ?? 8) + Math.random() * 12 + 4)),
            }
          }),
        )
      }, 280)
      progressTimers.current.push(timer)
    })

    try {
      const { results } = await onSubmit(
        toUpload.map((u) => ({ file: u.file, documentType: u.documentType })),
      )
      clearProgressTimers()

      const batch: ChatUploadResult[] = []
      results.forEach((res, i) => {
        const item = toUpload[i]
        if (!item) return
        const detail = formatUploadDetail(res.detail) ?? res.detail
        updateRow(item.slot, {
          status: res.status,
          detail,
          progress: 100,
          detectedType: res.detectedType,
          fileName: item.file.name,
          fileSize: item.file.size,
        })
        batch.push({
          slot: item.slot,
          status: res.status,
          detail,
          detectedType: res.detectedType,
          fileName: item.file.name,
          fileSize: item.file.size,
        })
      })

      const batchSlots = new Set(batch.map((b) => b.slot))
      const persist: ChatUploadResult[] = [
        ...batch,
        ...rowsRef.current
          .filter((r) => !batchSlots.has(r.slot))
          .map(toPersistResult)
          .filter((r): r is ChatUploadResult => r != null),
      ]
      onResultsPersist?.(persist)
    } catch (err) {
      clearProgressTimers()
      setError(err instanceof Error ? err.message : 'Upload failed. Please try again.')
      toUpload.forEach((u) => updateRow(u.slot, { status: 'picked', progress: undefined }))
    } finally {
      setSubmitting(false)
    }
  }

  if (targets.length === 0 && rows.length === 0) return null

  const previewRow = previewSlot ? rows.find((r) => r.slot === previewSlot) : null
  const previewFile = previewSlot ? filesRef.current.get(previewSlot) ?? null : null
  // filesVersion ensures Eye visibility updates when filesRef changes
  void filesVersion

  const pendingCount = Math.max(
    pendingRows.length,
    targets.filter((t) => {
      const r = (initialResults ?? []).find((x) => x.slot === t.slot)
      return !r || r.status === 'fail' || r.status === 'validation'
    }).length || targets.length,
  )

  const subtitle = allDone
    ? `${verifiedCount} verified · no re-upload needed`
    : canSubmit
      ? 'All files chosen · ready to submit'
      : pickedCount > 0
        ? `${pickedCount} of ${pendingRows.length} chosen · pick remaining`
        : `${rows.length} document${rows.length === 1 ? '' : 's'} · choose a file for each`

  return (
    <>
      <div className="mt-2 w-full min-w-0 max-w-full overflow-x-hidden">
        {!expanded ? (
          <CollapsedCta
            allDone={allDone}
            verifiedCount={verifiedCount}
            pendingCount={pendingCount}
            disabled={disabled}
            onExpand={() => setExpanded(true)}
          />
        ) : (
          <div className="w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-line bg-surface shadow-sm">
            <div className="flex min-w-0 items-center justify-between gap-2 border-b border-line px-3 py-2.5">
              <div className="min-w-0 flex-1 overflow-hidden">
                <p className="truncate text-[13px] font-semibold text-content">Documents</p>
                <p className="truncate text-[11px] text-content-secondary">{subtitle}</p>
              </div>
              <button
                type="button"
                onClick={() => setExpanded(false)}
                aria-label="Collapse panel"
                className="shrink-0 cursor-pointer rounded-lg p-1.5 text-content-secondary hover:bg-raised hover:text-content"
              >
                <ChevronDown className="h-4 w-4 rotate-180" strokeWidth={2} />
              </button>
            </div>

            <div className="flex w-full min-w-0 max-w-full flex-col gap-2 overflow-x-hidden p-3">
              {rows.map((row) => (
                <DocRow
                  key={row.slot}
                  row={row}
                  disabled={disabled || submitting}
                  canView={filesRef.current.has(row.slot) || Boolean(row.file || row.previewUrl)}
                  onPick={(f) => handlePick(row.slot, f)}
                  onClear={() => handleClear(row.slot)}
                  onView={() => setPreviewSlot(row.slot)}
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

            <div className="flex min-w-0 items-center justify-between gap-2 overflow-hidden border-t border-line px-3 py-2.5">
              <p className="min-w-0 flex-1 truncate text-[11px] text-content-secondary">
                PDF · JPG · PNG · max 25 MB
              </p>
              {pendingRows.length > 0 ? (
                <button
                  type="button"
                  onClick={handleSubmit}
                  disabled={!canSubmit}
                  className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-lg bg-ember px-3.5 py-2 text-[12.5px] font-semibold text-oncolor transition hover:bg-ember-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {submitting ? (
                    <>
                      <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2.25} />
                      Uploading…
                    </>
                  ) : (
                    <>
                      <Upload className="h-3.5 w-3.5" strokeWidth={2.25} />
                      Submit
                    </>
                  )}
                </button>
              ) : (
                <span className="shrink-0 text-[12px] font-medium text-success-text">All set</span>
              )}
            </div>
          </div>
        )}
      </div>

      <AnimatePresence>
        {previewRow && (
          <DocPreviewModal
            row={previewRow}
            file={previewFile}
            onClose={() => setPreviewSlot(null)}
          />
        )}
      </AnimatePresence>
    </>
  )
}
