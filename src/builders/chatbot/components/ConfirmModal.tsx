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
      className="absolute inset-0 z-20 flex items-center justify-center bg-black/40 p-4 backdrop-blur-[2px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
    >
      <div className="w-full max-w-[340px] rounded-2xl border border-line bg-surface p-5 shadow-xl">
        <h3
          id="confirm-title"
          className="font-sans text-[16px] font-semibold tracking-[-0.015em] text-content"
        >
          {title}
        </h3>
        <p className="mt-2 font-sans text-[13.5px] leading-relaxed text-content-secondary">
          {description}
        </p>
        <div className="mt-5 flex gap-2.5">
          <button
            type="button"
            onClick={onCancel}
            className="flex h-10 flex-1 items-center justify-center rounded-xl border border-line bg-surface font-sans text-[13.5px] font-medium text-content transition-colors hover:bg-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="flex h-10 flex-1 items-center justify-center rounded-xl bg-ember font-sans text-[13.5px] font-medium text-oncolor transition-colors hover:opacity-90 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
