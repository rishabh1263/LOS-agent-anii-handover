import { useMemo, useState, type ReactNode } from 'react'

import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  FileText,
  Info,
  ShieldCheck,
  TrendingUp,
  User,
  Users,
  XCircle,
} from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import type {
  CrossCheck,
  CrossDocument,
  DocumentResult,
  KycField,
  KycResult,
  PartyResult,
} from '../../../runtime/api-tester'

const ACCORDION = { duration: 0.2, ease: [0.22, 1, 0.36, 1] as const }

export interface CrossDocumentReconciliationProps {
  crossDocument: CrossDocument
  documents: DocumentResult[]
  kyc?: KycResult
  /** Party summaries — used for Passed/Review KPIs when cross_document.checks is empty */
  primaryApplicant?: PartyResult | null
  coApplicant?: PartyResult | null
}

interface ComparisonRow {
  field: string
  sublabel?: string
  values: { [sourceId: string]: string | null }
  status: 'PASS' | 'SINGLE_SOURCE' | 'SKIPPED' | 'FAIL' | 'REVIEW'
  statusText: string
  matchScore?: number | null
  confidence?: number | null
  reason?: string | null
  reasonCode?: string | null
}

function getFieldFromDoc(key: string, doc: DocumentResult): string | null {
  if (!doc.extraction) return null
  const ext = doc.extraction

  const lookup = (...keys: string[]): string | null => {
    for (const k of keys) {
      if (ext[k] != null && ext[k] !== '') {
        const val = ext[k]
        if (Array.isArray(val)) return val.join(', ')
        return String(val)
      }
    }
    return null
  }

  switch (key) {
    case 'NAME':
      return lookup('name', 'full_name', 'applicant_name')
    case 'DOB':
      return lookup('date_of_birth', 'dob')
    case 'FATHER_NAME':
      return lookup('father_name', 'guardian_name')
    case 'DOCUMENT_ID':
      return lookup('dl_number', 'pan_number', 'passport_number', 'voter_id')
    case 'ADDRESS': {
      const addr = lookup('address', 'current_address')
      const pin = lookup('pin_code', 'pincode')
      if (addr && pin && !addr.includes(pin)) {
        return `${addr}, PIN: ${pin}`
      }
      return addr
    }
    case 'VEHICLE_CLASSES':
      return lookup('vehicle_classes')
    case 'VALIDITY': {
      const issued = lookup('date_of_issue')
      const validTill = lookup('valid_till')
      if (issued && validTill) return `Issued: ${issued} · Valid till: ${validTill}`
      if (validTill) return `Valid till: ${validTill}`
      return null
    }
    default:
      return lookup(key.toLowerCase(), key)
  }
}

function formatDocLabel(sourceId: string): string {
  return sourceId.replace(/\.[^/.]+$/, '')
}

