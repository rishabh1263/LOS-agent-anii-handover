import type { DocumentTypeHint, LosProcessResponse, PartyRole, UploadFileItem } from './los'

export type WizardStep = 'details' | 'party' | 'documents' | 'report'
export type ProfileFieldKey = 'name' | 'dob' | 'pan' | string
export type ActiveParty = PartyRole

export interface ProfileField {
  key: ProfileFieldKey
  label: string
  value: string
  builtin?: boolean
}

export interface PartySelection {
  applicant: boolean
  coApplicant: boolean
}

/** Per-document pipeline status */
export type DocVerifyStatus =
  | 'idle'
  | 'uploading'
  | 'verifying'
  | 'extracting'
  | 'success'
  | 'error'
  | 'type_mismatch'

/** One field compared: system profile vs EXTRACT output */
export interface ProfileFieldMatch {
  key: string
  label: string
  entered: string
  extracted: string | null
  status: 'match' | 'mismatch' | 'missing'
}

export interface VerifiedDoc {
  item: UploadFileItem
  status: DocVerifyStatus
  error?: string
  progress?: number // 0–100
  detectedType?: string | null
  /** Profile vs extraction (filled after successful EXTRACT) */
  profileMatches?: ProfileFieldMatch[]
  verifyResponse?: LosProcessResponse | null
  extractResponse?: LosProcessResponse | null
  response?: LosProcessResponse | null
}

export const DEFAULT_PROFILE_FIELDS: ProfileField[] = [
  { key: 'name', label: 'Full name', value: '', builtin: true },
  { key: 'dob', label: 'Date of birth', value: '', builtin: true },
  { key: 'pan', label: 'PAN', value: '', builtin: true },
]

export interface DocTypeOption {
  value: DocumentTypeHint
  label: string
  short: string
}

export interface DocCategory {
  id: string
  label: string
  types: DocTypeOption[]
}

/** Production document catalogue grouped by purpose */
export const DOC_CATEGORIES: DocCategory[] = [
  {
    id: 'age_proof',
    label: 'Age Proof',
    types: [
      { value: 'AUTO', label: 'Aadhaar', short: 'Aadhaar' },
      { value: 'VOTER_ID', label: 'Voter ID', short: 'Voter ID' },
    ],
  },
  {
    id: 'signature',
    label: 'Signature Verification',
    types: [
      { value: 'BANK_SIGNATURE', label: 'Bank Sign Verification', short: 'Bank Sign' },
      { value: 'PAN', label: 'PAN', short: 'PAN' },
      { value: 'DRIVING_LICENCE', label: 'Driving License', short: 'DL' },
      { value: 'PASSPORT', label: 'Passport', short: 'Passport' },
    ],
  },
  {
    id: 'identity',
    label: 'Identity Proof',
    types: [
      { value: 'PAN', label: 'PAN', short: 'PAN' },
      { value: 'AUTO', label: 'ID Proof', short: 'ID' },
    ],
  },
  {
    id: 'address',
    label: 'Address Proof',
    types: [
      { value: 'AUTO', label: 'Aadhaar', short: 'Aadhaar' },
      { value: 'DRIVING_LICENCE', label: 'Driving License', short: 'DL' },
    ],
  },
  {
    id: 'income',
    label: 'Income Proof',
    types: [
      { value: 'ITR', label: 'ITR Return Document', short: 'ITR' },
      { value: 'BANK_STATEMENT', label: 'Bank Statement', short: 'Bank' },
      { value: 'SALARY_SLIP', label: 'Salary Slip', short: 'Salary' },
    ],
  },
  {
    id: 'property',
    label: 'Property Ownership Proof',
    types: [{ value: 'SALE_DEED', label: 'Sale Deed', short: 'Sale Deed' }],
  },
  {
    id: 'business',
    label: 'Business Photographs',
    types: [
      { value: 'BUSINESS_PROOF_1', label: 'Business Proof 1', short: 'Biz 1' },
      { value: 'BUSINESS_PROOF_2', label: 'Business Proof 2', short: 'Biz 2' },
    ],
  },
  {
    id: 'other',
    label: 'Other',
    types: [{ value: 'AUTO', label: 'Other document', short: 'Other' }],
  },
]

export const WIZARD_DOC_TYPES: DocTypeOption[] = DOC_CATEGORIES.flatMap((c) => c.types)

/** Normalize type strings (DRIVING_LICENCE ≈ DRIVING_LICENSE) */
export function normalizeDocType(type: string | null | undefined): string {
  if (!type) return ''
  return type.toUpperCase().replace(/[\s-]+/g, '_').replace(/LICENCE/g, 'LICENSE')
}

/** True when expected matches detected; AUTO / empty detected always pass */
export function isDocTypeMatch(
  expected: DocumentTypeHint | string,
  detected: string | null | undefined,
): boolean {
  if (!expected || expected === 'AUTO' || !detected) return true
  const a = normalizeDocType(expected)
  const b = normalizeDocType(detected)
  if (a === b) return true
  return a.replace(/_SIGNATURE$/, '') === b.replace(/_SIGNATURE$/, '')
}

/** Human label for a type enum */
export function formatDocTypeLabel(type: string | null | undefined): string {
  if (!type) return 'Unknown'
  const n = normalizeDocType(type)
  const hit = WIZARD_DOC_TYPES.find(
    (t) => normalizeDocType(t.value) === n || normalizeDocType(t.short) === n,
  )
  if (hit) return hit.label
  return n
    .replace(/_SIGNATURE$/i, ' (Signature)')
    .split('_')
    .filter(Boolean)
    .map((w) => w.charAt(0) + w.slice(1).toLowerCase())
    .join(' ')
}

/** User-facing mismatch message for VERIFY or EXTRACT */
export function buildTypeMismatchError(
  stage: 'VERIFY' | 'EXTRACT',
  selected: string,
  detected: string,
): string {
  const sel = formatDocTypeLabel(selected)
  const det = formatDocTypeLabel(detected)
  const when =
    stage === 'VERIFY' ? 'during authenticity check' : 'after field extraction'
  return `Rejected ${when}: selected “${sel}”, system detected “${det}”. Remove and re-upload under the correct type.`
}
