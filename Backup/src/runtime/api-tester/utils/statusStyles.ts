import type {
  AppStatus,
  CheckStatus,
  Decision,
  DocStatus,
  NextAction,
  VerificationStatus,
} from '../types'

const STATUS_MAP: Record<string, string> = {
  SUCCESS: 'bg-success-subtle text-success-text',
  PASS: 'bg-success-subtle text-success-text',
  PARTIAL: 'bg-warning-subtle text-warning-text',
  REVIEW: 'bg-warning-subtle text-warning-text',
  REJECTED: 'bg-danger-subtle text-danger-text',
  REJECT: 'bg-danger-subtle text-danger-text',
  FAILED: 'bg-danger-subtle text-danger-text',
  FAIL: 'bg-danger-subtle text-danger-text',
  SKIPPED: 'bg-raised text-content-secondary',
}

const FALLBACK = 'bg-raised text-content-secondary'

export function appStatusClass(status: AppStatus): string {
  return STATUS_MAP[status] ?? FALLBACK
}

export function docStatusClass(status: DocStatus): string {
  return STATUS_MAP[status] ?? FALLBACK
}

export function verificationClass(v: VerificationStatus): string {
  return STATUS_MAP[v] ?? FALLBACK
}

export function decisionClass(d: Decision): string {
  return STATUS_MAP[d] ?? FALLBACK
}

export function checkStatusClass(s: CheckStatus): string {
  return STATUS_MAP[s] ?? FALLBACK
}

export function nextActionLabel(a: NextAction): string {
  switch (a) {
    case 'CONTINUE':
      return 'Continue'
    case 'MANUAL_REVIEW':
      return 'Manual review'
    case 'REQUEST_VALID_DOCUMENT':
      return 'Request clearer document'
    case 'REQUEST_CORRECT_DOCUMENT':
      return 'Request correct document type'
    default:
      return a
  }
}
