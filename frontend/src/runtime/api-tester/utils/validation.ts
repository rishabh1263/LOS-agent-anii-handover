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