function formatDocType(type: string): string {
  return type
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/** Map API KYC field name → our internal row key */
function kycFieldKey(field: string): string {
  const u = field.toUpperCase()
  if (u === 'NAME' || u === 'FULL_NAME') return 'NAME'
  if (u === 'DATE_OF_BIRTH' || u === 'DOB') return 'DOB'
  if (u === 'FATHER_NAME' || u === 'GUARDIAN_NAME') return 'FATHER_NAME'
  if (u === 'ADDRESS' || u === 'RESIDENTIAL_ADDRESS') return 'ADDRESS'
  if (u === 'PAN_NUMBER' || u === 'PAN') return 'DOCUMENT_ID'
  if (u === 'INCOME') return 'INCOME'
  return u
}

type PartyBucket = 'applicant' | 'co_applicant' | 'other'

function resolvePartyBucket(
  c: CrossCheck,
  primaryApplicant?: PartyResult | null,
  coApplicant?: PartyResult | null,
): PartyBucket {
  if (!c.party_id) return 'other'
  if (primaryApplicant?.party_id && c.party_id === primaryApplicant.party_id) return 'applicant'
  if (coApplicant?.party_id && c.party_id === coApplicant.party_id) return 'co_applicant'
  // Heuristic: first half of checks without matching ids still tagged by label patterns
  return 'other'
}

function ChecklistPartyBlock({
  title,
  icon,
  checks,
  primaryApplicant,
  coApplicant,
  kycFields,
  statusPill,
  checkIcon,
  formatDocLabel,
  kycFieldKey,
}: {
  title: string
  icon: ReactNode
  checks: CrossCheck[]
  primaryApplicant?: PartyResult | null
  coApplicant?: PartyResult | null
  kycFields: KycField[]
  statusPill: (status: string, text: string) => ReactNode
  checkIcon: (status: string) => ReactNode
  formatDocLabel: (id: string) => string
  kycFieldKey: (field: string) => string
}) {
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())

  const passed = checks.filter((c) => c.status === 'PASS').length
  const failed = checks.filter((c) => c.status === 'FAIL').length
  const review = checks.filter((c) => c.status === 'REVIEW').length
  const skipped = checks.filter((c) => c.status === 'SKIPPED').length

  const toggle = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  if (checks.length === 0) return null

  return (
    <section className="overflow-hidden rounded-sm border border-line bg-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-3.5 py-2.5 text-left transition-colors hover:bg-raised/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
      >
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-xs bg-raised text-content-secondary">
            {icon}
          </span>
          <div className="min-w-0">
            <p className="font-display text-[13px] font-semibold text-content">{title}</p>
            <p className="text-[11px] text-content-secondary">
              <span className="font-medium tabular-nums text-content">
                {passed}/{checks.length}
              </span>{' '}
              passed
              {failed > 0 && <span className="text-danger-text"> · {failed} failed</span>}
              {review > 0 && <span className="text-warning-text"> · {review} review</span>}
              {skipped > 0 && <span> · {skipped} skipped</span>}
            </p>
          </div>
        </div>
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-content-secondary transition-transform duration-200 ${open ? 'rotate-180' : ''
            }`}
          aria-hidden
        />
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={ACCORDION}
            className="overflow-hidden border-t border-line-divider"
          >
            <ul className="divide-y divide-line-divider">
              {checks.map((c, idx) => {
                const partyKycFields =
                  c.party_id && primaryApplicant?.party_id === c.party_id
                    ? primaryApplicant.kyc?.fields
                    : c.party_id && coApplicant?.party_id === c.party_id
                      ? coApplicant.kyc?.fields
                      : kycFields

                const kycMatch = (partyKycFields || kycFields).find(
                  (f) =>
                    kycFieldKey(f.field) === c.check.toUpperCase() ||
                    f.field.toUpperCase() === c.check.toUpperCase(),
                )
                const score = kycMatch?.match_score
                const reason = kycMatch?.reason
                const rowKey = `${c.party_id || 'all'}-${c.check}-${idx}`
                const isOpen = expanded.has(rowKey)
                const hasDetail =
                  Boolean(reason) ||
                  Boolean(c.reason_codes?.length) ||
                  Boolean(c.sources?.length) ||
                  Boolean(c.details && Object.keys(c.details).length > 0) ||
                  (score != null && score > 0)

                const statusText =
                  c.status === 'PASS'
                    ? 'PASS'
                    : c.status === 'FAIL'
                      ? 'FAIL'
                      : c.status === 'REVIEW'
                        ? 'REVIEW'
                        : 'SKIPPED'

                return (
                  <li key={rowKey}>
                    <button
                      type="button"
                      onClick={() => hasDetail && toggle(rowKey)}
                      aria-expanded={hasDetail ? isOpen : undefined}
                      disabled={!hasDetail}
                      className={`flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ember ${hasDetail ? 'cursor-pointer hover:bg-raised/30' : 'cursor-default'
                        }`}
                    >
                      {checkIcon(c.status)}
                      <span className="min-w-0 flex-1 font-display text-[12px] font-bold uppercase tracking-wide text-content">
                        {c.check.replace(/_/g, ' ')}
                      </span>
                      {statusPill(statusText, statusText)}
                      {hasDetail && (
                        <ChevronDown
                          className={`h-3.5 w-3.5 shrink-0 text-content-disabled transition-transform duration-200 ${isOpen ? 'rotate-180' : ''
                            }`}
                          aria-hidden
                        />
                      )}
                    </button>

                    <AnimatePresence initial={false}>
                      {isOpen && hasDetail && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: 'auto', opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
                          className="overflow-hidden"
                        >
                          <div className="space-y-2 bg-raised/25 px-3.5 py-2.5 pl-11">
                            {score != null && score > 0 && (
                              <span className="chip text-[11px] font-mono font-semibold text-ember">
                                {score}% match
                              </span>
                            )}
                            {c.sources && c.sources.length > 0 && (
                              <p className="text-[11px] text-content-secondary">
                                <span className="font-medium text-content">Sources: </span>
                                {c.sources.map(formatDocLabel).join(', ')}
                              </p>
                            )}
                            {c.details && Object.keys(c.details).length > 0 && (
                              <div className="flex flex-wrap gap-1.5">
                                {Object.entries(c.details).map(([src, val]) => (
                                  <span
                                    key={src}
                                    className="inline-flex max-w-full items-center gap-1 rounded-xs border border-line bg-surface px-2 py-0.5 text-[11px]"
                                  >
                                    <span className="truncate font-mono text-content-secondary">
                                      {formatDocLabel(src)}
                                    </span>
                                    <span className="text-content-disabled">→</span>
                                    <span className="truncate font-medium text-content">{val}</span>
                                  </span>
                                ))}
                              </div>
                            )}
                            {(reason || (c.reason_codes && c.reason_codes.length > 0)) && (
                              <p className="rounded-md bg-raised/60 px-2.5 py-1.5 text-[11px] leading-relaxed text-content-secondary">
                                {reason || c.reason_codes?.join(', ')}
                              </p>
                            )}
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </li>
                )
              })}
            </ul>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}

export function CrossDocumentReconciliation({
  crossDocument,
  documents,
  kyc,
  primaryApplicant,
  coApplicant,
}: CrossDocumentReconciliationProps) {
  const [matrixOpen, setMatrixOpen] = useState(false)
  const [checklistOpen, setChecklistOpen] = useState(true)
  const [infoOpen, setInfoOpen] = useState<string | null>(null)

  const checks = crossDocument?.checks || []
  const kycFields = kyc?.fields || []

  const { applicantChecks, coChecks, otherChecks } = useMemo(() => {
    const applicantChecks: CrossCheck[] = []
    const coChecks: CrossCheck[] = []
    const otherChecks: CrossCheck[] = []
    for (const c of checks) {
      const bucket = resolvePartyBucket(c, primaryApplicant, coApplicant)
      if (bucket === 'applicant') applicantChecks.push(c)
      else if (bucket === 'co_applicant') coChecks.push(c)
      else otherChecks.push(c)
    }
    // If API didn't stamp party_id, keep a single "All checks" group via other
    return { applicantChecks, coChecks, otherChecks }
  }, [checks, primaryApplicant, coApplicant])

  const kycByKey = new Map<string, KycField>()
  kycFields.forEach((f) => {
    kycByKey.set(kycFieldKey(f.field), f)
  })

  const checkStatusMap = new Map<string, { status: string; reason_codes?: string[] }>()
  checks.forEach((c) => {
    checkStatusMap.set(c.check.toUpperCase(), {
      status: c.status,
      reason_codes: c.reason_codes,
    })
  })

  const rows: ComparisonRow[] = []

  // 1. Name
  {
    const nameCheck = checkStatusMap.get('NAME')
    const kycName = kycByKey.get('NAME')
    const nameValues: { [sourceId: string]: string | null } = {}
    documents.forEach((d) => {
      nameValues[d.source_id] = getFieldFromDoc('NAME', d)
    })
    const st = nameCheck?.status || kycName?.status
    rows.push({
      field: 'Full Name',
      sublabel: 'Applicant Identity',
      values: nameValues,
      status: st === 'PASS' ? 'PASS' : st === 'FAIL' ? 'FAIL' : st === 'REVIEW' ? 'REVIEW' : 'SINGLE_SOURCE',
      statusText:
        st === 'PASS' ? 'Matched' : st === 'FAIL' ? 'Mismatch' : st === 'REVIEW' ? 'Review' : 'Single source',
      matchScore: kycName?.match_score ?? null,
      confidence: kycName?.confidence ?? null,
      reason: kycName?.reason ?? null,
      reasonCode: kycName?.reason_code ?? null,
    })
  }

  // 2. DOB
  {
    const dobCheck = checkStatusMap.get('DOB')
    const kycDob = kycByKey.get('DOB')
    const dobValues: { [sourceId: string]: string | null } = {}
    documents.forEach((d) => {
      dobValues[d.source_id] = getFieldFromDoc('DOB', d)
    })
    const st = dobCheck?.status || kycDob?.status
    rows.push({
      field: 'Date of Birth',
      sublabel: 'DOB Verification',
      values: dobValues,
      status: st === 'PASS' ? 'PASS' : st === 'FAIL' ? 'FAIL' : st === 'REVIEW' ? 'REVIEW' : 'SINGLE_SOURCE',
      statusText:
        st === 'PASS' ? 'Exact Match' : st === 'FAIL' ? 'Mismatch' : st === 'REVIEW' ? 'Review' : 'Single source',
      matchScore: kycDob?.match_score ?? null,
      confidence: kycDob?.confidence ?? null,
      reason: kycDob?.reason ?? null,
      reasonCode: kycDob?.reason_code ?? null,
    })
  }

  // 3. Father / Guardian
  {
    const parentValues: { [sourceId: string]: string | null } = {}
    let hasParentName = false
    documents.forEach((d) => {
      const v = getFieldFromDoc('FATHER_NAME', d)
      if (v) hasParentName = true
      parentValues[d.source_id] = v
    })
    if (hasParentName) {
      const fatherCheck = checkStatusMap.get('FATHER_NAME')
      const kycFather = kycByKey.get('FATHER_NAME')
      const st = fatherCheck?.status || kycFather?.status
      const nonNulls = Object.values(parentValues).filter(Boolean)
      const isMulti = nonNulls.length >= 2
      rows.push({
        field: 'Father / Guardian',
        sublabel: 'Secondary Lineage',
        values: parentValues,
        status:
          st === 'PASS'
            ? 'PASS'
            : st === 'FAIL'
              ? 'FAIL'
              : st === 'REVIEW'
                ? 'REVIEW'
                : isMulti
                  ? 'PASS'
                  : 'SINGLE_SOURCE',
        statusText:
          st === 'PASS'
            ? 'Matched'
            : st === 'FAIL'
              ? 'Mismatch'
              : st === 'REVIEW'
                ? 'Review'
                : isMulti
                  ? 'Reconciled'
                  : 'Single source',
        matchScore: kycFather?.match_score ?? null,
        confidence: kycFather?.confidence ?? null,
        reason: kycFather?.reason ?? null,
        reasonCode: kycFather?.reason_code ?? null,
      })
    }
  }

  // 4. Document Number
  {
    const idValues: { [sourceId: string]: string | null } = {}
    documents.forEach((d) => {
      idValues[d.source_id] = getFieldFromDoc('DOCUMENT_ID', d)
    })
    const kycPan = kycByKey.get('DOCUMENT_ID')
    rows.push({
      field: 'Document Number',
      sublabel: 'Official Identifier',
      values: idValues,
      status: 'SINGLE_SOURCE',
      statusText: 'Extracted',
      matchScore: kycPan?.match_score ?? null,
      confidence: kycPan?.confidence ?? null,
      reason: kycPan?.reason ?? null,
      reasonCode: kycPan?.reason_code ?? null,
    })
  }

  // 5. Address
  {
    const addrValues: { [sourceId: string]: string | null } = {}
    let hasAddress = false
    documents.forEach((d) => {
      const v = getFieldFromDoc('ADDRESS', d)
      if (v) hasAddress = true
      addrValues[d.source_id] = v
    })
    if (hasAddress) {
      const addrCheck = checkStatusMap.get('ADDRESS')
      const kycAddr = kycByKey.get('ADDRESS')
      const st = addrCheck?.status || kycAddr?.status
      rows.push({
        field: 'Residential Address',
        sublabel: 'Address Proof',
        values: addrValues,
        status:
          st === 'PASS'
            ? 'PASS'
            : st === 'FAIL'
              ? 'FAIL'
              : st === 'SKIPPED'
                ? 'SKIPPED'
                : 'SINGLE_SOURCE',
        statusText:
          st === 'PASS'
            ? 'Matched'
            : st === 'FAIL'
              ? 'Mismatch'
              : addrCheck?.reason_codes?.includes('ADDRESS_SINGLE_SOURCE')
                ? 'Single Source'
                : 'Extracted',
        matchScore: kycAddr?.match_score ?? null,
        confidence: kycAddr?.confidence ?? null,
        reason: kycAddr?.reason ?? null,
        reasonCode: kycAddr?.reason_code ?? null,
      })
    }
  }

  // 6. Validity
  {
    const validityValues: { [sourceId: string]: string | null } = {}
    let hasValidity = false
    documents.forEach((d) => {
      const v = getFieldFromDoc('VALIDITY', d)
      if (v) hasValidity = true
      validityValues[d.source_id] = v
    })
    if (hasValidity) {
      rows.push({
        field: 'Validity & Expiry',
        sublabel: 'Document Lifecycle',
        values: validityValues,
        status: 'PASS',
        statusText: 'Active',
        matchScore: null,
        confidence: null,
        reason: null,
        reasonCode: null,
      })
    }
  }

  // 7. Vehicle Classes
  {
    const vehicleValues: { [sourceId: string]: string | null } = {}
    let hasVehicle = false
    documents.forEach((d) => {
      const v = getFieldFromDoc('VEHICLE_CLASSES', d)
      if (v) hasVehicle = true
      vehicleValues[d.source_id] = v
    })
    if (hasVehicle) {
      rows.push({
        field: 'Vehicle Classes',
        sublabel: 'Authorised Categories',
        values: vehicleValues,
        status: 'SINGLE_SOURCE',
        statusText: 'Extracted (DL)',
        matchScore: null,
        confidence: null,
        reason: null,
        reasonCode: null,
      })
    }
  }

  // 8. Income
  {
    const incomeCheck = checkStatusMap.get('INCOME')
    const kycIncome = kycByKey.get('INCOME')
    if (incomeCheck || kycIncome) {
      const incValues: { [sourceId: string]: string | null } = {}
      documents.forEach((d) => {
        incValues[d.source_id] = getFieldFromDoc('INCOME', d)
      })
      rows.push({
        field: 'Income & Earnings',
        sublabel: 'Financial Proof',
        values: incValues,
        status: 'SKIPPED',
        statusText: 'Not provided',
        matchScore: kycIncome?.match_score ?? null,
        confidence: kycIncome?.confidence ?? null,
        reason: kycIncome?.reason ?? null,
        reasonCode: kycIncome?.reason_code ?? null,
      })
    }
  }

  const totalChecks = checks.length
  // Prefer cross_document.checks when present; otherwise aggregate party verification_summary
  // (and fall back to per-document status) so KPIs match Applicant / Co-applicant cards.
  const checksPass = checks.filter((c) => c.status === 'PASS').length
  const checksFail = checks.filter((c) => c.status === 'FAIL').length
  const checksReview = checks.filter((c) => c.status === 'REVIEW').length

  const partyPassed =
    (primaryApplicant?.verification_summary?.passed ?? 0) +
    (coApplicant?.verification_summary?.passed ?? 0)
  const partyReview =
    (primaryApplicant?.verification_summary?.review ?? 0) +
    (coApplicant?.verification_summary?.review ?? 0)
  const partyFailed =
    (primaryApplicant?.verification_summary?.failed ?? 0) +
    (coApplicant?.verification_summary?.failed ?? 0)

  const docsPassed = documents.filter(
    (d) => d.status === 'SUCCESS' || d.verification === 'PASS',
  ).length
  const docsReview = documents.filter(
    (d) => d.status === 'REVIEW' || d.verification === 'REVIEW',
  ).length
  const docsFailed = documents.filter(
    (d) =>
      d.status === 'FAILED' ||
      d.status === 'REJECTED' ||
      d.verification === 'FAIL',
  ).length

  const hasPartySummary =
    primaryApplicant?.verification_summary != null ||
    coApplicant?.verification_summary != null

  const passCount =
    totalChecks > 0 ? checksPass : hasPartySummary ? partyPassed : docsPassed
  const failCount =
    totalChecks > 0 ? checksFail : hasPartySummary ? partyFailed : docsFailed
  const reviewCount =
    totalChecks > 0 ? checksReview : hasPartySummary ? partyReview : docsReview

  const matchScore = kyc?.overall_score ?? 0
  const overallConfidence =
    kyc?.overall_confidence != null ? Math.round(kyc.overall_confidence) : null

  const isOverallPass = crossDocument.status === 'PASS'
  const isOverallFail = crossDocument.status === 'FAIL'

  const statusPill = (status: ComparisonRow['status'], text: string) => {
    const base =
      'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold leading-none whitespace-nowrap'
    if (status === 'PASS') {
      return (
        <span className={`${base} bg-success/10 text-success`}>
          <CheckCircle2 className="h-2.5 w-2.5 shrink-0" />
          {text}
        </span>
      )
    }
    if (status === 'FAIL') {
      return (
        <span className={`${base} bg-danger/10 text-danger`}>
          <XCircle className="h-2.5 w-2.5 shrink-0" />
          {text}
        </span>
      )
    }
    if (status === 'SKIPPED') {
      return (
        <span className={`${base} bg-content-disabled/10 text-content-disabled italic`}>{text}</span>
      )
    }
    if (status === 'REVIEW') {
      return (
        <span className={`${base} bg-warning/10 text-warning`}>
          <AlertTriangle className="h-2.5 w-2.5 shrink-0" />
          {text}
        </span>
      )
    }
    return (
      <span className={`${base} bg-raised text-content-secondary border border-line`}>{text}</span>
    )
  }

  const checkIcon = (status: string) => {
    if (status === 'PASS') return <CheckCircle2 className="h-4 w-4 text-success shrink-0" />
    if (status === 'FAIL') return <XCircle className="h-4 w-4 text-danger shrink-0" />
    if (status === 'REVIEW') return <AlertTriangle className="h-4 w-4 text-warning shrink-0" />
    return <Info className="h-4 w-4 text-content-disabled shrink-0" />
  }

  return (
    <div className="space-y-5">
      {/* Section header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-ember/10 text-ember">
            <ShieldCheck className="h-4.5 w-4.5" />
          </span>
          <div>
            <h3 className="font-display text-[16px] font-bold tracking-tight text-content leading-tight">
              Cross-Document Comparison Matrix
            </h3>
            <p className="mt-0.5 text-[12px] text-content-secondary">
              {documents.length} document{documents.length !== 1 ? 's' : ''} · {rows.length} attribute
              {rows.length !== 1 ? 's' : ''} reconciled
              {overallConfidence != null && (
                <span className="ml-1.5 text-content-disabled">· Confidence {overallConfidence}%</span>
              )}
            </p>
          </div>
        </div>

        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider border ${isOverallPass
              ? 'bg-success-subtle text-success-text border-success/25'
              : isOverallFail
                ? 'bg-danger-subtle text-danger-text border-danger/25'
                : 'bg-warning-subtle text-warning-text border-warning/25'
            }`}
        >
          {isOverallPass ? (
            <CheckCircle2 className="h-3.5 w-3.5" />
          ) : isOverallFail ? (
            <XCircle className="h-3.5 w-3.5" />
          ) : (
            <AlertTriangle className="h-3.5 w-3.5" />
          )}
          {isOverallPass ? 'All Checks Passed' : isOverallFail ? 'Checks Failed' : 'Review Required'}
        </span>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div className="card border-line bg-surface p-4 shadow-xs flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wider text-content-disabled">
              Match Score
            </span>
            <TrendingUp className="h-3.5 w-3.5 text-ember" />
          </div>
          <div className="flex items-end gap-1">
            <span
              className={`font-display text-[28px] font-bold leading-none tracking-tight ${matchScore >= 80 ? 'text-success' : matchScore >= 50 ? 'text-warning' : 'text-danger'
                }`}
            >
              {matchScore}
            </span>
            <span className="mb-0.5 text-[13px] font-semibold text-content-secondary">%</span>
          </div>
          <div className="h-1.5 w-full rounded-full bg-raised overflow-hidden">
            <div
              className={`h-full rounded-full transition-all ${matchScore >= 80 ? 'bg-success' : matchScore >= 50 ? 'bg-warning' : 'bg-danger'
                }`}
              style={{ width: `${Math.min(100, matchScore)}%` }}
            />
          </div>
          <span className="text-[10px] text-content-secondary">
            {/* {passCount} of {totalChecks} checks passed */}
            KYC Match Score
          </span>
        </div>

        <div className="card border-line bg-surface p-4 shadow-xs flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wider text-content-disabled">
              Documents
            </span>
            <FileText className="h-3.5 w-3.5 text-ember" />
          </div>
          <span className="font-display text-[28px] font-bold leading-none tracking-tight text-content">
            {documents.length}
          </span>
          <span className="text-[10px] text-content-secondary">Submitted & analyzed</span>
        </div>

        <div className="card border-success/25 bg-success-subtle p-4 shadow-xs flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wider text-success/70">Passed</span>
            <CheckCircle2 className="h-3.5 w-3.5 text-success" />
          </div>
          <span className="font-display text-[28px] font-bold leading-none tracking-tight text-success">
            {passCount}
          </span>
          <span className="text-[10px] text-success/70">
            {totalChecks > 0 ? 'Identity checks matched' : 'Documents passed'}
          </span>
        </div>

        <div
          className={`card p-4 shadow-xs flex flex-col gap-2 ${failCount > 0
              ? 'border-danger/25 bg-danger-subtle'
              : reviewCount > 0
                ? 'border-warning/25 bg-warning-subtle'
                : 'border-line bg-surface'
            }`}
        >
          <div className="flex items-center justify-between">
            <span
              className={`text-[10px] font-bold uppercase tracking-wider ${failCount > 0
                  ? 'text-danger/70'
                  : reviewCount > 0
                    ? 'text-warning/70'
                    : 'text-content-disabled'
                }`}
            >
              {failCount > 0 ? 'Failed' : 'Review'}
            </span>
            {failCount > 0 ? (
              <XCircle className="h-3.5 w-3.5 text-danger" />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5 text-warning" />
            )}
          </div>
          <span
            className={`font-display text-[28px] font-bold leading-none tracking-tight ${failCount > 0
                ? 'text-danger'
                : reviewCount > 0
                  ? 'text-warning'
                  : 'text-content-disabled'
              }`}
          >
            {failCount > 0 ? failCount : reviewCount}
          </span>
          <span
            className={`text-[10px] ${failCount > 0
                ? 'text-danger/70'
                : reviewCount > 0
                  ? 'text-warning/70'
                  : 'text-content-disabled'
              }`}
          >
            {failCount > 0
              ? totalChecks > 0
                ? 'Checks failed — review needed'
                : 'Documents failed'
              : reviewCount > 0
                ? totalChecks > 0
                  ? 'Flagged for review'
                  : 'Documents need review'
                : 'No issues detected'}
          </span>
        </div>
      </div>

      {/* Verification Checklist — Applicant / Co-applicant bifurcation; fields closed by default */}
      <div className="card overflow-hidden border-line bg-surface p-0 shadow-xs">
        <button
          type="button"
          onClick={() => setChecklistOpen((o) => !o)}
          className="flex w-full items-center justify-between gap-3 border-b border-line-divider bg-raised/40 px-5 py-3 text-left transition-colors hover:bg-raised/60"
        >
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-bold uppercase tracking-wider text-content-secondary">
              Verification Checklist
            </span>
            <span className="rounded-full bg-raised px-2 py-0.5 text-[10px] font-semibold tabular-nums text-content-disabled">
              {passCount}/{totalChecks}
            </span>
          </div>
          <ChevronDown
            className={`h-4 w-4 text-content-secondary transition-transform duration-200 ${checklistOpen ? 'rotate-180' : ''
              }`}
          />
        </button>

        <AnimatePresence initial={false}>
          {checklistOpen && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={ACCORDION}
              className="overflow-hidden"
            >
              <div className="space-y-2.5 p-3 sm:p-4">
                {checks.length === 0 ? (
                  <p className="py-6 text-center text-[13px] text-content-disabled">
                    No cross-document checks available.
                  </p>
                ) : (
                  <>
                    <ChecklistPartyBlock
                      title="Applicant"
                      icon={<User className="h-3.5 w-3.5" aria-hidden />}
                      checks={applicantChecks}
                      primaryApplicant={primaryApplicant}
                      coApplicant={coApplicant}
                      kycFields={kycFields}
                      statusPill={statusPill as (s: string, t: string) => ReactNode}
                      checkIcon={checkIcon}
                      formatDocLabel={formatDocLabel}
                      kycFieldKey={kycFieldKey}
                    />
                    <ChecklistPartyBlock
                      title="Co-applicant"
                      icon={<Users className="h-3.5 w-3.5" aria-hidden />}
                      checks={coChecks}
                      primaryApplicant={primaryApplicant}
                      coApplicant={coApplicant}
                      kycFields={kycFields}
                      statusPill={statusPill as (s: string, t: string) => ReactNode}
                      checkIcon={checkIcon}
                      formatDocLabel={formatDocLabel}
                      kycFieldKey={kycFieldKey}
                    />
                    {/* Ungrouped checks (no party_id) — only when present */}
                    {otherChecks.length > 0 && (
                      <ChecklistPartyBlock
                        title={
                          applicantChecks.length === 0 && coChecks.length === 0
                            ? 'All checks'
                            : 'Other'
                        }
                        icon={<Info className="h-3.5 w-3.5" aria-hidden />}
                        checks={otherChecks}
                        primaryApplicant={primaryApplicant}
                        coApplicant={coApplicant}
                        kycFields={kycFields}
                        statusPill={statusPill as (s: string, t: string) => ReactNode}
                        checkIcon={checkIcon}
                        formatDocLabel={formatDocLabel}
                        kycFieldKey={kycFieldKey}
                      />
                    )}
                  </>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Attribute Reconciliation Matrix (collapsible) */}
      <div className="card overflow-hidden border-line bg-surface p-0 shadow-xs">
        <button
          type="button"
          onClick={() => setMatrixOpen((o) => !o)}
          className="flex w-full items-center justify-between gap-3 border-b border-line-divider bg-raised/40 px-5 py-3 text-left hover:bg-raised/60 transition-colors"
        >
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-bold uppercase tracking-wider text-content-secondary">
              Attribute Reconciliation
            </span>
            <span className="text-[11px] text-content-disabled tabular-nums">
              {rows.length} fields · {documents.length} sources
            </span>
          </div>
          <ChevronDown
            className={`h-4 w-4 text-content-secondary transition-transform duration-200 ${matrixOpen ? 'rotate-180' : ''
              }`}
          />
        </button>

        {matrixOpen && (
          <>
            <div className="relative">
              <div className="overflow-x-auto overflow-y-visible">
                <table className="w-full min-w-max border-collapse text-left text-[13px]">
                  <thead>
                    <tr className="border-b border-line bg-raised">
                      <th
                        scope="col"
                        className="sticky left-0 z-20 bg-raised py-3.5 pl-5 pr-4 text-[10px] font-bold uppercase tracking-wider text-content-secondary shadow-[2px_0_6px_-1px_rgba(0,0,0,0.08)]"
                        style={{ minWidth: 180, width: 180 }}
                      >
                        Source
                      </th>

                      {rows.map((row) => (
                        <th
                          key={row.field}
                          scope="col"
                          className="py-3.5 px-4 align-bottom"
                          style={{ minWidth: 168 }}
                        >
                          <div className="flex flex-col gap-1.5">
                            <div>
                              <div className="flex items-center gap-1.5">
                                <span className="text-[12px] font-semibold text-content normal-case tracking-normal leading-snug">
                                  {row.field}
                                </span>
                                {row.reason && (
                                  <button
                                    type="button"
                                    onClick={(e) => {
                                      e.stopPropagation()
                                      setInfoOpen(infoOpen === row.field ? null : row.field)
                                    }}
                                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border transition-colors ${infoOpen === row.field
                                        ? 'border-ember/40 bg-ember/10 text-ember'
                                        : 'border-line text-content-disabled hover:border-ember/30 hover:text-ember'
                                      }`}
                                    title={row.reason}
                                    aria-label={`Info: ${row.field}`}
                                  >
                                    <Info className="h-2.5 w-2.5" />
                                  </button>
                                )}
                              </div>
                              {row.sublabel && (
                                <div className="mt-0.5 text-[10px] font-normal text-content-disabled normal-case tracking-normal">
                                  {row.sublabel}
                                </div>
                              )}
                            </div>
                            <div className="flex flex-wrap items-center gap-1.5">
                              {statusPill(row.status, row.statusText)}
                              {row.matchScore != null && row.matchScore > 0 && (
                                <span className="inline-flex items-center rounded-full bg-ember/10 px-2 py-0.5 text-[10px] font-bold tabular-nums text-ember">
                                  {row.matchScore}% match
                                </span>
                              )}
                            </div>
                            {infoOpen === row.field && row.reason && (
                              <p className="mt-1 max-w-[220px] rounded-md bg-raised px-2 py-1.5 text-[10px] font-normal text-content-secondary leading-relaxed normal-case tracking-normal">
                                {row.reason}
                              </p>
                            )}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>

                  <tbody className="divide-y divide-line-divider">
                    {documents.length === 0 ? (
                      <tr>
                        <td
                          colSpan={rows.length + 1}
                          className="px-5 py-10 text-center text-[13px] text-content-disabled"
                        >
                          No documents available for comparison.
                        </td>
                      </tr>
                    ) : (
                      documents.map((doc, idx) => (
                        <tr
                          key={doc.source_id}
                          className={`group ${idx % 2 === 1 ? 'bg-raised' : 'bg-surface'}`}
                        >
                          <td
                            className={`sticky left-0 z-10 py-3.5 pl-5 pr-4 align-top shadow-[2px_0_6px_-1px_rgba(0,0,0,0.08)] ${idx % 2 === 1 ? 'bg-raised' : 'bg-surface'
                              }`}
                            style={{ minWidth: 180, width: 180 }}
                          >
                            <div className="flex items-start gap-2">
                              <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ember" />
                              <div className="min-w-0">
                                <div
                                  className="truncate text-[12px] font-semibold text-content leading-snug"
                                  title={doc.source_id}
                                >
                                  {formatDocLabel(doc.source_id)}
                                </div>
                                <span className="mt-1 inline-block rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider bg-ember/10 text-ember">
                                  {formatDocType(doc.type)}
                                </span>
                              </div>
                            </div>
                          </td>

                          {rows.map((row) => {
                            const val = row.values[doc.source_id]
                            return (
                              <td key={row.field} className="py-3.5 px-4 align-top">
                                {val ? (
                                  <span className="block text-[12px] font-medium text-content leading-snug break-words max-w-[220px]">
                                    {val}
                                  </span>
                                ) : (
                                  <span className="text-[12px] italic text-content-disabled">—</span>
                                )}
                              </td>
                            )
                          })}
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="flex items-start gap-2 border-t border-line-divider bg-raised/25 px-5 py-3 text-[11px] text-content-secondary">
              <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-content-disabled" />
              <span>
                {isOverallPass
                  ? 'All identity attributes cross-verified with full concordance across submitted documents.'
                  : isOverallFail
                    ? 'One or more identity attributes could not be reconciled across documents. Manual review required before proceeding.'
                    : 'Cross-document comparison complete. Some attributes require further human verification.'}
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
