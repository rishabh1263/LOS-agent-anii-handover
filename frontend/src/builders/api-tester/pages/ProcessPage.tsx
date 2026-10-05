import { useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { FolderOpen, Loader2 } from 'lucide-react'
import { useKycWizard } from '../../../runtime/api-tester'
import { useAuth } from '../../../runtime/auth'
import { Chatbot } from '../../chatbot'
import {
  ApplicationDetailsStep,
  BasicDetailsStep,
  DocumentUploadStep,
  PartySelectStep,
  ProfileMatchPanel,
  ResultsPanel,
  WizardProgress,
} from '../components'

const FADE = { duration: 0.22, ease: [0.22, 1, 0.36, 1] as const }

/**
 * KYC wizard: details → application → party → documents → report.
 * Auth is handled by App (RequireAuth). Chatbot receives case context.
 */
export function ProcessPage() {
  const { user, accessToken } = useAuth()
  const {
    step,
    setStep,
    profileFields,
    updateProfileField,
    addCustomField,
    removeProfileField,
    canProceedFromDetails,
    application,
    updateApplicationField,
    canProceedFromApplication,
    submittingApplicant,
    submitApplicantAndContinue,
    goToApplication,
    partySelection,
    setPartySelection,
    canProceedFromParty,
    activeParty,
    setActiveParty,
    applicantId,
    coApplicantId,
    caseId,
    verifiedDocs,
    primaryDocs,
    coDocs,
    successDocs,
    loading,
    verifying,
    error,
    setError,
    result,
    showOtherPartyPrompt,
    profileSnapshot,
    goToDocuments,
    uploadAndVerify,
    removeDoc,
    switchToOtherParty,
    dismissOtherPartyPrompt,
    runVerification,
    reset,
    addMoreDocuments,
    resumeExistingCase,
    resumingCase,
  } = useKycWizard()

  const partyId =
    activeParty === 'CO_APPLICANT' ? coApplicantId || undefined : applicantId || undefined

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6">
      <div className="card space-y-4">
        <div>
          <p className="font-display text-[12px] font-semibold uppercase tracking-[0.09em] text-content-secondary">
            Document verification
            {user?.stage ? ` · ${user.stage}` : ''}
          </p>
          <h1 className="mt-2 font-display text-[24px] font-bold tracking-tight text-content sm:text-[28px]">
            KYC verification
          </h1>
          <p className="mt-1.5 max-w-2xl text-[14px] leading-relaxed text-content-secondary">
            Enter applicant and application details, create the FOS record, select party, then
            upload documents. Each upload runs VERIFY then EXTRACT. Run verification executes
            PROCESS for the full report.
          </p>
          {(applicantId || caseId) && (
            <p className="mt-2 font-mono text-[12px] text-content-secondary">
              {applicantId && <span>Applicant: {applicantId}</span>}
              {applicantId && caseId && <span className="mx-2 text-content-disabled">·</span>}
              {caseId && <span>Case: {caseId}</span>}
            </p>
          )}
          {resumingCase && (
            <p className="mt-2 text-[13px] font-medium text-ember-text">
              Loading existing case…
            </p>
          )}
        </div>
        <WizardProgress current={step === 'report' ? 'report' : step} />
      </div>

      {/* Resume existing case while already signed in (details step only) */}
      {step === 'details' && !applicantId && !caseId && (
        <ResumeCaseCard
          loading={resumingCase}
          onResume={(appId, cId) => void resumeExistingCase(appId, cId)}
        />
      )}

      <AnimatePresence mode="wait">
        {step === 'details' && (
          <motion.div
            key="details"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={FADE}
          >
            <BasicDetailsStep
              fields={profileFields}
              onChange={updateProfileField}
              onAddField={addCustomField}
              onRemoveField={removeProfileField}
              onContinue={goToApplication}
              canContinue={canProceedFromDetails}
              error={error}
            />
          </motion.div>
        )}

        {step === 'application' && (
          <motion.div
            key="application"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={FADE}
          >
            <ApplicationDetailsStep
              values={application}
              onChange={updateApplicationField}
              onBack={() => {
                setError(null)
                setStep('details')
              }}
              onContinue={() => void submitApplicantAndContinue()}
              canContinue={canProceedFromApplication}
              submitting={submittingApplicant}
              error={error}
            />
          </motion.div>
        )}

        {step === 'party' && (
          <motion.div
            key="party"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={FADE}
          >
            <PartySelectStep
              selection={partySelection}
              onChange={(next) => {
                setPartySelection(next)
                setError(null)
              }}
              onBack={() => {
                setError(null)
                setStep('application')
              }}
              onContinue={goToDocuments}
              canContinue={canProceedFromParty}
              error={error}
            />
          </motion.div>
        )}

        {step === 'documents' && (
          <motion.div
            key="documents"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={FADE}
          >
            <DocumentUploadStep
              partySelection={partySelection}
              activeParty={activeParty}
              onActivePartyChange={setActiveParty}
              primaryDocs={primaryDocs}
              coDocs={coDocs}
              loading={loading}
              verifying={verifying}
              error={error}
              showOtherPartyPrompt={showOtherPartyPrompt}
              onUpload={uploadAndVerify}
              onRemoveDoc={removeDoc}
              onSwitchOtherParty={switchToOtherParty}
              onDismissOtherPartyPrompt={dismissOtherPartyPrompt}
              onBack={() => {
                setError(null)
                setStep('party')
              }}
              onRunVerification={runVerification}
              canRunVerification={successDocs.length > 0}
            />
          </motion.div>
        )}

        {step === 'report' && result && (
          <motion.div
            key="report"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
            className="space-y-4"
          >
            <ProfileMatchPanel profile={profileSnapshot} result={result} />
            <ResultsPanel
              result={result}
              onReset={reset}
              onAddDocuments={addMoreDocuments}
              uploadedFiles={verifiedDocs.map((d) => d.item)}
            />
          </motion.div>
        )}

        {step === 'report' && !result && (
          <motion.div
            key="report-empty"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="card space-y-3 p-6 text-center"
          >
            <p className="text-[14px] text-content-secondary">
              No verification result. Upload documents and run verification.
            </p>
            <button type="button" className="btn btn-primary" onClick={() => setStep('documents')}>
              Back to documents
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      <Chatbot
        caseId={caseId || undefined}
        applicantId={applicantId || undefined}
        partyId={partyId}
        stage={user?.stage}
        accessToken={accessToken || undefined}
      />
    </div>
  )
}

function ResumeCaseCard({
  loading,
  onResume,
}: {
  loading: boolean
  onResume: (applicantId: string, caseId: string) => void
}) {
  const [appId, setAppId] = useState('')
  const [cId, setCId] = useState('')

  return (
    <div className="card space-y-3 border border-ember/20 bg-ember/[0.03] p-4 sm:p-5">
      <div className="flex items-start gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-ember/10 text-ember">
          <FolderOpen className="h-4 w-4" strokeWidth={2} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-[14px] font-semibold text-content">Open existing case</p>
          <p className="mt-0.5 text-[12.5px] leading-snug text-content-secondary">
            Already have an Applicant ID and Case ID? Load them to continue verification and see
            checklist status in the chatbot.
          </p>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="resume-app-id" className="label">
            Applicant ID
          </label>
          <input
            id="resume-app-id"
            className="input font-mono text-[13px]"
            value={appId}
            disabled={loading}
            onChange={(e) => setAppId(e.target.value)}
            placeholder="APP-… or uuid"
            autoComplete="off"
          />
        </div>
        <div>
          <label htmlFor="resume-case-id" className="label">
            Case ID
          </label>
          <input
            id="resume-case-id"
            className="input font-mono text-[13px]"
            value={cId}
            disabled={loading}
            onChange={(e) => setCId(e.target.value)}
            placeholder="CASE-… or uuid"
            autoComplete="off"
          />
        </div>
      </div>
      <button
        type="button"
        disabled={loading || !appId.trim() || !cId.trim()}
        onClick={() => onResume(appId.trim(), cId.trim())}
        className="btn btn-primary inline-flex items-center gap-2"
      >
        {loading ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading…
          </>
        ) : (
          <>
            <FolderOpen className="h-4 w-4" />
            Load case
          </>
        )}
      </button>
    </div>
  )
}
