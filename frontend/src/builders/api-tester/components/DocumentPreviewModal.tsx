import { useEffect, useState } from 'react'
import { Eye, FileText, MapPin, X, ZoomIn } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import type { DocumentResult, UploadFileItem } from '../../../runtime/api-tester'
import { docStatusClass, verificationClass } from '../../../runtime/api-tester'
import { StatusChip } from './StatusChip'

export interface DocumentPreviewModalProps {
  doc: DocumentResult | null
  uploadedFiles?: UploadFileItem[]
  onClose: () => void
}

const ADDRESS_KEYS = new Set([
  'address',
  'permanent_address',
  'current_address',
  'residential_address',
  'mailing_address',
])

function formatLabel(key: string) {
  return key.replace(/_/g, ' ')
}

function isPlainObject(val: unknown): val is Record<string, unknown> {
  return typeof val === 'object' && val !== null && !Array.isArray(val)
}

function partyLabel(role?: string | null) {
  if (role === 'PRIMARY_APPLICANT') return 'Applicant'
  if (role === 'CO_APPLICANT') return 'Co-applicant'
  return role || null
}

function normalizeName(name: string) {
  return name
    .toLowerCase()
    .trim()
    .replace(/\\/g, '/')
    .split('/')
    .pop()!
    .replace(/\s+/g, ' ')
}

function baseName(name: string) {
  return normalizeName(name).replace(/\.[^.]+$/, '')
}

function matchUploadedFile(
  doc: DocumentResult,
  files: UploadFileItem[] | undefined,
): UploadFileItem | undefined {
  if (!files?.length) return undefined

  const sid = normalizeName(doc.source_id || '')
  const sidBase = baseName(doc.source_id || '')
  const role = doc.party_role

  // Prefer same party when API stamps party_role on the result
  const ordered = [
    ...files.filter((f) => !role || f.partyRole === role),
    ...files.filter((f) => role && f.partyRole !== role),
  ]

  const byExact = ordered.find((f) => normalizeName(f.file.name) === sid)
  if (byExact) return byExact

  const byBase = ordered.find((f) => baseName(f.file.name) === sidBase && sidBase.length > 0)
  if (byBase) return byBase

  const byIncludes = ordered.find((f) => {
    const n = normalizeName(f.file.name)
    const b = baseName(f.file.name)
    return (
      (sid.length > 2 && (n.includes(sid) || sid.includes(n))) ||
      (sidBase.length > 2 && (b.includes(sidBase) || sidBase.includes(b)))
    )
  })
  if (byIncludes) return byIncludes

  // Single upload in that party — safe fallback for rename-on-server cases
  const partyFiles = role ? files.filter((f) => f.partyRole === role) : files
  if (partyFiles.length === 1) return partyFiles[0]
  if (files.length === 1) return files[0]

  return undefined
}

function detectKind(file: File): 'pdf' | 'image' | 'other' {
  const name = file.name.toLowerCase()
  const type = (file.type || '').toLowerCase()

  if (type === 'application/pdf' || name.endsWith('.pdf')) return 'pdf'

  // Browsers often leave type empty or as octet-stream for camera rolls / Windows
  if (
    type.startsWith('image/') ||
    type === 'application/octet-stream' ||
    type === '' ||
    /\.(jpe?g|png|gif|webp|bmp|heic|heif)$/i.test(name)
  ) {
    if (/\.(jpe?g|png|gif|webp|bmp)$/i.test(name) || type.startsWith('image/')) {
      // TIFF / HEIC: mark as image for messaging, browser may not render
      return 'image'
    }
    if (/\.(tif|tiff|heic|heif)$/i.test(name)) return 'image'
  }

  if (/\.(jpe?g|png|gif|webp|bmp|tif|tiff)$/i.test(name)) return 'image'

  return 'other'
}

