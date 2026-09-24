interface ConfirmModalProps {
  open: boolean
  title: string
  description: string
  confirmLabel?: string
  cancelLabel?: string
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmModal({
  open,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  if (!open) return null

  return (
    <div
      className="absolute inset-0 z-20 flex items-center justify-center bg-[rgba(24,24,27,0.5)] p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
    >
      <div className="w-full max-w-[360px] rounded-lg border border-line bg-surface p-5 shadow-lg">
        <h3
          id="confirm-title"
          className="font-display text-[18px] font-semibold tracking-[-0.015em] text-content"
        >
          {title}
        </h3>
        <p className="mt-2 font-sans text-[14px] leading-[22px] text-content-secondary">
          {description}
        </p>
        <div className="mt-5 flex gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="flex h-11 flex-1 items-center justify-center rounded-sm border border-line bg-surface font-sans text-[14px] font-medium text-content transition-colors hover:bg-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="flex h-11 flex-1 items-center justify-center rounded-sm bg-ink font-sans text-[14px] font-medium text-on-ink transition-colors hover:bg-ink-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
