/**
 * Decision helper: when (and which docs) to show the upload option.
 * Never decide from answer text — only from structured backend fields.
 * See LOS-agentic-ai/docs/APPLICANT_AGENT.md §17.
 */

export type UploadTarget = {
  slot: string
  acceptedTypes: string[]
  reason: string
  /** Human label when available */
  label?: string
  status?: string
}

export type UploadDecision = {
  targets: UploadTarget[]
  showGeneralUpload: boolean
}

const UPLOAD_CODES = ['DOCUMENT_MISSING', 'DOCUMENT_REJECTED'] as const
const UPLOAD_NEXT = ['COLLECT_DOCUMENT', 'REQUEST_CORRECT_DOCUMENT'] as const

type LooseAction = {
  action?: string
  enabled?: boolean
  document_type?: string
  accepted_types?: string[]
  [key: string]: unknown
}

type LooseChecklistRow = {
  slot?: string
  label?: string
  status?: string
  accepted_types?: string[]
  accepts?: string[]
  actions?: LooseAction[]
  [key: string]: unknown
}

type LoosePending = {
  type?: string
  code?: string
  slot?: string
  accepts?: string[]
  label?: string
  [key: string]: unknown
}

type LooseNextAction = {
  action?: string
  target?: string
  [key: string]: unknown
}

type LooseVerificationDoc = {
  document_type?: string
  verdict?: string
  next_action?: {
    action?: string
    accepted_types?: string[]
  }
  [key: string]: unknown
}

/** Minimal shape we read from a /fos/copilot (or documents) response */
export type FosUploadResponse = {
  actions?: LooseAction[]
  checklist?: LooseChecklistRow[]
  pending_items?: LoosePending[]
  next_action?: LooseNextAction | null
  verification?: {
    documents?: LooseVerificationDoc[]
    documents_processed?: unknown[]
  } | null
  available_actions?: LooseAction[]
  [key: string]: unknown
}

/**
 * Returns the set of documents the UI should offer for upload, plus a
 * flag for a generic "Upload documents" button.
 *
 * Checks all six signals because free-text intents strip unrelated fields.
 */
export function getUploadTargets(r: FosUploadResponse | null | undefined): UploadDecision {
  const out = new Map<string, UploadTarget>()

  const add = (slot?: string, types?: string[], reason = '', label?: string, status?: string) => {
    if (!slot || out.has(slot)) return
    out.set(slot, {
      slot,
      acceptedTypes: types?.length ? types : [slot],
      reason,
      label,
      status,
    })
  }

  r?.actions?.forEach((a) => {
    if (a.action === 'UPLOAD_DOCUMENT') {
      add(a.document_type, a.accepted_types, 'requested')
    }
  })

  r?.checklist?.forEach((row) => {
    const canUpload = row.actions?.some(
      (a) => a.action === 'UPLOAD_DOCUMENT' && a.enabled !== false,
    )
    if (canUpload) {
      add(
        row.slot,
        row.accepted_types ?? row.accepts,
        row.status ?? 'MISSING',
        row.label,
        row.status,
      )
    }
  })

  r?.pending_items?.forEach((p) => {
    if (p.type === 'DOCUMENT' && p.code && UPLOAD_CODES.includes(p.code as (typeof UPLOAD_CODES)[number])) {
      add(p.slot, p.accepts, p.code, p.label)
    }
  })

  if (r?.next_action && UPLOAD_NEXT.includes(r.next_action.action as (typeof UPLOAD_NEXT)[number])) {
    add(r.next_action.target, undefined, r.next_action.action ?? '')
  }

  r?.verification?.documents?.forEach((d) => {
    if (d.next_action?.action === 'UPLOAD_DOCUMENT') {
      add(d.document_type, d.next_action.accepted_types, d.verdict ?? 'FAIL')
    }
  })

  const showGeneralUpload =
    r?.available_actions?.some((a) => a.action === 'UPLOAD_DOCUMENT' && a.enabled !== false) ?? false

  return { targets: [...out.values()], showGeneralUpload }
}

/** Status chips that replace the upload button (do NOT show upload). */
export function getStatusChip(
  r: FosUploadResponse | null | undefined,
): { label: string; tone: 'review' | 'verifying' | 'waiting' | 'done' | 'ready' } | null {
  const pending = r?.pending_items as LoosePending[] | undefined
  if (pending?.some((p) => p.code === 'DOCUMENT_UNDER_REVIEW')) {
    return { label: 'Under review', tone: 'review' }
  }
  if (
    pending?.some((p) => p.code === 'DOCUMENT_NOT_VERIFIED') ||
    (r?.next_action as LooseNextAction | undefined)?.action === 'AWAIT_VERIFICATION'
  ) {
    return { label: 'Verifying…', tone: 'verifying' }
  }
  if ((r?.next_action as LooseNextAction | undefined)?.action === 'RESOLVE_DOCUMENT_REVIEW') {
    return { label: 'Waiting for reviewer', tone: 'waiting' }
  }
  if ((r?.next_action as LooseNextAction | undefined)?.action === 'SUBMIT_TO_CPA') {
    return { label: 'All documents done, ready for CPA', tone: 'ready' }
  }
  return null
}
