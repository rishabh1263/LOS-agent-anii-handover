import { ChevronDown, Eye, FileText, MapPin, Link2 } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import type { DocumentResult } from '../../../runtime/api-tester'
import { docStatusClass, verificationClass } from '../../../runtime/api-tester'
import { StatusChip } from './StatusChip'

export interface DocumentResultCardProps {
  doc: DocumentResult
  isOpen: boolean
  onToggle: () => void
  onPreview?: () => void
}

const ADDRESS_KEYS = new Set([
  'address',
  'permanent_address',
  'current_address',
  'residential_address',
  'mailing_address',
])

const EVIDENCE_KEYS = new Set([
  'evidence',
  'evidence_refs',
  'evidences',
  'references',
])

function formatLabel(key: string) {
  return key.replace(/_/g, ' ')
}

function isPlainObject(val: unknown): val is Record<string, unknown> {
  return typeof val === 'object' && val !== null && !Array.isArray(val)
}

/** Pretty address block — never raw JSON */
function AddressBlock({ value }: { value: Record<string, unknown> }) {
  const order = [
    'house',
    'line1',
    'line2',
    'street',
    'locality',
    'landmark',
    'city',
    'district',
    'state',
    'pin_code',
    'pincode',
    'postal_code',
    'country',
  ]
  const keys = [
    ...order.filter((k) => value[k] != null && String(value[k]).trim() !== ''),
    ...Object.keys(value).filter(
      (k) => !order.includes(k) && value[k] != null && String(value[k]).trim() !== '',
    ),
  ]

  if (keys.length === 0) {
    return <span className="text-[13px] text-content-secondary">—</span>
  }

  const line1 = [value.house, value.line1, value.street, value.locality]
    .filter((x) => x != null && String(x).trim())
    .map(String)
  const line2 = [value.line2, value.landmark]
    .filter((x) => x != null && String(x).trim())
    .map(String)
  const cityLine = [value.city, value.district, value.state]
    .filter((x) => x != null && String(x).trim())
    .map(String)
  const pin = value.pin_code ?? value.pincode ?? value.postal_code
  const country = value.country

  // Prefer structured multi-line if we recognized common keys
  const hasStructured = line1.length > 0 || cityLine.length > 0

  return (
    <div className="rounded-sm border border-line bg-surface px-3 py-2.5 space-y-1">
      <div className="flex items-start gap-2">
        <MapPin className="h-3.5 w-3.5 text-ember shrink-0 mt-0.5" aria-hidden />
        <div className="min-w-0 space-y-0.5 text-[13px] text-content leading-snug">
          {hasStructured ? (
            <>
              {line1.length > 0 && <p>{line1.join(', ')}</p>}
              {line2.length > 0 && (
                <p className="text-content-secondary">{line2.join(', ')}</p>
              )}
              {cityLine.length > 0 && <p>{cityLine.join(', ')}</p>}
              {(pin != null || country != null) && (
                <p className="font-mono text-[12px] text-content-secondary">
                  {[pin != null ? String(pin) : null, country != null ? String(country) : null]
                    .filter(Boolean)
                    .join(' · ')}
                </p>
              )}
            </>
          ) : (
            <dl className="grid gap-1 sm:grid-cols-2">
              {keys.map((k) => (
                <div key={k} className="flex flex-col">
                  <dt className="text-[10px] font-bold uppercase tracking-wider text-content-secondary">
                    {formatLabel(k)}
                  </dt>
                  <dd className="font-medium text-content">{String(value[k])}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      </div>
    </div>
  )
}

/** Evidence / refs — chips or source list, never JSON blob */
function EvidenceBlock({ value }: { value: unknown }) {
  if (value == null) return <span className="text-content-secondary">—</span>

  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="text-[13px] text-content-secondary">None</span>
    }
    return (
      <ul className="space-y-1.5">
        {value.map((item, idx) => {
          if (isPlainObject(item)) {
            const sourceId = item.source_id ?? item.id ?? item.ref
            const locator = item.locator ?? item.page ?? item.location
            return (
              <li
                key={idx}
                className="flex flex-wrap items-center gap-2 rounded-sm border border-line bg-surface px-2.5 py-1.5 text-[12px]"
              >
                <Link2 className="h-3.5 w-3.5 text-content-secondary shrink-0" />
                {sourceId != null && (
                  <span className="font-mono font-semibold text-content">{String(sourceId)}</span>
                )}
                {locator != null && (
                  <span className="text-content-secondary">@ {String(locator)}</span>
                )}
                {Object.entries(item)
                  .filter(([k]) => !['source_id', 'id', 'ref', 'locator', 'page', 'location'].includes(k))
                  .map(([k, v]) => (
                    <span key={k} className="chip text-[10px]">
                      {formatLabel(k)}: {String(v)}
                    </span>
                  ))}
              </li>
            )
          }
          return (
            <li key={idx}>
              <span className="chip text-[11px] font-mono">{String(item)}</span>
            </li>
          )
        })}
      </ul>
    )
  }

  if (isPlainObject(value)) {
    return <AddressBlock value={value} />
  }

  return <span className="font-mono text-[13px] font-medium text-content">{String(value)}</span>
}

function renderValue(key: string, val: unknown) {
  if (val == null) return <span className="text-content-secondary">—</span>

  const keyLower = key.toLowerCase()

  if (ADDRESS_KEYS.has(keyLower) && isPlainObject(val)) {
    return <AddressBlock value={val} />
  }

  if (EVIDENCE_KEYS.has(keyLower) || keyLower.includes('evidence')) {
    return <EvidenceBlock value={val} />
  }

  // Generic nested object → key/value grid (not JSON)
  if (isPlainObject(val)) {
    const entries = Object.entries(val).filter(([, v]) => v != null)
    if (entries.length === 0) return <span className="text-content-secondary">—</span>
    return (
      <dl className="grid gap-1.5 sm:grid-cols-2 rounded-sm border border-line bg-surface px-3 py-2">
        {entries.map(([k, v]) => (
          <div key={k} className="min-w-0">
            <dt className="text-[10px] font-bold uppercase tracking-wider text-content-secondary">
              {formatLabel(k)}
            </dt>
            <dd className="mt-0.5 text-[13px] font-medium text-content break-words">
              {typeof v === 'object' ? (
                Array.isArray(v) ? (
                  <span className="flex flex-wrap gap-1">
                    {v.map((item, i) => (
                      <span key={i} className="chip text-[11px]">
                        {String(item)}
                      </span>
                    ))}
                  </span>
                ) : (
                  String(JSON.stringify(v))
                )
              ) : (
                String(v)
              )}
            </dd>
          </div>
        ))}
      </dl>
    )
  }

  if (Array.isArray(val)) {
    return (
      <div className="flex flex-wrap gap-1">
        {val.map((item, idx) => (
          <span key={idx} className="chip text-[11px] py-0.5 px-2 font-mono">
            {typeof item === 'object' ? String(JSON.stringify(item)) : String(item)}
          </span>
        ))}
      </div>
    )
  }

  return <span className="font-mono text-[13px] font-medium text-content">{String(val)}</span>
}

function partyLabel(role?: string | null) {
  if (role === 'PRIMARY_APPLICANT') return 'Applicant'
  if (role === 'CO_APPLICANT') return 'Co-applicant'
  return role || null
}

export function DocumentResultCard({
  doc,
  isOpen,
  onToggle,
  onPreview,
}: DocumentResultCardProps) {
  const extraction = doc.extraction || {}
  const entries = Object.entries(extraction).filter(([, v]) => v != null)
  const party = partyLabel(doc.party_role)

  // Prefer known order for common KYC fields
  const preferredOrder = [
    'name',
    'pan_number',
    'dl_number',
    'date_of_birth',
    'father_name',
    'guardian_name',
    'date_of_issue',
    'valid_till',
    'address',
    'pin_code',
    'vehicle_classes',
  ]
  const sortedEntries = [
    ...preferredOrder
      .filter((k) => extraction[k] != null)
      .map((k) => [k, extraction[k]] as [string, unknown]),
    ...entries.filter(([k]) => !preferredOrder.includes(k)),
  ]

  return (
    <div className="card overflow-hidden border-line bg-surface p-0 shadow-xs">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={isOpen}
        className="w-full flex items-center justify-between px-5 py-3.5 text-left hover:bg-raised/30 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
      >
        <div className="flex items-center gap-3 min-w-0">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xs bg-raised border border-line text-icon-default">
            <FileText className="h-4 w-4 text-ember" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="font-mono text-[13px] font-bold text-content truncate">
                {doc.source_id}
              </h4>
              <span className="chip text-[10px] uppercase font-bold py-0.5 px-2">
                {doc.type}
              </span>
              {party && (
                <span className="chip text-[10px] font-semibold py-0.5 px-2 bg-ember-subtle text-ember-text border-ember/20">
                  {party}
                </span>
              )}
              {doc.expected_type && doc.expected_type !== doc.type && (
                <span className="chip text-[10px] py-0.5 px-2 text-warning-text bg-warning-subtle">
                  expected: {doc.expected_type}
                </span>
              )}
            </div>
            <p className="text-[11px] text-content-secondary mt-0.5">
              {sortedEntries.length} fields extracted
              {doc.verification_confidence != null && (
                <> · confidence {doc.verification_confidence}%</>
              )}
            </p>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {onPreview && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                onPreview()
              }}
              className="btn btn-outline inline-flex h-8 items-center gap-1.5 px-2.5 text-[12px]"
              title="Preview document"
            >
              <Eye className="h-3.5 w-3.5" aria-hidden />
              <span className="hidden sm:inline">Preview</span>
            </button>
          )}
          <StatusChip label={doc.status} className={docStatusClass(doc.status)} />
          <StatusChip label={doc.verification} className={verificationClass(doc.verification)} />
          <ChevronDown
            className={`h-4 w-4 text-content-secondary transition-transform duration-200 ${isOpen ? 'rotate-180' : ''
              }`}
          />
        </div>
      </button>

      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden border-t border-line-divider"
          >
            {(doc.advisories?.length || doc.reasons?.length || doc.reason_codes?.length) ? (
              <div className="px-5 pt-3 flex flex-wrap gap-1.5">
                {doc.advisories?.map((a) => (
                  <span key={a} className="chip text-[11px] bg-warning-subtle text-warning-text">
                    {a}
                  </span>
                ))}
                {doc.reasons?.map((r, i) => (
                  <span key={`r-${i}`} className="chip text-[11px] bg-danger-subtle text-danger-text">
                    {r}
                  </span>
                ))}
                {doc.reason_codes?.map((c) => (
                  <span key={c} className="chip text-[11px] font-mono">
                    {c}
                  </span>
                ))}
              </div>
            ) : null}

            {sortedEntries.length > 0 ? (
              <div className="p-5 grid gap-x-6 gap-y-3 sm:grid-cols-2 bg-raised/20">
                {sortedEntries.map(([k, v]) => {
                  const isWide =
                    ADDRESS_KEYS.has(k.toLowerCase()) ||
                    EVIDENCE_KEYS.has(k.toLowerCase()) ||
                    isPlainObject(v) ||
                    String(v).length > 40
                  return (
                    <div
                      key={k}
                      className={`flex flex-col py-1.5 border-b border-line-divider/60 ${isWide ? 'sm:col-span-2' : ''
                        }`}
                    >
                      <span className="font-display text-[10px] font-bold uppercase tracking-wider text-content-secondary">
                        {formatLabel(k)}
                      </span>
                      <div className="mt-1">{renderValue(k, v)}</div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div className="p-5 text-center text-[12px] text-content-secondary">
                No attributes extracted from this document.
              </div>
            )}

            {/* Doc-level evidence_refs if present on document (not only in extraction) */}
            {doc.evidence_refs && doc.evidence_refs.length > 0 && (
              <div className="px-5 pb-5">
                <span className="font-display text-[10px] font-bold uppercase tracking-wider text-content-secondary">
                  Evidence references
                </span>
                <div className="mt-1.5">
                  <EvidenceBlock value={doc.evidence_refs} />
                </div>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
