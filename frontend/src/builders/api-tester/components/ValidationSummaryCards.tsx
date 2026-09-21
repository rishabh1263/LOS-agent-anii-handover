import { AlertTriangle, CheckCircle2, FileSearch, ShieldAlert, ShieldCheck, UserCheck } from 'lucide-react'
import type { CrossDocument, Decision, KycResult } from '../../../runtime/api-tester'
import { checkStatusClass } from '../../../runtime/api-tester'
import { StatusChip } from './StatusChip'

export interface ValidationSummaryCardsProps {
  kyc: KycResult
  crossDocument: CrossDocument
  decision: Decision
}

export function ValidationSummaryCards({ kyc, crossDocument, decision }: ValidationSummaryCardsProps) {
  const isKycPass   = kyc.status          === 'PASS'
  const isCrossPass = crossDocument.status === 'PASS'
  const isKycFail   = kyc.status          === 'FAIL'
  const isCrossFail = crossDocument.status === 'FAIL'

  const isDecisionPass   = decision === 'PASS'
  const isDecisionReject = decision === 'REJECT'

  // ── helpers ──────────────────────────────────────────────────────────────
  function statusDot(pass: boolean, fail: boolean) {
    if (pass) return 'bg-success'
    if (fail) return 'bg-danger'
    return 'bg-warning'
  }

  function rowBorder(pass: boolean, fail: boolean) {
    if (pass) return 'border-success/40'
    if (fail) return 'border-danger/40'
    return 'border-warning/40'
  }

  // ── decision meta ─────────────────────────────────────────────────────────
  const decisionConfig = isDecisionPass
    ? {
        icon      : <CheckCircle2 className="h-5 w-5" />,
        bg        : 'bg-success-subtle',
        border    : 'border-success/30',
        text      : 'text-success',
        textLabel : 'text-success-text',
        badge     : 'APPROVED',
        heading   : 'Verification Passed',
        sub       : 'Document package validated. Workflow may proceed.',
      }
    : isDecisionReject
    ? {
        icon      : <ShieldAlert className="h-5 w-5" />,
        bg        : 'bg-danger-subtle',
        border    : 'border-danger/30',
        text      : 'text-danger',
        textLabel : 'text-danger-text',
        badge     : 'REJECTED',
        heading   : 'Verification Rejected',
        sub       : 'Application does not meet document requirements.',
      }
    : {
        icon      : <AlertTriangle className="h-5 w-5" />,
        bg        : 'bg-warning-subtle',
        border    : 'border-warning/30',
        text      : 'text-warning',
        textLabel : 'text-warning-text',
        badge     : 'MANUAL REVIEW',
        heading   : 'Manual Review Required',
        sub       : 'One or more checks require human verification.',
      }

  return (
    <div className="card overflow-hidden border-line bg-surface p-0 shadow-xs">

      {/* ── Section header ─────────────────────────────────────────────── */}
      <div className="px-6 py-4 border-b border-line-divider flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-ember" />
          <h3 className="font-display text-[15px] font-bold tracking-tight text-content">
            Compliance Audit
          </h3>
        </div>
        <span className="chip text-[10px] font-semibold uppercase tracking-wider">Regulatory Policy</span>
      </div>

      {/* ── Check table ────────────────────────────────────────────────── */}
      <div className="divide-y divide-line-divider">

        {/* Column headers */}
        <div className="grid grid-cols-[1fr_auto] px-6 py-2 bg-raised/40 text-[10px] font-bold uppercase tracking-wider text-content-disabled">
          <span>Check</span>
          <span className="text-right">Result</span>
        </div>

        {/* Row — KYC */}
        <div className={`grid grid-cols-[1fr_auto] items-center gap-4 px-6 py-4 border-l-[3px] ${rowBorder(isKycPass, isKycFail)} hover:bg-raised/20 transition-colors`}>
          <div className="flex items-center gap-3 min-w-0">
            <span className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-xs ${
              isKycPass ? 'bg-success-subtle text-success' : isKycFail ? 'bg-danger-subtle text-danger' : 'bg-warning-subtle text-warning'
            }`}>
              <UserCheck className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${statusDot(isKycPass, isKycFail)}`} />
                <span className="font-semibold text-[13px] text-content truncate">KYC / Identity Validation</span>
              </div>
              <p className="text-[11px] text-content-secondary mt-0.5 pl-3.5">
                {isKycPass
                  ? 'All identity criteria met — no flags raised.'
                  : 'Identity parameters require review.'}
              </p>
            </div>
          </div>
          <StatusChip label={kyc.status} className={checkStatusClass(kyc.status)} />
        </div>

        {/* Row — Cross-Document */}
        <div className={`grid grid-cols-[1fr_auto] items-center gap-4 px-6 py-4 border-l-[3px] ${rowBorder(isCrossPass, isCrossFail)} hover:bg-raised/20 transition-colors`}>
          <div className="flex items-center gap-3 min-w-0">
            <span className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-xs ${
              isCrossPass ? 'bg-success-subtle text-success' : isCrossFail ? 'bg-danger-subtle text-danger' : 'bg-warning-subtle text-warning'
            }`}>
              <FileSearch className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${statusDot(isCrossPass, isCrossFail)}`} />
                <span className="font-semibold text-[13px] text-content truncate">Cross-Document Reconciliation</span>
              </div>
              <p className="text-[11px] text-content-secondary mt-0.5 pl-3.5">
                {isCrossPass
                  ? 'Attributes corroborated across all submitted documents.'
                  : 'Discrepancies detected during cross-comparison.'}
              </p>
            </div>
          </div>
          <StatusChip label={crossDocument.status} className={checkStatusClass(crossDocument.status)} />
        </div>
      </div>

      {/* ── Decision banner ─────────────────────────────────────────────── */}
      <div className={`${decisionConfig.bg} border-t ${decisionConfig.border} px-6 py-4 flex items-center justify-between gap-4`}>
        <div className="flex items-center gap-3">
          <span className={`${decisionConfig.text} flex h-8 w-8 shrink-0 items-center justify-center rounded-xs ${decisionConfig.bg} border ${decisionConfig.border}`}>
            {decisionConfig.icon}
          </span>
          <div>
            <p className={`font-bold text-[13px] ${decisionConfig.text}`}>{decisionConfig.heading}</p>
            <p className="text-[11px] text-content-secondary">{decisionConfig.sub}</p>
          </div>
        </div>
        <span className={`shrink-0 font-bold text-[11px] tracking-widest uppercase px-3 py-1.5 rounded-xs border ${decisionConfig.bg} ${decisionConfig.border} ${decisionConfig.textLabel}`}>
          {decisionConfig.badge}
        </span>
      </div>

    </div>
  )
}
