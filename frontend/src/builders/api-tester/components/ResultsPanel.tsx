import { useState } from 'react'
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
  X,
} from 'lucide-react'
import type { LosProcessResponse } from '../../../runtime/api-tester'
import { CrossDocumentReconciliation } from './CrossDocumentReconciliation'
import { DocumentResultCard } from './DocumentResultCard'
import { ValidationSummaryCards } from './ValidationSummaryCards'

export interface ResultsPanelProps {
  result: LosProcessResponse
  onReset: () => void
}

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

export function ResultsPanel({ result, onReset }: ResultsPanelProps) {
  const [activeTab, setActiveTab] = useState<'all' | string>('all')
  const [expandedDocIds, setExpandedDocIds] = useState<Set<string>>(
    () => new Set(result.documents.map((d) => d.source_id)),
  )
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

  const filteredDocs =
    activeTab === 'all'
      ? result.documents
      : result.documents.filter((d) => d.source_id === activeTab)

  return (
    <div className="space-y-6">
      {/* 1. Header Summary Card */}
      <section className="card border-line bg-surface p-6 sm:p-7 shadow-xs space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line-divider pb-5">
          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
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
                  {isSuccess ? 'Verification Passed' : isReject ? 'Verification Rejected' : 'Manual Review Required'}
                  {' · '}{result.status}
                </span>
              </span>
              <span className="chip text-[11px] font-semibold">
                Decision: {result.decision}
              </span>
            </div>

            <h2 className="font-display text-[22px] font-bold tracking-tight text-content sm:text-[24px]">
              Document Verification Report
            </h2>

            <div className="flex flex-wrap items-center gap-3 pt-0.5 text-[12px] text-content-secondary">
              <span className="inline-flex items-center gap-1 font-mono">
                <Clock className="h-3.5 w-3.5 text-icon-default" />
                {(result.processing_ms / 1000).toFixed(2)}s (
                {result.processing_ms.toFixed(0)} ms)
              </span>
              <span>·</span>
              <span className="font-medium text-content">
                {result.documents.length} Documents Analyzed
              </span>
              <span>·</span>
              <span className="font-mono text-[11px] text-content-secondary">
                Action: {result.next_action}
              </span>
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
              <span>New Request</span>
            </button>
          </div>
        </div>


        {/* Copyable Identifiers */}
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <CopyableBadge label="Request ID" value={result.request_id} />
          <CopyableBadge label="Case ID" value={result.case_id} />
          {result.applicant_id && (
            <CopyableBadge label="Applicant ID" value={result.applicant_id} />
          )}
        </div>
      </section>

      {/* Errors alert (if any) */}
      {result.errors?.length > 0 && (
        <div
          role="alert"
          className="rounded-lg border border-danger/30 bg-danger-subtle p-4 text-danger-text"
        >
          <div className="flex items-center gap-2 font-semibold">
            <AlertCircle className="h-4 w-4 shrink-0" />
            <span>Errors ({result.errors.length})</span>
          </div>
          <ul className="mt-2 space-y-1 text-[13px] font-mono">
            {result.errors.map((err, i) => (
              <li key={i}>
                {err.code}: {err.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 2. PRIMARY FEATURE: Cross-Document Comparison Table */}
      <CrossDocumentReconciliation
        crossDocument={result.cross_document}
        documents={result.documents}
        kyc={result.kyc}
      />

      {/* 3. Document Attributes Profile with Filter Tabs */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <FileText className="h-4 w-4 text-ember" />
            <h3 className="font-display text-[16px] font-bold tracking-tight text-content">
              Document Extractions
            </h3>
          </div>

          {/* Simple Clean Tabs — scrollable when many docs */}
          <div className="overflow-x-auto max-w-full">
            <div className="flex items-center gap-1 rounded-sm bg-raised p-1 border border-line min-w-max">
              <button
                type="button"
                onClick={() => setActiveTab('all')}
                className={`rounded-xs px-2.5 py-1 text-[11px] font-semibold transition-colors whitespace-nowrap ${
                  activeTab === 'all'
                    ? 'bg-surface text-content shadow-xs border border-line/60'
                    : 'text-content-secondary hover:text-content'
                }`}
              >
                All ({result.documents.length})
              </button>
              {result.documents.map((doc) => (
                <button
                  key={doc.source_id}
                  type="button"
                  onClick={() => setActiveTab(doc.source_id)}
                  className={`rounded-xs px-2.5 py-1 text-[11px] font-semibold font-mono transition-colors whitespace-nowrap max-w-[160px] truncate ${
                    activeTab === doc.source_id
                      ? 'bg-surface text-content shadow-xs border border-line/60'
                      : 'text-content-secondary hover:text-content'
                  }`}
                >
                  {doc.source_id}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="space-y-3 max-h-[60vh] overflow-y-auto pr-2 custom-scrollbar">
          {filteredDocs.map((doc) => (
            <DocumentResultCard
              key={doc.source_id}
              doc={doc}
              isOpen={expandedDocIds.has(doc.source_id)}
              onToggle={() => toggleDoc(doc.source_id)}
            />
          ))}
        </div>
      </section>

      {/* 4. Compliance Checklist */}
      <ValidationSummaryCards
        kyc={result.kyc}
        crossDocument={result.cross_document}
        decision={result.decision}
      />

      {/* 5. Raw JSON Inspector Modal */}
      {showJsonModal && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4 animate-in fade-in duration-150"
        >
          <div className="card w-full max-w-3xl max-h-[85vh] flex flex-col border border-line bg-surface shadow-xl p-0 overflow-hidden">
            {/* Modal Header */}
            <div className="flex items-center justify-between border-b border-line-divider px-6 py-4">
              <div className="flex items-center gap-2">
                <Code2 className="h-5 w-5 text-ember" />
                <h3 className="font-display text-[16px] font-bold text-content">
                  Raw API JSON Response
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
                      <span>Copied!</span>
                    </>
                  ) : (
                    <>
                      <Copy className="h-3.5 w-3.5" />
                      <span>Copy JSON</span>
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

            {/* Modal Body */}
            <div className="flex-1 overflow-auto p-6 bg-raised/40">
              <pre className="font-mono text-[12px] leading-relaxed text-content overflow-x-auto select-all">
                {JSON.stringify(result, null, 2)}
              </pre>
            </div>

            {/* Modal Footer */}
            <div className="border-t border-line-divider px-6 py-3 flex justify-end">
              <button
                type="button"
                onClick={() => setShowJsonModal(false)}
                className="btn btn-secondary h-9 px-4 text-[13px]"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
