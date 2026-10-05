import type { CheckStatus, DocStatus, VerificationStatus } from '../types'

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

export function docStatusClass(status: DocStatus): string {
  return STATUS_MAP[status] ?? FALLBACK
}

export function verificationClass(v: VerificationStatus): string {
  return STATUS_MAP[v] ?? FALLBACK
}

export function checkStatusClass(s: CheckStatus): string {
  return STATUS_MAP[s] ?? FALLBACK
}
