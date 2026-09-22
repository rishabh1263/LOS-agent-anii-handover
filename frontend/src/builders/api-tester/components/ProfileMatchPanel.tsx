import { CheckCircle2, XCircle, MinusCircle } from 'lucide-react'
import type { LosProcessResponse } from '../../../runtime/api-tester'
import { namesMatch } from '../../../runtime/api-tester'

export interface ProfileMatchPanelProps {
  profile: Record<string, string>
  result: LosProcessResponse | null
}

function normalize(s: string) {
  return s.toLowerCase().replace(/[\s\-./]/g, '')
}

function findExtractedValue(result: LosProcessResponse, keys: string[]): string | null {
  const fields = result.kyc?.fields ?? []
  for (const key of keys) {
    const f = fields.find(
      (x) => x.field?.toLowerCase() === key || x.field?.toLowerCase().includes(key),
    )
    if (f?.sources?.[0]) {
      const v = f.sources[0].value
      if (typeof v === 'string' && v.trim()) return v.trim()
      if (v && typeof v === 'object' && 'value' in (v as object)) {
        const inner = (v as { value?: unknown }).value
        if (typeof inner === 'string') return inner
      }
    }
  }

  for (const doc of result.documents ?? []) {
    const ext = doc.extraction
    if (!ext || typeof ext !== 'object') continue
    for (const key of keys) {
      for (const [k, v] of Object.entries(ext)) {
        if (k.toLowerCase().includes(key) && typeof v === 'string' && v.trim()) {
          return v.trim()
        }
      }
    }
  }
  return null
}

const FIELD_MAP: {
  profileKey: string
  label: string
  extractKeys: string[]
  kind: 'name' | 'exact'
}[] = [
  {
    profileKey: 'name',
    label: 'Name',
    extractKeys: ['name', 'full_name', 'applicant_name'],
    kind: 'name',
  },
  {
    profileKey: 'dob',
    label: 'Date of birth',
    extractKeys: ['dob', 'date_of_birth', 'birth'],
    kind: 'exact',
  },
  {
    profileKey: 'pan',
    label: 'PAN',
    extractKeys: ['pan', 'pan_number'],
    kind: 'exact',
  },
]

export function ProfileMatchPanel({ profile, result }: ProfileMatchPanelProps) {
  if (!result) return null

  const rows = FIELD_MAP.filter((m) => profile[m.profileKey]).map((m) => {
    const entered = profile[m.profileKey]
    const extracted = findExtractedValue(result, m.extractKeys)
    let status: 'match' | 'mismatch' | 'missing' = 'missing'
    if (extracted) {
      if (m.kind === 'name') {
        status = namesMatch(entered, extracted) ? 'match' : 'mismatch'
      } else {
        status = normalize(entered) === normalize(extracted) ? 'match' : 'mismatch'
      }
    }
    return { ...m, entered, extracted, status }
  })

  for (const [key, value] of Object.entries(profile)) {
    if (FIELD_MAP.some((m) => m.profileKey === key)) continue
    const extracted = findExtractedValue(result, [key])
    let status: 'match' | 'mismatch' | 'missing' = 'missing'
    if (extracted) {
      status = namesMatch(value, extracted) || normalize(value) === normalize(extracted)
        ? 'match'
        : 'mismatch'
    }
    rows.push({
      profileKey: key,
      label: key.replace(/^custom_/, '').replace(/_/g, ' '),
      extractKeys: [key],
      kind: 'exact',
      entered: value,
      extracted,
      status,
    })
  }

  if (rows.length === 0) return null

  return (
    <section className="card space-y-3" aria-labelledby="profile-match-title">
      <div>
        <h3 id="profile-match-title" className="font-display text-[16px] font-semibold text-content">
          Profile match
        </h3>
        <p className="mt-0.5 text-[12px] text-content-secondary">
          Entered details compared with values from the verification response.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[320px] text-left text-[13px]">
          <thead>
            <tr className="border-b border-line-divider text-[11px] font-bold uppercase tracking-wider text-content-secondary">
              <th className="pb-2 pr-3 font-medium">Field</th>
              <th className="pb-2 pr-3 font-medium">Entered</th>
              <th className="pb-2 pr-3 font-medium">From documents</th>
              <th className="pb-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.profileKey} className="border-b border-line-divider/60 last:border-0">
                <td className="py-2.5 pr-3 font-medium capitalize text-content">{r.label}</td>
                <td className="py-2.5 pr-3 font-mono text-[12px] text-content">{r.entered}</td>
                <td className="py-2.5 pr-3 font-mono text-[12px] text-content-secondary">
                  {r.extracted ?? '—'}
                </td>
                <td className="py-2.5">
                  {r.status === 'match' && (
                    <span className="inline-flex items-center gap-1 text-success">
                      <CheckCircle2 className="h-4 w-4" /> Match
                    </span>
                  )}
                  {r.status === 'mismatch' && (
                    <span className="inline-flex items-center gap-1 text-danger">
                      <XCircle className="h-4 w-4" /> Mismatch
                    </span>
                  )}
                  {r.status === 'missing' && (
                    <span className="inline-flex items-center gap-1 text-content-secondary">
                      <MinusCircle className="h-4 w-4" /> Not found
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
