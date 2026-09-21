/** Types matching the LOS Process API PRD */

export type Operation = 'PROCESS' | 'EXTRACT' | 'VERIFY'

export type AppStatus = 'SUCCESS' | 'PARTIAL' | 'REVIEW' | 'REJECTED' | 'FAILED'
export type DocStatus = 'SUCCESS' | 'REVIEW' | 'SKIPPED' | 'FAILED'
export type VerificationStatus = 'PASS' | 'REVIEW' | 'FAIL' | 'SKIPPED'
export type Decision = 'PASS' | 'REVIEW' | 'REJECT'
export type NextAction =
  | 'CONTINUE'
  | 'MANUAL_REVIEW'
  | 'REQUEST_VALID_DOCUMENT'
  | 'REQUEST_CORRECT_DOCUMENT'
export type CheckStatus = 'PASS' | 'REVIEW' | 'FAIL' | 'SKIPPED'
export type SummarySource = 'llm' | 'deterministic'

export type DocumentTypeHint =
  | 'AUTO'
  | 'PAN'
  | 'DRIVING_LICENCE'
  | 'VOTER_ID'
  | 'PASSPORT'
  | 'BANK_STATEMENT'
  | 'ITR'
  | 'SALARY_SLIP'
  | 'SALE_DEED'
  | 'BUSINESS_PROOF_1'
  | 'BUSINESS_PROOF_2'
  | 'BANK_SIGNATURE'
  | 'PAN_SIGNATURE'
  | 'DRIVING_LICENSE_SIGNATURE'
  | 'PASSPORT_SIGNATURE'
  | 'SIGNATURE'
  | 'STANDALONE_SIGNATURE'

export interface LosError {
  code: string
  message: string
  source_id?: string | null
}

export interface SignatureBlock {
  present?: boolean
  presence?: string
  quality?: string
  comparison?: string
  match_score?: number | null
  synthetic_risk?: string
  manipulation_risk?: string
}

export interface SpecialistResult {
  decision: string
  reason_codes?: string[]
  subtype?: string | null
  signature?: SignatureBlock
  ownership_verified?: boolean | null
  authenticity_verified?: boolean | null
  business_existence_verified?: boolean | null
  [key: string]: unknown
}

export interface DocumentResult {
  source_id: string
  type: string
  status: DocStatus
  verification: VerificationStatus
  extraction?: Record<string, unknown> | null
  reason_codes?: string[]
  expected_type?: string | null
  hint?: string | null
  specialist?: SpecialistResult | null
  evidence_refs?: Array<{ source_id: string; locator: string }>
  errors?: LosError[]
  has_extracted_fields?: boolean
  authenticity?: string
  advisories?: string[]
}

export interface KycFieldSource {
  source_id: string
  document_type: string
  value: string | Record<string, unknown>
  normalized_value?: string | Record<string, unknown>
}

export interface KycField {
  field: string
  status: CheckStatus
  match_score: number
  confidence: number
  reason_code?: string
  reason?: string
  sources?: KycFieldSource[]
}

export interface KycResult {
  status: CheckStatus
  reason_codes?: string[]
  overall_score?: number
  overall_confidence?: number
  fields?: KycField[]
}

export interface CrossCheck {
  check: 'NAME' | 'DOB' | 'ADDRESS' | 'PAN' | 'INCOME' | string
  status: CheckStatus
  reason_codes?: string[]
  sources?: string[]
  details?: Record<string, string> | null
}

export interface CrossDocument {
  status: CheckStatus
  checks: CrossCheck[]
}

export interface LosProcessResponse {
  request_id: string
  applicant_id: string | null
  case_id: string
  status: AppStatus
  documents: DocumentResult[]
  kyc: KycResult
  cross_document: CrossDocument
  decision: Decision
  next_action: NextAction
  summary: string
  summary_source: SummarySource
  processing_ms: number
  errors: LosError[]
}

export interface ApiErrorBody {
  request_id?: string
  error?: string
  message?: string
  detail?: string
}

export interface UploadFileItem {
  id: string
  file: File
  expectedType: DocumentTypeHint
}

export const DOCUMENT_TYPE_OPTIONS: { value: DocumentTypeHint; label: string }[] = [
  { value: 'AUTO', label: 'Auto-detect' },
  { value: 'PAN', label: 'PAN Card' },
  { value: 'DRIVING_LICENCE', label: 'Driving Licence' },
  { value: 'VOTER_ID', label: 'Voter ID' },
  { value: 'PASSPORT', label: 'Passport' },
  { value: 'BANK_STATEMENT', label: 'Bank Statement' },
  { value: 'ITR', label: 'ITR' },
  { value: 'SALARY_SLIP', label: 'Salary Slip' },
  { value: 'SALE_DEED', label: 'Sale Deed' },
  { value: 'BUSINESS_PROOF_1', label: 'Business Proof 1' },
  { value: 'BUSINESS_PROOF_2', label: 'Business Proof 2' },
  { value: 'BANK_SIGNATURE', label: 'Bank Signature' },
  { value: 'PAN_SIGNATURE', label: 'PAN Signature' },
  { value: 'DRIVING_LICENSE_SIGNATURE', label: 'DL Signature' },
  { value: 'PASSPORT_SIGNATURE', label: 'Passport Signature' },
  { value: 'SIGNATURE', label: 'Signature' },
  { value: 'STANDALONE_SIGNATURE', label: 'Standalone Signature' },
]

export const ACCEPTED_MIME = [
  'application/pdf',
  'image/jpeg',
  'image/png',
  'image/tiff',
  'image/webp',
] as const

export const ACCEPTED_EXT = '.pdf,.jpg,.jpeg,.png,.tif,.tiff,.webp'
export const MAX_BYTES = 25 * 1024 * 1024
export const MAX_FILES = 10
