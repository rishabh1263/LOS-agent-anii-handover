import { useCallback, useRef, useState } from 'react'
import { ChevronDown, FileText, Trash2, UploadCloud } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import {
  ACCEPTED_EXT,
  DOCUMENT_TYPE_OPTIONS,
  MAX_FILES,
  formatBytes,
  type DocumentTypeHint,
  type UploadFileItem,
} from '../../../runtime/api-tester'

export interface FileDropZoneProps {
  items: UploadFileItem[]
  onChange: (items: UploadFileItem[]) => void
  disabled?: boolean
}

function makeId() {
  return `f_${Math.random().toString(36).slice(2, 10)}`
}

export function FileDropZone({ items, onChange, disabled }: FileDropZoneProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)

  const addFiles = useCallback(
    (fileList: FileList | File[]) => {
      const incoming = Array.from(fileList)
      const room = MAX_FILES - items.length
      if (room <= 0) return
      onChange([
        ...items,
        ...incoming.slice(0, room).map((file) => ({
          id: makeId(),
          file,
          expectedType: 'AUTO' as DocumentTypeHint,
        })),
      ])
    },
    [items, onChange],
  )

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    if (disabled) return
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files)
  }

  const updateType = (id: string, expectedType: DocumentTypeHint) => {
    onChange(items.map((it) => (it.id === id ? { ...it, expectedType } : it)))
  }

  const remove = (id: string) => {
    onChange(items.filter((it) => it.id !== id))
  }

  const openPicker = () => {
    if (!disabled) inputRef.current?.click()
  }

  return (
    <div className="space-y-3">
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label="Upload documents. Drop files or press Enter to browse."
        aria-disabled={disabled}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            openPicker()
          }
        }}
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled) setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={openPicker}
        className={`
          group relative flex flex-col items-center justify-center gap-3
          rounded-lg border-2 border-dashed px-5 py-8 transition-colors duration-150
          focus:outline-none focus-visible:ring-2 focus-visible:ring-ember
          ${
            dragOver
              ? 'border-ember bg-ember-tint'
              : 'border-line bg-surface hover:border-line-strong hover:bg-raised/40'
          }
          ${disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'}
        `}
      >
        <div
          className={`flex h-11 w-11 items-center justify-center rounded-xs transition-all duration-150 ${
            dragOver
              ? 'bg-ember text-oncolor scale-105'
              : 'bg-raised text-icon-default group-hover:text-content group-hover:scale-105'
          }`}
          aria-hidden="true"
        >
          <UploadCloud className="h-5 w-5" />
        </div>

        <div className="text-center">
          <p className="font-sans text-[14px] font-medium text-content">
            Drop files or{' '}
            <span className="font-semibold text-link underline decoration-link/40 underline-offset-2 hover:decoration-link">
              browse
            </span>
          </p>
          <p className="mt-1 font-sans text-[12px] text-content-secondary">
            PDF, JPEG, PNG, TIFF, WebP · max 25 MB · up to {MAX_FILES} files
          </p>
        </div>

        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPTED_EXT}
          className="sr-only"
          disabled={disabled}
          onChange={(e) => {
            if (e.target.files?.length) addFiles(e.target.files)
            e.target.value = ''
          }}
        />
      </div>

      {items.length > 0 && (
        <ul className="space-y-2 pt-1" aria-label="Selected files">
          <AnimatePresence>
            {items.map((item, index) => (
              <motion.li
                key={item.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.96 }}
                transition={{ duration: 0.15 }}
                className="flex flex-wrap items-center gap-3 rounded-sm border border-line bg-surface p-3 transition hover:border-line-strong"
              >
                {/* Icon well — Section 9.6: 34px, radius 6px, raised background */}
                <div className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-xs bg-raised text-content-secondary">
                  <span className="font-sans text-[11px] font-bold tabular-nums text-content">
                    {index + 1}
                  </span>
                </div>

                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <FileText className="h-3.5 w-3.5 text-content-secondary shrink-0" />
                    <p className="truncate font-sans text-[14px] font-medium text-content">{item.file.name}</p>
                  </div>
                  <div className="flex items-center gap-2 font-sans text-[12px] text-content-secondary mt-0.5">
                    <span className="font-mono tabular-nums">{formatBytes(item.file.size)}</span>
                  </div>
                </div>

                <label className="sr-only" htmlFor={`type-${item.id}`}>
                  Document type for {item.file.name}
                </label>
                <div className="relative">
                  <select
                    id={`type-${item.id}`}
                    value={item.expectedType}
                    disabled={disabled}
                    onChange={(e) => updateType(item.id, e.target.value as DocumentTypeHint)}
                    className="h-9 min-w-[150px] appearance-none rounded-sm border border-line bg-surface py-1 pl-3 pr-8 font-sans text-[13px] font-medium text-content focus:border-2 focus:border-ember focus:outline-none"
                  >
                    {DOCUMENT_TYPE_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                  <ChevronDown
                    className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-content-secondary"
                    aria-hidden="true"
                  />
                </div>

                <button
                  type="button"
                  disabled={disabled}
                  onClick={() => remove(item.id)}
                  className="inline-flex items-center gap-1 h-9 rounded-xs px-2.5 font-sans text-[13px] font-medium text-danger transition hover:bg-danger-subtle hover:text-danger-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
                  aria-label={`Remove ${item.file.name}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  <span>Remove</span>
                </button>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
      )}
    </div>
  )
}
