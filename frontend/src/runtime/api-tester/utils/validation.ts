import {
  ACCEPTED_MIME,
  MAX_BYTES,
  MAX_FILES,
  type UploadFileItem,
} from '../types'

export interface ValidationIssue {
  id?: string
  message: string
}

/** Indian mobile: 10 digits, starts with 6–9 (optional +91 / 0 prefix stripped). */
export function isValidMobile(value: string): boolean {
  const digits = value.replace(/\D/g, '')
  const local = digits.length === 12 && digits.startsWith('91') ? digits.slice(2) : digits.length === 11 && digits.startsWith('0') ? digits.slice(1) : digits
  return /^[6-9]\d{9}$/.test(local)
}

/** PAN: 5 letters + 4 digits + 1 letter (e.g. ABCDE1234F). */
export function isValidPan(value: string): boolean {
  return /^[A-Z]{5}[0-9]{4}[A-Z]$/i.test(value.trim())
}

/** Aadhaar: 12 digits, not starting with 0/1 (Verhoeff not enforced client-side). */
export function isValidAadhaar(value: string): boolean {
  const digits = value.replace(/\s/g, '')
  return /^[2-9]\d{11}$/.test(digits)
}

export function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/i.test(value.trim())
}

/**
 * Parse DOB from YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, or DD.MM.YYYY.
 * Returns ISO YYYY-MM-DD or null.
 */
export function parseDobToIso(value: string): string | null {
  const raw = value.trim()
  if (!raw) return null

  let y: number
  let mo: number
  let d: number

  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw)
  const dmy = /^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/.exec(raw)

  if (iso) {
    y = Number(iso[1])
    mo = Number(iso[2])
    d = Number(iso[3])
  } else if (dmy) {
    d = Number(dmy[1])
    mo = Number(dmy[2])
    y = Number(dmy[3])
  } else {
    return null
  }

  const dt = new Date(y, mo - 1, d)
  if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return null
  return `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`
}

/** DOB valid and age between 18 and 100 (inclusive). */
export function isValidDob(value: string): boolean {
  const iso = parseDobToIso(value)
  if (!iso) return false
  const [ys, ms, ds] = iso.split('-').map(Number)
  const now = new Date()
  let age = now.getFullYear() - ys
  if (now.getMonth() < ms - 1 || (now.getMonth() === ms - 1 && now.getDate() < ds)) age -= 1
  return age >= 18 && age <= 100
}

export function validateProfileFields(
  fields: { key: string; label: string; value: string }[],
): ValidationIssue[] {
  const issues: ValidationIssue[] = []
  const get = (k: string) => fields.find((f) => f.key === k)?.value.trim() || ''

  if (!get('full_name')) {
    issues.push({ id: 'full_name', message: 'Full name is required.' })
  }

  if (!get('mobile')) {
    issues.push({ id: 'mobile', message: 'Mobile number is required.' })
  } else if (!isValidMobile(get('mobile'))) {
    issues.push({
      id: 'mobile',
      message: 'Mobile must be 10 digits and start with 6, 7, 8, or 9 (e.g. 9876543210).',
    })
  }

  if (!get('email')) {
    issues.push({ id: 'email', message: 'Email is required.' })
  } else if (!isValidEmail(get('email'))) {
    issues.push({ id: 'email', message: 'Enter a valid email address.' })
  }

  if (!get('date_of_birth')) {
    issues.push({ id: 'date_of_birth', message: 'Date of birth is required.' })
  } else if (!isValidDob(get('date_of_birth'))) {
    issues.push({
      id: 'date_of_birth',
      message: 'Enter a valid date of birth (age 18–100). Use the date picker or YYYY-MM-DD.',
    })
  }

  if (!get('address')) {
    issues.push({ id: 'address', message: 'Address is required.' })
  }

  const pan = get('pan')
  if (pan && !isValidPan(pan)) {
    issues.push({
      id: 'pan',
      message: 'PAN must be like ABCDE1234F (5 letters, 4 digits, 1 letter).',
    })
  }

  const aadhaar = get('aadhaar') || get('aadhar')
  if (aadhaar && !isValidAadhaar(aadhaar)) {
    issues.push({
      id: 'aadhaar',
      message: 'Aadhaar must be a 12-digit number (not starting with 0 or 1).',
    })
  }

  return issues
}

export function validateFiles(items: UploadFileItem[]): ValidationIssue[] {
  const issues: ValidationIssue[] = []

  if (items.length === 0) {
    issues.push({ message: 'Add at least one document to process.' })
    return issues
  }

  if (items.length > MAX_FILES) {
    issues.push({ message: `At most ${MAX_FILES} documents per request.` })
  }

  const seenNames = new Set<string>()
  for (const item of items) {
    const { file, id } = item
    if (file.size === 0) {
      issues.push({ id, message: `"${file.name}" is empty.` })
    }
    if (file.size > MAX_BYTES) {
      issues.push({
        id,
        message: `"${file.name}" exceeds 25 MB limit.`,
      })
    }
    const mimeOk =
      ACCEPTED_MIME.includes(file.type as (typeof ACCEPTED_MIME)[number]) ||
      /\.(pdf|jpe?g|png|tiff?|webp)$/i.test(file.name)
    if (!mimeOk) {
      issues.push({
        id,
        message: `"${file.name}" is not a supported format (PDF, JPEG, PNG, TIFF, WebP).`,
      })
    }
    if (seenNames.has(file.name)) {
      issues.push({
        id,
        message: `Duplicate filename "${file.name}". Rename before uploading.`,
      })
    }
    seenNames.add(file.name)
  }

  return issues
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

/** Normalize API decision / verification / status for branch logic. */
export function normalizeDecision(...values: unknown[]): string {
  for (const v of values) {
    if (v == null || v === '') continue
    return String(v).toUpperCase()
  }
  return ''
}

export function isRejectDecision(code: string): boolean {
  return ['FAIL', 'FAILED', 'REJECT', 'REJECTED'].includes(code)
}

export function isReviewDecision(code: string): boolean {
  return code === 'REVIEW' || code === 'MANUAL_REVIEW'
}