function FieldValue({ fieldKey, value }: { fieldKey: string; value: unknown }) {
  if (value == null) return <span className="text-content-secondary">—</span>

  const keyLower = fieldKey.toLowerCase()

  if (ADDRESS_KEYS.has(keyLower) && isPlainObject(value)) {
    const keys = Object.keys(value).filter((k) => value[k] != null && String(value[k]).trim() !== '')
    return (
      <div className="space-y-0.5 rounded-sm border border-line bg-raised/40 px-3 py-2 text-[13px]">
        <div className="flex items-start gap-2">
          <MapPin className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ember" aria-hidden />
          <div className="min-w-0 space-y-0.5 leading-snug text-content">
            {keys.map((k) => (
              <p key={k}>
                <span className="text-content-secondary">{formatLabel(k)}: </span>
                {String(value[k])}
              </p>
            ))}
          </div>
        </div>
      </div>
    )
  }

  if (isPlainObject(value)) {
    return (
      <dl className="grid gap-1.5 rounded-sm border border-line bg-raised/40 px-3 py-2 sm:grid-cols-2">
        {Object.entries(value)
          .filter(([, v]) => v != null)
          .map(([k, v]) => (
            <div key={k} className="min-w-0">
              <dt className="text-[10px] font-bold uppercase tracking-wider text-content-secondary">
                {formatLabel(k)}
              </dt>
              <dd className="mt-0.5 break-words text-[13px] font-medium text-content">
                {typeof v === 'object' ? JSON.stringify(v) : String(v)}
              </dd>
            </div>
          ))}
      </dl>
    )
  }

  if (Array.isArray(value)) {
    return (
      <div className="flex flex-wrap gap-1">
        {value.map((item, i) => (
          <span key={i} className="chip px-2 py-0.5 font-mono text-[11px]">
            {typeof item === 'object' ? JSON.stringify(item) : String(item)}
          </span>
        ))}
      </div>
    )
  }

  return (
    <span className="break-all font-mono text-[13px] font-medium text-content">{String(value)}</span>
  )
}

function FilePreviewPane({ file }: { file: File }) {
  const kind = detectKind(file)
  const isTiffOrHeic = /\.(tif|tiff|heic|heif)$/i.test(file.name)
  const [url, setUrl] = useState<string | null>(null)
  const [imgError, setImgError] = useState(false)
  const [imgLoaded, setImgLoaded] = useState(false)

  // Create object URL in effect (not useMemo). Strict Mode remounts would
  // revoke a memoized URL and leave <img> pointing at a dead blob.
  useEffect(() => {
    const objectUrl = URL.createObjectURL(file)
    setUrl(objectUrl)
    setImgError(false)
    setImgLoaded(false)
    return () => {
      URL.revokeObjectURL(objectUrl)
    }
  }, [file])

  if (!url) {
    return (
      <div className="flex min-h-[200px] items-center justify-center rounded-sm border border-line bg-raised/40 text-[12px] text-content-secondary">
        Loading preview…
      </div>
    )
  }

  if (kind === 'pdf') {
    return (
      <iframe
        title={`Preview ${file.name}`}
        src={url}
        className="h-full min-h-[320px] w-full rounded-sm border border-line bg-raised"
      />
    )
  }

  if (kind === 'image') {
    if (isTiffOrHeic) {
      return (
        <div className="flex min-h-[200px] flex-col items-center justify-center gap-2 rounded-sm border border-dashed border-line bg-raised/40 px-4 py-8 text-center">
          <FileText className="h-8 w-8 text-content-disabled" />
          <p className="text-[13px] font-medium text-content">{file.name}</p>
          <p className="max-w-xs text-[12px] leading-relaxed text-content-secondary">
            TIFF/HEIC previews are not supported in most browsers. Convert to JPG or PNG to
            preview here.
          </p>
          <a href={url} download={file.name} className="btn btn-outline mt-1 h-8 px-3 text-[12px]">
            Download file
          </a>
        </div>
      )
    }

    if (imgError) {
      return (
        <div className="flex min-h-[200px] flex-col items-center justify-center gap-2 rounded-sm border border-dashed border-line bg-raised/40 px-4 py-8 text-center">
          <FileText className="h-8 w-8 text-content-disabled" />
          <p className="text-[13px] font-medium text-content">{file.name}</p>
          <p className="max-w-xs text-[12px] leading-relaxed text-content-secondary">
            Could not render this image in the browser.
          </p>
          <a href={url} download={file.name} className="btn btn-outline mt-1 h-8 px-3 text-[12px]">
            Download file
          </a>
        </div>
      )
    }

    return (
      <div className="relative flex min-h-[280px] w-full items-center justify-center overflow-auto rounded-sm border border-line bg-raised/60 p-3">
        {!imgLoaded && (
          <p className="absolute text-[12px] text-content-secondary">Loading image…</p>
        )}
        <img
          key={url}
          src={url}
          alt={file.name}
          onLoad={() => setImgLoaded(true)}
          onError={() => setImgError(true)}
          className={`mx-auto block h-auto max-h-[min(55vh,480px)] w-auto max-w-full object-contain shadow-sm transition-opacity ${
            imgLoaded ? 'opacity-100' : 'opacity-0'
          }`}
        />
      </div>
    )
  }

  return (
    <div className="flex min-h-[160px] flex-col items-center justify-center gap-2 rounded-sm border border-dashed border-line bg-raised/40 px-4 py-8 text-center">
      <FileText className="h-8 w-8 text-content-disabled" />
      <p className="text-[13px] font-medium text-content">{file.name}</p>
      <p className="text-[12px] text-content-secondary">
        In-browser preview is not available for this file type.
      </p>
      <a href={url} download={file.name} className="btn btn-outline mt-1 h-8 px-3 text-[12px]">
        Download file
      </a>
    </div>
  )
}

