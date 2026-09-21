import { useMemo, useState } from 'react'
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Clock,
  Code2,
  Copy,
  FileText,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  User,
  Users,
  X,
} from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import type { DocumentResult, LosProcessResponse, PartyResult } from '../../../runtime/api-tester'
import { CrossDocumentReconciliation } from './CrossDocumentReconciliation'
import { DocumentResultCard } from './DocumentResultCard'
import { ValidationSummaryCards } from './ValidationSummaryCards'
import { StatusChip } from './StatusChip'

export interface ResultsPanelProps {
  result: LosProcessResponse
  onReset: () => void
}

type PartyView = 'overview' | 'applicant' | 'co_applicant'

function CopyableBadge({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    navigator.clipboard?.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }

  return (
    <button
      type="button"
      onClick={copy}
      title={`Click to copy ${label}`}
      className="inline-flex items-center gap-1.5 rounded-xs border border-line bg-raised px-2.5 py-1 font-mono text-[12px] text-content transition-all hover:border-line-strong hover:bg-raised-hover active:scale-95 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
    >
      <span className="text-content-secondary font-sans">{label}:</span>
      <span className="font-semibold text-content">{value}</span>
      {copied ? (
        <Check className="h-3.5 w-3.5 text-success animate-in zoom-in-50 duration-150" />
      ) : (
        <Copy className="h-3.5 w-3.5 text-content-disabled" />
      )}
    </button>
  )
}

