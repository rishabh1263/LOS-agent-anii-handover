import type { DocumentTypeHint, LosProcessResponse, PartyRole, UploadFileItem } from './los'

export type WizardStep = 'details' | 'application' | 'party' | 'documents' | 'report'

export type ProfileFieldKey =
  | 'full_name'
  | 'mobile'
  | 'email'
  | 'date_of_birth'
  | 'address'
  | 'name'
  | 'dob'
  | 'pan'
  | string

export interface ProfileField {
  key: ProfileFieldKey
  label: string
  value: string
  /** Built-in fields cannot be removed */
  builtin?: boolean
  /** HTML input type hint */
  inputType?: 'text' | 'email' | 'tel' | 'date'
}

export interface ApplicationDetails {
  product: string
  loan_amount: string
  employment_type: string
  tenure_months: string
  interest_rate_pct: string
  declared_monthly_obligations: string
  property_value: string
}

export const DEFAULT_APPLICATION: ApplicationDetails = {
  product: 'PERSONAL_LOAN',
  loan_amount: '',
  employment_type: 'SALARIED',
  tenure_months: '',
  interest_rate_pct: '',
  declared_monthly_obligations: '',
  property_value: '',
}

export const PRODUCT_OPTIONS = [
  { value: 'PERSONAL_LOAN', label: 'Personal Loan' },
  { value: 'HOME_LOAN', label: 'Home Loan' },
  { value: 'BUSINESS_LOAN', label: 'Business Loan' },
  { value: 'LAP', label: 'Loan Against Property' },
] as const

export const EMPLOYMENT_OPTIONS = [
  { value: 'SALARIED', label: 'Salaried' },
  { value: 'SELF_EMPLOYED', label: 'Self Employed' },
  { value: 'PROFESSIONAL', label: 'Professional' },
  { value: 'OTHER', label: 'Other' },
] as const

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
  | 'review'
  | 'error'
  | 'type_mismatch'

export interface VerifiedDoc {
  item: UploadFileItem
  status: DocVerifyStatus
  error?: string
  /** 0–100 pipeline progress for UI bar */
  progress?: number
  /** Detected type from API response when available */
  detectedType?: string | null
  /** VERIFY response (authenticity) */
  verifyResponse?: LosProcessResponse | null
  /** EXTRACT response (fields) */
  extractResponse?: LosProcessResponse | null
  response?: LosProcessResponse | null
}

export const DEFAULT_PROFILE_FIELDS: ProfileField[] = [
  { key: 'full_name', label: 'Full name', value: '', builtin: true },
  { key: 'mobile', label: 'Mobile', value: '', builtin: true, inputType: 'tel' },
  { key: 'email', label: 'Email', value: '', builtin: true, inputType: 'email' },
  { key: 'date_of_birth', label: 'Date of birth', value: '', builtin: true, inputType: 'date' },
  { key: 'address', label: 'Address', value: '', builtin: true },
  { key: 'pan', label: 'PAN', value: '', builtin: true },
]

/** Single selectable document type in the upload grid */
export interface DocTypeOption {
  value: DocumentTypeHint
  label: string
  short: string
}

/** Grouped document types for the upload step */
export interface DocCategory {
  id: string
  label: string
  types: DocTypeOption[]
}

/**
 * Production document catalogue grouped by purpose.
 * Values align with LOS DocumentTypeHint where possible.
 */
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

/** Flat list for lookups (legacy consumers) */
export const WIZARD_DOC_TYPES: DocTypeOption[] = DOC_CATEGORIES.flatMap((c) => c.types)

/**
 * Normalize API / expected type strings for comparison.
 * Treats DRIVING_LICENCE ≈ DRIVING_LICENSE, etc.
 */
export function normalizeDocType(type: string | null | undefined): string {
  if (!type) return ''
  return type
    .toUpperCase()
    .replace(/[\s-]+/g, '_')
    .replace(/LICENCE/g, 'LICENSE')
}

/**
 * True when expected hint matches detected API type.
 * AUTO always accepts. Empty detected type is treated as unknown (accept).
 */
export function isDocTypeMatch(
  expected: DocumentTypeHint | string,
  detected: string | null | undefined,
): boolean {
  if (!expected || expected === 'AUTO') return true
  if (!detected) return true
  const a = normalizeDocType(expected)
  const b = normalizeDocType(detected)
  if (a === b) return true
  // Signature variants may map back to base identity types
  if (a.replace(/_SIGNATURE$/, '') === b.replace(/_SIGNATURE$/, '')) return true
  return false
}

export type ActiveParty = PartyRole