export function DocumentPreviewModal({
  doc,
  uploadedFiles,
  onClose,
}: DocumentPreviewModalProps) {
  const matched = doc ? matchUploadedFile(doc, uploadedFiles) : undefined
  const extraction = doc?.extraction || {}
  const preferredOrder = [
    'name',
    'pan_number',
    'dl_number',
    'date_of_birth',
    'father_name',
    'guardian_name',
    'date_of_issue',
    'valid_till',
    'address',
    'pin_code',
    'vehicle_classes',
  ]
  const sortedEntries = doc
    ? [
        ...preferredOrder
          .filter((k) => extraction[k] != null)
          .map((k) => [k, extraction[k]] as [string, unknown]),
        ...Object.entries(extraction).filter(
          ([k, v]) => v != null && !preferredOrder.includes(k),
        ),
      ]
    : []

  useEffect(() => {
    if (!doc) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [doc, onClose])

  return (
    <AnimatePresence>
      {doc && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center p-0 sm:items-center sm:p-4"
          role="presentation"
        >
          <motion.button
            type="button"
            aria-label="Close preview"
            className="absolute inset-0 bg-black/60 backdrop-blur-xs"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            onClick={onClose}
          />

          <motion.div
            role="dialog"
            aria-modal="true"
            aria-labelledby="doc-preview-title"
            initial={{ opacity: 0, y: 24, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 16, scale: 0.98 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            className="relative z-10 flex max-h-[92vh] w-full max-w-4xl flex-col overflow-hidden rounded-t-lg border border-line bg-surface shadow-xl sm:rounded-lg"
          >
            <div className="flex shrink-0 items-start justify-between gap-3 border-b border-line-divider px-4 py-3.5 sm:px-6">
              <div className="min-w-0 space-y-1.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="inline-flex h-8 w-8 items-center justify-center rounded-xs border border-line bg-raised text-ember">
                    <Eye className="h-4 w-4" aria-hidden />
                  </span>
                  <h2
                    id="doc-preview-title"
                    className="truncate font-display text-[16px] font-bold text-content sm:text-[17px]"
                  >
                    Document preview
                  </h2>
                </div>
                <div className="flex flex-wrap items-center gap-2 sm:pl-10">
                  <span className="font-mono text-[12px] font-semibold text-content">
                    {doc.source_id}
                  </span>
                  <span className="chip text-[10px] font-bold uppercase">{doc.type}</span>
                  {partyLabel(doc.party_role) && (
                    <span className="chip border-ember/20 bg-ember-subtle text-[10px] font-semibold text-ember-text">
                      {partyLabel(doc.party_role)}
                    </span>
                  )}
                  <StatusChip label={doc.status} className={docStatusClass(doc.status)} />
                  <StatusChip
                    label={doc.verification}
                    className={verificationClass(doc.verification)}
                  />
                </div>
              </div>
              <button
                type="button"
                onClick={onClose}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xs text-content-secondary transition-colors hover:bg-raised hover:text-content focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
                aria-label="Close"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto">
              <div className="grid gap-0 lg:grid-cols-5">
                <div className="space-y-3 border-b border-line-divider p-4 sm:p-5 lg:col-span-2 lg:border-b-0 lg:border-r">
                  <div className="flex items-center gap-2">
                    <ZoomIn className="h-3.5 w-3.5 text-content-secondary" aria-hidden />
                    <p className="text-[11px] font-bold uppercase tracking-wider text-content-secondary">
                      Source file
                    </p>
                  </div>
                  {matched ? (
                    <>
                      <p className="truncate text-[12px] text-content-secondary">
                        {matched.file.name}
                        <span className="text-content-disabled">
                          {' '}
                          · {(matched.file.size / 1024).toFixed(0)} KB
                        </span>
                      </p>
                      <div className="h-[min(50vh,420px)]">
                        <FilePreviewPane file={matched.file} />
                      </div>
                    </>
                  ) : (
                    <div className="flex min-h-[180px] flex-col items-center justify-center gap-2 rounded-sm border border-dashed border-line bg-raised/40 px-4 py-10 text-center">
                      <FileText className="h-8 w-8 text-content-disabled" />
                      <p className="text-[13px] font-medium text-content">No local file attached</p>
                      <p className="max-w-xs text-[12px] leading-relaxed text-content-secondary">
                        Original file preview is available when the upload is still in this
                        session. Extracted fields are shown on the right.
                      </p>
                    </div>
                  )}
                </div>

                <div className="space-y-4 p-4 sm:p-5 lg:col-span-3">
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-[11px] font-bold uppercase tracking-wider text-content-secondary">
                      Extracted fields
                    </p>
                    <span className="chip tabular-nums text-[11px]">
                      {sortedEntries.length} fields
                    </span>
                  </div>

                  {(doc.advisories?.length ||
                    doc.reasons?.length ||
                    doc.reason_codes?.length) ? (
                    <div className="flex flex-wrap gap-1.5">
                      {doc.advisories?.map((a) => (
                        <span
                          key={a}
                          className="chip bg-warning-subtle text-[11px] text-warning-text"
                        >
                          {a}
                        </span>
                      ))}
                      {doc.reasons?.map((r, i) => (
                        <span
                          key={`r-${i}`}
                          className="chip bg-danger-subtle text-[11px] text-danger-text"
                        >
                          {r}
                        </span>
                      ))}
                      {doc.reason_codes?.map((c) => (
                        <span key={c} className="chip font-mono text-[11px]">
                          {c}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  {sortedEntries.length > 0 ? (
                    <div className="grid gap-x-5 gap-y-3 sm:grid-cols-2">
                      {sortedEntries.map(([k, v]) => {
                        const wide =
                          ADDRESS_KEYS.has(k.toLowerCase()) ||
                          isPlainObject(v) ||
                          String(v).length > 48
                        return (
                          <div
                            key={k}
                            className={`border-b border-line-divider/70 pb-2.5 ${
                              wide ? 'sm:col-span-2' : ''
                            }`}
                          >
                            <p className="font-display text-[10px] font-bold uppercase tracking-wider text-content-secondary">
                              {formatLabel(k)}
                            </p>
                            <div className="mt-1">
                              <FieldValue fieldKey={k} value={v} />
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  ) : (
                    <p className="rounded-sm border border-dashed border-line py-10 text-center text-[13px] text-content-secondary">
                      No attributes extracted from this document.
                    </p>
                  )}
                </div>
              </div>
            </div>

            <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line-divider bg-raised/30 px-4 py-3 sm:px-6">
              <button type="button" onClick={onClose} className="btn btn-secondary h-9 px-4 text-[13px]">
                Close
              </button>
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