function PartyKycDetail({ party, title }: { party: PartyResult; title: string }) {
  const kyc = party.kyc
  const vs = party.verification_summary

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="font-display text-[16px] font-bold text-content">{title}</h3>
          <p className="font-mono text-[12px] text-content-secondary mt-0.5">{party.party_id}</p>
        </div>
        <StatusChip label={String(party.status)} />
      </div>

      {/* KPI row */}
      {vs && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <KpiTile label="Documents" value={vs.total_documents} />
          <KpiTile label="Passed" value={vs.passed} tone="success" />
          <KpiTile label="Review" value={vs.review} tone="warning" />
          <KpiTile label="Failed" value={vs.failed} tone="danger" />
        </div>
      )}

      {kyc && (
        <div className="card border-line bg-surface p-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-semibold text-content">KYC status</span>
            <StatusChip label={kyc.status} />
            {kyc.overall_score != null && (
              <span className="chip text-[11px]">Score {kyc.overall_score}</span>
            )}
            {kyc.overall_confidence != null && (
              <span className="chip text-[11px]">Confidence {kyc.overall_confidence}%</span>
            )}
          </div>
          {kyc.reason_codes && kyc.reason_codes.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {kyc.reason_codes.map((c) => (
                <span key={c} className="chip text-[10px] font-mono bg-warning-subtle text-warning-text">
                  {c}
                </span>
              ))}
            </div>
          )}
          {kyc.fields && kyc.fields.length > 0 && (
            <div className="grid gap-2 sm:grid-cols-2">
              {kyc.fields.map((f) => (
                <div
                  key={f.field}
                  className="rounded-sm border border-line bg-raised/40 px-3 py-2 space-y-1"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[12px] font-semibold text-content">{f.field}</span>
                    <StatusChip label={f.status} />
                  </div>
                  <div className="flex flex-wrap gap-2 text-[11px] text-content-secondary">
                    {f.match_score != null && <span>Match {f.match_score}%</span>}
                    {f.confidence != null && <span>Conf {f.confidence}%</span>}
                    {f.reason_code && (
                      <span className="font-mono">{f.reason_code}</span>
                    )}
                  </div>
                  {f.sources && f.sources.length > 0 && (
                    <div className="pt-1 space-y-1 border-t border-line-divider/60">
                      {f.sources.map((s, i) => (
                        <div key={i} className="text-[11px] flex flex-wrap gap-x-2 gap-y-0.5">
                          <span className="font-mono text-content-secondary">{s.source_id}</span>
                          <span className="chip text-[10px]">{s.document_type}</span>
                          <span className="text-content font-medium break-all">
                            {typeof s.value === 'object'
                              ? formatSourceValue(s.value)
                              : String(s.value)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function formatSourceValue(val: unknown): string {
  if (val == null) return '—'
  if (typeof val !== 'object') return String(val)
  if (Array.isArray(val)) return val.map(String).join(', ')
  const o = val as Record<string, unknown>
  const parts = [
    o.house,
    o.line1,
    o.city,
    o.state,
    o.pin_code ?? o.pincode,
  ].filter((x) => x != null && String(x).trim())
  if (parts.length) return parts.map(String).join(', ')
  return Object.entries(o)
    .filter(([, v]) => v != null)
    .map(([k, v]) => `${k}: ${v}`)
    .join(' · ')
}

function KpiTile({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone?: 'success' | 'warning' | 'danger'
}) {
  const toneClass =
    tone === 'success'
      ? 'text-success-text bg-success-subtle border-success/20'
      : tone === 'warning'
        ? 'text-warning-text bg-warning-subtle border-warning/20'
        : tone === 'danger'
          ? 'text-danger-text bg-danger-subtle border-danger/20'
          : 'text-content bg-raised border-line'

  return (
    <div className={`rounded-sm border px-3 py-2.5 ${toneClass}`}>
      <p className="text-[10px] font-bold uppercase tracking-wider opacity-80">{label}</p>
      <p className="mt-0.5 font-display text-[20px] font-bold tabular-nums">{value}</p>
    </div>
  )
}

function docsForParty(docs: DocumentResult[], role: 'PRIMARY_APPLICANT' | 'CO_APPLICANT') {
  return docs.filter((d) => d.party_role === role)
}

export function ResultsPanel({ result, onReset }: ResultsPanelProps) {
  const hasCo = Boolean(result.co_applicant || result.co_applicant_id)
  const [partyView, setPartyView] = useState<PartyView>('overview')
  // Collapsed by default — open only what you need
  const [expandedDocIds, setExpandedDocIds] = useState<Set<string>>(() => new Set())
  const [showJsonModal, setShowJsonModal] = useState(false)
  const [copiedJson, setCopiedJson] = useState(false)

  const toggleDoc = (id: string) => {
    setExpandedDocIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleCopyJson = () => {
    navigator.clipboard?.writeText(JSON.stringify(result, null, 2))
    setCopiedJson(true)
    setTimeout(() => setCopiedJson(false), 2000)
  }

  const isSuccess = result.status === 'SUCCESS' && result.decision === 'PASS'
  const isReject = result.decision === 'REJECT'

  const applicantDocs = useMemo(
    () => docsForParty(result.documents, 'PRIMARY_APPLICANT'),
    [result.documents],
  )
  const coDocs = useMemo(
    () => docsForParty(result.documents, 'CO_APPLICANT'),
    [result.documents],
  )

  const visibleDocs =
    partyView === 'applicant'
      ? applicantDocs
      : partyView === 'co_applicant'
        ? coDocs
        : result.documents

  return (
    <div className="space-y-6">
      {/* Header */}
      <section className="card border-line bg-surface p-6 sm:p-7 shadow-xs space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line-divider pb-5">
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={`inline-flex items-center gap-1.5 rounded-xs px-2.5 py-1 text-[11px] font-bold uppercase tracking-wider ${
                  isSuccess
                    ? 'bg-success-subtle text-success-text border border-success/30'
                    : isReject
                      ? 'bg-danger-subtle text-danger-text border border-danger/30'
                      : 'bg-warning-subtle text-warning-text border border-warning/30'
                }`}
              >
                {isSuccess ? (
                  <CheckCircle2 className="h-3.5 w-3.5" />
                ) : isReject ? (
                  <ShieldAlert className="h-3.5 w-3.5" />
                ) : (
                  <ShieldCheck className="h-3.5 w-3.5" />
                )}
                <span>
                  {isSuccess
                    ? 'Verification Passed'
                    : isReject
                      ? 'Verification Rejected'
                      : 'Manual Review Required'}
                  {' · '}
                  {result.status}
                </span>
              </span>
              <span className="chip text-[11px] font-semibold">Decision: {result.decision}</span>
            </div>

            <h2 className="font-display text-[22px] font-bold tracking-tight text-content sm:text-[24px]">
              Verification report
            </h2>

            <div className="flex flex-wrap items-center gap-3 pt-0.5 text-[12px] text-content-secondary">
              <span className="inline-flex items-center gap-1 font-mono">
                <Clock className="h-3.5 w-3.5 text-icon-default" />
                {(result.processing_ms / 1000).toFixed(2)}s
              </span>
              <span>·</span>
              <span className="font-medium text-content">
                {result.documents.length} documents
              </span>
              <span>·</span>
              <span className="font-mono text-[11px]">Action: {result.next_action}</span>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setShowJsonModal(true)}
              className="btn btn-outline h-9 px-3 text-[12px] inline-flex items-center gap-1.5"
              title="Inspect raw response JSON"
            >
              <Code2 className="h-3.5 w-3.5" />
              <span>Inspect JSON</span>
            </button>
            <button
              type="button"
              onClick={onReset}
              className="btn btn-accent h-9 px-4 text-[12px] inline-flex items-center gap-1.5 shadow-xs"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              <span>New request</span>
            </button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <CopyableBadge label="Request" value={result.request_id} />
          <CopyableBadge label="Case" value={result.case_id} />
          {result.applicant_id && (
            <CopyableBadge label="Applicant" value={result.applicant_id} />
          )}
          {result.co_applicant_id && (
            <CopyableBadge label="Co-applicant" value={result.co_applicant_id} />
          )}
        </div>

        {result.summary && (
          <p className="text-[13px] text-content-secondary leading-relaxed border-t border-line-divider pt-3">
            {result.summary}
          </p>
        )}
      </section>

      {/* Party slider / segmented control */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-3">
        <p className="text-[12px] font-semibold text-content-secondary uppercase tracking-wider shrink-0">
          View
        </p>
        <div
          role="tablist"
          aria-label="Report view"
          className="relative flex w-full sm:w-auto rounded-lg border border-line bg-raised p-1 gap-0.5"
        >
          {(
            [
              { id: 'overview' as const, label: 'Overview', icon: null },
              {
                id: 'applicant' as const,
                label: 'Applicant',
                icon: <User className="h-3.5 w-3.5" />,
                count: applicantDocs.length,
              },
              {
                id: 'co_applicant' as const,
                label: 'Co-applicant',
                icon: <Users className="h-3.5 w-3.5" />,
                count: coDocs.length,
                disabled: !hasCo && coDocs.length === 0,
              },
            ] as const
          ).map((tab) => {
            const active = partyView === tab.id
            const disabled = 'disabled' in tab && tab.disabled
            return (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={active}
                disabled={disabled}
                onClick={() => !disabled && setPartyView(tab.id)}
                className={`relative flex-1 sm:flex-none inline-flex items-center justify-center gap-1.5 rounded-md px-3.5 py-2 text-[13px] font-semibold transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-ember disabled:opacity-40 disabled:cursor-not-allowed ${
                  active
                    ? 'bg-surface text-content shadow-xs border border-line/60'
                    : 'text-content-secondary hover:text-content border border-transparent'
                }`}
              >
                {tab.icon}
                <span>{tab.label}</span>
                {'count' in tab && tab.count != null && (
                  <span className="tabular-nums text-[11px] opacity-70">({tab.count})</span>
                )}
              </button>
            )
          })}
        </div>
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={partyView}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
          className="space-y-6"
        >
          {/* Party KYC detail when filtered */}
          {partyView === 'applicant' && result.primary_applicant && (
            <PartyKycDetail party={result.primary_applicant} title="Applicant KYC" />
          )}
          {partyView === 'co_applicant' && result.co_applicant && (
            <PartyKycDetail party={result.co_applicant} title="Co-applicant KYC" />
          )}

          {/* Overview: both party summary cards */}
          {partyView === 'overview' && (result.primary_applicant || result.co_applicant) && (
            <section className="grid gap-3 sm:grid-cols-2">
              {result.primary_applicant && (
                <button
                  type="button"
                  onClick={() => setPartyView('applicant')}
                  className="card border-line bg-surface p-4 text-left space-y-3 hover:border-line-strong transition focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <User className="h-4 w-4 text-ember" />
                      <div>
                        <h4 className="font-display text-[14px] font-semibold text-content">
                          Applicant
                        </h4>
                        <p className="font-mono text-[11px] text-content-secondary">
                          {result.primary_applicant.party_id}
                        </p>
                      </div>
                    </div>
                    <StatusChip label={String(result.primary_applicant.status)} />
                  </div>
                  {result.primary_applicant.verification_summary && (
                    <div className="flex flex-wrap gap-1.5 text-[11px]">
                      <span className="chip">
                        Docs {result.primary_applicant.verification_summary.total_documents}
                      </span>
                      <span className="chip text-success-text bg-success-subtle">
                        Pass {result.primary_applicant.verification_summary.passed}
                      </span>
                      {result.primary_applicant.verification_summary.failed > 0 && (
                        <span className="chip text-danger-text bg-danger-subtle">
                          Fail {result.primary_applicant.verification_summary.failed}
                        </span>
                      )}
                    </div>
                  )}
                  {result.primary_applicant.kyc && (
                    <div className="flex items-center gap-2 text-[12px]">
                      <span className="text-content-secondary">KYC</span>
                      <StatusChip label={result.primary_applicant.kyc.status} />
                    </div>
                  )}
                </button>
              )}
              {result.co_applicant && (
                <button
                  type="button"
                  onClick={() => setPartyView('co_applicant')}
                  className="card border-line bg-surface p-4 text-left space-y-3 hover:border-line-strong transition focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <Users className="h-4 w-4 text-ember" />
                      <div>
                        <h4 className="font-display text-[14px] font-semibold text-content">
                          Co-applicant
                        </h4>
                        <p className="font-mono text-[11px] text-content-secondary">
                          {result.co_applicant.party_id}
                        </p>
                      </div>
                    </div>
                    <StatusChip label={String(result.co_applicant.status)} />
                  </div>
                  {result.co_applicant.verification_summary && (
                    <div className="flex flex-wrap gap-1.5 text-[11px]">
                      <span className="chip">
                        Docs {result.co_applicant.verification_summary.total_documents}
                      </span>
                      <span className="chip text-success-text bg-success-subtle">
                        Pass {result.co_applicant.verification_summary.passed}
                      </span>
                      {result.co_applicant.verification_summary.failed > 0 && (
                        <span className="chip text-danger-text bg-danger-subtle">
                          Fail {result.co_applicant.verification_summary.failed}
                        </span>
                      )}
                    </div>
                  )}
                  {result.co_applicant.kyc && (
                    <div className="flex items-center gap-2 text-[12px]">
                      <span className="text-content-secondary">KYC</span>
                      <StatusChip label={result.co_applicant.kyc.status} />
                    </div>
                  )}
                </button>
              )}
            </section>
          )}

          {/* Errors */}
          {result.errors?.length > 0 && partyView === 'overview' && (
            <div
              role="alert"
              className="rounded-lg border border-danger/30 bg-danger-subtle p-4 text-danger-text"
            >
              <div className="flex items-center gap-2 font-semibold">
                <AlertCircle className="h-4 w-4 shrink-0" />
                <span>Errors ({result.errors.length})</span>
              </div>
              <ul className="mt-2 space-y-1 text-[13px]">
                {result.errors.map((err, i) => (
                  <li key={i}>
                    <span className="font-mono font-semibold">{err.code}</span>: {err.message}
                    {err.source_id ? (
                      <span className="text-danger-text/80"> ({err.source_id})</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Cross-doc — overview only */}
          {partyView === 'overview' && (
            <CrossDocumentReconciliation
              crossDocument={result.cross_document}
              documents={result.documents}
              kyc={result.kyc}
              primaryApplicant={result.primary_applicant}
              coApplicant={result.co_applicant}
            />
          )}

          {/* Documents for current view */}
          <section className="space-y-3">
            <div className="flex items-center gap-2">
              <FileText className="h-4 w-4 text-ember" />
              <h3 className="font-display text-[16px] font-bold tracking-tight text-content">
                {partyView === 'applicant'
                  ? 'Applicant documents'
                  : partyView === 'co_applicant'
                    ? 'Co-applicant documents'
                    : 'All documents'}
              </h3>
              <span className="chip text-[11px] tabular-nums">{visibleDocs.length}</span>
            </div>

            {visibleDocs.length === 0 ? (
              <p className="text-[13px] text-content-secondary py-6 text-center border border-dashed border-line rounded-sm">
                No documents in this view.
              </p>
            ) : (
              <div className="space-y-3">
                {visibleDocs.map((doc) => (
                  <DocumentResultCard
                    key={doc.source_id}
                    doc={doc}
                    isOpen={expandedDocIds.has(doc.source_id)}
                    onToggle={() => toggleDoc(doc.source_id)}
                  />
                ))}
              </div>
            )}
          </section>

          {/* Compliance — overview */}
          {partyView === 'overview' && (
            <ValidationSummaryCards
              kyc={result.kyc}
              crossDocument={result.cross_document}
              decision={result.decision}
            />
          )}
        </motion.div>
      </AnimatePresence>

      {/* JSON modal */}
      {showJsonModal && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4"
        >
          <div className="card w-full max-w-3xl max-h-[85vh] flex flex-col border border-line bg-surface shadow-xl p-0 overflow-hidden">
            <div className="flex items-center justify-between border-b border-line-divider px-6 py-4">
              <div className="flex items-center gap-2">
                <Code2 className="h-5 w-5 text-ember" />
                <h3 className="font-display text-[16px] font-bold text-content">
                  Raw API JSON
                </h3>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleCopyJson}
                  className="btn btn-outline h-8 px-2.5 text-[12px] inline-flex items-center gap-1.5"
                >
                  {copiedJson ? (
                    <>
                      <Check className="h-3.5 w-3.5 text-success" />
                      <span>Copied</span>
                    </>
                  ) : (
                    <>
                      <Copy className="h-3.5 w-3.5" />
                      <span>Copy</span>
                    </>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setShowJsonModal(false)}
                  className="flex h-8 w-8 items-center justify-center rounded-xs text-content-secondary hover:bg-raised hover:text-content transition-colors"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-auto p-6 bg-raised/40">
              <pre className="font-mono text-[12px] leading-relaxed text-content overflow-x-auto select-all">
                {JSON.stringify(result, null, 2)}
              </pre>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
