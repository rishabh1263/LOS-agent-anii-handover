import { namesMatch } from './nameMatch'
import type { ProfileField, ProfileFieldMatch } from '../types/wizard'

/** Map profile key → extraction object keys to try */
const EXTRACT_KEYS: Record<string, string[]> = {
  name: ['name', 'full_name', 'applicant_name'],
  dob: ['dob', 'date_of_birth', 'birth', 'date_of_birth_on_card'],
  pan: ['pan', 'pan_number', 'permanent_account_number'],
}

function normalizeExact(s: string) {
  return s.toLowerCase().replace(/[\s\-./]/g, '')
}

function readExtraction(
  extraction: Record<string, unknown> | null | undefined,
  keys: string[],
): string | null {
  if (!extraction || typeof extraction !== 'object') return null
  const entries = Object.entries(extraction)
  for (const key of keys) {
    const kl = key.toLowerCase()
    for (const [k, v] of entries) {
      if (!k.toLowerCase().includes(kl)) continue
      if (typeof v === 'string' && v.trim()) return v.trim()
      if (v != null && typeof v !== 'object') {
        const s = String(v).trim()
        if (s) return s
      }
    }
  }
  return null
}

/**
 * Compare entered profile fields with a single document's EXTRACT payload.
 * Called right after EXTRACT succeeds (before final PROCESS).
 */
export function matchProfileToExtraction(
  profileFields: ProfileField[],
  extraction: Record<string, unknown> | null | undefined,
): ProfileFieldMatch[] {
  const matches: ProfileFieldMatch[] = []

  for (const field of profileFields) {
    const entered = field.value.trim()
    if (!entered) continue

    const keys = EXTRACT_KEYS[field.key] ?? [field.key]
    const extracted = readExtraction(extraction, keys)

    let status: ProfileFieldMatch['status'] = 'missing'
    if (extracted) {
      if (field.key === 'name') {
        status = namesMatch(entered, extracted) ? 'match' : 'mismatch'
      } else {
        status = normalizeExact(entered) === normalizeExact(extracted) ? 'match' : 'mismatch'
      }
    }

    matches.push({
      key: field.key,
      label: field.label,
      entered,
      extracted,
      status,
    })
  }

  return matches
}
